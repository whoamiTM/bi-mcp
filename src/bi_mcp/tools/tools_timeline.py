"""Timeline tool — 24h activity buckets per camera."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_get_timeline")
def _tool_get_timeline(client: BiClients, args: dict) -> Any:
    if not args.get("camera"):
        raise BiBadRequest("bi_get_timeline requires a 'camera' argument (camera short name)")
    payload: dict[str, Any] = {}
    for k in ("camera", "startdate", "enddate", "msecpp"):
        if k in args:
            payload[k] = args[k]
    raw = client.call("timeline", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_timeline(raw)


def register() -> None:
    register_tool(
        "bi_get_timeline",
        _tool_get_timeline,
        description=(
            "24-hour activity timeline (motion/trigger/alert buckets) for a camera. "
            "Requires 'camera' short name."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {"type": "string", "description": "Camera short name. Required."},
                "startdate": {"type": "integer", "description": "Unix epoch start."},
                "enddate": {"type": "integer", "description": "Unix epoch end."},
                "msecpp": {
                    "type": "integer",
                    "description": "Display quantization (ms per pixel), min 128.",
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI timeline"},
    )
