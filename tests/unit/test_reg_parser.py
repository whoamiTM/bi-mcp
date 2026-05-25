"""Regression tests for the .reg hive parser.

These tests confirm that every .reg export in `cam settings/` still parses
without crashing — guards against future python-registry upgrades or
parser tweaks that silently drop subtrees.

Requires the .reg-venv sibling virtualenv (auto-detected by conftest.py).
Skipped cleanly when unavailable so the suite stays runnable on CI hosts
that don't have python-registry installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bi_mcp.reg import parse_reg


def _camera_shorts(cam_dir: Path) -> list[str]:
    """Camera short names that have a .reg export (excludes .backup/.txt etc.)."""
    return sorted(p.stem for p in cam_dir.glob("*.reg"))


def test_at_least_one_reg_file_exists(cam_settings_dir: Path) -> None:
    """If this fails, the fixtures we test against are gone — bail early."""
    shorts = _camera_shorts(cam_settings_dir)
    assert shorts, f"no .reg files found under {cam_settings_dir}"


@pytest.mark.parametrize("short", _camera_shorts(Path(__file__).resolve().parents[2].parent / "cam settings"))
def test_each_reg_file_parses_without_error(
    short: str,
    reg_venv_available: bool,
) -> None:
    """Each .reg in cam settings/ parses to a non-empty dict."""
    if not reg_venv_available:
        pytest.skip(".reg-venv not present — install python-registry there to enable")
    parsed, age_days = parse_reg(short)
    assert isinstance(parsed, dict)
    assert parsed, f"{short}.reg parsed to an empty dict"
    assert age_days >= 0.0


def test_reg_parser_returns_flat_backslash_joined_subkey_paths(reg_venv_available: bool) -> None:
    """Per reg.py module docstring: keys are backslash-joined subkey paths
    relative to the hive root (the camera short *is* the root, so it's not
    prefixed onto child keys). Output is a single flat dict, not nested.

    Pins down the contract: callers walk by string-prefix matching (e.g.
    ``Alerts\\OnTrigger\\``), not by nested dict traversal.
    """
    if not reg_venv_available:
        pytest.skip(".reg-venv not present")
    parsed, _ = parse_reg("SecCam_3")
    # No nested dicts — every value at top level is a dict-of-values, not a dict-of-subkeys.
    for k, v in parsed.items():
        assert isinstance(k, str) and k, "registry path keys must be non-empty strings"
        assert "\\\\" not in k, f"key {k!r} has a double backslash — escaping bug?"
        assert isinstance(v, dict), f"top-level value at {k!r} should be a values dict"
        # Each value entry is a registry value (name → primitive/list/binary-marker),
        # never another subkey dict.
        for vname, vval in v.items():
            assert isinstance(vname, str)
            assert not (isinstance(vval, dict) and set(vval.keys()) > {"_type", "hex", "len"}), (
                f"value {k}\\{vname} looks like a nested subkey dict, not a leaf value"
            )
