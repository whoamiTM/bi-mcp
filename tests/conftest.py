"""Shared pytest fixtures and path resolution.

Tests run from any CWD: we resolve the parent blueiris/ project root
(which holds `cam settings/` and `.reg-venv/`) relative to this file,
then point bi-mcp at it via env vars before any reg.py call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# bi-mcp/tests/conftest.py → bi-mcp/tests → bi-mcp → blueiris/
BIMCP_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BIMCP_ROOT.parent
CAM_SETTINGS_DIR = PROJECT_ROOT / "cam settings"
REG_VENV_PY_POSIX = PROJECT_ROOT / ".reg-venv" / "bin" / "python3"
REG_VENV_PY_WIN = PROJECT_ROOT / ".reg-venv" / "Scripts" / "python.exe"


def _reg_venv_python() -> Path | None:
    for candidate in (REG_VENV_PY_POSIX, REG_VENV_PY_WIN):
        if candidate.exists():
            return candidate
    return None


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Point bi-mcp at the parent blueiris/ resources for the whole test session.

    Done at configure-time (not via an autouse fixture) so the env is set
    before module-scoped fixtures or parametrize-time code reads it.
    """
    os.environ["BI_MCP_REG_DIR"] = str(CAM_SETTINGS_DIR)
    py = _reg_venv_python()
    if py is not None:
        os.environ["BI_MCP_REG_VENV_PYTHON"] = str(py)


@pytest.fixture(scope="session")
def reg_venv_available() -> bool:
    return _reg_venv_python() is not None


@pytest.fixture(scope="session")
def cam_settings_dir() -> Path:
    return CAM_SETTINGS_DIR
