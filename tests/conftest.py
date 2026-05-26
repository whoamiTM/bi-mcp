"""Shared pytest fixtures and path resolution.

Tests run from any CWD: we resolve the parent blueiris/ project root
(which holds `cam settings/`) relative to this file, then point bi-mcp at
it via BI_MCP_REG_DIR before any reg.py call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# bi-mcp/tests/conftest.py → bi-mcp/tests → bi-mcp → blueiris/
BIMCP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BIMCP_ROOT.parent
CAM_SETTINGS_DIR = PROJECT_ROOT / "cam settings"


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Point bi-mcp at the parent blueiris/ cam settings/ for the test session.

    Done at configure-time (not via an autouse fixture) so the env is set
    before module-scoped fixtures or parametrize-time code reads it.
    """
    os.environ["BI_MCP_REG_DIR"] = str(CAM_SETTINGS_DIR)


@pytest.fixture(scope="session")
def cam_settings_dir() -> Path:
    return CAM_SETTINGS_DIR
