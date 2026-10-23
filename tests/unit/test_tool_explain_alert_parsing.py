"""Tests for bi_explain_alert_chain's memo parsing and log-window helpers."""

from __future__ import annotations

from bi_mcp.tools.tools_explain_alert import (
    _evaluate_compound_predicate,
    _has_retrigger,
    _parse_memo_objects,
)


def test_memo_comma_separated() -> None:
    assert _parse_memo_objects("car:71%,truck:55%") == {"car": 71, "truck": 55}


def test_memo_semicolon_joined_sources() -> None:
    # BI joins memos from different sources with ';' (live 2026-10-19:
    # 'person:62%;occupied', 'preset_2;car:80%'). Non-object tokens are kept
    # as zero-confidence keys and must not break the scored terms.
    assert _parse_memo_objects("person:62%;occupied") == {"person": 62, "occupied": None}
    assert _parse_memo_objects("preset_2;car:80%,truck:40%") == {
        "preset_2": None, "car": 80, "truck": 40,
    }


def test_memo_duplicate_object_keeps_highest_confidence() -> None:
    assert _parse_memo_objects("person:55%;person:81%") == {"person": 81}
    assert _parse_memo_objects("person:81%;person") == {"person": 81}


def test_threshold_on_semicolon_memo() -> None:
    mo = _parse_memo_objects("person:62%;nothing found")
    assert _evaluate_compound_predicate("person:50", mo, None)["matched"] is True


def test_retrigger_before_alert_is_ignored() -> None:
    before = [{"obj": "C", "msg": "Retriggered", "_epoch": 970}]
    after = [{"obj": "C", "msg": "Retriggered", "_epoch": 1030}]
    assert _has_retrigger(before, "C", 1000) is False
    assert _has_retrigger(after, "C", 1000) is True


def test_threshold_ignores_confidence_less_terms() -> None:
    mo = _parse_memo_objects("person:85%;preset_1")
    r = _evaluate_compound_predicate("person+preset_1:80", mo, None)
    assert r["matched"] is True and r["min_component_confidence_pct"] == 85
    r = _evaluate_compound_predicate("person+preset_1:90", mo, None)
    assert r["matched"] is False


def test_threshold_with_no_confidence_bearing_term_is_undetermined() -> None:
    mo = _parse_memo_objects("person;preset_1")
    r = _evaluate_compound_predicate("person+preset_1:80", mo, None)
    assert r["matched"] is None
