"""PTZ status (query-only) tool."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_get_ptz_status")
def _tool_get_ptz_status(client: BiClients, args: dict) -> Any:
    camera = args.get("camera")
    if not camera:
        raise BiBadRequest(
            "bi_get_ptz_status requires a 'camera' argument (camera short name)"
        )
    # Per BI manual § *ptz*: omit `button` to query state; any button value
    # triggers an actual PTZ operation. We never send `button` from this tool.
    raw = client.call("ptz", camera=camera)
    if args.get("raw"):
        return raw
    return shapers.shape_ptz_status(raw)


def register() -> None:
    register_tool(
        "bi_get_ptz_status",
        _tool_get_ptz_status,
        description=(
            "PTZ current position, preset list, and lock state for one camera. "
            "Camera must have PTZ enabled in BI."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "PTZ camera short name. Required.",
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI PTZ status"},
    )
