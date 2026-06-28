"""Tests for shape_timeline.

The timeline cmd replies with a single object ``{colors, alerts, clips}`` (BI
manual § JSON Interface, lines 9286-9300). The shaper passes it through verbatim
so that:
  * empty ``alerts``/``clips`` arrays are PRESERVED (callers can tell "no
    activity" from "field absent"),
  * unknown future BI keys survive (forward-compat with BI 6+),
  * span element dicts are left intact (they carry no timestamp fields),
  * the output is a fresh top-level dict (a shallow copy — nested lists are
    still shared with the input, which is fine: BI returns a fresh parse per
    call, nothing caches the response).
"""

from __future__ import annotations

from bi_mcp.shapers import shape_timeline


def test_keeps_empty_alerts_and_clips() -> None:
    raw = {"colors": [9068350], "alerts": [], "clips": []}
    out = shape_timeline(raw)
    assert out == {"colors": [9068350], "alerts": [], "clips": []}


def test_populated_shape_round_trips() -> None:
    raw = {
        "colors": [9068350],
        "alerts": [{"x1": -53, "x2": 8, "type": 402857986, "record": "@655252", "tracks": 1}],
        "clips": [{"x1": 19908, "x2": 86392, "track": 0}],
    }
    out = shape_timeline(raw)
    assert out["colors"] == [9068350]
    assert out["alerts"] == raw["alerts"]
    assert out["clips"] == raw["clips"]


def test_absent_keys_stay_absent() -> None:
    out = shape_timeline({"colors": [9068350]})
    assert out == {"colors": [9068350]}


def test_unknown_keys_preserved() -> None:
    raw = {"colors": [1], "alerts": [], "clips": [], "future_bi6_field": 42}
    out = shape_timeline(raw)
    assert out["future_bi6_field"] == 42
    assert out["alerts"] == []


def test_returns_fresh_top_level_dict() -> None:
    # Shallow copy: the top-level dict is independent (adding/removing a key on
    # the result doesn't touch the input). Nested lists are deliberately shared
    # — documented, and safe because BI returns a fresh parse per call.
    raw = {"colors": [1], "alerts": [], "clips": []}
    out = shape_timeline(raw)
    assert out is not raw
    out["added"] = 1
    assert "added" not in raw


def test_non_dict_passthrough() -> None:
    assert shape_timeline(42) == 42
    assert shape_timeline("x") == "x"
