"""Action-set decoder tool — semantic view over Alerts\\OnTrigger and OnReset.

Action sets are the per-camera lists of actions BI runs when an alert fires
(or resets). They live in the camera's .reg export under Alerts\\OnTrigger\\<N>
and Alerts\\OnReset\\<N>, and contain integer-coded fields (`type`, `command`,
`trig_source`, `web_proto1`, plus bitmasks like `profiles` and `trig_zones`).

bi_get_reg already returns this data raw; bi_get_actionset wraps it with a
decoder layer derived empirically from this install (Pass 1, 2026-05-17),
extended against jaydeel's authoritative decoder tables on ipcamtalk thread
85627 (2026-05-21). Remaining unknowns: bit 7 of `trig_source`, and
per-type payload field names for action kinds we haven't exercised in the UI.
See shapers.shape_actionset for the decoder tables.
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

_VALID_HOOKS = ("on_trigger", "on_reset", "both")


@log_tool_usage("bi_get_actionset")
def _tool_get_actionset(client: BiClients, args: dict) -> Any:
    short = args.get("camera") or args.get("short") or args.get("short_name")
    if not short:
        raise BiBadRequest(
            "bi_get_actionset requires a 'camera' argument (camera short name, e.g. 'SecCam_3')"
        )
    hook = args.get("hook", "both")
    if hook not in _VALID_HOOKS:
        raise BiBadRequest(
            f"bi_get_actionset 'hook' must be one of {_VALID_HOOKS}, got {hook!r}"
        )
    parsed, age_days = reg_mod.parse_reg(short, key_path="Alerts")
    if args.get("raw"):
        return {
            "camera": short,
            "mtime_age_days": age_days,
            "data": parsed,
        }
    return shapers.shape_actionset(
        parsed, camera_short=short, mtime_age_days=age_days, hook=hook
    )


def register() -> None:
    register_tool(
        "bi_get_actionset",
        _tool_get_actionset,
        description=(
            "Return the semantic action set (OnTrigger and/or OnReset) for a "
            "camera. Decodes the full action `type` map (0-13), the `command` "
            "table for type=12 do-commands (PTZ presets 2201-2456, action "
            "sets, brightness/contrast/gain, plus ~60 individual codes), "
            "`web_proto1` (http/https/mqtt), `run_action`, `trig_allzones`, "
            "and the profiles/zones/diobits/trig_source bitmasks into "
            "readable lists. Unmapped values fall through with the raw int "
            "preserved alongside (e.g. `command_raw`, `trig_source_raw`). "
            "Source data comes from the camera's .reg export, so changes "
            "made via the BI UI mid-session won't be visible until a "
            "re-export."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
                "hook": {
                    "type": "string",
                    "enum": list(_VALID_HOOKS),
                    "description": (
                        "Which hook to return: 'on_trigger', 'on_reset', or "
                        "'both' (default)."
                    ),
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI camera action set"},
    )
