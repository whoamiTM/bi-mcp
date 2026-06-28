"""Timeline tool — 24h activity buckets per camera."""

from __future__ import annotations

import time
from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from ..utils.time import parse_since
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_get_timeline")
def _tool_get_timeline(client: BiClients, args: dict) -> Any:
    if not args.get("camera"):
        raise BiBadRequest("bi_get_timeline requires a 'camera' argument (camera short name)")
    payload: dict[str, Any] = {}
    for k in ("camera", "msecpp"):
        if k in args:
            payload[k] = args[k]
    for k in ("startdate", "enddate"):
        if k in args and args[k] is not None:
            try:
                payload[k] = parse_since(args[k], arg_name=k)
            except ValueError as e:
                raise BiBadRequest(str(e)) from e
    # BI's timeline cmd defaults a missing enddate to epoch 0, which inverts the
    # window — a lone startdate ("activity since T") returns nothing. A missing
    # startdate is fine (BI treats it as "from the beginning"). So: backfill a
    # missing enddate to now, and default a fully-bare call to a trailing 24h
    # window (matching the tool's name; a rangeless query returns empty spans).
    if "startdate" in payload and "enddate" not in payload:
        payload["enddate"] = int(time.time())
    elif "startdate" not in payload and "enddate" not in payload:
        now = int(time.time())
        payload["enddate"] = now
        payload["startdate"] = now - 86400
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
            "Requires 'camera' short name. If both 'startdate' and 'enddate' are "
            "omitted, defaults to the last 24 hours (BI returns empty spans for a "
            "rangeless query)."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {"type": "string", "description": "Camera short name. Required."},
                "startdate": {
                    "description": (
                        "Window start. Int (UTC sec), ISO-8601, or relative shorthand "
                        "like '-2h', '-1d'. If given without 'enddate', the end "
                        "defaults to now (so 'activity since T' works). Defaults to "
                        "24h before now when both bounds are omitted."
                    ),
                },
                "enddate": {
                    "description": (
                        "Window end. Int (UTC sec), ISO-8601, or relative shorthand "
                        "like '-2h', '-1d'. Defaults to now if omitted."
                    ),
                },
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
