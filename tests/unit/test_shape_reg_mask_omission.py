"""Regression tests for maskbits_* hex omission in shape_reg.

A single `maskbits_127x72` blob is ~9KB of hex; a full PTZ\\Presets
read covers 15 presets = ~135KB and overflows tool-result token
budgets. shape_reg now strips the `hex` value from any
`{"_type": "binary", "hex": "...", "len": N}` payload whose containing
key starts with `maskbits_`, replacing it with `_omitted: "hex"` and
preserving `len`.

The omission is the default. Callers who actually need the polygon
bytes pass `include_masks=True`. Non-`maskbits_*` binary blobs are
never touched — only the mask family is in scope.
"""

from __future__ import annotations

from bi_mcp.shapers import shape_reg


def _binary(hex_str: str = "deadbeef") -> dict:
    return {"_type": "binary", "hex": hex_str, "len": len(hex_str) // 2}


def _parsed_with_masks() -> dict:
    return {
        "PTZ\\Presets\\6": {
            "name": "6",
            "desc": "SecCam_1 IVS-2",
            "maskbits_127x72": _binary("a" * 9144),
        },
        "PTZ\\Presets\\7": {
            "name": "8",
            "desc": "Preset8",
            "maskbits_127x72": _binary("b" * 9144),
        },
        "Motion": {
            "maskbits_127x72": _binary("c" * 9144),
            "some_other_binary": _binary("ff" * 32),
        },
    }


def test_masks_omitted_by_default() -> None:
    parsed = _parsed_with_masks()
    out = shape_reg(parsed, camera_short="SecCam_11AI", mtime_age_days=0.1)
    p6 = out["data"]["PTZ\\Presets\\6"]
    mask = p6["maskbits_127x72"]
    assert mask["_type"] == "binary"
    assert "len" in mask  # preserved from input
    assert mask.get("_omitted") == "hex"
    assert "hex" not in mask


def test_include_masks_returns_full_hex() -> None:
    parsed = _parsed_with_masks()
    out = shape_reg(
        parsed, camera_short="SecCam_11AI", mtime_age_days=0.1, include_masks=True
    )
    mask = out["data"]["PTZ\\Presets\\6"]["maskbits_127x72"]
    assert "hex" in mask
    assert mask["hex"] == "a" * 9144
    assert "_omitted" not in mask


def test_non_maskbits_binaries_are_never_touched() -> None:
    parsed = _parsed_with_masks()
    out = shape_reg(parsed, camera_short="SecCam_X", mtime_age_days=0.1)
    other = out["data"]["Motion"]["some_other_binary"]
    assert other["hex"] == "ff" * 32, "non-mask binaries must keep their hex"


def test_all_maskbits_keys_in_tree_get_stripped() -> None:
    parsed = _parsed_with_masks()
    out = shape_reg(parsed, camera_short="X", mtime_age_days=0.1)
    # All three maskbits_127x72 occurrences should be omitted.
    paths = [
        ("PTZ\\Presets\\6", "maskbits_127x72"),
        ("PTZ\\Presets\\7", "maskbits_127x72"),
        ("Motion", "maskbits_127x72"),
    ]
    for outer, inner in paths:
        mask = out["data"][outer][inner]
        assert "hex" not in mask, f"{outer}/{inner} hex should be stripped"


def test_meta_block_still_present() -> None:
    out = shape_reg({}, camera_short="X", mtime_age_days=0.5)
    assert out["meta"]["mtime_age_days"] == 0.5
    assert out["meta"]["stale"] is False
