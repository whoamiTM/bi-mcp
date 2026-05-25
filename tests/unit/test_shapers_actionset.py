"""Action-set decoder regression tests.

Two layers of coverage:

1. **Clone-fixture tests** use `clone_seccam_10_test.reg` — a small,
   predictable BI clone with one action row of most kinds. These pin
   down the decoder's basic shape (on_trigger block exists, no row
   falls through to "unknown", staleness meta is honest).

2. **Production-hive union test** walks every committed `.reg` in
   `cam settings/` and asserts the union of all action `type` ints
   seen across them covers every code mapped by `_ACTION_TYPE`. This
   is what actually exercises the decoder against real BI installs —
   if a future BI build adds a new `type` code and you re-export, the
   test will flag the gap automatically.

If a future BI build adds a new type code and you HAVEN'T re-exported
anything, the test won't see it — but you also can't be hurt by a
decoder bug for a code that doesn't exist in any hive you care about.
"""

from __future__ import annotations

import pytest

from bi_mcp.errors import BiNotFound
from bi_mcp.reg import list_reg_cameras, parse_reg
from bi_mcp.shapers import _ACTION_TYPE, shape_actionset

# Action-row keys live under both hook subtrees — the type-union must
# scan both, since `_shape_action_entry` decodes them identically.
_ACTION_ROW_PREFIXES = ("Alerts\\OnTrigger\\", "Alerts\\OnReset\\")

CLONE_FIXTURE_SHORT = "clone_seccam_10_test"


# ---------------------------------------------------------------------------
# Clone-fixture tests — small, predictable input
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clone_actionset(reg_venv_available: bool) -> dict:
    if not reg_venv_available:
        pytest.skip(".reg-venv not present — install python-registry there to enable")
    parsed, age_days = parse_reg(CLONE_FIXTURE_SHORT, key_path="Alerts")
    return shape_actionset(parsed, camera_short=CLONE_FIXTURE_SHORT,
                           mtime_age_days=age_days, hook="both")


def test_clone_fixture_has_on_trigger_block(clone_actionset: dict) -> None:
    assert "on_trigger" in clone_actionset, (
        f"clone_seccam_10_test.reg should produce on_trigger; "
        f"got keys: {list(clone_actionset)}"
    )
    assert isinstance(clone_actionset["on_trigger"]["actions"], list)
    assert clone_actionset["on_trigger"]["actions"], "on_trigger.actions is empty"


def test_clone_fixture_decodes_with_no_unknown_types(clone_actionset: dict) -> None:
    """No action row in the clone fixture should fall through to 'unknown'.

    'unknown' means the integer `type` field in the .reg hive isn't in
    `_ACTION_TYPE`. Either BI added a new code or our table is stale.
    """
    unknowns = [a for a in clone_actionset["on_trigger"]["actions"] if a["type"] == "unknown"]
    assert not unknowns, (
        f"{len(unknowns)} action(s) fell through to 'unknown' "
        f"(raw type ints: {[a.get('type_raw') for a in unknowns]})"
    )


def test_clone_fixture_meta_marks_age(clone_actionset: dict) -> None:
    """`meta.stale` is True when the .reg is older than 7 days.

    Pins down the staleness contract — tools rely on this to warn callers
    that they're quoting values from a possibly out-of-date export.
    """
    meta = clone_actionset["meta"]
    assert "mtime_age_days" in meta
    assert isinstance(meta["stale"], bool)
    assert meta["stale"] == (meta["mtime_age_days"] > 7.0)


# ---------------------------------------------------------------------------
# Production-hive union test — every committed .reg, all type codes
# ---------------------------------------------------------------------------


def _collect_seen_type_ints(reg_venv_available: bool) -> tuple[set[int], dict[int, list[str]]]:
    """Walk every committed .reg, return (seen_type_ints, cameras_per_int).

    `cameras_per_int` is for diagnostics — when a code is missing from the
    union, it's useful to know which cameras DO have it (or that none do).
    """
    if not reg_venv_available:
        pytest.skip(".reg-venv not present")
    seen: set[int] = set()
    per_int: dict[int, list[str]] = {}
    for short in list_reg_cameras():
        try:
            parsed, _ = parse_reg(short, key_path="Alerts")
        except BiNotFound:
            # Cameras without an Alerts subtree (rare — usually clones with
            # no actions) are not a coverage gap; skip silently. Any other
            # exception (parser breakage, malformed hive) propagates and
            # fails the test loudly.
            continue
        for k, v in parsed.items():
            matched_prefix = next((p for p in _ACTION_ROW_PREFIXES if k.startswith(p)), None)
            if matched_prefix is None:
                continue
            tail = k[len(matched_prefix):]
            if not tail.isdigit():
                continue
            t = v.get("type")
            if isinstance(t, int):
                seen.add(t)
                per_int.setdefault(t, []).append(short)
    return seen, per_int


def test_production_hives_cover_every_mapped_action_type(reg_venv_available: bool) -> None:
    """Every type code in `_ACTION_TYPE` must appear in at least one committed hive.

    This is the real coverage test: it doesn't care what one synthetic
    fixture contains, it checks that the decoder is exercised end-to-end
    against the type codes you actually run in production.

    If this test fails with a missing code, it means `_ACTION_TYPE` has
    a mapping for something none of your cameras use. Either:
      - delete the unused mapping from `_ACTION_TYPE` (it's dead code),
      - or re-export a camera that uses that action kind so the
        decoder gets real coverage.
    """
    expected = set(_ACTION_TYPE.keys())
    seen, per_int = _collect_seen_type_ints(reg_venv_available)
    missing = expected - seen
    extra_in_hives = seen - expected  # type ints present in hives but unmapped
    assert not missing, (
        f"type code(s) {sorted(missing)} are mapped in _ACTION_TYPE "
        f"({[_ACTION_TYPE[t] for t in sorted(missing)]}) but appear in NO "
        f"committed .reg. Either delete the unused mapping or re-export a "
        f"camera that uses the action."
    )
    assert not extra_in_hives, (
        f"type code(s) {sorted(extra_in_hives)} appear in committed hives "
        f"but are NOT mapped in _ACTION_TYPE (cameras: "
        f"{ {t: per_int[t] for t in extra_in_hives} }). "
        f"Add them to _ACTION_TYPE or they'll decode as 'unknown'."
    )


def test_production_hives_decode_with_no_unknown_types(reg_venv_available: bool) -> None:
    """Across every committed .reg, no action row should decode to 'unknown'.

    Catches the case where a hive contains a type code _ACTION_TYPE doesn't
    map. Overlaps with the union test above but reports differently — this
    one fires per-row, useful when only one camera is affected.
    """
    if not reg_venv_available:
        pytest.skip(".reg-venv not present")
    offenders: list[tuple[str, int, int | None]] = []  # (camera, row_idx, raw_type)
    for short in list_reg_cameras():
        try:
            parsed, age_days = parse_reg(short, key_path="Alerts")
        except BiNotFound:
            continue
        shaped = shape_actionset(parsed, camera_short=short,
                                 mtime_age_days=age_days, hook="both")
        for block_name in ("on_trigger", "on_reset"):
            block = shaped.get(block_name)
            if not block:
                continue
            for action in block["actions"]:
                if action["type"] == "unknown":
                    offenders.append((short, action["index"], action.get("type_raw")))
    assert not offenders, (
        f"{len(offenders)} action row(s) decoded as 'unknown': {offenders}"
    )


# ---------------------------------------------------------------------------
# trig_source bit 7 regression — see reference_bi_trig_source_bit7 memory
# ---------------------------------------------------------------------------


def test_trig_source_raw_preserves_bit7_decoded_list_excludes_it(
    reg_venv_available: bool,
) -> None:
    """trig_source contract across production hives:

    - `trig_source_raw` is the unmodified BI value (bit 7 still set if BI
      set it) so bi_audit_actions can flag any row where bit 7 *isn't*
      set — that would invalidate the "always set" assumption in
      reference_bi_trig_source_bit7.md.
    - `trig_source` (decoded list) only contains UI-mapped sources; bit
      7 is not in `_TRIG_SOURCE_BITS` and must never appear in the list.

    If a future BI version starts toggling bit 7, this test still passes
    (raw stays raw) but `bi_audit_actions` will surface the divergence.
    The test fails only if someone re-introduces a mask before raw
    assignment, or accidentally adds bit 7 to the decoded label map.
    """
    if not reg_venv_available:
        pytest.skip(".reg-venv not present")

    valid_source_labels = {"motion", "onvif", "audio", "external", "dio", "group", "ai"}
    rows_with_trig_source = 0
    bit7_set_count = 0

    for short in list_reg_cameras():
        try:
            parsed, age_days = parse_reg(short, key_path="Alerts")
        except BiNotFound:
            continue
        shaped = shape_actionset(parsed, camera_short=short,
                                 mtime_age_days=age_days, hook="both")
        for block_name in ("on_trigger", "on_reset"):
            block = shaped.get(block_name)
            if not block:
                continue
            for action in block["actions"]:
                filters = action.get("filters")
                if not filters or "trig_source_raw" not in filters:
                    continue
                rows_with_trig_source += 1
                raw = filters["trig_source_raw"]
                # Must be the unmodified int BI stored (no masking).
                assert isinstance(raw, int), (
                    f"{short} row {action.get('index')}: trig_source_raw "
                    f"should be int, got {type(raw).__name__}"
                )
                if raw & 0b10000000:
                    bit7_set_count += 1
                # Decoded list (if present) must only contain known labels —
                # bit 7 has no label, so it must never leak through.
                decoded = filters.get("trig_source", [])
                stray = set(decoded) - valid_source_labels
                assert not stray, (
                    f"{short} row {action.get('index')}: decoded trig_source "
                    f"contains unmapped label(s) {stray} (raw={raw})"
                )

    # Sanity: the production hives must actually exercise this field, or
    # the test above is vacuous. As of 2026-05-25 all 170 observed rows
    # have bit 7 set — if that ever drops to zero, the "always set"
    # assumption is broken and reference_bi_trig_source_bit7.md needs
    # revisiting (audit tooling will flag it independently).
    assert rows_with_trig_source > 0, (
        "no rows with trig_source seen across production hives — test is vacuous"
    )
    assert bit7_set_count == rows_with_trig_source, (
        f"bit 7 was clear on {rows_with_trig_source - bit7_set_count} of "
        f"{rows_with_trig_source} rows — the 'always set' assumption in "
        f"reference_bi_trig_source_bit7.md is broken. Re-investigate what "
        f"bit 7 actually represents before relying on the mask."
    )
