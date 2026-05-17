"""Tools package — per-domain tool modules + auto-discovery registry.

Public surface for callers (server.py, cli.py):

    from .tools import TOOLS, TOOL_DESCRIPTIONS, TOOL_ANNOTATIONS, TOOL_SCHEMAS

New tool modules just have to (a) live in ``tools_<domain>.py`` here, and
(b) expose a ``register()`` function that calls ``register_tool(...)`` once
per tool — they don't have to touch this file.

**Important:** callers MUST run ``load_dotenv()`` (or otherwise populate
env vars) before importing this module — ``collect_tools()`` reads
``BI_MCP_ALLOW_MUTATIONS`` to decide whether to register the mutation
module. Both ``server.py`` and ``cli.py`` handle that ordering correctly.
"""

from __future__ import annotations

from .registry import (
    TOOLS,
    TOOL_DESCRIPTIONS,
    TOOL_ANNOTATIONS,
    TOOL_SCHEMAS,
    collect_tools,
    register_tool,
    mutations_enabled,
)

# Trigger discovery on import. Callers must have loaded .env first (server.py
# and cli.py both do, then import this module).
collect_tools()

__all__ = [
    "TOOLS",
    "TOOL_DESCRIPTIONS",
    "TOOL_ANNOTATIONS",
    "TOOL_SCHEMAS",
    "collect_tools",
    "register_tool",
    "mutations_enabled",
]
