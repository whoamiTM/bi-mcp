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


def _pick_clipcreate_client(client: BiClients):
    """Return the BiClient whose user has the `clipcreate` capability.

    The BI `export` cmd is gated on the per-user **`clipcreate`** privilege
    (the "Create clips" checkbox on the Users page — manual § 6605), NOT
    on `admin`. Both axes are independent: a non-admin user with
    `clipcreate=true` can export, and an admin user with `clipcreate=false`
    cannot.

    Selection order:

      1. If an explicit admin client is configured, log it in and try it
         FIRST. The operator set `BI_ADMIN_USER` intentionally — and a
         stale/locked `BI_USER` credential must not prevent the admin
         account from being used for export.
      2. If the admin candidate doesn't qualify (or isn't configured),
         fall back to the read client (logging it in if needed).
      3. Both candidates have their `login_data` checked against
         `clipcreate`. If either reports `true`, that client is picked.
      4. If a candidate's `login_data` *omits* the `clipcreate` key
         (older BI builds may), treat the field as unknown and accept
         the candidate — defer the access decision to BI rather than
         refuse spuriously.
      5. Raise `BiBadRequest` only after every configured user has been
         consulted and none qualifies.

    Returns the picked `BiClient` (not a `BiClients` pair). Callers use
    `picked.call_raw("export", ...)` directly.
    """
    def _has_clipcreate(login_data: Any) -> bool | None:
        """True/False if the field is present; None if BI didn't include it."""
        if not isinstance(login_data, dict) or "clipcreate" not in login_data:
            return None
        return bool(login_data["clipcreate"])

    explicit_admin = client._explicit_admin  # noqa: SLF001
    read_client = client.read

    # Lazy-login helpers wrapped so a failed read login doesn't taint the
    # admin path (and vice versa). Each helper returns the BiClient on
    # success or None if its login_data still isn't usable afterward.
    def _ensure_login(bic, on_auth_fail_reraise_as=None):
        if bic.login_data is not None:
            return bic
        from ..errors import BiAuthFailed
        try:
            bic.login()
        except BiAuthFailed as e:
            if on_auth_fail_reraise_as is not None:
                raise on_auth_fail_reraise_as(str(e)) from e
            raise
        return bic if bic.login_data is not None else None

    candidates: list[tuple[str, Any]] = []  # (label, BiClient or None)
    auth_failure: Exception | None = None  # remember the first soft-fail

    # --- Try explicit admin FIRST when configured. -------------------------
    # This is the split-account shape (configuration shape 1, see BiClients
    # docstring). If BI_USER is stale, we must not touch it before giving
    # the admin a chance.
    if explicit_admin is not None:
        from ..errors import BiAdminAuthFailed, BiAuthFailed
        try:
            _ensure_login(explicit_admin, on_auth_fail_reraise_as=BiAdminAuthFailed)
            candidates.append(("admin", explicit_admin))
        except BiAuthFailed as e:
            # Don't bail yet — the read client may still be able to export.
            # Remember the failure so the final error message reflects it.
            auth_failure = e

    # --- Decide whether to also consult the read client. -------------------
    # Optimisation: if the admin already qualifies, skip the read login
    # entirely (P2 fix). Only fall through to read when the admin didn't
    # work OR isn't configured.
    admin_qualifies = (
        explicit_admin is not None
        and explicit_admin.login_data is not None
        and _has_clipcreate(explicit_admin.login_data) is True
    )

    if not admin_qualifies:
        try:
            _ensure_login(read_client)
            candidates.append(("read", read_client))
        except Exception as e:
            # If we already have an admin candidate (logged in OK above),
            # don't kill the whole call on a read-login failure — let the
            # admin path try. Otherwise re-raise — there's no usable client.
            if not candidates:
                raise
            if auth_failure is None:
                auth_failure = e  # surface in final error if needed

    # --- First pass: pick the first candidate that explicitly qualifies. ---
    for _label, bic in candidates:
        if _has_clipcreate(bic.login_data) is True:
            return bic

    # --- Second pass: accept candidates with the field missing. ------------
    for _label, bic in candidates:
        if _has_clipcreate(bic.login_data) is None:
            return bic

    # --- No candidate qualifies. Build a helpful diagnostic. ---------------
    if not candidates:
        # Both logins failed. Surface the first auth failure verbatim — it's
        # the actionable signal (creds wrong, BI unreachable, etc.).
        if auth_failure is not None:
            raise auth_failure  # noqa: TRY301 (re-raise captured)
        raise BiBadRequest(
            "bi_export_clip could not authenticate any Blue Iris user "
            "(no candidates logged in successfully)."
        )

    user_descriptions = []
    for label, bic in candidates:
        user = (bic.login_data or {}).get("user") or f"<{label} user>"
        user_descriptions.append(f"'{user}' ({label})")
    raise BiBadRequest(
        "bi_export_clip requires the 'Create clips' privilege on the Blue "
        "Iris user that runs the export. Checked: "
        + ", ".join(user_descriptions) + ". None has `clipcreate=true`. The "
        "BI `export` cmd is gated on this capability independently of admin "
        "(manual § 6605: 'A user must have the Create clips privilege in "
        "order to create snapshots, create manual video recordings, or to "
        "crop and export video'). Fix: BI Console → Settings → Users → "
        "<the chosen user> → check 'Create clips' → OK."
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
# bi_export_clip — async MP4/AVI/WMV export from a clip range
# ---------------------------------------------------------------------------


# Manual § *export* (BlueIris_Manual.md § 8963). Three call modes:
#   mode="create" → submit a new export job. Returns {item: {path:"@record",
#                   status:"queued"|...}}; agent polls via mode="status".
#   mode="status" → poll one queued/active/done/error item by its export
#                   @record path. Returns {item: {...}}.
#   mode="list"   → no path; returns the full export queue as {items: [...]}.
#
# The destructive `delete=true` flag from the BI cmd is intentionally NOT
# exposed (AGENTS.md Rule 5 — point users at the BI UI for destructive ops,
# consistent with our non-wrap of delalert/delclip/moveclip).

_EXPORT_FORMATS = {0: "AVI", 1: "MP4", 2: "WMV", 3: "BVR"}


def _validate_timelapse(spec: str) -> None:
    """Manual § *export*: timelapse is 'A.B@C.D' — input-fps@output-fps.
    Both halves must parse as positive floats."""
    if "@" not in spec:
        raise BiBadRequest(
            f"'timelapse' must be in 'input_fps@output_fps' form (e.g. '2.0@30.0'); got {spec!r}"
        )
    in_s, out_s = spec.split("@", 1)
    try:
        in_f = float(in_s)
        out_f = float(out_s)
    except ValueError as e:
        raise BiBadRequest(
            f"'timelapse' halves must be floats; got {spec!r}"
        ) from e
    if in_f <= 0 or out_f <= 0:
        raise BiBadRequest(
            f"'timelapse' fps values must be > 0; got {spec!r}"
        )


def _build_export_payload(args: dict) -> dict[str, Any]:
    """Build the JSON payload for an `export` create call.

    Validates everything the manual documents up front so the agent gets a
    typed error instead of a BI-flavored one. Cross-field rules:

      * reencode=false is incompatible with format=WMV (2), overlay=true,
        and any timelapse value.
      * timelapse is incompatible with audio=true and reencode=false.
    """
    path = args.get("path")
    if not path:
        raise BiBadRequest(
            "bi_export_clip mode='create' requires a 'path' (the source clip's "
            "@record id, e.g. from bi_list_clips or bi_list_alerts)"
        )
    startms = args.get("startms")
    if startms is None:
        raise BiBadRequest(
            "bi_export_clip mode='create' requires 'startms' (start position "
            "in ms inside the source clip; BI snaps to the nearest keyframe)"
        )
    try:
        startms_int = int(startms)
    except (TypeError, ValueError) as e:
        raise BiBadRequest(f"'startms' must be an integer (got {startms!r})") from e
    if startms_int < 0:
        raise BiBadRequest(f"'startms' must be >= 0 (got {startms_int})")

    payload: dict[str, Any] = {"path": path, "startms": startms_int}

    if "msec" in args and args["msec"] is not None:
        try:
            msec_int = int(args["msec"])
        except (TypeError, ValueError) as e:
            raise BiBadRequest(f"'msec' must be an integer (got {args['msec']!r})") from e
        if msec_int <= 0:
            raise BiBadRequest(f"'msec' must be > 0 (got {msec_int}); omit it to export to end of file")
        payload["msec"] = msec_int

    fmt = args.get("format")
    if fmt is not None:
        try:
            fmt_int = int(fmt)
        except (TypeError, ValueError) as e:
            raise BiBadRequest(f"'format' must be an integer 0-3 (got {fmt!r})") from e
        if fmt_int not in _EXPORT_FORMATS:
            raise BiBadRequest(
                f"'format' must be 0=AVI, 1=MP4, 2=WMV, 3=BVR (got {fmt_int})"
            )
        payload["format"] = fmt_int
    else:
        fmt_int = 1  # MP4 default per manual

    profile = args.get("profile")
    if profile is not None:
        try:
            profile_int = int(profile)
        except (TypeError, ValueError) as e:
            raise BiBadRequest(f"'profile' must be an integer 0-2 (got {profile!r})") from e
        if not (0 <= profile_int <= 2):
            raise BiBadRequest(f"'profile' must be 0-2 (got {profile_int})")
        payload["profile"] = profile_int

    audio = args.get("audio")
    overlay = args.get("overlay")
    reencode = args.get("reencode")
    timelapse = args.get("timelapse")

    if audio is not None:
        payload["audio"] = bool(audio)
    if overlay is not None:
        payload["overlay"] = bool(overlay)
    if reencode is not None:
        payload["reencode"] = bool(reencode)
    if timelapse is not None:
        if not isinstance(timelapse, str):
            raise BiBadRequest(
                f"'timelapse' must be a string like '2.0@30.0' (got {type(timelapse).__name__})"
            )
        _validate_timelapse(timelapse)
        payload["timelapse"] = timelapse

    # Cross-field rules from manual § *export*:
    if reencode is False:
        if fmt_int == 2:
            raise BiBadRequest(
                "reencode=false is incompatible with format=2 (WMV); per BI manual "
                "§ *export*, 'direct-to-disk' export cannot transcode to WMV"
            )
        if overlay is True:
            raise BiBadRequest(
                "reencode=false is incompatible with overlay=true; per BI manual § *export*"
            )
        if timelapse is not None:
            raise BiBadRequest(
                "reencode=false is incompatible with timelapse; per BI manual § *export*"
            )
    if timelapse is not None:
        # audio defaults to true at BI; refuse the combo unless caller passed audio=false explicitly.
        if audio is not False:
            raise BiBadRequest(
                "timelapse is incompatible with audio=true (the default); per BI manual "
                "§ *export*, pass audio=false alongside timelapse"
            )
        if reencode is False:
            # already caught above, but keep the symmetric check for clarity
            raise BiBadRequest(
                "timelapse is incompatible with reencode=false; per BI manual § *export*"
            )

    return payload


@log_tool_usage("bi_export_clip")
def _tool_export_clip(client: BiClients, args: dict) -> Any:
    _require_mutations()

    mode = args.get("mode", "create")
    if mode not in ("create", "status", "list"):
        raise BiBadRequest(
            f"bi_export_clip 'mode' must be 'create', 'status', or 'list' (got {mode!r})"
        )

    # Pick the BI user that actually has `clipcreate`. BI gates the `export`
    # cmd on this capability independently of admin (manual § 6605); this
    # tool used to refuse non-admin users with `clipcreate=true`, which is
    # wrong — they're a supported configuration. See `_pick_clipcreate_client`
    # for the selection order. The picker also handles the preflight error
    # for users that lack clipcreate, with a typed BiBadRequest naming every
    # candidate it checked.
    picked = _pick_clipcreate_client(client)

    if mode == "list":
        # No `path` → BI returns the full export-queue array.
        raw = picked.call_raw("export")
    elif mode == "status":
        path = args.get("path")
        if not path:
            raise BiBadRequest(
                "bi_export_clip mode='status' requires 'path' (the export @record "
                "returned by a prior mode='create' call). Omit path + use mode='list' "
                "to see the whole queue."
            )
        # A polled export's lifecycle ends *outside* the export-queue namespace:
        # once BI marks the job done, `cmd=export,path=@…` returns
        # `{result:"fail", reason:"Clip not BVR"}` (see AGENTS.md Rule 6.5).
        # `BiClient.call_raw` raises a **bare `BiError`** on `result:"fail"`,
        # which would otherwise force every polling loop into try/except. Catch
        # ONLY that specific reason and shape it back as `{ok:false, reason:…}`.
        #
        # Three-layer narrowing:
        #
        #   (a) `type(e) is BiError` — bare base class only. Every other
        #       failure path raises a typed *subclass* of `BiError`
        #       (`BiUnreachable`, `BiAuthFailed`/`BiAdminAuthFailed`,
        #       `BiBadRequest`, `BiNotFound`), and those must propagate so
        #       polling loops surface real outages instead of mistaking a
        #       network fault for "export completed".
        #
        #   (b) The exception message must contain `"Clip not BVR"` (the
        #       literal BI reason for queue graduation, observed in 5.9.9.71).
        #       Other `result:"fail"` reasons — stale/typoed `@record`,
        #       "Not found", BI-side rate limits, etc. — also raise bare
        #       BiError, but they are NOT the documented graduation case.
        #       Silently shaping them as `{ok:false}` would let a caller
        #       mistake "BI rejected your path" for "export completed".
        #
        #   (c) `raw=True` must surface the typed BiError verbatim — we
        #       cannot fabricate an envelope here because the `raw` contract
        #       (per `tools_status.py`) is "the exact BI payload", and once
        #       BI rejected the cmd there *is* no BI payload to return.
        #       The shaped path keeps the `{ok:false}` ergonomic behavior;
        #       raw callers see the underlying error.
        try:
            raw = picked.call_raw("export", path=path)
        except BiError as e:
            if type(e) is not BiError:
                raise  # typed subclass — durable failure, not a queue miss
            if "Clip not BVR" not in str(e):
                raise  # bare BiError but unknown reason — also surface it
            if args.get("raw"):
                raise  # raw=true contract: no fabricated envelopes
            raw = {"result": "fail", "data": {"reason": str(e)}}
    else:  # create
        payload = _build_export_payload(args)
        raw = picked.call_raw("export", **payload)

    if args.get("raw"):
        return raw
    shaped = shapers.shape_export_result(raw)
    shaped["mode"] = mode
    return shaped


# ---------------------------------------------------------------------------
# bi_update_record — set memo / flags on one alert or clip
# ---------------------------------------------------------------------------


# Manual § *update* (BlueIris_Manual.md § 9317): adjust a database entry by
# its @record path. Sets `memo` (≤35 chars) and toggles flag bits via the
# (flags, mask) pair. We expose two ergonomic layers:
#
#   * Raw: `flags` + `mask` integers, passed straight through. BI applies
#     mask to select which bits change and flags for the on/off state.
#   * Named bits: booleans `flagged`, `protected`, `archive`, `export_flag`
#     compile into a (flags, mask) pair under the hood so the agent doesn't
#     have to do bitmath.
#
# The named and raw paths are mutually exclusive — mixing them produces a
# (flags, mask) pair whose semantics are easy to get wrong, so we reject
# the combination up front. Manual-listed internal-use fields
# (`exportfolder`, `exportprofile`, `timelapseprofile`, `date`) are
# intentionally NOT exposed in v1 — the manual calls them out as internal
# and no curation workflow needs them. Add later if a real use case appears.

_FLAG_BITS: dict[str, int] = {
    "flagged": 2,
    "protected": 4,
    "archive": 64,
    "export_flag": 512,
}

_MEMO_MAX = 35  # Manual § *update*: "up to 35 characters"


def _build_update_payload(args: dict) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate args, return (BI payload, requested state).

    The second element is the *intended* post-state used by the verify step:
    ``{"memo": "...", "flags_on": {bit:int → True}, "flags_off": {bit:int → True}}``.
    Only fields actually being mutated appear. Verify checks each: memo
    matches; for each masked bit, the post-update integer matches the
    requested on/off state.
    """
    path = args.get("path")
    if not path:
        raise BiBadRequest(
            "bi_update_record requires 'path' (the @record id of the alert or "
            "clip to update; e.g. from bi_list_alerts or bi_list_clips)"
        )
    if not isinstance(path, str) or not path.startswith("@"):
        raise BiBadRequest(
            f"'path' must be an @record string (got {path!r}). @records come "
            "from bi_list_alerts / bi_list_clips."
        )

    payload: dict[str, Any] = {"path": path}
    intent: dict[str, Any] = {}

    # --- memo ---------------------------------------------------------------
    if "memo" in args and args["memo"] is not None:
        memo = args["memo"]
        if not isinstance(memo, str):
            raise BiBadRequest(
                f"'memo' must be a string (got {type(memo).__name__})"
            )
        if len(memo) > _MEMO_MAX:
            raise BiBadRequest(
                f"'memo' must be ≤{_MEMO_MAX} characters per BI manual § *update* "
                f"(got {len(memo)})"
            )
        payload["memo"] = memo
        intent["memo"] = memo

    # --- flags / mask -------------------------------------------------------
    raw_flags = args.get("flags")
    raw_mask = args.get("mask")
    raw_pair_present = raw_flags is not None or raw_mask is not None

    named_present = {name: args.get(name) for name in _FLAG_BITS if args.get(name) is not None}

    if raw_pair_present and named_present:
        raise BiBadRequest(
            "bi_update_record: pass either raw 'flags'+'mask' OR named flag args "
            f"({'/'.join(_FLAG_BITS)}), not both. Mixing the two produces "
            "ambiguous bit semantics."
        )

    if raw_pair_present:
        if raw_flags is None or raw_mask is None:
            raise BiBadRequest(
                "bi_update_record: 'flags' and 'mask' must be passed together. "
                "'mask' picks which bits to change; 'flags' picks the on/off state "
                "of those bits. Pass one without the other and BI's semantics "
                "are undefined."
            )
        try:
            flags_int = int(raw_flags)
            mask_int = int(raw_mask)
        except (TypeError, ValueError) as e:
            raise BiBadRequest(
                f"'flags' and 'mask' must be integers (got flags={raw_flags!r}, mask={raw_mask!r})"
            ) from e
        if not (0 <= flags_int <= 0xFFFFFFFF):
            raise BiBadRequest(f"'flags' must be in 0..0xFFFFFFFF (got {flags_int})")
        if not (0 <= mask_int <= 0xFFFFFFFF):
            raise BiBadRequest(f"'mask' must be in 0..0xFFFFFFFF (got {mask_int})")
        payload["flags"] = flags_int
        payload["mask"] = mask_int
        intent["flags_on"] = {bit for bit in range(32) if (mask_int >> bit) & 1 and (flags_int >> bit) & 1}
        intent["flags_off"] = {bit for bit in range(32) if (mask_int >> bit) & 1 and not ((flags_int >> bit) & 1)}
    elif named_present:
        flags_int = 0
        mask_int = 0
        for name, value in named_present.items():
            if not isinstance(value, bool):
                raise BiBadRequest(
                    f"'{name}' must be a boolean (got {type(value).__name__})"
                )
            bit_value = _FLAG_BITS[name]
            mask_int |= bit_value
            if value:
                flags_int |= bit_value
        payload["flags"] = flags_int
        payload["mask"] = mask_int
        # Derive intent from the constructed (flags, mask) so this branch
        # and the raw branch produce identical intent shapes — and so the
        # verify loop only ever sees one representation (bit indices 0..31).
        intent["flags_on"] = {b for b in range(32) if (mask_int >> b) & 1 and (flags_int >> b) & 1}
        intent["flags_off"] = {b for b in range(32) if (mask_int >> b) & 1 and not ((flags_int >> b) & 1)}

    # --- at-least-one-mutation guard ---------------------------------------
    if "memo" not in payload and "flags" not in payload:
        raise BiBadRequest(
            "bi_update_record: no fields to update. Pass 'memo' and/or a flag "
            f"argument ({'/'.join(_FLAG_BITS)}, or raw 'flags'+'mask')."
        )

    return payload, intent


# BI reason fragments (case-insensitive) that prove `clipstats` rejected the
# path because it isn't backed by a clip file (alert-only @records, etc.).
# Anything else from BI — including auth, unreachable, rate-limit, server
# error — must propagate with its original typed class so callers get the
# right recovery hint.
_CLIPSTATS_NOT_A_CLIP_FRAGMENTS = (
    "not bvr",      # observed in BI 5.9.9.71's export-graduation path
    "not a clip",   # defensive: undocumented but plausible BI phrasing
    "no clip",
)


def _read_record_state(client: BiClients, path: str) -> tuple[Any, Any]:
    """Pre-read memo + flags for the target @record via `clipstats`.

    Returns ``(memo, flags)`` from BI's reply. Either may be ``None`` if BI
    didn't include the field.

    Exception handling is deliberately narrow:

      * **Typed subclasses** of ``BiError`` (``BiUnreachable``,
        ``BiAuthFailed``/``BiAdminAuthFailed``, ``BiBadRequest``,
        ``BiNotFound``, …) propagate unchanged. These represent durable
        infrastructure / auth / validation failures that need their own
        recovery path; remapping them to ``BiBadRequest`` would hide
        outages behind a misleading "alert-only" message.

      * A **bare** ``BiError`` whose message matches a known "not a clip"
        reason fragment is remapped to ``BiBadRequest`` with the v1
        "clip-backed records only" hint. This is the documented limit.

      * A bare ``BiError`` with any other reason is re-raised unchanged
        so transient/unknown BI faults surface with their original
        wording instead of being misclassified.
    """
    try:
        raw = client.call("clipstats", path=path)
    except BiError as e:
        if type(e) is BiError:
            msg = str(e).lower()
            if any(frag in msg for frag in _CLIPSTATS_NOT_A_CLIP_FRAGMENTS):
                raise BiBadRequest(
                    f"bi_update_record pre-read: Blue Iris rejected {path!r} "
                    f"via clipstats as not a clip-backed record ({e}). v1 "
                    "supports clip-backed @records only. If this is an "
                    "alert-only @record, the tool will be extended; for now "
                    "use the BI UI."
                ) from e
        raise
    if not isinstance(raw, dict):
        return None, None
    data = raw.get("data") if "data" in raw else raw
    if not isinstance(data, dict):
        return None, None
    return data.get("memo"), data.get("flags")


@log_tool_usage("bi_update_record")
def _tool_update_record(client: BiClients, args: dict) -> Any:
    _require_mutations()
    payload, intent = _build_update_payload(args)
    path = payload["path"]

    # Read-before-write (AGENTS.md Rule 1). Capture so the response can
    # surface previous_memo / previous_flags for revert, AND so we can
    # auto-preserve the `flagged` bit on memo-only writes (see below).
    previous_memo, previous_flags = _read_record_state(client, path)

    # --- Auto-preserve `flagged` on memo-only writes --------------------
    # BI 5.9.9.71 quirk (AGENTS.md Rule 7): sending `update` with only
    # `memo` (no flags/mask) silently clears the `flagged` bit. To avoid
    # losing user curation state on what looks like a harmless memo edit,
    # we synthesize a (flags, mask) pair that pins the `flagged` bit to
    # its current value whenever:
    #
    #   * the caller is changing memo,
    #   * the caller did NOT pass any flag args,
    #   * the pre-read returned an integer flags field, and
    #   * `preserve_flagged` is not explicitly false.
    #
    # We pin ONLY the `flagged` bit (not all four) because that is the
    # only side effect characterized on this BI build. Expanding to other
    # bits would mutate state we haven't observed BI touching. Callers
    # who want raw BI semantics can pass `preserve_flagged=False`.
    # Validate `preserve_flagged` as a strict boolean. Truthy/falsy coercion
    # is unsafe here: this flag controls safety-critical branching (auto-pin
    # of the flagged bit AND the safety-net's flagged-drift carve-out). A
    # string `'false'` would coerce to True (preserving when caller meant
    # opt-out), and an int `1` is truthy but `1 is False` is False (so the
    # safety net would skip its carve-out). The named flag args validate
    # the same way — mirror that.
    if "preserve_flagged" in args:
        preserve_flagged = args["preserve_flagged"]
        if not isinstance(preserve_flagged, bool):
            raise BiBadRequest(
                f"'preserve_flagged' must be a boolean "
                f"(got {type(preserve_flagged).__name__})"
            )
    else:
        preserve_flagged = True
    flagged_was_preserved = False
    if (
        preserve_flagged
        and "memo" in payload
        and "flags" not in payload
        and isinstance(previous_flags, int)
    ):
        flagged_bit = _FLAG_BITS["flagged"]
        was_flagged_on = bool(previous_flags & flagged_bit)
        payload["mask"] = flagged_bit
        payload["flags"] = flagged_bit if was_flagged_on else 0
        # Verify will check this bit too.
        if was_flagged_on:
            intent["flags_on"] = intent.get("flags_on", set()) | {1}
        else:
            intent["flags_off"] = intent.get("flags_off", set()) | {1}
        flagged_was_preserved = True

    # First try via the read client. If BI rejects with admin-required-style
    # access denial, fall through to admin (mirrors what `trigger` turned out
    # to need on 5.9.9.71). The manual doesn't mark `update` as admin-only,
    # so optimistically attempt the lighter-privilege path first.
    try:
        raw = client.call_raw("update", **payload)
    except BiError as e:
        msg = str(e).lower()
        if type(e) is BiError and ("access denied" in msg or "not authorized" in msg):
            if client.resolve_admin() is None:
                from ..errors import BiAdminRequired
                raise BiAdminRequired(
                    "bi_update_record was refused by Blue Iris on the read user "
                    "(Access denied). Configure admin credentials (BI_ADMIN_USER/"
                    "BI_ADMIN_PASS) and retry — the `update` cmd is gated on a "
                    "permission the read user lacks."
                ) from e
            raw = client.admin_call_raw("update", **payload)
        else:
            raise

    # Verify-after-write (AGENTS.md Rule 2). Re-read clipstats and confirm
    # the requested changes landed. We deliberately use clipstats (not the
    # update reply) because BI's `update` echo can be partial.
    post_memo, post_flags = _read_record_state(client, path)

    if "memo" in intent and post_memo != intent["memo"]:
        raise BiError(
            f"bi_update_record sent memo={intent['memo']!r} but post-write "
            f"clipstats shows memo={post_memo!r}. Update did not land."
        )
    if "flags_on" in intent or "flags_off" in intent:
        if not isinstance(post_flags, int):
            raise BiError(
                f"bi_update_record verify: post-write clipstats returned no "
                f"integer flags field (got {post_flags!r}). Cannot confirm "
                "flag bits changed."
            )
        for bit in intent.get("flags_on", set()):
            if not ((post_flags >> bit) & 1):
                raise BiError(
                    f"bi_update_record: requested flag bit {1 << bit} ON but "
                    f"post-write flags={post_flags} has it OFF."
                )
        for bit in intent.get("flags_off", set()):
            if (post_flags >> bit) & 1:
                raise BiError(
                    f"bi_update_record: requested flag bit {1 << bit} OFF but "
                    f"post-write flags={post_flags} has it ON."
                )

    # Extra safety net: if the caller made NO flag claims AND we didn't
    # auto-preserve flagged, BI's update should not have changed the
    # named bits. If it did, surface it loudly — the caller cannot have
    # intended it. (When preserve_flagged is on this path is already
    # covered by the intent loop above.)
    #
    # `flagged` is excluded from this loop when `preserve_flagged=False`:
    # the documented opt-out contract is "raw BI semantics, including
    # the flagged-clear side effect," so a flagged-bit change is
    # *expected* in that mode and must not trip the safety net. The
    # other three named bits (protected/archive/export_flag) remain
    # watched even with the opt-out — opting out of flagged
    # preservation isn't opting out of safety for the other three.
    if (
        not flagged_was_preserved
        and "flags_on" not in intent
        and "flags_off" not in intent
        and isinstance(previous_flags, int)
        and isinstance(post_flags, int)
    ):
        bits_to_watch = dict(_FLAG_BITS)
        if preserve_flagged is False:
            bits_to_watch.pop("flagged", None)
        changed_bits = []
        for name, bit in bits_to_watch.items():
            before = bool(previous_flags & bit)
            after = bool(post_flags & bit)
            if before != after:
                changed_bits.append((name, before, after))
        if changed_bits:
            details = ", ".join(
                f"{name} {b}->{a}" for name, b, a in changed_bits
            )
            raise BiError(
                f"bi_update_record verify: caller made no flag claims, but "
                f"Blue Iris changed named flag bits as a side effect "
                f"({details}). Refusing to silently lose curation state. "
                "Re-run with explicit flag args, or pass preserve_flagged=true "
                "(default) to auto-preserve the flagged bit."
            )

    if args.get("raw"):
        return raw
    shaped = shapers.shape_update_record_result(
        raw,
        previous_memo=previous_memo,
        previous_flags=previous_flags,
    )
    # Always surface the verified post-write state, even if BI's `update`
    # echo omitted it. clipstats is authoritative.
    shaped["path"] = path
    if post_memo is not None:
        shaped["memo"] = post_memo
    if isinstance(post_flags, int):
        shaped["flags"] = post_flags
        shaped["flags_decoded"] = {
            name: bool(post_flags & bit) for name, bit in _FLAG_BITS.items()
        }
    if flagged_was_preserved:
        shaped["flagged_auto_preserved"] = True
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
        "bi_export_clip",
        _tool_export_clip,
        description=(
            "Wrap the BI `export` cmd (manual § *export*). Async MP4/AVI/WMV "
            "export from a clip range. Three modes: "
            "mode='create' submits a job (requires path=@record of source clip "
            "+ startms; optional msec, format 0-3, profile 0-2, audio, overlay, "
            "reencode, timelapse 'in_fps@out_fps'). Returns the new export "
            "@record under item.path. "
            "mode='status' polls one queued/active/done/error item by its export "
            "@record (requires path). "
            "mode='list' returns the full export queue (no path). "
            "Validates manual-documented incompatibilities up front (reencode=false "
            "vs WMV/overlay/timelapse; timelapse vs audio=true). Does NOT expose "
            "the destructive delete=true flag — cancel via BI UI. Requires "
            "BI_MCP_ALLOW_MUTATIONS=1."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "mode": {
                    "type": "string",
                    "enum": ["create", "status", "list"],
                    "description": "create=submit job, status=poll one item, list=queue snapshot. Default 'create'.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "For mode='create': @record of the source clip/alert. "
                        "For mode='status': @record of the export item returned by a prior create."
                    ),
                },
                "startms": {
                    "type": "integer",
                    "description": "create only. Start offset in ms inside the source clip. BI snaps to the nearest keyframe.",
                },
                "msec": {
                    "type": "integer",
                    "description": "create only. Duration in ms. Omit to export to end of source file.",
                },
                "format": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3],
                    "description": "create only. 0=AVI, 1=MP4 (default), 2=WMV, 3=BVR clipboard reference.",
                },
                "profile": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": "create only. Encoding profile slot configured via BI's convert/export dialog.",
                },
                "audio": {
                    "type": "boolean",
                    "description": "create only. Include audio. Default true. Must be false when using timelapse.",
                },
                "overlay": {
                    "type": "boolean",
                    "description": "create only. Burn overlay onto frames. Default false. Incompatible with reencode=false.",
                },
                "reencode": {
                    "type": "boolean",
                    "description": "create only. Default true. false = direct-to-disk (fast, no transcode); incompatible with WMV, overlay=true, timelapse.",
                },
                "timelapse": {
                    "type": "string",
                    "description": "create only. Form 'input_fps@output_fps' e.g. '2.0@30.0'. Requires audio=false and reencode=true.",
                },
            },
            "required": [],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "title": "Export BI clip",
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

    register_tool(
        "bi_update_record",
        _tool_update_record,
        description=(
            "Set memo (≤35 chars) and/or flag bits on one alert or clip @record, "
            "wrapping BI `update` (manual § *update*). How UI3 curates the "
            "alert/clip database — mark records flagged, protected, archive, or "
            "export. "
            "Two ways to drive flag bits: "
            "(a) named booleans 'flagged' / 'protected' / 'archive' / 'export_flag' "
            "(mutually exclusive with raw flags+mask), or "
            "(b) raw 'flags' + 'mask' integers passed together (mask selects which "
            "bits change, flags sets their on/off state). "
            "Mandatory pre-read via clipstats captures previous_memo + previous_flags "
            "for revert; mandatory post-read verifies the change landed. "
            "**BI 5.9.9.71 quirk**: a memo-only `update` silently clears the "
            "`flagged` bit. To protect curation state, this tool auto-preserves "
            "the existing `flagged` bit on memo-only writes by default; the response "
            "includes `flagged_auto_preserved: true` when this happens. Pass "
            "`preserve_flagged=false` to opt out (raw BI semantics). Verify-after-write "
            "also refuses silent named-flag changes when the caller made no flag claims. "
            "v1 supports clip-backed @records only (alert-only records may be "
            "rejected by clipstats; revisit if it becomes friction). "
            "Internal-use fields (exportfolder, exportprofile, timelapseprofile, "
            "date) are intentionally not exposed. "
            "Requires BI_MCP_ALLOW_MUTATIONS=1."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "path": {
                    "type": "string",
                    "description": (
                        "@record id of the alert or clip to update (e.g. from "
                        "bi_list_alerts or bi_list_clips). Required."
                    ),
                },
                "memo": {
                    "type": "string",
                    "description": "Up to 35 characters; replaces the existing memo on the record.",
                },
                "flagged": {
                    "type": "boolean",
                    "description": "Set the 'flagged' bit (BI flag 2). Mutually exclusive with raw flags+mask.",
                },
                "protected": {
                    "type": "boolean",
                    "description": "Set the 'protected' bit (BI flag 4). Mutually exclusive with raw flags+mask.",
                },
                "archive": {
                    "type": "boolean",
                    "description": "Set the 'archive' bit (BI flag 64). Mutually exclusive with raw flags+mask.",
                },
                "export_flag": {
                    "type": "boolean",
                    "description": "Set the 'export' bit (BI flag 512). Mutually exclusive with raw flags+mask.",
                },
                "flags": {
                    "type": "integer",
                    "description": (
                        "Raw flag bits (must be paired with 'mask'). 'mask' picks "
                        "which bits to change, 'flags' picks their on/off state. "
                        "Mutually exclusive with named flag args."
                    ),
                },
                "mask": {
                    "type": "integer",
                    "description": (
                        "Raw mask bits (must be paired with 'flags'). See 'flags'."
                    ),
                },
                "preserve_flagged": {
                    "type": "boolean",
                    "description": (
                        "Default true. On memo-only writes (no flag args), the "
                        "tool auto-pins the `flagged` bit to its current value to "
                        "work around a BI 5.9.9.71 quirk that clears it silently. "
                        "Pass false for raw BI semantics."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "title": "Update BI record memo/flags",
        },
    )
