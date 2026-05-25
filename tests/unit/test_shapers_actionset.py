"""Action-set decoder regression tests.

`clone_seccam_10_test.reg` is a throwaway BI clone fixture (commit d43b63f)
crafted to contain exactly one action row of every kind in BI 5.9.9.71.
Per AGENTS.md § *Action-set decoder coverage*, all 13 mapped type strings
should appear and nothing should fall through to "unknown".

If a future BI build adds a new type code, or python-registry changes how
values are decoded, these tests will fail loudly with the specific
fixture/field that drifted.
"""

from __future__ import annotations

import pytest

from bi_mcp.reg import parse_reg
from bi_mcp.shapers import shape_actionset

FIXTURE_SHORT = "clone_seccam_10_test"

# All 14 action types mapped by _ACTION_TYPE in shapers.py (codes 0-13).
#
# Note: `web_or_mqtt` is one entry (further disambiguated via protocol
# field), not split into separate `web`/`mqtt` types.
#
# TODO(fixture-gap): `clone_seccam_10_test.reg` currently contains 13 of
# these 14 — it is missing `do_command` (type=12). AGENTS.md and the
# fixture's commit message both claim "all 13 action types", but
# _ACTION_TYPE actually defines 14 (codes 0-13 inclusive). To close this
# gap: open the clone camera's alert settings in BI, add one "do command"
# action row, re-export `clone_seccam_10_test.reg`, and this test will
# go green. Until then, the failing test is the reminder.
EXPECTED_TYPES = {
    "sound", "push", "run", "web_or_mqtt", "email", "sms", "phone",
    "dio", "toast", "ftp", "shield", "schedule", "do_command", "wait",
}


@pytest.fixture(scope="module")
def actionset(reg_venv_available: bool) -> dict:
    if not reg_venv_available:
        pytest.skip(".reg-venv not present — install python-registry there to enable")
    parsed, age_days = parse_reg(FIXTURE_SHORT, key_path="Alerts")
    return shape_actionset(parsed, camera_short=FIXTURE_SHORT,
                           mtime_age_days=age_days, hook="both")


def test_actionset_has_on_trigger_block(actionset: dict) -> None:
    assert "on_trigger" in actionset, (
        f"clone_seccam_10_test.reg should produce on_trigger; got keys: {list(actionset)}"
    )
    assert isinstance(actionset["on_trigger"]["actions"], list)
    assert actionset["on_trigger"]["actions"], "on_trigger.actions is empty"


def test_no_action_type_falls_through_to_unknown(actionset: dict) -> None:
    """Every row's `type` must map to a named string — none should be 'unknown'.

    'unknown' means the integer `type` field in the .reg hive isn't in our
    _ACTION_TYPE table. Either BI added a new code or our table is stale.
    """
    unknowns = [a for a in actionset["on_trigger"]["actions"] if a["type"] == "unknown"]
    assert not unknowns, (
        f"{len(unknowns)} action(s) fell through to 'unknown' "
        f"(raw type ints: {[a.get('type_raw') for a in unknowns]})"
    )


def test_fixture_covers_all_14_action_types(actionset: dict) -> None:
    """The fixture should contain one row of every type mapped by _ACTION_TYPE.

    Currently expected to FAIL — see the TODO(fixture-gap) note above on
    EXPECTED_TYPES. Closing the gap = adding a `do_command` row to
    clone_seccam_10_test.reg via the BI UI and re-exporting.
    """
    seen = {a["type"] for a in actionset["on_trigger"]["actions"]}
    missing = EXPECTED_TYPES - seen
    extra = seen - EXPECTED_TYPES
    assert not missing, f"fixture is missing action types: {sorted(missing)}"
    assert not extra, f"fixture has unexpected action types: {sorted(extra)}"


def test_meta_marks_fixture_age(actionset: dict) -> None:
    """`meta.stale` is True when the .reg is older than 7 days.

    Pins down the staleness contract — tools rely on this to warn callers
    that they're quoting values from a possibly out-of-date export.
    """
    meta = actionset["meta"]
    assert "mtime_age_days" in meta
    assert isinstance(meta["stale"], bool)
    # Sanity: the boolean tracks the age field
    assert meta["stale"] == (meta["mtime_age_days"] > 7.0)
