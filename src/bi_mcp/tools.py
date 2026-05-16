"""Tool dispatch table — single source of truth for both MCP server and CLI.

Each entry maps a public tool name (e.g. ``bi_status``) to a callable that
takes the BiClient and a kwargs dict and returns the shaped (or raw) result.
This is what both server.py (MCP) and cli.py (terminal) call into.

The shapers in ``shapers.py`` are kept pure (data in, data out); this module
is where the BI call is performed and the shaper is invoked.
"""

from __future__ import annotations

from typing import Any, Callable

from .client import BiClient
from .errors import BiBadRequest, BiNotFound
from . import shapers

ToolFn = Callable[[BiClient, dict], Any]


def _tool_status(client: BiClient, args: dict) -> Any:
    raw = client.call("status")
    if args.get("raw"):
        return raw
    return shapers.shape_status(raw)


def _tool_session_info(client: BiClient, args: dict) -> Any:
    # Force a login if we haven't yet, then return the stored login data.
    if not client.login_data:
        client.login()
    raw = client.login_data or {}
    if args.get("raw"):
        return raw
    return shapers.shape_session_info(raw)


def _tool_cameras(client: BiClient, args: dict) -> Any:
    raw = client.call("camlist")
    if args.get("raw"):
        return raw
    limit = args.get("limit")
    return shapers.shape_camlist(raw, limit=limit)


def _tool_camera_config(client: BiClient, args: dict) -> Any:
    short = args.get("short") or args.get("short_name") or args.get("camera")
    if not short:
        raise BiBadRequest("bi_camera_config requires a 'short' (camera short name) argument")
    raw = client.call("camlist")
    if args.get("raw"):
        return raw
    shaped = shapers.shape_camera_config(raw, short)
    if shaped is None:
        raise BiNotFound(f"No camera with short name '{short}' found in camlist")
    return shaped


def _tool_log(client: BiClient, args: dict) -> Any:
    payload: dict[str, Any] = {}
    # Pass-through only read-side args. `reset=true` clears the Status/Log
    # display in Blue Iris — that's a mutation, deliberately not exposed.
    for k in ("level", "id"):
        if k in args:
            payload[k] = args[k]
    raw = client.call("log", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_log(raw, limit=int(args.get("limit", 100)))


def _tool_alerts(client: BiClient, args: dict) -> Any:
    if not args.get("camera"):
        raise BiBadRequest(
            "bi_alerts requires a 'camera' argument (camera short name, e.g. 'SecCam_3', "
            "or 'Index' for all cameras)"
        )
    payload: dict[str, Any] = {}
    # `reset=true` clears the current user's new-alert counters in BI — that's
    # a mutation. Deliberately not forwarded.
    for k in ("camera", "startdate", "enddate", "view", "search"):
        if k in args:
            payload[k] = args[k]
    raw = client.call("alertlist", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_alerts(raw, limit=int(args.get("limit", 50)))


def _tool_alert_tracks(client: BiClient, args: dict) -> Any:
    path = args.get("path") or args.get("alert")
    if not path:
        raise BiBadRequest("bi_alert_tracks requires an 'path' argument (the alert path/identifier)")
    raw = client.call("tracks", path=path)
    if args.get("raw"):
        return raw
    return shapers.shape_alert_tracks(raw)


def _tool_clip_info(client: BiClient, args: dict) -> Any:
    path = args.get("path") or args.get("clip")
    if not path:
        raise BiBadRequest("bi_clip_info requires a 'path' argument (the clip path/identifier)")
    raw = client.call("clipstats", path=path)
    if args.get("raw"):
        return raw
    return shapers.shape_clip_info(raw)


def _tool_timeline(client: BiClient, args: dict) -> Any:
    if not args.get("camera"):
        raise BiBadRequest("bi_timeline requires a 'camera' argument (camera short name)")
    payload: dict[str, Any] = {}
    for k in ("camera", "startdate", "enddate"):
        if k in args:
            payload[k] = args[k]
    raw = client.call("timeline", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_timeline(raw)


def _tool_ptz_status(client: BiClient, args: dict) -> Any:
    camera = args.get("camera")
    if not camera:
        raise BiBadRequest("bi_ptz_status requires a 'camera' argument (camera short name)")
    # Per BI manual: omit `button` to query PTZ status; a button value triggers
    # an actual PTZ operation.
    raw = client.call("ptz", camera=camera)
    if args.get("raw"):
        return raw
    return shapers.shape_ptz_status(raw)


TOOLS: dict[str, ToolFn] = {
    "bi_status": _tool_status,
    "bi_session_info": _tool_session_info,
    "bi_cameras": _tool_cameras,
    "bi_camera_config": _tool_camera_config,
    "bi_log": _tool_log,
    "bi_alerts": _tool_alerts,
    "bi_alert_tracks": _tool_alert_tracks,
    "bi_clip_info": _tool_clip_info,
    "bi_timeline": _tool_timeline,
    "bi_ptz_status": _tool_ptz_status,
}


TOOL_DESCRIPTIONS: dict[str, str] = {
    "bi_status": (
        "Snapshot of Blue Iris system state: active profile, schedule hold/run, "
        "CPU%, RAM, disk usage, uptime, DIO outputs, warnings."
    ),
    "bi_session_info": (
        "Blue Iris version/license, time zone, capabilities of the current user "
        "(admin/ptz/clips/etc), and available profile/schedule/stream names."
    ),
    "bi_cameras": (
        "List of all cameras and groups: online state, motion/trigger/alert counts, "
        "stream bitrate/FPS/resolution, last alert time, error state."
    ),
    "bi_camera_config": (
        "Full config and current state for one camera by short name "
        "(e.g. 'SecCam_3'). Includes trigger, motion, AI, recording settings."
    ),
    "bi_log": (
        "Recent Blue Iris system log entries. Optional 'level' (0=info, 1=warning, "
        "2=error) and 'limit' (default 100). NOTE: Blue Iris gates the `log` cmd "
        "behind admin; a read-only user will get 'Access denied'."
    ),
    "bi_alerts": (
        "Recent alerts with AI memo (object, confidence, license plate), zones "
        "triggered, and clip path. Requires 'camera' short name (or 'Index' for all). "
        "Optional 'startdate'/'enddate' (unix epoch), 'view' (e.g. 'people','vehicles'), "
        "'search' (memo text). 'limit' default 50."
    ),
    "bi_alert_tracks": (
        "AI object tracks (per-frame bounding boxes) inside one alert. Pass the "
        "alert's 'path' from bi_alerts."
    ),
    "bi_clip_info": (
        "Forensic detail for one clip/alert: resolution, duration, AI/profile/"
        "schedule/zones active at trigger time. Pass clip 'path' from bi_alerts."
    ),
    "bi_timeline": (
        "24-hour activity timeline (motion/trigger/alert buckets) for a camera. "
        "Requires 'camera' short name."
    ),
    "bi_ptz_status": (
        "PTZ current position, preset list, and lock state for one camera. "
        "Camera must have PTZ enabled in BI."
    ),
}
