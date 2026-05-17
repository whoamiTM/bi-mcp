"""Camera .reg export tool — exposes what `camconfig` can't reach.

The BI JSON API does not expose trigger zone polygons, per-class AI
confidence thresholds, per-preset alert-skip flags, ONVIF event handlers,
or alert action definitions. All of those live in a camera's .reg export.
bi_get_reg parses that export and returns the requested subtree.
"""

from __future__ import annotations

from typing import Any

from .. import reg as reg_mod
from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_get_reg")
def _tool_get_reg(client: BiClients, args: dict) -> Any:
    short = args.get("camera") or args.get("short") or args.get("short_name")
    if not short:
        raise BiBadRequest(
            "bi_get_reg requires a 'camera' argument (camera short name, e.g. 'SecCam_3')"
        )
    key_path = args.get("key_path") or args.get("key") or None
    parsed, age_days = reg_mod.parse_reg(short, key_path=key_path)
    if args.get("raw"):
        return {
            "camera": short,
            "mtime_age_days": age_days,
            "data": parsed,
        }
    return shapers.shape_reg(parsed, camera_short=short, mtime_age_days=age_days)


def register() -> None:
    register_tool(
        "bi_get_reg",
        _tool_get_reg,
        description=(
            "Parse a camera's .reg export and return the requested key subtree. "
            "Use this for what the BI JSON API does NOT expose: trigger zone "
            "polygons (Motion\\<profile>\\maskbits_*), per-class AI confidence "
            "thresholds (AI\\<profile>\\smartconf), per-preset alert-skip flags "
            "(PTZ\\Presets\\<n>\\noalerts), ONVIF event handlers (camevents\\<n>), "
            "and alert action definitions (Alerts\\OnTrigger). Optional 'key_path' "
            "limits the response to that subtree (e.g. 'AI\\\\3' for profile 3 "
            "AI config). Returns staleness warning if the .reg file is >7 days old."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
                "key_path": {
                    "type": "string",
                    "description": (
                        "Optional registry subkey path relative to the hive root, "
                        "e.g. 'AI\\\\3', 'Motion\\\\1', 'PTZ\\\\Presets', "
                        "'camevents'. Omit to return the full hive. "
                        "Motion off-by-one quirk (per jaydeel on ipcamtalk, "
                        "'legacy reasons'): 'Motion' (no number) = profile 1; "
                        "'Motion\\\\1' = profile 2; 'Motion\\\\2' = profile 3; "
                        "etc. AI\\\\<N> and PTZ\\\\Presets\\\\<N> use straight "
                        "1:1 indexing, NOT this offset."
                    ),
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI camera .reg subtree"},
    )
