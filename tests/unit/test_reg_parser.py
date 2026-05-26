"""Regression tests for the .reg hive parser.

These tests confirm that every .reg export in `cam settings/` still parses
without crashing — guards against future python-registry upgrades or
parser tweaks that silently drop subtrees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bi_mcp.errors import BiError
from bi_mcp.reg import _MAX_HIVE_DEPTH, _MAX_HIVE_KEYS, _walk, parse_reg


def _camera_shorts(cam_dir: Path) -> list[str]:
    """Camera short names that have a .reg export (excludes .backup/.txt etc.)."""
    return sorted(p.stem for p in cam_dir.glob("*.reg"))


def test_at_least_one_reg_file_exists(cam_settings_dir: Path) -> None:
    """If this fails, the fixtures we test against are gone — bail early."""
    shorts = _camera_shorts(cam_settings_dir)
    assert shorts, f"no .reg files found under {cam_settings_dir}"


@pytest.mark.parametrize("short", _camera_shorts(Path(__file__).resolve().parents[2].parent / "cam settings"))
def test_each_reg_file_parses_without_error(short: str) -> None:
    """Each .reg in cam settings/ parses to a non-empty dict."""
    parsed, age_days = parse_reg(short)
    assert isinstance(parsed, dict)
    assert parsed, f"{short}.reg parsed to an empty dict"
    assert age_days >= 0.0


def test_reg_parser_returns_flat_backslash_joined_subkey_paths() -> None:
    """Per reg.py module docstring: keys are backslash-joined subkey paths
    relative to the hive root (the camera short *is* the root, so it's not
    prefixed onto child keys). Output is a single flat dict, not nested.

    Pins down the contract: callers walk by string-prefix matching (e.g.
    ``Alerts\\OnTrigger\\``), not by nested dict traversal.
    """
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


# ---------------------------------------------------------------------------
# Parser bounds — depth + key-count guards in _walk()
#
# These guards exist because the in-process parser has no subprocess timeout
# to fall back on. A pathological hive (corrupted file, future BI schema
# regression, or a deliberately-crafted input) could otherwise spin a tool
# thread for an unbounded time. Real BI hives are ~4 deep and ~200 keys,
# so the ceilings (50 / 100_000) are generous and only trip on pathology.
# ---------------------------------------------------------------------------


class _FakeValue:
    def __init__(self, name: str, value: object) -> None:
        self._name = name
        self._value = value

    def name(self) -> str:
        return self._name

    def value(self) -> object:
        return self._value


class _FakeKey:
    """Stands in for a python-registry RegistryKey for _walk() unit tests.

    Implements just the surface _walk() touches: .name(), .values(),
    .subkeys(). No real regf parsing.
    """

    def __init__(self, name: str, values: list[_FakeValue] | None = None,
                 subkeys: list["_FakeKey"] | None = None) -> None:
        self._name = name
        self._values = values or []
        self._subkeys = subkeys or []

    def name(self) -> str:
        return self._name

    def values(self) -> list[_FakeValue]:
        return self._values

    def subkeys(self) -> list["_FakeKey"]:
        return self._subkeys


def _make_chain(depth: int) -> _FakeKey:
    """Return a single-child chain of depth `depth` (root at depth 0)."""
    leaf = _FakeKey(f"k{depth}", values=[_FakeValue("v", 1)])
    cur = leaf
    for i in range(depth - 1, -1, -1):
        cur = _FakeKey(f"k{i}", subkeys=[cur])
    return cur


def test_walk_rejects_excessive_depth() -> None:
    """A hive nested deeper than _MAX_HIVE_DEPTH must raise BiError, not
    RecursionError. Build a chain one level past the limit."""
    chain = _make_chain(_MAX_HIVE_DEPTH + 2)
    with pytest.raises(BiError, match="exceeds 50 levels"):
        _walk(chain)


def test_walk_rejects_excessive_key_count() -> None:
    """A flat-but-huge hive (lots of siblings, shallow) must raise BiError
    once the budget is exhausted, not silently grind through millions of
    keys. Synthesize a root with > _MAX_HIVE_KEYS direct children."""
    children = [_FakeKey(f"k{i}", values=[_FakeValue("v", i)])
                for i in range(_MAX_HIVE_KEYS + 5)]
    root = _FakeKey("root", subkeys=children)
    with pytest.raises(BiError, match="more than 100000 keys"):
        _walk(root)


def test_walk_allows_normal_depth_and_size() -> None:
    """Sanity: a small hive well under both limits parses fine. Guards
    against the bounds accidentally rejecting realistic input."""
    chain = _make_chain(5)
    result = _walk(chain)
    # Only the leaf has a value in _make_chain; intermediate subkeys are
    # value-less and get dropped by the `if vals:` check in _walk.
    assert result == {"k0\\k1\\k2\\k3\\k4\\k5": {"v": 1}}


def test_parse_hive_key_budget_is_shared_across_subtrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key-count budget must persist across every top-level subtree
    in a full-hive parse — not reset per call. Otherwise a hive with N
    top-level subkeys can do N × _MAX_HIVE_KEYS work before tripping the
    guard, which defeats the protection.

    Build a fake hive with two top-level subtrees, each below
    _MAX_HIVE_KEYS individually but together exceeding it. Confirm
    _parse_hive raises BiError rather than completing the parse.
    """
    from bi_mcp import reg as reg_mod

    half = _MAX_HIVE_KEYS // 2 + 100  # each subtree alone fits, sum overflows
    subtree_a = _FakeKey(
        "A", subkeys=[_FakeKey(f"a{i}", values=[_FakeValue("v", i)]) for i in range(half)]
    )
    subtree_b = _FakeKey(
        "B", subkeys=[_FakeKey(f"b{i}", values=[_FakeValue("v", i)]) for i in range(half)]
    )
    root = _FakeKey("ROOT", subkeys=[subtree_a, subtree_b])

    class _FakeRegistry:
        def __init__(self, path: str) -> None:  # noqa: ARG002
            pass

        def root(self) -> _FakeKey:
            return root

    monkeypatch.setattr(reg_mod.Registry, "Registry", _FakeRegistry)
    with pytest.raises(BiError, match="more than 100000 keys"):
        reg_mod._parse_hive(Path("/unused"), key_path=None)

