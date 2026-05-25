"""Mutation registration gate test.

AGENTS.md § *Server identity*: mutating tools register only when
`BI_MCP_ALLOW_MUTATIONS=1`. The registry walks `tools_*.py` and skips
`tools_mutations` entirely when the flag is unset — they're never imported.

This test pins down that contract. A regression here (e.g. accidentally
removing the skip in registry.py) would silently expose mutating tools
to every MCP client on every install.

The registry uses module-level `_collected` state, so we reload the
package under each env permutation rather than mutating shared state.
"""

from __future__ import annotations

import importlib
import sys

import pytest

MUTATING_TOOL_NAMES = {
    "bi_trigger_camera",
    "bi_set_ptz_preset",
    "bi_export_clip",
    "bi_set_profile",
    "bi_update_record",
    "bi_set_camera",
}


def _reload_tools_pkg():
    """Force re-import of bi_mcp.tools so collect_tools() runs again."""
    for mod_name in list(sys.modules):
        if mod_name == "bi_mcp.tools" or mod_name.startswith("bi_mcp.tools."):
            del sys.modules[mod_name]
    return importlib.import_module("bi_mcp.tools")


@pytest.fixture
def fresh_tools_pkg():
    """Snapshot/restore sys.modules around the test so other tests are unaffected."""
    snapshot = {k: v for k, v in sys.modules.items() if k.startswith("bi_mcp.tools")}
    try:
        yield _reload_tools_pkg
    finally:
        for k in list(sys.modules):
            if k == "bi_mcp.tools" or k.startswith("bi_mcp.tools."):
                del sys.modules[k]
        sys.modules.update(snapshot)


def test_mutations_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, fresh_tools_pkg
) -> None:
    """With BI_MCP_ALLOW_MUTATIONS unset, no mutating tool is registered."""
    monkeypatch.delenv("BI_MCP_ALLOW_MUTATIONS", raising=False)
    tools_pkg = fresh_tools_pkg()
    leaked = MUTATING_TOOL_NAMES & set(tools_pkg.TOOLS)
    assert not leaked, (
        f"mutating tools registered without BI_MCP_ALLOW_MUTATIONS=1: {sorted(leaked)}"
    )


def test_mutations_disabled_with_falsy_values(
    monkeypatch: pytest.MonkeyPatch, fresh_tools_pkg
) -> None:
    """Common 'off' string values should NOT enable mutations."""
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "0")
    tools_pkg = fresh_tools_pkg()
    assert not (MUTATING_TOOL_NAMES & set(tools_pkg.TOOLS))


def test_mutations_enabled_with_flag(
    monkeypatch: pytest.MonkeyPatch, fresh_tools_pkg
) -> None:
    """With BI_MCP_ALLOW_MUTATIONS=1, every documented mutating tool registers."""
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "1")
    tools_pkg = fresh_tools_pkg()
    missing = MUTATING_TOOL_NAMES - set(tools_pkg.TOOLS)
    assert not missing, f"mutating tools missing even with flag on: {sorted(missing)}"
    # And each is annotated as not-read-only (destructive default).
    for name in MUTATING_TOOL_NAMES:
        ann = tools_pkg.TOOL_ANNOTATIONS[name]
        assert ann.get("readOnlyHint") is not True, (
            f"{name} should not be readOnlyHint=true (it mutates BI state)"
        )
