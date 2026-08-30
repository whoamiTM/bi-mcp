"""Regression: `bi_get_camera_config`'s camconfig→camlist fallback must not
be steerable by the caller's own `short`.

FOURTH instance of the class fixed three times already
(`is_terminal_status_message`, `_read_record_state`, and
`_tool_update_record`'s admin graduation): a substring match against a
stringified BI error whose text carries caller-supplied input.

Here the admin `camconfig` failure is matched against
`("access denied", "unknown", "invalid", "not supported")`; on a hit the real
BI error is SWALLOWED and the caller silently receives the shallow camlist
fallback with a `_note`. `short` is echoed by BI inside its own reason, and
two of those fragments are ordinary English words seven characters long — so
a camera merely NAMED `invalid-cam` or `unknown-2` downgraded a genuine BI
fault into "here's some shallow data". No adversary required.

The failure direction is a DOWNGRADE, not an escalation, which is why it
survived three passes: it never looks like a security bug from the outside,
just occasionally-wrong data.
"""

from __future__ import annotations

import pytest

from bi_mcp.client import BiClient, BiClients
from bi_mcp.errors import BiError


def _tool():
    from bi_mcp.tools.tools_cameras import _tool_get_camera_config

    return _tool_get_camera_config


CAMLIST = [{"optionValue": "invalid-cam", "shortName": "invalid-cam", "name": "invalid-cam"}]


class _Clients(BiClients):
    """`camconfig` (admin path) fails with `reason`; `camlist` (read path)
    succeeds, so a swallowed error is observable as a returned dict instead
    of a raise."""

    def __init__(self, reason: str, camlist: list | None = None) -> None:
        read = BiClient("host", 81, "u", "p")
        read.session = "s"
        read.login_data = {"admin": False}
        read.login = lambda: None  # type: ignore[method-assign]
        admin = BiClient("host", 81, "au", "ap")
        admin.session = "as"
        admin.login_data = {"admin": True}
        admin.login = lambda: None  # type: ignore[method-assign]
        super().__init__(read=read, admin=admin)
        self._reason = reason
        self._camlist = camlist if camlist is not None else CAMLIST
        self.camlist_calls = 0

    def resolve_admin(self):  # type: ignore[override]
        return self.admin

    def admin_call(self, cmd: str, **kw):  # type: ignore[override]
        assert cmd == "camconfig"
        raise BiError(f"Blue Iris cmd={cmd} failed: {self._reason}")

    def call(self, cmd: str, **kw):  # type: ignore[override]
        assert cmd == "camlist"
        self.camlist_calls += 1
        return self._camlist


# Camera short names that plant a fallback fragment BI then echoes back
# inside a genuine, unrelated rejection.
PLANTED_SHORTS = [
    "invalid-cam",
    "unknown-2",
    "cam access denied",
    "not supported cam",
    "INVALID-CAM",  # casing must not launder it
]


@pytest.mark.parametrize("short", PLANTED_SHORTS)
def test_planted_short_does_not_swallow_a_real_bi_error(short: str) -> None:
    """BI said "not found". The caller's own short name must not convert that
    into a silent downgrade to the shallow camlist fallback."""
    c = _Clients(f"Not found: {short}")
    with pytest.raises(BiError):
        _tool()(c, {"short": short})
    assert c.camlist_calls == 0, (
        f"short={short!r} steered the fallback: a genuine BI error was "
        "swallowed and the caller got shallow camlist data instead"
    )


def test_plain_unrelated_failure_still_propagates() -> None:
    """Control: the ordinary case the planted shorts are compared against."""
    c = _Clients("Not found: SecCam_3")
    with pytest.raises(BiError):
        _tool()(c, {"short": "SecCam_3"})
    assert c.camlist_calls == 0


# BI's own phrasings for the four recoverable conditions, carrying no caller
# text. Each must STILL fall back — narrowing the search space must not cost
# the feature.
GENUINE_FALLBACK_REASONS = [
    "Access denied",
    "access denied",
    "unknown cmd",
    "invalid command",
    "not supported",
    "NOT SUPPORTED",
]


@pytest.mark.parametrize("reason", GENUINE_FALLBACK_REASONS)
def test_genuine_reason_still_falls_back(reason: str) -> None:
    c = _Clients(
        reason,
        camlist=[{"optionValue": "SecCam_3", "shortName": "SecCam_3", "name": "SecCam_3"}],
    )
    out = _tool()(c, {"short": "SecCam_3"})
    assert c.camlist_calls == 1, (
        f"genuine BI reason {reason!r} must still reach the camlist fallback"
    )
    assert isinstance(out, dict)
    assert "_note" in out


def test_genuine_reason_still_falls_back_when_short_is_echoed() -> None:
    """The elision removes the short name, not the whole reason."""
    c = _Clients(
        "SecCam_3: Access denied",
        camlist=[{"optionValue": "SecCam_3", "shortName": "SecCam_3", "name": "SecCam_3"}],
    )
    _tool()(c, {"short": "SecCam_3"})
    assert c.camlist_calls == 1


# The other direction of the same anchoring: elision that removes the WHOLE
# reason. Unlike `bi_update_record`'s `path` (forced to start with `@`, so a
# reason equal to it is provably an echo), a camera short name has no shape
# rule — and `unknown`/`invalid` are ordinary words. A camera named exactly
# what BI happens to say reduced the reason to whitespace, matched nothing,
# and RAISED instead of falling back: the recoverable condition became a hard
# failure. Elision must not be able to destroy every byte BI authored.
WHOLE_REASON_SHORTS = [
    "unknown command",
    "not supported",
    "invalid camera",
    "access denied",
]


@pytest.mark.parametrize("short", WHOLE_REASON_SHORTS)
def test_short_equal_to_the_whole_reason_still_falls_back(short: str) -> None:
    c = _Clients(
        short,
        camlist=[{"optionValue": short, "shortName": short, "name": short}],
    )
    out = _tool()(c, {"short": short})
    assert c.camlist_calls == 1, (
        f"short={short!r} equals BI's entire reason, so elision emptied it "
        "and the recoverable condition was raised instead of falling back"
    )
    assert isinstance(out, dict)
    assert "_note" in out


def test_whole_reason_fallback_does_not_reopen_the_downgrade() -> None:
    """Control pair for the fix: restoring the unelided reason must apply
    ONLY when elision left nothing. A partial echo — the original hazard —
    must still raise."""
    c = _Clients("Not found: unknown-2")
    with pytest.raises(BiError):
        _tool()(c, {"short": "unknown-2"})
    assert c.camlist_calls == 0


# The THIRD direction, and the one the whole-reason guard above did not cover:
# a camera whose name is a SUBSTRING of a genuine BI reason.
#
# Blanket elision — removing the caller's text from every position it happens
# to occupy — cannot distinguish an echoed name from the identical word BI
# authored itself, and two of the fragments are ordinary English. So for a
# camera named `unknown`, BI's own "Unknown command" was elided to " command",
# nothing matched, and the tool RAISED instead of degrading to the documented
# shallow camlist path. The whole-reason fallback did not catch it: " command"
# is non-blank, so elision "left something" and the unelided reason was never
# restored.
#
# The fix elides only where BI DEMONSTRABLY echoed the name — where it is the
# entire remainder after a colon ("Not found: <short>") — leaving an
# incidental mid-sentence occurrence, which is BI's prose rather than a
# quotation, in place to be matched.
SUBSTRING_SHORTS = [
    ("unknown", "Unknown command"),
    ("invalid", "Invalid request"),
    ("denied", "Access denied"),
    ("supported", "Command not supported"),
    ("unknown", "unknown cmd for this build"),
]


@pytest.mark.parametrize("short,reason", SUBSTRING_SHORTS)
def test_short_that_is_a_substring_of_the_reason_still_falls_back(
    short: str, reason: str
) -> None:
    """An ordinary camera name that happens to appear inside BI's own wording
    must not erase it. These are legitimate names, not attacks."""
    c = _Clients(
        reason,
        camlist=[{"optionValue": short, "shortName": short, "name": short}],
    )
    out = _tool()(c, {"short": short})
    assert c.camlist_calls == 1, (
        f"short={short!r} occurs inside BI's genuine reason {reason!r}; "
        "eliding it destroyed the fallback fragment and the recoverable "
        "condition was raised instead of falling back"
    )
    assert isinstance(out, dict)
    assert "_note" in out


@pytest.mark.parametrize("short", ["unknown", "invalid", "denied"])
def test_substring_fix_does_not_reopen_the_echo_downgrade(short: str) -> None:
    """Control pair. Restricting elision to echo positions must not restore
    the original hazard: when BI genuinely echoes the name back as the whole
    remainder after a colon, that IS caller text and must still be elided,
    so a real BI error still propagates instead of being swallowed."""
    c = _Clients(f"Not found: {short}")
    with pytest.raises(BiError):
        _tool()(c, {"short": short})
    assert c.camlist_calls == 0, (
        f"short={short!r} was echoed by BI in a demonstrable echo position; "
        "it must still be elided so the genuine error is not swallowed"
    )


# The FOURTH direction: a camera name that itself contains a colon.
#
# The echo gate used to partition the reason on its LAST ":" and compare only
# the tail. A name carrying its own colon therefore never matched: for
# `short="invalid:cam"` against BI's observed echo shape "Not found:
# invalid:cam", the comparison was "cam" vs "invalid:cam", no echo was seen,
# the planted name stayed in the reason, and the fallback fragment inside it
# matched — re-opening the exact downgrade `echoed_caller_text` exists to
# close. The gate is now a suffix test against the WHOLE needle with the ":"
# required immediately before it, so a colon inside the name is just a
# character.
COLON_BEARING_SHORTS = [
    "invalid:cam",
    "cam:access denied",
    "unknown:x",
]


@pytest.mark.parametrize("short", COLON_BEARING_SHORTS)
def test_colon_bearing_short_is_still_recognised_as_an_echo(short: str) -> None:
    """BI echoed the name back, so it must be elided and the genuine error
    must propagate — a colon in the name must not launder it."""
    c = _Clients(
        f"Not found: {short}",
        camlist=[{"optionValue": short, "shortName": short, "name": short}],
    )
    with pytest.raises(BiError):
        _tool()(c, {"short": short})
    assert c.camlist_calls == 0, (
        f"short={short!r} carries a colon, so the echo gate compared only the "
        "text after the LAST one, missed the echo, and a genuine BI error was "
        "swallowed into the shallow camlist fallback"
    )


@pytest.mark.parametrize("short", COLON_BEARING_SHORTS)
def test_colon_bearing_short_not_echoed_still_falls_back(short: str) -> None:
    """Positive control: the forge must stay closed in the other direction.

    The same colon-bearing names against a reason BI authored ITSELF, with no
    echo of the name anywhere. Nothing may be elided, so BI's own fragment
    survives and the documented shallow fallback still happens. Without this,
    "treat every colon-bearing needle as an echo" would pass the test above.
    """
    c = _Clients(
        "Access denied",
        camlist=[{"optionValue": short, "shortName": short, "name": short}],
    )
    out = _tool()(c, {"short": short})
    assert c.camlist_calls == 1, (
        f"short={short!r} is not echoed in BI's own reason; eliding it "
        "anyway destroyed the fallback fragment"
    )
    assert isinstance(out, dict)
    assert "_note" in out
