"""BI system log tool — admin-gated."""

from __future__ import annotations

from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiAdminRequired
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


@log_tool_usage("bi_list_log")
def _tool_list_log(client: BiClients, args: dict) -> Any:
    payload: dict[str, Any] = {}
    # `reset=true` clears BI's Status/Log display — a mutation, deliberately
    # not forwarded.
    for k in ("level", "id"):
        if k in args:
            payload[k] = args[k]
    # resolve_admin() triggers read.login() if needed so the lazy
    # BI_USER-as-admin probe answers correctly on a fresh process.
    if client.resolve_admin() is None:
        raise BiAdminRequired(
            "bi_list_log requires admin Blue Iris credentials; the `log` JSON cmd "
            "is gated behind admin. Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env, "
            "or grant admin to the existing BI_USER."
        )
    raw = client.admin_call("log", **payload)
    if args.get("raw"):
        return raw
    return shapers.shape_log(raw, limit=int(args.get("limit", 100)))


def register() -> None:
    register_tool(
        "bi_list_log",
        _tool_list_log,
        description=(
            "Recent Blue Iris system log entries. Optional 'level' (0=info, "
            "1=warning, 2=error) and 'limit' (default 100). Admin required."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "level": {"type": "integer", "description": "0=info, 1=warning, 2=error."},
                "limit": {"type": "integer", "description": "Max entries (default 100)."},
            },
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "List BI log entries"},
    )
