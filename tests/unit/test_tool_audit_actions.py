"""Tests for bi_audit_actions cohort voting."""

from __future__ import annotations

from typing import Any

from bi_mcp.tools.tools_audit_actions import _audit


def _shaped(profiles: Any) -> dict[str, Any]:
    return {
        "on_trigger": {
            "actions": [
                {"index": 0, "type": "web_or_mqtt", "description": "x", "path": "p",
                 "enabled": True, "profiles": profiles}
            ]
        }
    }


def _profiles_findings(values: list[str]) -> list[dict[str, Any]]:
    cohorts, _ = _audit({f"C{i}": _shaped(v) for i, v in enumerate(values)}, "both", 3)
    return [f for f in cohorts[0]["outliers"] if f["field"] == "profiles"]


def test_strict_majority_flags_the_minority() -> None:
    (f,) = _profiles_findings(["A", "A", "B"])
    assert f["majority_value"] == "A"
    assert f["outliers"] == [{"member": "C2#0@on_trigger", "value": "B"}]


def test_even_split_is_reported_as_no_majority() -> None:
    (f,) = _profiles_findings(["A", "A", "B", "B"])
    assert f["no_majority"] is True
    assert "outliers" not in f  # no camera is singled out
    assert f["values"] == [
        {"value": "A", "members": ["C0#0@on_trigger", "C1#0@on_trigger"]},
        {"value": "B", "members": ["C2#0@on_trigger", "C3#0@on_trigger"]},
    ]


def test_plurality_without_majority_is_reported() -> None:
    (f,) = _profiles_findings(["A", "A", "B", "C"])
    assert f["no_majority"] is True
    assert [v["value"] for v in f["values"]] == ["A", "B", "C"]
