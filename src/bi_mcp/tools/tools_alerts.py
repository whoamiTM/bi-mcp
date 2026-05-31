"""Alert + alert-tracks + clip-info tools — the alert investigation chain."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from ..utils.time import parse_since
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_list_alerts")
def _tool_list_alerts(client: BiClients, args: dict) -> Any:
    if not args.get("camera"):
        raise BiBadRequest(
            "bi_list_alerts requires a 'camera' argument (camera short name, e.g. "
            "'SecCam_3', or 'Index' for all cameras)"
        )
    payload: dict[str, Any] = {}
    # `reset=true` clears BI's new-alert counters — a mutation, deliberately
    # not forwarded.
    for k in ("camera", "view", "search"):
        if k in args:
            payload[k] = args[k]
    for k in ("startdate", "enddate"):
        if k in args and args[k] is not None:
            try:
                payload[k] = parse_since(args[k], arg_name=k)
            except ValueError as e:
                raise BiBadRequest(str(e)) from e
    raw = client.call("alertlist", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_alerts(raw, limit=int(args.get("limit", 50)))


@log_tool_usage("bi_get_alert_tracks")
def _tool_get_alert_tracks(client: BiClients, args: dict) -> Any:
    path = args.get("path") or args.get("alert")
    if not path:
        raise BiBadRequest(
            "bi_get_alert_tracks requires a 'path' argument (the alert path/identifier)"
        )
    if client.resolve_admin() is not None:
        raw = client.admin_call("tracks", path=path)
    else:
        raw = client.call("tracks", path=path)
    if args.get("raw"):
        return raw
    return shapers.shape_alert_tracks(raw)


@log_tool_usage("bi_get_clip_info")
def _tool_get_clip_info(client: BiClients, args: dict) -> Any:
    path = args.get("path") or args.get("clip")
    if not path:
        raise BiBadRequest(
            "bi_get_clip_info requires a 'path' argument (the clip path/identifier)"
        )
    raw = client.call("clipstats", path=path)
    if args.get("raw"):
        return raw
    return shapers.shape_clip_info(raw)


def register() -> None:
    register_tool(
        "bi_list_alerts",
        _tool_list_alerts,
        description=(
            "Recent alerts with AI memo (object, confidence, license plate), zones "
            "triggered, and clip path. Requires 'camera' short name (or 'Index' for all). "
            "**First stop for 'what fired when' / reconstructing an alert chain** — "
            "per-alert timestamps and memos, no dedup. Use this before bi_list_log "
            "when investigating a specific event. "
            "Optional 'startdate'/'enddate' accept unix epoch int, ISO-8601 "
            "('2026-05-27T14:09:02Z'), or relative shorthand ('-2h', '-1d'). "
            "'view' (filter; see schema for full enum), 'search' (memo substring). "
            "'limit' default 50. "
            "Crossover note: if 'view' is set to 'flagged', BI may also return *clip* "
            "items here; those clips lack the 'zones' field and their 'msec' is the "
            "clip length, not alert length."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (required). Use 'Index' for all cameras.",
                },
                "startdate": {
                    "description": (
                        "Earliest alert. Int (UTC sec), ISO-8601, or relative "
                        "shorthand like '-2h', '-1d'."
                    ),
                },
                "enddate": {
                    "description": (
                        "Latest alert. Int (UTC sec), ISO-8601, or relative "
                        "shorthand like '-2h', '-1d'."
                    ),
                },
                "view": {
                    "type": "string",
                    "description": (
                        "Database view filter. Per BI manual § *alertlist*: 'all', "
                        "'new', 'stored', 'alerts', 'aux1'..'aux7', 'flagged', 'export', "
                        "'archive', 'people', 'vehicles', 'confirmed', 'canceled'. "
                        "Per UI3 source (additional values it sends): 'zonea'..'zoneh', "
                        "'dio', 'onvif', 'audio', 'external', 'cancelled' (British). "
                        "Crossover: 'flagged' may also return clip items (no 'zones' "
                        "field); see manual § *cliplist* note on shared views."
                    ),
                },
                "search": {"type": "string", "description": "Memo substring filter (server-side)."},
                "limit": {"type": "integer", "description": "Max alerts (default 50)."},
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "List BI alerts"},
    )

    register_tool(
        "bi_get_alert_tracks",
        _tool_get_alert_tracks,
        description=(
            "AI object tracks (per-frame bounding boxes) inside one alert. Pass the "
            "alert's 'path' from bi_list_alerts. "
            "**KNOWN BROKEN on BI 5.9.9.71**: returns 'Access denied' from both the "
            "read-user and admin-user paths. The `tracks` cmd appears in BI's JSON "
            "cmd list but the manual provides no spec for it, and its gating "
            "mechanism is undocumented. Uses admin if configured (in case a future "
            "build accepts it), otherwise uses the read client. Do not rely on "
            "this tool until the gating is characterized."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "path": {
                    "type": "string",
                    "description": "Alert path/identifier from bi_list_alerts.",
                },
            },
            "required": ["path"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI alert tracks"},
    )

    register_tool(
        "bi_get_clip_info",
        _tool_get_clip_info,
        description=(
            "Forensic detail for one clip/alert: resolution, duration, AI/profile/"
            "schedule/zones active at trigger time. Pass clip 'path' from bi_list_alerts."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "path": {
                    "type": "string",
                    "description": "Clip path/identifier from bi_list_alerts.",
                },
            },
            "required": ["path"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI clip info"},
    )
