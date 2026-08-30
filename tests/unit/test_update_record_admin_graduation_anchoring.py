"""Regression: `bi_update_record`'s read→admin graduation must not be
steerable by the caller's own `path`, and the caller-text elision must not
be shreddable into a false NEGATIVE.

Two defects, one file, because they share a call site.

DEFECT 1 (privilege): `_tool_update_record` retries the `update` WRITE with
admin credentials when BI's rejection looks like an auth denial. That match
used to run against `str(e)` — the whole wrapped message, which carries
`path`, which BI echoes back inside its own reason ("Not found: @access
denied"). So a caller-planted path escalated a MUTATION to admin from what
was really a non-auth failure. Third instance of the class already fixed at
`is_terminal_status_message` (export graduation) and `_read_record_state`
(not-a-clip remap).

DEFECT 2 (false negative): the elision was a bare
`reason.replace(path.strip().lower(), " ")`. `str.replace("")` interleaves
the replacement between EVERY character, so an empty/whitespace path turned
"clip not bvr" into " c l i p  n o t  b v r " and no fragment matched — a
genuine not-a-clip silently stopped being recognised. Short paths shred it
too ("t" makes "not bvr" into "no  bvr"). It was unreachable only because a
guard in a DIFFERENT function (`_build_update_payload`) forces a non-empty
`@`-prefixed path; nothing asserted that locally, and
`bi_get_camera_config` shares the helper with no such rule at all.

These tests drive the REAL graduation site (not just the predicate) and
count `admin_call_raw` invocations, because the predicate firing is only
half the claim — what matters is whether admin credentials get spent.
"""

from __future__ import annotations

import sys

import pytest

from bi_mcp.client import BiClient, BiClients, _elide_caller_text
from bi_mcp.errors import BiBadRequest, BiError


@pytest.fixture(autouse=True)
def _allow_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "1")


def _tool():
    from bi_mcp.tools.tools_mutations import _tool_update_record

    return _tool_update_record


class _CountingClients(BiClients):
    """Drives the real graduation site.

    `clipstats` (the pre-read AND the post-write verify) succeeds, so control
    actually REACHES the `update` call — the trap the brief flagged is that a
    naive stub makes `_read_record_state` raise first and the graduation site
    is never exercised at all. Only `update` fails, with `reason`.

    `admin_call_raw` is a spy that counts, so "did the fix hold" is answered
    by whether admin credentials were spent, not by re-testing the predicate.
    """

    def __init__(self, reason: str, memo: str = "before") -> None:
        read = BiClient("host", 81, "u", "p")
        read.session = "sess"
        read.login_data = {"admin": False}
        read.login = lambda: None  # type: ignore[method-assign]
        admin = BiClient("host", 81, "au", "ap")
        admin.session = "asess"
        admin.login_data = {"admin": True}
        admin.login = lambda: None  # type: ignore[method-assign]
        super().__init__(read=read, admin=admin)
        self._reason = reason
        self._memo = memo
        self.admin_calls: list[tuple[str, dict]] = []

    # `clipstats` goes through `call`; `update` through `call_raw`.
    def call(self, cmd: str, **kw):  # type: ignore[override]
        assert cmd == "clipstats"
        return {"data": {"memo": self._memo, "flags": 0}}

    def call_raw(self, cmd: str, **kw):  # type: ignore[override]
        assert cmd == "update"
        # Exactly what the real client builds for a `result:"fail"` reply:
        # a BARE BiError wrapped in the documented frame.
        raise BiError(f"Blue Iris cmd={cmd} failed: {self._reason}")

    def admin_call_raw(self, cmd: str, **kw):  # type: ignore[override]
        self.admin_calls.append((cmd, kw))
        # Graduation succeeded; let the caller's memo land so verify passes.
        self._memo = kw.get("memo", self._memo)
        return {"result": "success"}

    def resolve_admin(self):  # type: ignore[override]
        return self.admin


# --- Defect 1: the graduation site -------------------------------------------

# Paths that plant an auth phrase BI then echoes back inside a genuine
# NON-auth rejection. The only reason the phrase appears is the caller.
PLANTED_AUTH_PATHS = [
    "@access denied",
    "@not authorized",
    "@ACCESS DENIED",                 # casing must not launder it
    "@backup access denied 2024",     # fragment embedded mid-path
    "@x failed: not authorized",      # + a wrapper separator the caller owns
]


@pytest.mark.parametrize("path", PLANTED_AUTH_PATHS)
def test_planted_path_does_not_spend_admin_credentials(path: str) -> None:
    """BI said "not found". The caller's own path must not turn that into an
    admin-credentialed retry of a WRITE."""
    c = _CountingClients(f"Not found: {path}")
    with pytest.raises(BiError) as ei:
        _tool()(c, {"path": path, "memo": "after"})
    assert c.admin_calls == [], (
        f"path={path!r} steered the graduation: BI's reason was a plain "
        f"'Not found' and the admin retry must not fire (got "
        f"{len(c.admin_calls)} admin_call_raw invocations)"
    )
    # And it must surface as the original bare BiError, not a remap.
    assert type(ei.value) is BiError


def test_plain_non_auth_failure_does_not_graduate() -> None:
    """Control: the ordinary case the planted paths are compared against."""
    c = _CountingClients("Not found: @1")
    with pytest.raises(BiError):
        _tool()(c, {"path": "@1", "memo": "after"})
    assert c.admin_calls == []


# BI's own access-denial phrasings, carrying no caller text at all.
GENUINE_AUTH_REASONS = [
    "Access denied",
    "access denied",
    "ACCESS DENIED",
    "not authorized",
    "Not Authorized",
]


@pytest.mark.parametrize("reason", GENUINE_AUTH_REASONS)
def test_genuine_access_denied_still_graduates(reason: str) -> None:
    """Narrowing the search space must not cost the feature: a REAL denial
    from BI still escalates to admin exactly once."""
    c = _CountingClients(reason)
    out = _tool()(c, {"path": "@1", "memo": "after"})
    assert len(c.admin_calls) == 1, (
        f"genuine BI reason {reason!r} must still graduate (got "
        f"{len(c.admin_calls)} admin_call_raw invocations)"
    )
    assert c.admin_calls[0][0] == "update"
    assert out is not None


def test_genuine_denial_still_graduates_when_path_is_echoed() -> None:
    """The elision removes the path, not the whole reason: BI genuinely
    denying access AND echoing the path must still graduate."""
    c = _CountingClients("@1: Access denied")
    _tool()(c, {"path": "@1", "memo": "after"})
    assert len(c.admin_calls) == 1


def test_typed_subclass_never_graduates() -> None:
    """The `type(e) is BiError` guard is bare-only and must be untouched by
    the anchoring fix — a typed subclass escapes the graduation entirely,
    even when its message says "Access denied"."""
    from bi_mcp.errors import BiUnreachable

    class _Typed(_CountingClients):
        def call_raw(self, cmd: str, **kw):  # type: ignore[override]
            raise BiUnreachable("Blue Iris cmd=update failed: Access denied")

    c = _Typed("unused")
    with pytest.raises(BiUnreachable):
        _tool()(c, {"path": "@1", "memo": "after"})
    assert c.admin_calls == []


def test_graduation_delegates_to_the_shared_extraction_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the anti-duplication property: the graduation must go THROUGH the
    shared anchored parse, not a fourth private re-implementation.

    The wrapper at this call site is invariant (`cmd` is the literal
    "update"), so no black-box input can distinguish "extract then elide"
    from "elide only" — a spy is the only instrument that can see it.
    """
    fn = _tool()
    tm = sys.modules[fn.__module__]
    import bi_mcp.client as client_mod

    calls: list[str] = []

    def _spy(message: str) -> str:
        calls.append(message)
        return client_mod.bi_authored_reason(message)

    monkeypatch.setattr(tm, "bi_authored_reason", _spy)

    c = _CountingClients("Not found: @1")
    with pytest.raises(BiError):
        fn(c, {"path": "@1", "memo": "after"})

    assert "Blue Iris cmd=update failed: Not found: @1" in calls, (
        "the graduation must delegate to the shared extraction helper; a "
        "private copy of the parsing is how these matchers drifted before"
    )


# --- Defect 2: the elision must not shred BI's own wording -------------------

# Needles that a bare `str.replace` mangles the reason with. Each must leave
# BI's reason UNCHANGED so the fragment match still sees it.
SHREDDING_NEEDLES = ["", " ", "   ", "t", "@1", "no", "clip"]


@pytest.mark.parametrize("needle", SHREDDING_NEEDLES)
@pytest.mark.parametrize(
    "reason", ["clip not bvr", "record is not a clip", "no clip", "access denied"]
)
def test_degenerate_needle_leaves_bi_reason_intact(needle: str, reason: str) -> None:
    """A needle too short to have CARRIED a matcher fragment must not be
    elided — eliding it can only destroy signal, never protect anything."""
    assert _elide_caller_text(reason, needle) == reason


@pytest.mark.parametrize("needle", SHREDDING_NEEDLES)
def test_degenerate_path_still_remaps_a_genuine_not_a_clip(needle: str) -> None:
    """End-to-end for defect 2 at the not-a-clip remap: BI genuinely says
    "Clip not BVR" and a degenerate `path` must not turn that into a bare
    BiError (the false NEGATIVE this fix exists to prevent).

    Driven through `_read_record_state` directly because
    `_build_update_payload` rejects these paths before the tool reaches it —
    which is exactly the distant, unasserted invariant the fix stops relying
    on.
    """
    from bi_mcp.tools.tools_mutations import _read_record_state

    c = BiClient("host", 81, "u", "p")
    c.session = "sess"
    c.login_data = {"admin": True}
    c.login = lambda: None  # type: ignore[method-assign]
    c._post = lambda body: {  # type: ignore[method-assign]
        "result": "fail",
        "data": {"reason": "Clip not BVR"},
    }
    with pytest.raises(BiBadRequest):
        _read_record_state(BiClients(read=c, admin=None), needle)


def test_elision_still_removes_a_real_planted_needle() -> None:
    """The guards must not disable the defence they protect: a needle long
    enough to carry a fragment, and actually present, is still elided."""
    assert "access denied" not in _elide_caller_text(
        "not found: @access denied", "@access denied"
    )
    assert "not bvr" not in _elide_caller_text(
        "not found: @clip not bvr", "@clip not bvr"
    )


def test_elision_skipped_when_needle_absent_from_reason() -> None:
    """BI didn't echo the path — there is nothing to defend against, and
    rewriting the text could only lose signal."""
    assert _elide_caller_text("clip not bvr", "@some-other-record") == "clip not bvr"


def test_elision_tolerates_a_non_string_needle() -> None:
    """`caller_text` comes straight from caller args and the jsonschema type
    check is optional in this server; a TypeError raised from inside an error
    handler would escape unhandled."""
    assert _elide_caller_text("clip not bvr", 12345) == "clip not bvr"  # type: ignore[arg-type]


# --- Defect 3: `memo` is the OTHER caller-controlled string ------------------
#
# The anchoring above removed `path` from the reason but left `memo`, which
# travels in the SAME rejected `update` payload and is equally caller-chosen.
# If BI echoes a rejected memo back the way it echoes a rejected path, a memo
# of "access denied" steers the identical admin-credentialed retry of a
# mutation that `path` used to.
#
# Whether BI 5.9.9.71 actually echoes memo text is unverified — this is
# defence in depth on the cheap side of the trade: redacting costs at most a
# real denial going un-graduated when the caller put that exact phrase in
# their own memo (loud, and self-inflicted), while not redacting costs an
# admin-credentialed write the caller steered.


PLANTED_AUTH_MEMOS = [
    "access denied",
    "not authorized",
    "ACCESS DENIED",
    "job 7 access denied",
]


@pytest.mark.parametrize("memo", PLANTED_AUTH_MEMOS)
def test_planted_memo_does_not_spend_admin_credentials(memo: str) -> None:
    """A caller-chosen memo echoed in BI's reason must not graduate either."""
    c = _CountingClients(f"Invalid memo: {memo}")
    with pytest.raises(BiError) as ei:
        _tool()(c, {"path": "@1", "memo": memo})
    assert c.admin_calls == [], (
        f"memo={memo!r} steered the graduation: BI rejected the memo and the "
        f"admin retry must not fire (got {len(c.admin_calls)} "
        "admin_call_raw invocations)"
    )
    assert type(ei.value) is BiError


def test_planted_memo_control_is_not_vacuous() -> None:
    """Positive control for the test above.

    Its reason must really carry an auth marker BI hands back, or "did not
    graduate" would be true for the wrong reason and the regression would be
    undetectable. Driven through the SAME site with a memo that does NOT
    contain the marker: the identical reason must then graduate, so the
    non-graduation above is attributable to the redaction and nothing else.
    """
    c = _CountingClients("Invalid memo: access denied")
    _tool()(c, {"path": "@1", "memo": "UPS delivery"})
    assert len(c.admin_calls) == 1, (
        "with no marker in the caller's memo to redact, this reason must "
        "still graduate — otherwise the planted-memo tests prove nothing"
    )


def test_genuine_denial_still_graduates_with_an_innocuous_memo() -> None:
    """Surgical: redacting `memo` must not cost the feature. An ordinary memo
    alongside a REAL BI denial still escalates to admin exactly once."""
    c = _CountingClients("Access denied")
    _tool()(c, {"path": "@1", "memo": "UPS delivery"})
    assert len(c.admin_calls) == 1


def test_short_memo_cannot_shred_a_genuine_denial() -> None:
    """`_elide_caller_text`'s length floor still applies to the memo needle.

    A 1-char memo run through a bare `str.replace` would interleave spaces
    through "access denied" and destroy a genuine graduation (the same false
    NEGATIVE defect 2 covers for `path`).
    """
    c = _CountingClients("Access denied")
    _tool()(c, {"path": "@1", "memo": "d"})
    assert len(c.admin_calls) == 1


# --- Defect 4: elision must not SHRED the marker it is protecting ------------
#
# The length floor above stops a needle too SHORT to have carried a fragment.
# It does not stop a needle that clears the floor and is still a strict PIECE
# of one: `memo="authorized"` is 10 chars, sails past the 7-char floor, and
# turns BI's own "Not authorized" into "not  ". The graduation match then goes
# False and the admin retry that would have SUCCEEDED never fires — a lost
# retry, not a privilege leak (shredding can only remove markers, never add
# them, so no unwarranted retry and no unintended write is possible).
#
# Two costs, the second worse than the first:
#   * admin configured  -> BiError "Not authorized"; the memo edit silently
#     fails to land even though the escalation would have worked.
#   * admin NOT configured -> a bare BiError instead of `BiAdminRequired`, so
#     the caller loses the actionable "configure BI_ADMIN_USER/BI_ADMIN_PASS"
#     guidance and is told nothing about how to recover.
#
# `memo` is free-form (validated only as a str within `_MEMO_MAX`), so
# "authorized" is a plausible curation memo and this needs no adversary.
# Fixed by `_is_marker_fragment`, the marker-tuple-parameterised twin of
# `_is_auth_marker_fragment` that already closed the identical hole in
# `_redact_echoed_args`.


class _NoAdminClients(_CountingClients):
    """`_CountingClients` with NO admin configured.

    Isolates the more damaging half of the defect: the branch that should
    raise `BiAdminRequired` with the credentials hint is reached only when
    the marker survives elision, so shredding downgrades it to a bare
    `BiError` that says nothing actionable.
    """

    def __init__(self, reason: str, memo: str = "before") -> None:
        super().__init__(reason, memo)
        # `admin` is a read-only property over this backing field, so the
        # "no admin configured" state is set the way the real client sets it.
        self._explicit_admin = None

    def resolve_admin(self):  # type: ignore[override]
        return None


def test_marker_fragment_memo_does_not_shred_a_genuine_denial() -> None:
    """THE live case. BI genuinely says "Not authorized"; a memo that is a
    strict fragment of that marker must not erase it."""
    c = _CountingClients("Not authorized")
    out = _tool()(c, {"path": "@1", "memo": "authorized"})
    assert len(c.admin_calls) == 1, (
        "memo='authorized' is a strict fragment of BI's own 'not authorized' "
        "marker. Eliding it shreds the marker into 'not  ', the graduation "
        "goes False, and the admin retry that would have SUCCEEDED never "
        f"fires (got {len(c.admin_calls)} admin_call_raw invocations)"
    )
    assert c.admin_calls[0][0] == "update"
    assert out is not None


def test_marker_fragment_memo_still_reports_admin_required() -> None:
    """The more damaging half: with no admin configured, the shred costs the
    caller the actionable `BiAdminRequired` guidance entirely."""
    from bi_mcp.errors import BiAdminRequired

    c = _NoAdminClients("Not authorized")
    with pytest.raises(BiAdminRequired) as ei:
        _tool()(c, {"path": "@1", "memo": "authorized"})
    assert "BI_ADMIN_USER" in str(ei.value)
    assert c.admin_calls == []


def test_marker_fragment_memo_positive_control() -> None:
    """Control, so the two tests above cannot pass vacuously.

    An ORDINARY memo against the identical BI reason must graduate. If this
    ever stopped graduating, "it graduated" above would be evidence of
    nothing.
    """
    c = _CountingClients("Not authorized")
    _tool()(c, {"path": "@1", "memo": "car in driveway"})
    assert len(c.admin_calls) == 1


def test_no_admin_control_still_reports_admin_required() -> None:
    """Control for the no-admin variant: an ordinary memo reaches the same
    `BiAdminRequired` branch, so its absence above would be attributable to
    the shred and nothing else."""
    from bi_mcp.errors import BiAdminRequired

    c = _NoAdminClients("Not authorized")
    with pytest.raises(BiAdminRequired):
        _tool()(c, {"path": "@1", "memo": "car in driveway"})


# The forge class the fragment guard must NOT reopen. A needle that IS a
# marker, or CONTAINS one, is still elided — only a STRICT fragment is spared.
FORGE_MEMOS = [
    "not authorized",        # memo IS the marker exactly
    "job 7 not authorized",  # memo CONTAINS the marker
    "access denied",         # the other marker, exactly
]


@pytest.mark.parametrize("memo", FORGE_MEMOS)
def test_fragment_guard_does_not_reopen_the_memo_forge(memo: str) -> None:
    """A memo BI echoes back must still not steer an admin-credentialed
    retry of a WRITE — the guard spares strict fragments only."""
    c = _CountingClients(f"Invalid memo: {memo}")
    with pytest.raises(BiError) as ei:
        _tool()(c, {"path": "@1", "memo": memo})
    assert c.admin_calls == [], (
        f"memo={memo!r} carries a whole marker and must still be elided; the "
        f"fragment guard reopened the forge (got {len(c.admin_calls)} "
        "admin_call_raw invocations)"
    )
    assert type(ei.value) is BiError


def test_fragment_guard_is_keyed_to_this_sites_markers() -> None:
    """The guard must read the SITE's tuple, not a global list.

    `_elide_caller_text` guards three sites with three different
    vocabularies; a needle harmless at one is a whole marker at another.
    "not bvr" is a marker for the not-a-clip remap and merely ordinary text
    for the graduation site, so it must still be elided here.
    """
    from bi_mcp.client import _elide_caller_text as elide
    from bi_mcp.tools.tools_mutations import (
        _CLIPSTATS_NOT_A_CLIP_FRAGMENTS,
        _UPDATE_ACCESS_DENIED_FRAGMENTS,
    )

    # Strict fragment of a not-a-clip marker: spared THERE...
    assert elide("clip not bvr", "not bvr ok", _CLIPSTATS_NOT_A_CLIP_FRAGMENTS)
    assert (
        elide("record is not a clip", "not a cli", _CLIPSTATS_NOT_A_CLIP_FRAGMENTS)
        == "record is not a clip"
    )
    # ...but "not a cli" is nobody's fragment at the graduation site, so the
    # guard there must not spare it.
    assert (
        elide("x not a cli y", "not a cli", _UPDATE_ACCESS_DENIED_FRAGMENTS)
        != "x not a cli y"
    )


def test_no_marker_fragment_can_shred_its_own_marker_at_any_eliding_site() -> None:
    """Programmatic invariant, derived from each site's LIVE marker tuple.

    Mirrors `test_no_marker_word_can_shred_its_own_marker` on the
    `_redact_echoed_args` side. Every strict substring of every marker that
    clears the length floor is fed in as a caller needle against that marker
    as BI's own reason; the marker must survive. A marker added later is
    covered automatically, rather than depending on someone remembering to
    extend a hand-written list.
    """
    from bi_mcp.client import _MIN_ELIDABLE_NEEDLE, _elide_caller_text as elide
    from bi_mcp.tools.tools_cameras import _CAMCONFIG_FALLBACK_FRAGMENTS
    from bi_mcp.tools.tools_mutations import (
        _CLIPSTATS_NOT_A_CLIP_FRAGMENTS,
        _UPDATE_ACCESS_DENIED_FRAGMENTS,
    )

    tuples = {
        "tools_mutations._UPDATE_ACCESS_DENIED_FRAGMENTS": (
            _UPDATE_ACCESS_DENIED_FRAGMENTS
        ),
        "tools_mutations._CLIPSTATS_NOT_A_CLIP_FRAGMENTS": (
            _CLIPSTATS_NOT_A_CLIP_FRAGMENTS
        ),
        "tools_cameras._CAMCONFIG_FALLBACK_FRAGMENTS": _CAMCONFIG_FALLBACK_FRAGMENTS,
    }
    checked = 0
    for name, markers in tuples.items():
        for marker in markers:
            for i in range(len(marker)):
                for j in range(i + 1, len(marker) + 1):
                    needle = marker[i:j].strip().lower()
                    # Only needles a caller could actually get elided: below
                    # the floor the reason is returned untouched anyway, and
                    # the full marker is the accepted forge-symmetric cost.
                    if len(needle) < _MIN_ELIDABLE_NEEDLE or needle in markers:
                        continue
                    checked += 1
                    assert marker in elide(marker, needle, markers), (
                        f"{name}: caller needle {needle!r} is a strict "
                        f"fragment of marker {marker!r} but eliding it "
                        "destroyed the marker. BI's own wording no longer "
                        "matches, and the site's match silently flips."
                    )
    assert checked > 0, "invariant scanned nothing — marker tuples empty?"
