"""Camera-list + camera-config tools."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import (
    BiClients,
    _elide_caller_text,
    bi_authored_reason,
    echoed_caller_text,
)
from ..errors import (
    BiAdminAuthFailed,
    BiAdminRequired,
    BiAuthFailed,
    BiBadRequest,
    BiError,
    BiNotFound,
)
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


# BI reasons that mean "the camconfig path is unavailable to us" rather than
# "this request was bad". Matched as substrings against the BI-authored reason
# because BI's wording for cmd-not-recognised / capability denial is
# undocumented. Module-level so the elision-safety test can enumerate it (see
# tests/unit/test_elidable_needle_floor.py) — a fragment shorter than
# `_MIN_ELIDABLE_NEEDLE` added here would silently disable the elision guard.
_CAMCONFIG_FALLBACK_FRAGMENTS = (
    "access denied",
    "unknown",
    "invalid",
    "not supported",
)


@log_tool_usage("bi_list_cameras")
def _tool_list_cameras(client: BiClients, args: dict) -> Any:
    raw = client.call("camlist")
    if args.get("raw"):
        return raw
    limit = args.get("limit")
    return shapers.shape_camlist(raw, limit=limit)


@log_tool_usage("bi_get_camera_config")
def _tool_get_camera_config(client: BiClients, args: dict) -> Any:
    short = args.get("short") or args.get("short_name") or args.get("camera")
    if not short:
        raise BiBadRequest(
            "bi_get_camera_config requires a 'short' (camera short name) argument"
        )
    # Prefer the undocumented `camconfig` cmd when admin creds are available.
    # Fall back to a shaped camlist entry when admin is unavailable, admin auth
    # fails, BI denies access, or BI doesn't recognise the cmd. Other BI errors
    # propagate.
    #
    # ``resolve_admin()`` runs up front so the lazy BI_USER-as-admin probe
    # answers correctly on a fresh process. We catch ONLY admin-auth failures
    # from it — those are recoverable (we fall back to camlist). Read-path
    # failures (BiUnreachable, BiAuthFailed from the read user) MUST propagate:
    # silently dropping into the camlist path would trigger a second login
    # with the same broken creds, and BI locks accounts after repeated failed
    # logins. One user request must never double-spend auth.
    admin_error: str | None = None
    try:
        client.resolve_admin()
    except BiAdminAuthFailed as e:
        # Separate-user admin login failed (BI_ADMIN_USER creds rejected).
        # The read user is still usable — proceed to camlist fallback.
        admin_error = str(e)
    # Note: BiAuthFailed (read user), BiUnreachable, and any other BiError
    # propagate to the caller. Only camlist becomes the next call after a
    # *successful* read login (or after BiAdminAuthFailed, which doesn't
    # touch the read client).
    if client.admin is not None:
        try:
            raw = client.admin_call("camconfig", camera=short)
            if isinstance(raw, dict) and not raw:
                raise BiNotFound(f"No camera with short name '{short}' found")
            if args.get("raw"):
                return raw
            return shapers.shape_camera_config_deep(raw)
        except BiAuthFailed as e:
            admin_error = str(e)
        except BiError as e:
            # Narrow to text BI actually authored, and drop the caller's own
            # `short` from it, before matching — the same anchoring
            # `bi_update_record` uses on both of its matchers. `str(e)` carries
            # the wrapper frame AND `short`, which BI echoes back in its reason
            # ("Not found: <short>"). Under a whole-message match a camera
            # merely NAMED `invalid-cam` or `unknown-2` swallowed a real BI
            # fault and silently downgraded the caller to the shallow camlist
            # fallback — no adversary required, just a plausible short name.
            # Substring semantics on the remainder are kept: BI's wording for
            # "cmd not recognised" / capability denial is undocumented.
            #
            # Elide `short` ONLY where BI demonstrably echoed it — i.e. where
            # it is the whole remainder after a colon ("Not found: <short>").
            # A blanket elision of every occurrence was worse than the hole it
            # closed: two of the fragments below are ordinary English words,
            # and a blanket pass cannot tell the caller's echoed name from the
            # SAME word BI authored itself. A camera legitimately named
            # `unknown` turned BI's own "Unknown command" into " command", the
            # fragment vanished, and this tool RAISED instead of degrading to
            # the documented shallow camlist fallback — no adversary needed,
            # just an ordinary name. `echoed_caller_text` returns "" when the
            # reason is not an echo, and `_elide_caller_text`'s length floor
            # turns that into a no-op, so the reason is matched as BI wrote it.
            #
            # The fragment tuple is passed as well, so a `short` that is a
            # strict PIECE of a fragment (`supported` inside "not supported",
            # 9 chars and so past the length floor) cannot shred the fragment
            # it belongs to. Belt-and-braces here rather than a live fix:
            # `echoed_caller_text` already blocks that path, because BI's own
            # "Not supported" is not an echo of the name and yields an empty
            # needle. It is passed anyway so this site does not depend on the
            # gate in front of it staying exactly as narrow as it is today.
            reason = bi_authored_reason(str(e))
            msg = _elide_caller_text(
                reason,
                echoed_caller_text(reason, short),
                _CAMCONFIG_FALLBACK_FRAGMENTS,
            )
            if any(kw in msg for kw in _CAMCONFIG_FALLBACK_FRAGMENTS):
                admin_error = str(e)
            else:
                raise
    if args.get("raw"):
        if admin_error:
            raise BiError(
                f"raw=true requires the admin camconfig path, but it failed: {admin_error}"
            )
        raise BiAdminRequired(
            "raw=true requires admin BI credentials so the underlying camconfig "
            "payload can be returned. Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env, "
            "or call without raw=true to get the shaped camlist fallback."
        )
    raw = client.call("camlist")
    entry: dict[str, Any] | None = None
    if isinstance(raw, list):
        for cam in raw:
            if isinstance(cam, dict) and (
                cam.get("optionValue") == short
                or cam.get("shortName") == short
                or cam.get("name") == short
            ):
                entry = cam
                break
    if entry is None:
        raise BiNotFound(f"No camera with short name '{short}' found in camlist")
    shaped = shapers.shape_camera_config(raw, short)
    if shaped is None:
        raise BiNotFound(f"No camera with short name '{short}' found in camlist")
    if admin_error:
        shaped["_note"] = (
            f"admin camconfig call failed ({admin_error}); returned shallow state from camlist. "
            "Check BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env."
        )
    else:
        shaped["_note"] = (
            "admin BI creds not configured; returned shallow state from camlist. "
            "Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env for the camconfig path."
        )
    return shaped


@log_tool_usage("bi_get_camera_motion_config")
def _tool_get_camera_motion_config(client: BiClients, args: dict) -> Any:
    short = args.get("short") or args.get("short_name") or args.get("camera")
    if not short:
        raise BiBadRequest(
            "bi_get_camera_motion_config requires a 'short' (camera short name) argument"
        )
    # Admin-required, no camlist fallback. camlist carries no motion data, so a
    # shallow fallback would mislead the caller — the whole point of this tool
    # is the live setmotion/setpost subtrees. Surface a typed error instead.
    #
    # Let BiAdminAuthFailed propagate (configured creds rejected — admin_auth
    # remediation: rotate creds / check lockout). Only synthesize
    # BiAdminRequired when no admin path is configured at all (admin_required
    # remediation: set creds). Conflating the two misleads the caller into the
    # wrong fix and, in the lockout case, into retries that deepen the lockout.
    if client.resolve_admin() is None:
        raise BiAdminRequired(
            "bi_get_camera_motion_config requires admin BI credentials. "
            "Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env."
        )
    raw = client.admin_call("camconfig", camera=short)
    # Unknown-camera reply on BI 5.9.9.71 is an empty dict (empirically
    # confirmed 2026-05-23 with cmd=camconfig camera=ZZZ_does_not_exist).
    if isinstance(raw, dict) and not raw:
        raise BiNotFound(f"No camera with short name '{short}' found")
    # Strict invariant for the shaped path: the whole point of this tool is
    # the live `setmotion` + `setpost` subtrees. If BI returns a non-dict, or
    # a dict missing either key, the response is malformed (schema drift,
    # partial error envelope, future BI build that renamed the field) and
    # callers must NOT receive a structurally-valid-looking but empty
    # motion/post payload. Raw=true callers opt out of this check — they
    # explicitly asked for the wire payload, drift and all.
    if not args.get("raw"):
        if not isinstance(raw, dict) or not isinstance(raw.get("setmotion"), dict) or not isinstance(raw.get("setpost"), dict):
            keys = sorted(raw.keys()) if isinstance(raw, dict) else None
            raise BiError(
                f"camconfig response for '{short}' is missing required "
                f"`setmotion` and/or `setpost` subtrees. This indicates BI "
                f"schema drift or a malformed reply. Top-level keys observed: "
                f"{keys}. Re-run with raw=true to inspect the wire payload."
            )
    if args.get("raw"):
        return raw
    return shapers.shape_motion_config(raw)


def register() -> None:
    register_tool(
        "bi_list_cameras",
        _tool_list_cameras,
        description=(
            "List of all cameras and groups: online state, motion/trigger/alert counts, "
            "stream bitrate/FPS/resolution, last alert time, error state."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "limit": {"type": "integer", "description": "Cap number of cameras returned."},
            },
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "List BI cameras"},
    )

    register_tool(
        "bi_get_camera_config",
        _tool_get_camera_config,
        description=(
            "Per-camera config + state. With admin creds, calls `camconfig` to return "
            "motion sensitivity, AI zones, recording mode, stream paths, schedule/"
            "profile flags. Without admin, falls back to filtered `camlist` state. "
            "Trigger zone polygons, per-class AI thresholds, and alert action "
            "definitions are NOT exposed by BI's JSON API — use bi_get_reg for those."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "short": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
            },
            "required": ["short"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI camera config"},
    )

    register_tool(
        "bi_get_camera_motion_config",
        _tool_get_camera_motion_config,
        description=(
            "Live motion + post-trigger settings for a camera, read from BI's admin "
            "`camconfig` cmd. Use this instead of `bi_get_reg(key_path='Motion')` to "
            "avoid stale .reg exports when tuning sensitivity/contrast/breaktime. "
            "Returns `motion` (12 keys: sense, contrast, breaktime, maketime, usemask, "
            "objects, ai_zones, shadows, luminance, showmotion, audio_trigger, "
            "audio_sense) and `post` (timed, timed_interval) plus verbatim `motion_raw` "
            "/ `post_raw` twins. AI thresholds (smartconf, smartlabels, periodic, "
            "static-objects) are NOT in camconfig — use bi_get_reg(key_path='AI\\\\<n>') "
            "for those. Trigger-zone polygons stay in bi_get_reg(key_path='Motion') "
            "under maskbits_*. Admin-required. Note: the camconfig set-half for "
            "setmotion/setpost is a silent no-op in 5.9.9.71 — this tool is read-only "
            "by design; tune in the BI UI."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "short": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
            },
            "required": ["short"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI camera motion config"},
    )
