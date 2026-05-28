"""Clip-list tool — full clip inventory, complementary to bi_list_alerts."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from ..utils.time import parse_since
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_list_clips")
def _tool_list_clips(client: BiClients, args: dict) -> Any:
    # Per BI manual § *cliplist*: `camera` is required (short name or group
    # name). Use 'Index' for all cameras, matching `alertlist`'s convention.
    camera = args.get("camera")
    if not camera:
        raise BiBadRequest(
            "bi_list_clips requires a 'camera' argument (camera short name, e.g. "
            "'SecCam_3', or 'Index' for all cameras)"
        )
    payload: dict[str, Any] = {"camera": camera}
    # `delete=true` deletes ALL items matched by the query — a destructive
    # mutation deliberately not forwarded. Same reasoning as
    # `bi_list_alerts` stripping `reset`.
    for k in ("view", "search", "tiles"):
        if k in args:
            payload[k] = args[k]
    for k in ("startdate", "enddate"):
        if k in args and args[k] is not None:
            try:
                payload[k] = parse_since(args[k], arg_name=k)
            except ValueError as e:
                raise BiBadRequest(str(e)) from e
    raw = client.call("cliplist", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_cliplist(raw, limit=int(args.get("limit", 50)))


def register() -> None:
    register_tool(
        "bi_list_clips",
        _tool_list_clips,
        description=(
            "Recent recorded clips for a camera: path, duration, resolution, flags, "
            "memo. Complementary to bi_list_alerts (clips include continuous "
            "recordings; alerts are AI/motion events). Requires 'camera' short name "
            "(or 'Index' for all). Optional 'view' (filter; see schema for full enum), "
            "'startdate'/'enddate' accept unix epoch int, ISO-8601, or relative "
            "shorthand ('-2h', '-1d'). 'search' (memo substring, server-side), "
            "'tiles' (true=one entry per day, useful for calendar views). 'limit' "
            "default 50. Crossover note: alert-side view values (e.g. 'alerts', "
            "'people', 'zonea') will return *alert* items in this response — they "
            "have an 'msec' field meaning alert length (not clip length) and lack "
            "the 'zones' field. UI3 v91 fixed a bug where this was mishandled."
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
                        "Earliest clip. Int (UTC sec), ISO-8601, or relative "
                        "shorthand like '-2h', '-1d'."
                    ),
                },
                "enddate": {
                    "description": (
                        "Latest clip. Int (UTC sec), ISO-8601, or relative "
                        "shorthand like '-2h', '-1d'."
                    ),
                },
                "view": {
                    "type": "string",
                    "description": (
                        "Database view filter. Per BI manual § *cliplist*: 'all', "
                        "'new', 'stored', 'alerts', 'aux1'..'aux5', 'flagged', 'export', "
                        "'archive', 'confirmed', 'canceled'. Per UI3 source (extra "
                        "alert-side values that work here, returning alert items): "
                        "'people', 'vehicles', 'zonea'..'zoneh', 'dio', 'onvif', "
                        "'audio', 'external', 'cancelled' (British)."
                    ),
                },
                "search": {"type": "string", "description": "Memo substring filter (server-side)."},
                "tiles": {
                    "type": "boolean",
                    "description": "If true, return one entry per day instead of one per clip.",
                },
                "limit": {"type": "integer", "description": "Max clips returned (default 50)."},
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "List BI clips"},
    )
