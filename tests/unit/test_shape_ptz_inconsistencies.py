"""Regression tests for preset_inconsistencies in shape_ptz_status.

Real-world case from SecCam_11AI: preset slot 7 has desc="Preset8",
suggesting an earlier preset was deleted without renumbering. The
shaper now surfaces these mismatches as informational warnings under
`preset_inconsistencies` — only when at least one is detected, so the
common clean case stays uncluttered.

Two heuristics:
  1. Dict-form entries: `num` field disagrees with 1-indexed position.
  2. String-form entries: desc matching `^Preset\\s*(\\d+)$` whose
     captured digit != slot.

Names like "SecCam_1" or "SecCam_1 IVS-2" never match the regex, so
named presets are never flagged.
"""

from __future__ import annotations

from bi_mcp.shapers import shape_ptz_status


def test_no_inconsistencies_when_presets_are_named() -> None:
    raw = {
        "presets": [
            "SecCam_1",
            "SecCam_3",
            "SecCam_4",
            "SecCam_5",
            "SecCam_6",
            "SecCam_1 IVS-2",
        ],
        "presetnum": 0,
    }
    out = shape_ptz_status(raw)
    assert "preset_inconsistencies" not in out


def test_slot_7_named_preset8_is_flagged() -> None:
    # Real SecCam_11AI shape from the session: slot 7 desc="Preset8".
    raw = {
        "presets": [
            "SecCam_1",       # slot 1
            "SecCam_3",       # slot 2
            "SecCam_4",       # slot 3
            "SecCam_5",       # slot 4
            "SecCam_6",       # slot 5
            "SecCam_1 IVS-2", # slot 6
            "Preset8",        # slot 7 — off-by-one
            "Preset9",        # slot 8 — also off-by-one
        ],
        "presetnum": 0,
    }
    out = shape_ptz_status(raw)
    assert "preset_inconsistencies" in out
    flagged_slots = {item["slot"] for item in out["preset_inconsistencies"]}
    assert 7 in flagged_slots
    assert 8 in flagged_slots


def test_preset_matching_slot_is_not_flagged() -> None:
    raw = {
        "presets": ["Preset 1", "Preset 2", "Preset 3"],
        "presetnum": 0,
    }
    out = shape_ptz_status(raw)
    assert "preset_inconsistencies" not in out


def test_dict_form_num_mismatch_is_flagged() -> None:
    raw = {
        "presets": [
            {"num": 1, "desc": "A"},
            {"num": 5, "desc": "B"},  # at index 1 (slot 2), num=5 — mismatch
            {"num": 3, "desc": "C"},
        ],
        "presetnum": 0,
    }
    out = shape_ptz_status(raw)
    assert "preset_inconsistencies" in out
    flagged_slots = {item["slot"] for item in out["preset_inconsistencies"]}
    assert 2 in flagged_slots


def test_empty_presets_returns_no_inconsistencies_key() -> None:
    out = shape_ptz_status({"presets": [], "presetnum": 0})
    assert "preset_inconsistencies" not in out


def test_inconsistency_entry_has_useful_fields() -> None:
    raw = {"presets": ["SecCam_1", "SecCam_3", "Preset8"]}
    out = shape_ptz_status(raw)
    items = out["preset_inconsistencies"]
    assert len(items) == 1
    item = items[0]
    assert item["slot"] == 3
    assert item["desc"] == "Preset8"
    assert "hint" in item and item["hint"]
