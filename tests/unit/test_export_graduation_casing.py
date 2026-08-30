"""Tool-level regression: export-graduation must be casing-insensitive.

`_classify_fail` normalises `data.status` with `.strip().lower()` before
matching `_TERMINAL_STATUSES`, but `bi_export_clip`'s graduation guard used to
match the literal `"Clip not BVR" in str(e)`. A case-varied terminal status
therefore classified correctly (non-auth, no re-login) and then RAISED instead
of returning the documented `{ok:false, mode:"status"}` envelope.

`tests/unit/test_client_auth_retry.py` already pins the casings at the CLIENT
level; the absence of an equivalent TOOL-level test is exactly why the
mismatch survived. This module closes that gap: it drives the real
`_tool_export_clip` through the real `BiClient` failure path, so the two halves
are exercised together rather than in isolation.
"""

from __future__ import annotations

import pytest

from bi_mcp.client import BiClient, BiClients, _TERMINAL_STATUSES, is_terminal_status_message
from bi_mcp.errors import BiError, BiNotFound


@pytest.fixture(autouse=True)
def _allow_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_tool_export_clip` refuses to run unless mutations are enabled."""
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "1")


def _export_tool():
    # Imported lazily: the module is only meaningful with the gate on, and
    # importing at collection time would bind before the fixture runs.
    from bi_mcp.tools.tools_mutations import _tool_export_clip

    return _tool_export_clip


def _clients(fail_data: dict, *, exc: BaseException | None = None) -> BiClients:
    """A BiClients pair whose read user has `clipcreate` and whose every POST
    fails with `fail_data` — so the real `_call_with_auth_retry` builds the
    real `BiError` message the guard then has to interpret."""
    c = BiClient("host", 81, "u", "p")
    c.session = "sess"
    c.login_data = {"clipcreate": True, "admin": True}
    c.login = lambda: None  # type: ignore[method-assign]
    if exc is not None:
        def _boom(body):  # noqa: ANN001, ARG001
            raise exc
        c._post = _boom  # type: ignore[method-assign]
    else:
        c._post = lambda body: {"result": "fail", "data": fail_data}  # type: ignore[method-assign]
    return BiClients(read=c, admin=None)


# The casings BI might plausibly send. `_classify_fail` accepts all four; the
# tool must too, or classification and graduation disagree.
TERMINAL_CASINGS = ["Clip not BVR", "CLIP NOT BVR", "clip not bvr", " Clip not BVR "]


@pytest.mark.parametrize("status", TERMINAL_CASINGS)
def test_graduated_export_returns_envelope_for_every_casing(status: str) -> None:
    """Every casing/whitespace variant returns `{ok:false, mode:"status"}`."""
    result = _export_tool()(_clients({"status": status}), {"mode": "status", "path": "@1"})
    assert result["ok"] is False, f"{status!r} must shape as ok=false, not raise"
    assert result["mode"] == "status"


@pytest.mark.parametrize("status", TERMINAL_CASINGS)
def test_graduation_message_preserves_bi_casing(status: str) -> None:
    """BI's ORIGINAL wording must survive into the envelope — lowercasing the
    message to make the match work would be a diagnostics regression."""
    result = _export_tool()(_clients({"status": status}), {"mode": "status", "path": "@1"})
    assert status.strip() in str(result), "BI's own casing must be surfaced verbatim"


@pytest.mark.parametrize("status", TERMINAL_CASINGS)
def test_raw_true_still_reraises_for_every_casing(status: str) -> None:
    """Carve-out (c): `raw=true` promises the exact BI payload, so a graduated
    export must surface the error rather than a fabricated envelope — and that
    must not become casing-dependent either."""
    with pytest.raises(BiError):
        _export_tool()(_clients({"status": status}), {"mode": "status", "path": "@1", "raw": True})


@pytest.mark.parametrize("reason", ["Not found", "Access denied", "no such record"])
def test_unknown_bare_reason_still_raises(reason: str) -> None:
    """Carve-out (b): only KNOWN terminal statuses graduate. A bare BiError
    with any other reason must still propagate — shaping it as `{ok:false}`
    would let a caller read "BI rejected your path" as "export completed"."""
    with pytest.raises(BiError):
        _export_tool()(_clients({"reason": reason}), {"mode": "status", "path": "@1"})


def test_typed_subclass_still_propagates() -> None:
    """Carve-out (a): a typed subclass is a durable failure and must escape the
    guard even when its message happens to mention a terminal status."""
    boom = BiNotFound("Blue Iris cmd=export failed: Clip not BVR")
    with pytest.raises(BiNotFound):
        _export_tool()(_clients({}, exc=boom), {"mode": "status", "path": "@1"})


def test_predicate_covers_every_terminal_status() -> None:
    """The whole point of the shared predicate: a status added to
    `_TERMINAL_STATUSES` must work downstream with NO second edit. This fails
    if someone re-hardcodes a literal in the guard."""
    for status in _TERMINAL_STATUSES:
        assert is_terminal_status_message(f"Blue Iris cmd=export failed: {status.upper()}")
        assert is_terminal_status_message(f"Blue Iris cmd=export failed: {status.title()}")


@pytest.mark.parametrize("status", _TERMINAL_STATUSES)
def test_every_terminal_status_graduates_at_the_tool(status: str) -> None:
    """Same guarantee, driven end-to-end through the tool rather than the
    predicate — so a future `_TERMINAL_STATUSES` entry is proven to reach the
    graduation path, not merely the classifier."""
    result = _export_tool()(_clients({"status": status.upper()}), {"mode": "status", "path": "@1"})
    assert result["ok"] is False


def test_predicate_does_not_swallow_unrelated_messages() -> None:
    """The predicate is a substring match against the WRAPPED message, so pin
    that it stays narrow — a too-broad match would silently convert real BI
    rejections into "export completed"."""
    for msg in ["Blue Iris cmd=export failed: Not found",
                "Blue Iris cmd=export failed: Access denied",
                "Blue Iris cmd=export failed: clip is busy"]:
        assert not is_terminal_status_message(msg)


# ---------------------------------------------------------------------------
# Regression: the predicate must ANCHOR, not free-substring.
#
# `bi_export_clip`'s `path` is unconstrained free text ({"type": "string"} —
# no pattern, no `@` anchor) and BI echoes a rejected path back inside its
# `reason`. A free-substring matcher therefore let the CALLER manufacture a
# false graduation: BI genuinely rejected the path, but the guard read the
# echoed phrase and shaped `{ok:false, mode:"status"}`, so the caller stopped
# polling believing the export had completed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "@clip not bvr",          # the phrase echoed verbatim in the reason
        "@x: clip not bvr",       # + a `": "` the caller controls (defeats a
                                  #   last-separator split)
        "@1: Clip not BVR",
    ],
)
def test_echoed_path_does_not_manufacture_a_graduation(path: str) -> None:
    """A genuine BI rejection whose `reason` merely QUOTES a terminal status
    must still raise. Shaping it as `{ok:false}` is a false graduation."""
    clients = _clients({"reason": f"Not found: {path}"})
    with pytest.raises(BiError):
        _export_tool()(clients, {"mode": "status", "path": path})


# ---------------------------------------------------------------------------
# Regression: the anchoring itself. Each case below dies under exactly one
# plausible weakening of `_terminal_status_candidate`, so the extraction is
# pinned by assertion rather than only by its docstring.
#
#   * FIRST-separator split (`partition`, not `rpartition`) — a BI reason may
#     itself contain `" failed: "`, and splitting on the LAST one hands back
#     the caller-controlled tail: the exact false graduation this anchoring
#     exists to close.
#   * The `"blue iris cmd="` prefix anchor — without it any foreign wrapper
#     that happens to contain a separator is parsed as if it were ours.
#   * Equality, not `endswith` — an embedded status must not graduate.
#   * `.strip()` on both the message and the extracted reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        # Kills `rpartition`: BI's own reason embeds a second `" failed: "`,
        # and `bi_export_clip`'s `path` is free text, so a caller can plant it.
        # Last-separator split yields "clip not bvr" -> false graduation.
        "Blue Iris cmd=export failed: Not found: @x failed: Clip not BVR",
        # ...and via the re-login wrapper, whose separator is the other one.
        "Blue Iris cmd=export failed after re-login: "
        "Not found: @x failed after re-login: Clip not BVR",
        # Mixed separators: the FIRST one in the message must win.
        "Blue Iris cmd=export failed: Not found: @x failed after re-login: Clip not BVR",
        # Kills a dropped prefix anchor: a foreign frame carrying a separator.
        "Blue Iris returned HTTP 500: upstream failed: Clip not BVR",
        # Kills `endswith`: the status is only the TAIL of the real reason.
        "Blue Iris cmd=export failed: job 7: Clip not BVR",
    ],
)
def test_anchoring_rejects_caller_planted_terminal_status(message: str) -> None:
    """None of these is a graduation: each is a real BI failure whose reason
    merely CONTAINS a terminal status. Shaping them `{ok:false}` would tell the
    caller "export completed" and stop it polling."""
    assert not is_terminal_status_message(message)


@pytest.mark.parametrize(
    "message",
    [
        # Kills a dropped `.strip()` on the incoming message.
        "  Blue Iris cmd=export failed: Clip not BVR  ",
        # Kills a dropped `.strip()` on the EXTRACTED reason.
        "Blue Iris cmd=export failed:  Clip not BVR ",
        "Blue Iris cmd=export failed after re-login:  Clip not BVR ",
    ],
)
def test_anchoring_still_graduates_whitespace_padded_wrappers(message: str) -> None:
    """The other direction: stripping must survive. BI's bare status, however
    padded, is still a graduation — dropping either `.strip()` makes a real
    terminal export raise instead of returning its `{ok:false}` envelope."""
    assert is_terminal_status_message(message)


@pytest.mark.parametrize(
    "reason",
    [
        "Not found: @clip not bvr",   # echoed caller text
        "camera 'clip not bvr' offline",
        "job 7: Clip not BVR",        # EMBEDDED status — terminal on neither side
        "Clip not BVRX",              # terminal status as a word PREFIX
        "XClip not BVR",              # ...and as a SUFFIX
        "unclip not bvr",
        "Clip not BVR.",              # trailing punctuation
        "Clip not BVR!",
        "memo='Clip not BVR'",        # quoted inside unrelated text
        "line1\nClip not BVR",        # multi-line
        "Clip not BVR\nextra",
    ],
)
def test_predicate_rejects_near_miss_reasons(reason: str) -> None:
    """Adversarial false positives of a substring/endswith matcher. Each of
    these is a REAL BI failure that must propagate, not graduate."""
    assert not is_terminal_status_message(f"Blue Iris cmd=export failed: {reason}")


def test_predicate_rejects_foreign_wrapper_shapes() -> None:
    """The anchor is the wrapper `_call_with_auth_retry` actually builds. A
    message framed by any OTHER layer is not a graduation signal, even when it
    ends in the phrase — e.g. an HTTP-level error quoting BI's body."""
    for msg in [
        "Blue Iris returned HTTP 500: Clip not BVR",
        "bi_update_record pre-read: Blue Iris cmd=clipstats failed: Clip not BVR",
        # A newline anywhere in the FRAME means this is not the single-line
        # message `_call_with_auth_retry` renders — something else built it,
        # so the text after `" failed: "` is not a BI reason we can trust.
        "Blue Iris cmd=export\nfailed: Clip not BVR",
        "Blue Iris cmd=ex\nport failed: Clip not BVR",
    ]:
        assert not is_terminal_status_message(msg)


# ---------------------------------------------------------------------------
# Regression: the two sides must AGREE, so a terminal status costs no retry.
#
# When `_classify_fail` says "unclassifiable" (retry) but the guard said
# "terminal", the client burned a full re-login + second POST against a
# durable end-state before the guard graduated it anyway — exactly the wasted
# retry `_TERMINAL_STATUSES` exists to eliminate.
# ---------------------------------------------------------------------------


class _CountingClients:
    """Builds a BiClients whose POSTs and re-logins are counted."""

    def __init__(self, fail_data: dict) -> None:
        self.posts = 0
        self.relogins = 0
        c = BiClient("host", 81, "u", "p")
        c.session = "sess"
        c.login_data = {"clipcreate": True, "admin": True}

        def _login() -> None:
            self.relogins += 1
            c.session = "sess2"

        def _post(body):  # noqa: ANN001, ARG001
            self.posts += 1
            return {"result": "fail", "data": dict(fail_data)}

        c.login = _login  # type: ignore[method-assign]
        c._post = _post  # type: ignore[method-assign]
        self.clients = BiClients(read=c, admin=None)


@pytest.mark.parametrize("status", TERMINAL_CASINGS)
def test_terminal_status_costs_no_relogin(status: str) -> None:
    """A bare terminal status is decided non-auth on the FIRST reply: exactly
    one POST, zero re-logins. Fails if the classifier stops recognising it."""
    h = _CountingClients({"status": status})
    result = _export_tool()(h.clients, {"mode": "status", "path": "@1"})
    assert result["ok"] is False
    assert (h.posts, h.relogins) == (1, 0), "terminal status must not be retried"


@pytest.mark.parametrize("status", _TERMINAL_STATUSES)
def test_classifier_and_guard_agree_on_every_terminal_status(status: str) -> None:
    """Both sides, driven from the same frozenset, on the same input. A future
    entry that reaches only one side fails here."""
    from bi_mcp.client import _classify_fail

    for variant in [status, status.upper(), status.title(), f" {status} "]:
        assert _classify_fail({"result": "fail", "data": {"status": variant}}) is False
        assert is_terminal_status_message(f"Blue Iris cmd=export failed: {variant}")


def test_embedded_status_is_terminal_on_neither_side() -> None:
    """Deliberate choice: an EMBEDDED status is NOT terminal, on BOTH sides.

    Matching it would mean the guard graduates a message the classifier
    declines, and — because BI echoes caller-supplied text into `reason` — it
    would re-open the false graduation above for any reason ending in the
    phrase. BI's real graduation reply is the bare status. The cost is one
    wasted retry on a wording BI has never been observed to emit."""
    from bi_mcp.client import _classify_fail

    embedded = "job 7: Clip not BVR"
    assert _classify_fail({"result": "fail", "data": {"status": embedded}}) is None
    assert not is_terminal_status_message(f"Blue Iris cmd=export failed: {embedded}")
