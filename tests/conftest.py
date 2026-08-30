"""Shared pytest fixtures and path resolution.

Tests parse .reg hives from two places: live camera exports in the parent
blueiris/ `cam settings/` dir, and synthetic fixtures under `tests/fixtures/`
— hives for cameras that no longer exist in the live BI install (e.g.
clone_seccam_10_test, the all-action-types decoding sandbox dropped from
`cam settings/` in parent-repo commit 265b593). We merge both into one
session temp dir and point bi-mcp at it via BI_MCP_REG_DIR before any
reg.py call, so tests run from any CWD and never depend on a fixture
surviving a live re-export sweep.

Both sources are LOCAL-ONLY: `cam settings/` lives in the parent blueiris
repo, and `tests/fixtures/` is gitignored because the hives carry base64
camera credentials (`ippw`). An external clone of the public repo has
neither. Tests that need real hives must therefore gate on
`requires_reg_hives` / `requires_fixture`, which SKIP when the data is
absent — never silently pass. A hive-dependent assertion that runs with no
hives present proves nothing, so skipping is the only honest outcome.
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
        # Either may be absent on an external clone; glob on a missing dir
        # returns empty rather than raising, so this degrades to an empty
        # union dir and the requires_* gates turn that into skips.
        for reg in sorted(src_dir.glob("*.reg")):
            shutil.copy2(reg, _reg_union_dir)
    os.environ["BI_MCP_REG_DIR"] = _reg_union_dir


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001
    if _reg_union_dir:
        shutil.rmtree(_reg_union_dir, ignore_errors=True)


def reg_dir() -> Path:
    """The merged reg dir reg.py reads. Empty (not missing) when no hives exist."""
    return Path(os.environ["BI_MCP_REG_DIR"])


def available_reg_shorts() -> list[str]:
    """Camera shorts with a parseable .reg in the merged dir. May be empty."""
    d = os.environ.get("BI_MCP_REG_DIR")
    if not d:
        return []
    return sorted(p.stem for p in Path(d).glob("*.reg"))


def requires_reg_hives() -> None:
    """Skip the calling test unless at least one .reg hive is present.

    Call at the top of any test whose assertions are meaningless without
    real hives. Skips (never passes) so an external clone or CI checkout
    reports honestly instead of green-on-nothing.
    """
    if not available_reg_shorts():
        pytest.skip(
            "No .reg hives available — needs the parent blueiris repo's "
            "`cam settings/` or local-only tests/fixtures/ (both gitignored: "
            "they contain camera credentials). Local-only test."
        )


def requires_fixture(short: str) -> None:
    """Skip unless the named fixture hive is present (e.g. clone_seccam_10_test)."""
    if short not in available_reg_shorts():
        pytest.skip(
            f"Fixture hive {short}.reg not available — local-only "
            "(tests/fixtures/ is gitignored; hives carry `ippw` credentials)."
        )


@pytest.fixture(scope="session")
def cam_settings_dir() -> Path:
    """The merged reg dir reg.py actually reads (live exports + fixtures)."""
    return Path(os.environ["BI_MCP_REG_DIR"])
