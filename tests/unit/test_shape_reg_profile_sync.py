"""Regression tests for the profile-sync passthrough annotation in shape_reg.

BI's UI exposes a "Sync this profile with profile 1" checkbox on the
Alerts/Motion/AI/Clips/Watchdog pages for profiles 2-7. When checked, BI
either omits the `<Page>\\<N>` subkey entirely or writes it with
`sync: 1, camsync: ""` — in both cases the live config for profile N is
whatever `<Page>\\1` contains.

shape_reg annotates the materialized passthroughs with
`_synced_with: "profile_1"` so LLM callers don't misread the (stale)
fields under those subkeys as the active config. This was discovered on
2026-05-25 when SecCam_11's `AI\\2` and `AI\\3` were reported to the user
as "standalone profiles with `camsync` blank" instead of being recognized
as sync-with-1 passthroughs.

Fixtures: clone_seccam_10 (sync UNCHECKED via UI) and clone_seccam_10_test
(sync CHECKED — the default state). Diff revealed the encoding empirically.
"""

from __future__ import annotations

import pytest

from bi_mcp.reg import parse_reg
from bi_mcp.shapers import (
    _PROFILE_SYNC_PAGES,
    _annotate_profile_sync_passthroughs,
    _is_profile_sync_passthrough,
    shape_reg,
)


def _shaped(camera_short: str, reg_venv_available: bool) -> dict:
    if not reg_venv_available:
        pytest.skip(".reg-venv not present — install python-registry there to enable")
    parsed, age_days = parse_reg(camera_short)
    return shape_reg(parsed, camera_short=camera_short, mtime_age_days=age_days)


# ---------------------------------------------------------------------------
# Table-driven unit tests — no .reg-venv dependency, always run on CI.
#
# Codex adversarial review (2026-05-25) flagged that the original tests
# skip when .reg-venv is missing, so the safety net evaporates on hosts
# without python-registry. These tests exercise the classifier and the
# annotator directly with synthetic dicts so the encoding rules stay
# pinned regardless of fixture availability.
# ---------------------------------------------------------------------------


# (key, val, expected) — val is the inner subdict.
_CLASSIFIER_CASES: list[tuple[str, dict, bool]] = [
    # --- positives: sync=1, camsync='', profile-N for N >= 2 ---
    ("Alerts\\2",   {"sync": 1, "camsync": ""},                   True),
    ("Alerts\\7",   {"sync": 1, "camsync": ""},                   True),
    ("AI\\2",       {"sync": 1, "camsync": "",  "smart": 32763},  True),
    ("Clips\\3",    {"sync": 1, "camsync": ""},                   True),
    ("Watchdog\\2", {"sync": 1, "camsync": ""},                   True),

    # --- Motion off-by-one positives: N >= 1 ---
    ("Motion\\1",   {"sync": 1, "camsync": ""},                   True),  # profile 2
    ("Motion\\6",   {"sync": 1, "camsync": ""},                   True),  # profile 7

    # --- negatives: sync != 1 ---
    ("Alerts\\2",   {"sync": 0, "camsync": ""},                   False),
    ("AI\\3",       {"sync": 0, "camsync": "SecCam_1"},           False),  # cross-camera
    ("Motion\\1",   {"sync": 0, "camsync": ""},                   False),

    # --- negatives: camsync non-empty (cross-camera sync, not passthrough) ---
    ("Alerts\\3",   {"sync": 1, "camsync": "SecCam_1"},           False),
    ("AI\\2",       {"sync": 1, "camsync": "OtherCam"},           False),

    # --- negatives: profile 1 (sync target, not source) ---
    ("Alerts\\1",   {"sync": 1, "camsync": ""},                   False),
    ("AI\\1",       {"sync": 1, "camsync": ""},                   False),
    ("Clips\\1",    {"sync": 1, "camsync": ""},                   False),
    ("Watchdog\\1", {"sync": 1, "camsync": ""},                   False),
    # Motion's unnumbered top-level key is profile 1 — never a passthrough.
    ("Motion",      {"sync": 1, "camsync": ""},                   False),

    # --- negatives: non-profile-sync page ---
    ("PTZ\\2",      {"sync": 1, "camsync": ""},                   False),
    ("Alerts\\OnTrigger\\3", {"sync": 1, "camsync": ""},          False),  # action row, depth > 2
    ("Schedule\\2", {"sync": 1, "camsync": ""},                   False),

    # --- negatives: malformed tail ---
    ("AI\\foo",     {"sync": 1, "camsync": ""},                   False),
    ("AI\\2a",      {"sync": 1, "camsync": ""},                   False),

    # --- negatives: val isn't a dict ---
    ("AI\\2",       None,                                          False),  # type: ignore[list-item]
]


@pytest.mark.parametrize("key, val, expected", _CLASSIFIER_CASES)
def test_classifier_table(key: str, val, expected: bool) -> None:
    """_is_profile_sync_passthrough returns True iff the row meets all
    five encoding rules (page in set, tail is digit, N meets per-page
    minimum, sync==1, camsync==\"\"). One row per case so failures point
    at the exact rule violated."""
    assert _is_profile_sync_passthrough(key, val) is expected, (
        f"classifier mismatch for {key!r} → {val!r}"
    )


def test_annotator_does_not_mutate_input() -> None:
    """The annotator must not mutate `parsed` or any inner subdict.

    Codex flagged the original in-place mutation as a latent risk if the
    caller reuses `parsed` after shaping (e.g. for raw views or caching).
    Build a parsed dict, snapshot it deeply, run the annotator, assert
    the snapshot still matches.
    """
    import copy
    parsed = {
        "Alerts\\2": {"sync": 1, "camsync": ""},
        "AI\\3":     {"sync": 1, "camsync": "", "smart": 32763},
        "Motion\\1": {"sync": 1, "camsync": ""},
        "Alerts\\1": {"sync": 0, "camsync": "SecCam_1"},  # control: not touched
    }
    snapshot = copy.deepcopy(parsed)
    out = _annotate_profile_sync_passthroughs(parsed)
    assert parsed == snapshot, "annotator must not mutate input dict or inner subdicts"
    # Annotation must be present in output.
    assert out["Alerts\\2"]["_synced_with"] == "profile_1"
    assert out["AI\\3"]["_synced_with"] == "profile_1"
    assert out["Motion\\1"]["_synced_with"] == "profile_1"
    # Non-passthrough rows passed through by reference (no copy needed).
    assert out["Alerts\\1"] is parsed["Alerts\\1"]
    # Annotated rows must be distinct objects from the input.
    assert out["Alerts\\2"] is not parsed["Alerts\\2"]


def test_annotator_returns_same_keys() -> None:
    """The output dict's key set must equal the input's — annotation
    never adds or drops top-level entries."""
    parsed = {
        "Alerts\\2": {"sync": 1, "camsync": ""},
        "Alerts\\1": {"sync": 0, "camsync": ""},
        "PTZ":       {"foo": "bar"},
        "Motion":    {"sync": 0, "camsync": "SecCam_1"},
        "state":     {"x": 1},
    }
    out = _annotate_profile_sync_passthroughs(parsed)
    assert set(out.keys()) == set(parsed.keys())


def test_shape_reg_does_not_mutate_input() -> None:
    """End-to-end check: shape_reg's pipeline must preserve the input.

    Belt-and-suspenders on top of test_annotator_does_not_mutate_input —
    catches regressions where a future refactor reintroduces mutation in
    shape_reg's outer flow rather than the helper.
    """
    import copy
    parsed = {
        "Alerts\\2": {"sync": 1, "camsync": ""},
        "Motion\\1": {"sync": 1, "camsync": ""},
    }
    snapshot = copy.deepcopy(parsed)
    shape_reg(parsed, camera_short="TestCam", mtime_age_days=0.0)
    assert parsed == snapshot


def test_passthrough_marker_present_on_sync1_blank_camsync(reg_venv_available: bool) -> None:
    """A profile-N subkey with sync=1 and empty camsync gets `_synced_with: profile_1`."""
    out = _shaped("clone_seccam_10_test", reg_venv_available)
    data = out["data"]
    # clone_seccam_10_test has Alerts\2..7 all with sync=1, camsync=""
    for n in range(2, 8):
        key = f"Alerts\\{n}"
        if key not in data:
            continue
        entry = data[key]
        assert entry.get("sync") == 1 and entry.get("camsync", "") == "", (
            f"Fixture changed shape — {key} should still be sync=1, camsync=''"
        )
        assert entry.get("_synced_with") == "profile_1", (
            f"{key} should be annotated as sync-with-profile_1 passthrough"
        )


def test_passthrough_marker_absent_when_sync_zero(reg_venv_available: bool) -> None:
    """A profile-N subkey with sync=0 is NOT a passthrough — no marker."""
    out = _shaped("clone_seccam_10", reg_venv_available)
    data = out["data"]
    # clone_seccam_10 has Alerts\3 with sync=0, camsync='SecCam_1' (cross-camera, not passthrough)
    entry = data.get("Alerts\\3")
    if entry is None:
        pytest.skip("Fixture no longer has Alerts\\3 — un-skip after re-export")
    assert entry.get("sync") == 0, "Fixture changed — Alerts\\3 should be sync=0"
    assert "_synced_with" not in entry, (
        f"Alerts\\3 has sync=0 (cross-camera link), must NOT be marked as profile-1 passthrough"
    )


def test_passthrough_marker_absent_when_camsync_nonblank(reg_venv_available: bool) -> None:
    """sync=1 but camsync='<other_cam>' = cross-camera sync, not profile-1 passthrough.

    (None of our current fixtures exhibit this — sync=1 always pairs with
    blank camsync in clone_seccam_10 — so this test is forward-looking
    against the encoding rule. Add a fixture or skip cleanly if absent.)
    """
    out = _shaped("clone_seccam_10", reg_venv_available)
    data = out["data"]
    found_cross_camera = False
    for key, entry in data.items():
        parts = key.split("\\")
        if len(parts) != 2:
            continue
        page, tail = parts
        if page not in _PROFILE_SYNC_PAGES or not tail.isdigit() or int(tail) < 2:
            continue
        if entry.get("sync") == 1 and entry.get("camsync", "") not in ("", None):
            found_cross_camera = True
            assert "_synced_with" not in entry, (
                f"{key} has cross-camera sync ({entry['camsync']!r}), must NOT be marked passthrough"
            )
    if not found_cross_camera:
        pytest.skip("No cross-camera sync profile-N row in fixture — encoding rule untested for that case")


def test_profile_1_never_annotated(reg_venv_available: bool) -> None:
    """Profile 1 is the source of sync, not a target — never gets the marker.

    Profile 1's `sync`/`camsync` pair means cross-camera sync (to a peer
    camera), which is a different mechanism. We only annotate N>=2.
    """
    out = _shaped("clone_seccam_10_test", reg_venv_available)
    data = out["data"]
    for page in _PROFILE_SYNC_PAGES:
        key = f"{page}\\1"
        entry = data.get(key)
        if entry is None:
            continue
        assert "_synced_with" not in entry, (
            f"{key} (profile 1) must never be marked as profile-1 passthrough"
        )


def test_motion_off_by_one_is_handled(reg_venv_available: bool) -> None:
    """Motion uses off-by-one numbering: `Motion` = profile 1, `Motion\\1` = profile 2.

    Codex caught a bug where the original implementation blanket-skipped
    `N < 2`, so synced Motion profile 2 (stored at `Motion\\1`) never got
    annotated. Pin the per-page minimum-N rule via a fixture that exhibits
    sync=1,camsync="" on `Motion\\<N>` for N>=1.
    """
    out = _shaped("clone_seccam_10_test", reg_venv_available)
    data = out["data"]
    # In clone_seccam_10_test, Motion\2..6 all have sync=1, camsync=''.
    # These represent profiles 3..7 per BI's off-by-one numbering.
    annotated_any = False
    for n in range(2, 7):
        key = f"Motion\\{n}"
        entry = data.get(key)
        if entry is None:
            continue
        if entry.get("sync") == 1 and entry.get("camsync", "") == "":
            assert entry.get("_synced_with") == "profile_1", (
                f"{key} (profile {n+1}) should be annotated as profile-1 passthrough"
            )
            annotated_any = True
    assert annotated_any, (
        "Fixture clone_seccam_10_test should expose at least one synced "
        "Motion\\<N> entry to exercise this case — re-check fixture or rule"
    )


def test_motion_no_number_key_never_annotated(reg_venv_available: bool) -> None:
    """`Motion` with no profile number IS profile 1 — must never be marked passthrough.

    The off-by-one fix opens `Motion\\1` for annotation, but the unnumbered
    `Motion` key still represents profile 1 (the sync target). It must
    remain unannotated even if it has a sync/camsync pair (which it does,
    for cross-camera sync).
    """
    out = _shaped("clone_seccam_10_test", reg_venv_available)
    motion = out["data"].get("Motion")
    if motion is None:
        pytest.skip("Fixture has no top-level Motion key — encoding rule untested")
    assert "_synced_with" not in motion, (
        "Top-level `Motion` (profile 1) must never be marked as profile-1 passthrough"
    )


def test_non_profile_pages_not_annotated(reg_venv_available: bool) -> None:
    """Subkeys outside the per-profile pages never get the marker.

    E.g. `Alerts\\OnTrigger\\3` is action row 3, not profile 3. Its
    `sync`/`camsync` fields (if any) mean nothing in profile-sync terms.
    """
    out = _shaped("clone_seccam_10_test", reg_venv_available)
    data = out["data"]
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        parts = key.split("\\")
        if len(parts) != 2:
            continue
        page, tail = parts
        if page in _PROFILE_SYNC_PAGES:
            continue
        assert "_synced_with" not in entry, (
            f"{key} is not on a per-profile page — must not be marked passthrough"
        )
