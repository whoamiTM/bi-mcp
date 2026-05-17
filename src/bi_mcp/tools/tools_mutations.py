"""Mutating tools — fire triggers, drive PTZ presets, flip profiles.

Only loaded when ``BI_MCP_ALLOW_MUTATIONS=1`` (see ``registry._MUTATION_MODULES``).
When the flag is off, this module is never imported — the tools simply do
not exist on the MCP surface.

Operating rules (see AGENTS.md § Mutation patterns):

  * **Read before write.** Always confirm state (e.g. ``bi_get_ptz_status``)
    before calling a mutation, so the LLM can verify the action targets the
    right object.
  * **Verify after write.** Re-read state to confirm the change landed.
  * **Revert global state before turn-end.** If you flip a profile, flip it
    back unless the user explicitly asked for a persistent change.
  * **Don't loop ``bi_trigger_camera``.** Each call generates a real alert
    that pollutes the user's database. Trigger once, observe, move on.
"""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest, BiError, BiMutationsDisabled
from ..utils.logging import log_tool_usage
from .registry import mutations_enabled, register_tool
from .tools_status import COMMON_SCHEMA


def _require_mutations() -> None:
    """Defensive guard. The registry already skips this module when mutations
    are off — this only fires if a caller bypasses the registry."""
    if not mutations_enabled():
        raise BiMutationsDisabled(
            "Mutating tools require BI_MCP_ALLOW_MUTATIONS=1 in bi-mcp/.env"
        )


# ---------------------------------------------------------------------------
# bi_trigger_camera — fire a synthetic camera trigger
# ---------------------------------------------------------------------------


@log_tool_usage("bi_trigger_camera")
def _tool_trigger_camera(client: BiClients, args: dict) -> Any:
    _require_mutations()
    camera = args.get("camera")
    if not camera:
        raise BiBadRequest(
            "bi_trigger_camera requires a 'camera' argument (camera short name)"
        )
    # BI 5.9.9.71 gates the `trigger` cmd behind admin (returns "Access denied"
    # for non-admin users), even though the manual doesn't flag it as such.
    # Route through admin so the fresh-process lazy-probe works correctly.
    if client.resolve_admin() is None:
        from ..errors import BiAdminRequired
        raise BiAdminRequired(
            "bi_trigger_camera requires admin Blue Iris credentials; the "
            "`trigger` JSON cmd is gated behind admin. Set BI_ADMIN_USER/"
            "BI_ADMIN_PASS in bi-mcp/.env, or grant admin to BI_USER."
        )
    payload: dict[str, Any] = {"camera": camera}
    # Per BI manual § *trigger*: `memo` and `jpeg` are optional pass-through
    # fields that set the alert's database memo / use a supplied JPEG.
    for k in ("memo", "jpeg"):
        if k in args:
            payload[k] = args[k]
    raw = client.admin_call_raw("trigger", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_trigger_result(raw)


# ---------------------------------------------------------------------------
# bi_set_ptz_preset — recall a PTZ preset
# ---------------------------------------------------------------------------


@log_tool_usage("bi_set_ptz_preset")
def _tool_set_ptz_preset(client: BiClients, args: dict) -> Any:
    _require_mutations()
    camera = args.get("camera")
    if not camera:
        raise BiBadRequest(
            "bi_set_ptz_preset requires a 'camera' argument (camera short name)"
        )
    preset = args.get("preset")
    if preset is None:
        raise BiBadRequest("bi_set_ptz_preset requires a 'preset' number (1-20)")
    try:
        preset_int = int(preset)
    except (TypeError, ValueError) as e:
        raise BiBadRequest(f"'preset' must be an integer (got {preset!r})") from e
    if not (1 <= preset_int <= 20):
        raise BiBadRequest(f"'preset' must be 1..20 (got {preset_int})")

    # Per BI manual § *ptz*: button 101..120 = "Go to preset position 1..20".
    button = 100 + preset_int
    raw = client.call_raw("ptz", camera=camera, button=button)
    if args.get("raw"):
        return raw
    shaped = shapers.shape_ptz_command_result(raw)
    shaped["camera"] = camera
    shaped["preset"] = preset_int
    return shaped


# ---------------------------------------------------------------------------
# bi_set_profile — flip the active global profile
# ---------------------------------------------------------------------------


@log_tool_usage("bi_set_profile")
def _tool_set_profile(client: BiClients, args: dict) -> Any:
    _require_mutations()
    profile = args.get("profile")
    if profile is None:
        raise BiBadRequest(
            "bi_set_profile requires a 'profile' argument (profile number 0-7, "
            "or -1 to toggle schedule hold/run)"
        )
    # Per BI manual § *status*: profile is "a single digit 0-7 for the profile
    # number to set temporarily, send again to hold; or -1 to toggle the
    # hold/run state". Three behaviors are possible:
    #   * profile=N where N != current profile → switch to N, lock stays 0/run
    #   * profile=N where N == current profile → engage schedule hold (lock=1)
    #     (this is the BI "send again to hold" semantic — and the only way
    #     to enter hold via JSON; there is no `lock=0/1` arg on `status`)
    #   * profile=-1 → toggle hold/run state. With lock=0 → lock=1, vice versa.
    # Profiles are addressed by *number*, not by name, at the JSON level; we
    # accept names too for ergonomics and resolve them via the login data.
    profile_num: int
    if isinstance(profile, int) or (isinstance(profile, str) and profile.lstrip("-").isdigit()):
        profile_num = int(profile)
    else:
        if not client.login_data:
            client.login()
        profiles = (client.login_data or {}).get("profiles") or []
        if not isinstance(profiles, list):
            profiles = []
        try:
            profile_num = profiles.index(profile)
        except ValueError as e:
            raise BiBadRequest(
                f"Profile name '{profile}' not found. Available: {profiles!r}"
            ) from e

    if not (-1 <= profile_num <= 7):
        raise BiBadRequest(
            f"'profile' must be -1 or 0..7 (got {profile_num})"
        )

    # BI gates the `status` write path (profile set) behind admin. Route
    # through admin so the fresh-process lazy-probe works.
    if client.resolve_admin() is None:
        from ..errors import BiAdminRequired
        raise BiAdminRequired(
            "bi_set_profile requires admin Blue Iris credentials; the "
            "`status` write path is gated behind admin. Set BI_ADMIN_USER/"
            "BI_ADMIN_PASS in bi-mcp/.env, or grant admin to BI_USER."
        )

    # MANDATORY pre-read. Two things depend on it: (a) the agent needs both
    # `previous_profile` AND `previous_lock` to revert (BI's `status` has
    # two state axes that can both change); (b) we refuse no-op same-profile
    # calls before they enter hold-toggle territory by accident.
    pre = client.call("status")
    if not isinstance(pre, dict) or "profile" not in pre:
        raise BiError(
            "bi_set_profile pre-read of `status` returned no `profile` field. "
            "Refusing to mutate global profile state without a reliable revert "
            "target. Re-run after Blue Iris is responsive."
        )
    previous_profile = pre["profile"]
    # `lock` comes back as either an int or a string from BI (depends on
    # the cmd path). Normalize to int for comparison.
    pre_lock_raw = pre.get("lock")
    try:
        previous_lock = int(pre_lock_raw) if pre_lock_raw is not None else None
    except (TypeError, ValueError):
        previous_lock = None

    # Refuse the BI "send same profile to engage hold" footgun. The agent
    # almost never intends this, and there is no clean revert from the
    # subsequent lock state without a separate -1 toggle. If the user
    # genuinely wants to enter hold, they should call with profile=-1.
    if profile_num != -1 and profile_num == previous_profile:
        raise BiBadRequest(
            f"bi_set_profile requested profile={profile_num}, which is already "
            f"the active profile. BI interprets 'send same profile again' as "
            f"'engage schedule hold' — refusing to fire this no-op-that-becomes-hold "
            f"silently. To toggle hold/run, call with profile=-1. To verify the "
            f"current profile, use bi_get_status."
        )

    raw = client.admin_call_raw("status", profile=profile_num)

    # MANDATORY post-write verify. For profile=N (0..7), confirm the active
    # profile actually flipped. For profile=-1 (toggle), confirm that `lock`
    # actually flipped to the opposite of its previous value.
    post = client.call("status")
    actual_profile = post.get("profile") if isinstance(post, dict) else None
    actual_lock_raw = post.get("lock") if isinstance(post, dict) else None
    try:
        actual_lock = int(actual_lock_raw) if actual_lock_raw is not None else None
    except (TypeError, ValueError):
        actual_lock = None

    if profile_num == -1:
        # We toggled hold/run. Verify lock changed direction.
        if previous_lock is None or actual_lock is None or actual_lock == previous_lock:
            raise BiError(
                f"bi_set_profile profile=-1 was meant to toggle schedule hold/run, "
                f"but post-write lock={actual_lock!r} matches previous lock="
                f"{previous_lock!r}. The toggle did not land."
            )
    else:
        if actual_profile != profile_num:
            raise BiError(
                f"bi_set_profile sent profile={profile_num} but post-write status "
                f"shows profile={actual_profile!r}. The change may have been "
                f"blocked by a schedule hold (lock={actual_lock!r}) or refused by "
                f"Blue Iris. Previous profile was {previous_profile}."
            )

    if args.get("raw"):
        return raw
    shaped = shapers.shape_profile_set_result(raw, previous_profile=previous_profile)
    # Surface the verified post-write state. `previous_lock` and `lock` are
    # included so the agent can revert either axis cleanly.
    shaped["profile"] = actual_profile
    shaped["lock"] = actual_lock
    shaped["previous_lock"] = previous_lock
    return shaped


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register() -> None:
    register_tool(
        "bi_trigger_camera",
        _tool_trigger_camera,
        description=(
            "Fire a synthetic motion trigger on a camera. Used to test alert "
            "pipelines and AI configuration end-to-end. Optional 'memo' sets the "
            "alert's database memo. DO NOT LOOP — each call creates a real alert. "
            "Requires BI_MCP_ALLOW_MUTATIONS=1."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
                "memo": {
                    "type": "string",
                    "description": "Optional memo text stored on the resulting alert.",
                },
                "jpeg": {
                    "type": "string",
                    "description": "Optional fully-qualified path to a JPEG to use for the alert.",
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Trigger BI camera",
        },
    )

    register_tool(
        "bi_set_ptz_preset",
        _tool_set_ptz_preset,
        description=(
            "Recall a PTZ preset (1-20) on a camera. Confirm the preset exists "
            "via bi_get_ptz_status first. Returns ok=True on success. Requires "
            "BI_MCP_ALLOW_MUTATIONS=1."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "PTZ camera short name. Required.",
                },
                "preset": {
                    "type": "integer",
                    "description": "Preset number 1..20. Required.",
                },
            },
            "required": ["camera", "preset"],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "title": "Set BI PTZ preset",
        },
    )

    register_tool(
        "bi_set_profile",
        _tool_set_profile,
        description=(
            "Switch the active global profile. Accepts: profile number (0-7), "
            "profile name (resolved against bi_get_session profiles[]), or -1 "
            "to toggle schedule hold/run. Mandatory pre-read of `status` captures "
            "{previous_profile, previous_lock}; mandatory post-read verifies "
            "the change actually landed. Same-profile calls (profile==current) "
            "are refused because BI interprets them as 'engage hold' (use -1 "
            "explicitly for that). Returns {ok, profile, lock, previous_profile, "
            "previous_lock} so the caller can revert either axis. REVERT BEFORE "
            "TURN END unless the user asked for a persistent change. Requires "
            "BI_MCP_ALLOW_MUTATIONS=1."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "profile": {
                    "description": (
                        "Profile number (0-7), profile name, or -1 to toggle "
                        "schedule hold/run. A same-profile request (e.g. "
                        "profile=3 when 3 is already active) is rejected to "
                        "avoid the BI 'send-same-to-hold' footgun."
                    ),
                },
            },
            "required": ["profile"],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Set BI active profile",
        },
    )
