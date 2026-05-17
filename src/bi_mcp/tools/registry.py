"""Auto-discovery registry for bi-mcp tools.

Each ``tools_<domain>.py`` module in this package exposes a ``register()``
function that calls ``register_tool(name, fn, description, schema, annotations)``
once per tool. ``collect_tools()`` walks the package, imports each
``tools_<domain>`` module, and invokes its ``register()``.

Mutation modules (``tools_mutations``) are skipped when
``BI_MCP_ALLOW_MUTATIONS`` is unset — they're not even imported, so the tool
list stays clean and there are no "permission denied" surprises at runtime.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Any, Callable

from ..client import BiClients

ToolFn = Callable[[BiClients, dict], Any]

# Public registries. Keys are tool names (e.g. ``bi_get_status``).
TOOLS: dict[str, ToolFn] = {}
TOOL_DESCRIPTIONS: dict[str, str] = {}
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {}
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {}


# Modules whose tools mutate Blue Iris state. Skipped at discovery time unless
# the mutations env flag is set.
_MUTATION_MODULES = {"tools_mutations"}


def mutations_enabled() -> bool:
    """Return True iff ``BI_MCP_ALLOW_MUTATIONS`` is truthy in the environment."""
    return os.environ.get("BI_MCP_ALLOW_MUTATIONS", "0").strip() in ("1", "true", "yes", "on")


def register_tool(
    name: str,
    fn: ToolFn,
    *,
    description: str,
    schema: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = None,
) -> None:
    """Register one tool. Called by each ``tools_<domain>.register()``.

    ``annotations`` is the MCP safety-hint block — at minimum it should set
    ``readOnlyHint``. ``destructiveHint`` and ``idempotentHint`` are recommended
    on any mutating tool.
    """
    TOOLS[name] = fn
    TOOL_DESCRIPTIONS[name] = description
    if schema is None:
        schema = {"type": "object", "additionalProperties": True}
    TOOL_SCHEMAS[name] = schema
    if annotations is None:
        annotations = {"readOnlyHint": True}
    TOOL_ANNOTATIONS[name] = annotations


_collected = False


def collect_tools() -> None:
    """Walk this package, import each ``tools_*`` module, run ``register()``.

    Idempotent: safe to call repeatedly. Mutation modules are skipped unless
    ``BI_MCP_ALLOW_MUTATIONS=1``.
    """
    global _collected
    if _collected:
        return
    _collected = True

    pkg_name = __package__  # "bi_mcp.tools"
    pkg = importlib.import_module(pkg_name)
    allow_mutations = mutations_enabled()

    for mod_info in pkgutil.iter_modules(pkg.__path__):
        mod_name = mod_info.name
        if not mod_name.startswith("tools_"):
            continue
        if mod_name in _MUTATION_MODULES and not allow_mutations:
            continue
        mod = importlib.import_module(f"{pkg_name}.{mod_name}")
        register_fn = getattr(mod, "register", None)
        if register_fn is None:
            continue
        register_fn()
