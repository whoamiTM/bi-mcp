"""Clip-list tool — full clip inventory, complementary to bi_list_alerts."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
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
    for k in ("startdate", "enddate", "view", "search", "tiles"):
        if k in args:
            payload[k] = args[k]
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
            "(or 'Index' for all). Optional 'view' (one of: all, new, stored, alerts, "
            "aux1-5, flagged, export, archive, confirmed, canceled), 'startdate'/"
            "'enddate' (unix epoch), 'search' (memo text), 'tiles' (true=one entry "
            "per day). 'limit' default 50."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (required). Use 'Index' for all cameras.",
                },
                "startdate": {"type": "integer", "description": "Unix epoch start."},
                "enddate": {"type": "integer", "description": "Unix epoch end."},
                "view": {
                    "type": "string",
                    "description": (
                        "Database view filter: all, new, stored, alerts, aux1-5, "
                        "flagged, export, archive, confirmed, canceled."
                    ),
                },
                "search": {"type": "string", "description": "Memo substring filter."},
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
