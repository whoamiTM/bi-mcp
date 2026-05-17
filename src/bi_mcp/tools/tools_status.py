"""Status / session tools — BI system vitals + login capability data.

This module is the **canonical reference** for tool authoring style in bi-mcp.
Match these patterns when adding a new tool:

  * One dispatch function per tool, prefixed ``_tool_*``.
  * ``@log_tool_usage("<tool_name>")`` wraps the dispatch fn.
  * Validate args first; raise typed errors from ``..errors`` for known
    failure modes.
  * ``raw=True`` always returns the raw BI payload — never substitute a
    different shape.
  * Register every tool in ``register()`` with a description, schema, and
    safety annotations (readOnlyHint at minimum).
"""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiAdminRequired
from ..utils.logging import log_tool_usage
from .registry import register_tool


COMMON_SCHEMA: dict[str, Any] = {
    "raw": {
        "type": "boolean",
        "description": "If true, return the raw Blue Iris JSON instead of the shaped view.",
    },
}


# ---------------------------------------------------------------------------
# bi_get_status — system snapshot
# ---------------------------------------------------------------------------


@log_tool_usage("bi_get_status")
def _tool_get_status(client: BiClients, args: dict) -> Any:
    raw = client.call("status")
    if args.get("raw"):
        return raw
    return shapers.shape_status(raw)


# ---------------------------------------------------------------------------
# bi_get_session — login capability info
# ---------------------------------------------------------------------------


@log_tool_usage("bi_get_session")
def _tool_get_session(client: BiClients, args: dict) -> Any:
    if not client.login_data:
        client.login()
    raw = client.login_data or {}
    if args.get("raw"):
        return raw
    return shapers.shape_session_info(raw)


# ---------------------------------------------------------------------------
# bi_get_sysconfig — admin-gated system config snapshot
# ---------------------------------------------------------------------------


@log_tool_usage("bi_get_sysconfig")
def _tool_get_sysconfig(client: BiClients, args: dict) -> Any:
    # resolve_admin() triggers read.login() if needed so the lazy
    # BI_USER-as-admin probe answers correctly on a fresh process.
    if client.resolve_admin() is None:
        raise BiAdminRequired(
            "bi_get_sysconfig requires admin Blue Iris credentials; the `sysconfig` "
            "JSON cmd is gated behind admin. Set BI_ADMIN_USER/BI_ADMIN_PASS in "
            "bi-mcp/.env, or grant admin to the existing BI_USER."
        )
    # Per BI manual § *sysconfig*: read-side returns archive / schedule /
    # manrecsec. BI builds may include additional fields. Don't pass any
    # write-side args from `args` — those are the mutating fields and should
    # never be invoked here.
    raw = client.admin_call("sysconfig")
    if args.get("raw"):
        return raw
    return shapers.shape_sysconfig(raw)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register() -> None:
    register_tool(
        "bi_get_status",
        _tool_get_status,
        description=(
            "Snapshot of Blue Iris system state: active profile, schedule hold/run, "
            "CPU%, RAM, disk usage, uptime, DIO outputs, warnings."
        ),
        schema={"type": "object", "properties": {**COMMON_SCHEMA}, "additionalProperties": True},
        annotations={"readOnlyHint": True, "title": "Get BI status"},
    )

    register_tool(
        "bi_get_session",
        _tool_get_session,
        description=(
            "Blue Iris version/license, time zone, capabilities of the current user "
            "(admin/ptz/clips/etc), and available profile/schedule/stream names."
        ),
        schema={"type": "object", "properties": {**COMMON_SCHEMA}, "additionalProperties": True},
        annotations={"readOnlyHint": True, "title": "Get BI session info"},
    )

    register_tool(
        "bi_get_sysconfig",
        _tool_get_sysconfig,
        description=(
            "System config snapshot (admin required): FTP archive enable, global "
            "schedule on/off, manual record time limit, plus any DIO/MQTT state BI "
            "exposes inline. Use this instead of asking the user to screenshot "
            "Settings → Other."
        ),
        schema={"type": "object", "properties": {**COMMON_SCHEMA}, "additionalProperties": True},
        annotations={"readOnlyHint": True, "title": "Get BI system config"},
    )
