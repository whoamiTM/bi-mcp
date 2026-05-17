"""Camera-list + camera-config tools."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
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
            msg = str(e).lower()
            if any(kw in msg for kw in ("access denied", "unknown", "invalid", "not supported")):
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
