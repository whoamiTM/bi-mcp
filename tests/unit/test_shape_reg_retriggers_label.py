"""Regression tests for the retriggers_label annotation in shape_reg.

The BI Alert tab "When this camera is re-triggered" dropdown has 4 values
stored as the int ``retriggers`` under ``Alerts\\<N>``:

    0 = "New triggers only"
    1 = "New and all retriggers"
    2 = "New zones/sources only (additive)"
    3 = "New zones/sources only (exclusive)"

Enum confirmed empirically on 2026-05-26 by flipping each UI value on
SecCam_1 and diffing fresh .reg exports — every other field stayed
constant across the four exports.

shape_reg annotates any ``Alerts\\<N>`` block carrying an int ``retriggers``
with a ``retriggers_label`` field, so LLM callers don't have to memorize
the int mapping when reading raw .reg blocks.
"""

from __future__ import annotations

from bi_mcp.shapers import (
    _RETRIGGERS,
    _annotate_retriggers_label,
)


# ---------------------------------------------------------------------------
# Unit tests against synthetic dicts — pin the annotator contract.
# Production-hive walk lives below.
# ---------------------------------------------------------------------------


def test_each_known_enum_value_labels_correctly() -> None:
    """Each of the four UI options decodes to its labeled string."""
    parsed = {f"Alerts\\{i + 1}": {"retriggers": i} for i in range(4)}
    out = _annotate_retriggers_label(parsed)
    for i in range(4):
        entry = out[f"Alerts\\{i + 1}"]
        assert entry["retriggers"] == i
        assert entry["retriggers_label"] == _RETRIGGERS[i]


def test_unknown_int_gets_unknown_prefix() -> None:
    """An int outside 0-3 still gets a label so callers can rely on the
    field being present whenever ``retriggers`` is present."""
    parsed = {"Alerts\\1": {"retriggers": 99}}
    out = _annotate_retriggers_label(parsed)
    assert out["Alerts\\1"]["retriggers_label"] == "unknown_99"


def test_missing_retriggers_field_no_annotation() -> None:
    """An Alerts\\<N> block without ``retriggers`` is not annotated."""
    parsed = {"Alerts\\1": {"sync": 0, "camsync": ""}}
    out = _annotate_retriggers_label(parsed)
    assert "retriggers_label" not in out["Alerts\\1"]


def test_non_int_retriggers_no_annotation() -> None:
    """Non-int retriggers (e.g. corrupt .reg) is not labeled — safer to
    surface the raw value than to fabricate a label from a string."""
    parsed = {"Alerts\\1": {"retriggers": "3"}}
    out = _annotate_retriggers_label(parsed)
    assert "retriggers_label" not in out["Alerts\\1"]


def test_action_row_retriggers_field_not_annotated() -> None:
    """``Alerts\\OnTrigger\\<N>`` rows have unrelated fields; even if one
    happened to carry an int named ``retriggers``, it isn't the same
    enum and must not be annotated. The annotator scopes to per-profile
    ``Alerts\\<N>`` blocks only."""
    parsed = {
        "Alerts\\OnTrigger": {"enabled": 1, "count": 1},
        "Alerts\\OnTrigger\\0": {"command": 2201, "retriggers": 3},
        "Alerts\\OnReset\\0": {"retriggers": 2},
    }
    out = _annotate_retriggers_label(parsed)
    for k in parsed:
        assert "retriggers_label" not in out[k], f"{k} must not be annotated"


def test_annotator_does_not_mutate_input() -> None:
    """The annotator must not mutate ``parsed`` or any inner subdict."""
    import copy
    parsed = {
        "Alerts\\1": {"retriggers": 3, "sync": 0, "camsync": "SecCam_2"},
        "Alerts\\3": {"retriggers": 2, "sync": 1, "camsync": ""},
        "Motion": {"sync": 0},  # unrelated, passes through by reference
    }
    snapshot = copy.deepcopy(parsed)
    out = _annotate_retriggers_label(parsed)
    assert parsed == snapshot, "annotator must not mutate input"
    # Annotated entries must be distinct objects from the input.
    assert out["Alerts\\1"] is not parsed["Alerts\\1"]
    assert out["Alerts\\3"] is not parsed["Alerts\\3"]
    # Unrelated entries pass through by reference.
    assert out["Motion"] is parsed["Motion"]


def test_annotator_returns_same_keys() -> None:
    """The output dict's key set must equal the input's."""
    parsed = {
        "Alerts\\1": {"retriggers": 3},
        "Alerts\\OnTrigger\\0": {"command": 2201},
        "PTZ": {"enabled": 1},
    }
    out = _annotate_retriggers_label(parsed)
    assert set(out.keys()) == set(parsed.keys())
