"""Shared pytest fixtures and path resolution.

Tests parse .reg hives from two places: live camera exports in the parent
blueiris/ `cam settings/` dir, and synthetic fixtures under `tests/fixtures/`
— hives for cameras that no longer exist in the live BI install (e.g.
clone_seccam_10_test, the all-action-types decoding sandbox dropped from
`cam settings/` in parent-repo commit 265b593). We merge both into one
session temp dir and point bi-mcp at it via BI_MCP_REG_DIR before any
reg.py call, so tests run from any CWD and never depend on a fixture
surviving a live re-export sweep.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# bi-mcp/tests/conftest.py → bi-mcp/tests → bi-mcp → blueiris/
BIMCP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BIMCP_ROOT.parent
CAM_SETTINGS_DIR = PROJECT_ROOT / "cam settings"
FIXTURES_DIR = BIMCP_ROOT / "tests" / "fixtures"

_reg_union_dir: str | None = None


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Point bi-mcp at a merged live-exports + fixtures dir for the session.

    Done at configure-time (not via an autouse fixture) so the env is set
    before module-scoped fixtures or parametrize-time code reads it.
    copy2 preserves mtimes so the staleness meta stays honest.
    """
    global _reg_union_dir
    _reg_union_dir = tempfile.mkdtemp(prefix="bi-mcp-reg-union-")
    for src_dir in (CAM_SETTINGS_DIR, FIXTURES_DIR):
        for reg in sorted(src_dir.glob("*.reg")):
            shutil.copy2(reg, _reg_union_dir)
    os.environ["BI_MCP_REG_DIR"] = _reg_union_dir


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001
    if _reg_union_dir:
        shutil.rmtree(_reg_union_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def cam_settings_dir() -> Path:
    """The merged reg dir reg.py actually reads (live exports + fixtures)."""
    return Path(os.environ["BI_MCP_REG_DIR"])
