"""Blue Iris HTTP/JSON client.

Implements the two-step MD5 session handshake documented in
``BlueIris_Manual.md`` § *JSON Interface* (line 8353+):

  1. POST {"cmd":"login"} → server returns ``result:"fail"`` + a session token
  2. POST {"cmd":"login", "session":..., "response": MD5("user:session:pass")}
     → server returns ``result:"success"`` + login data

The session token is cached and reused for all subsequent calls. If a call
returns an auth-class ``result:"fail"`` mid-session (any reason mentioning a
session, or an unclassifiable reason), the client logs in once more and
retries the call transparently; non-auth failures (bad
camera name, per-cmd capability denial like "Access denied") raise
immediately. Login failures (wrong user/pass) are NOT retried — Blue Iris
has built-in brute-force lockout.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from typing import Any, Iterator

import httpx

from .errors import (
    BiAdminAuthFailed,
    BiAuthFailed,
    BiBadRequest,
    BiError,
    BiNotFound,
    BiUnreachable,
    BiVerifyAuthBlip,
    BiVerifyUnreachable,
)
from .logging_setup import get_logger

log = get_logger()

DEFAULT_TIMEOUT = 10.0

# BI reports ``result:"fail"`` for expired sessions AND legitimate cmd
# failures alike; the ``data.reason`` text is the only discriminator.
# Whitelist sourced from ha-blueiris (api/blue_iris_api.py:275-288), which
# only re-authenticates when the reason matches one of these substrings.
#
# Deliberate deviation from ha-blueiris: "access denied" is EXCLUDED. On BI
# 5.9.9.71 that reason means per-cmd capability gating on a fully valid
# session (``tracks`` returns it even for admin; ``export`` returns it when
# the user lacks clipcreate) — a re-login cannot fix it, and
# ``bi_update_record``'s read→admin graduation depends on it surfacing as a
# bare ``BiError`` (its guard is ``type(e) is BiError``).
# "session" (bare) is deliberately broad: BI documents no `data.reason`
# vocabulary anywhere in BlueIris_Manual.md, so the exact expiry wording is
# unknown. "invalid session" alone left "session expired", "session timeout",
# "bad session", "session not found" et al. classified NON-auth — a *decided*
# verdict that skips the retry outright, which is worse than the
# unclassifiable-None default (that still retries). Any reason mentioning a
# session is treated as auth-class; the "access denied" carve-out below is
# unaffected because it never contains the word.
_AUTH_FAIL_SUBSTRINGS = (
    "session",
    "unauthorized",
    "authorization",
    "not logged in",
    "login",
    "authentication",
    "not authenticated",
)


# Terminal `data.status` values: durable end-states, not transient conditions.
# An export job that has graduated out of the queue reports this forever, so a
# re-login + retry can never change the answer (AGENTS.md Rule 6.5).
_TERMINAL_STATUSES = frozenset({"clip not bvr"})


# The exact wrapper shapes `_call_with_auth_retry` builds around a BI failure
# reason. Anchoring on these — rather than on "the text after some colon" —
# is what keeps the reason itself opaque: BI echoes caller-supplied text
# (paths, camera names, memos) into `reason`, and that text may contain any
# punctuation, ``": "`` included.
_FAIL_MESSAGE_PREFIXES = (
    "blue iris cmd=",
)
_FAIL_MESSAGE_SEPARATORS = (
    " failed: ",
    " failed after re-login: ",
)


def bi_authored_reason(message: str) -> str:
    """The BI-authored reason inside a wrapped failure message.

    ``_call_with_auth_retry`` renders every one of its `BiError`s as
    ``"Blue Iris cmd=<cmd> failed: <reason>"`` (or ``" failed after
    re-login: "`` for the post-retry `BiAuthFailed`). We recognise exactly
    that frame and return ``<reason>``; everything else — including a bare
    status, which is how `_classify_fail` calls in — is returned unchanged.

    The separator is matched at its FIRST occurrence after the known prefix,
    never the last: a BI reason may itself contain ``": "`` (``"Not found:
    @some path"``), and splitting on the last one would hand back only the
    caller-controlled tail of that reason — re-opening the very false
    graduation this anchoring exists to close.

    Shared by every matcher that interprets a BI failure message
    (`is_terminal_status_message`, `bi_update_record`'s not-a-clip remap).
    They must all narrow to the BI-authored reason FIRST, because the wrapped
    message also carries caller-supplied text: a second private copy of this
    parsing is precisely how those two matchers drifted apart before.
    """
    lowered = message.strip().lower()
    if lowered.startswith(_FAIL_MESSAGE_PREFIXES):
        for sep in _FAIL_MESSAGE_SEPARATORS:
            head, found, reason = lowered.partition(sep)
            if found and "\n" not in head:
                return reason.strip()
    return lowered


# Shortest BI-authored fragment ANY of the message matchers looks for. A
# needle shorter than this cannot contain a fragment, so it cannot forge a
# match — eliding it could only ever destroy signal, never protect us.
# Current floor across every matcher: `"no clip"`/`"not bvr"` (not-a-clip
# remap) and `"unknown"`/`"invalid"` (camconfig fallback) are all 7;
# `"access denied"`/`"not supported"` are 13. Lower this only alongside a
# shorter fragment somewhere.
_MIN_ELIDABLE_NEEDLE = 7


def _is_marker_fragment(needle: str, markers: tuple[str, ...]) -> bool:
    """True if ``needle`` is a STRICT substring of one of ``markers``.

    The marker-tuple-parameterised form of `_is_auth_marker_fragment`, whose
    reasoning transfers verbatim: a strict substring of a marker contains no
    marker, so echoing it back adds no marker to the reason and it cannot
    forge a match — while removing it CAN destroy a marker BI authored. The
    hazard is one-directional, so the safe answer is to leave it alone.

    ``markers`` is the MATCHING SITE's own fragment tuple, never a global
    list: `_elide_caller_text` guards three sites with three different
    vocabularies, and a needle that is harmless at one may be a whole marker
    at another. Reading each site's live tuple also means a marker added
    later automatically extends the protection to its fragments.
    """
    return any(needle in m and needle != m for m in markers)


def _elide_caller_text(
    reason: str, caller_text: str, markers: tuple[str, ...] = ()
) -> str:
    """Remove the caller's own echoed text from a BI-authored reason.

    BI quotes caller-supplied arguments (`path`, camera names) verbatim
    inside its `reason`, so a caller can plant a matcher's trigger phrase in
    text BI hands straight back. Extraction alone doesn't remove it — the
    planted phrase IS inside BI's reason — so the caller's literal has to go
    before any fragment matching runs.

    Naive ``reason.replace(needle, " ")`` is fragile in the OTHER direction,
    a false NEGATIVE: ``str.replace("")`` interleaves the replacement between
    every character, so an empty or whitespace-only needle turns
    ``"clip not bvr"`` into ``" c l i p  n o t  b v r "`` and nothing matches
    — a genuine not-a-clip stops being recognised. Short needles do it too
    (``needle="t"`` shreds ``"not bvr"`` into ``"no  bvr"``).

    At `bi_update_record`'s call sites those hazards happen to be unreachable
    today: `_build_update_payload` rejects an empty path and forces a leading
    ``@``. But that guard lives in a DIFFERENT function, nothing asserts it
    here, `_read_record_state` is handed `path` directly, and
    `bi_get_camera_config` passes a camera short name with no such shape rule
    at all. So the robustness is made local instead of borrowed:

    elide only a needle of at least `_MIN_ELIDABLE_NEEDLE` characters. Below
    that the needle is too small to CONTAIN a matcher fragment, so it cannot
    forge a match and skipping it can't let one through — while eliding it
    demonstrably shreds real ones.

    That single guard cannot manufacture a false negative, because its
    fallback is the BI reason UNCHANGED, which is strictly more matchable
    than any elided form. The only text ever removed is a caller literal long
    enough to have carried a fragment in the first place.

    The floor is not the whole story, though — it only protects a needle too
    SHORT to carry a fragment. A needle can clear it and still be a strict
    piece of one: `memo="authorized"` is 10 chars, sails past the 7-char
    floor, and shreds BI's own ``"Not authorized"`` into ``"not  "`` — the
    graduation match goes False, the admin retry that would have succeeded
    never fires, and the memo edit silently fails to land (with no admin
    configured it is worse still: a bare ``BiError`` instead of the
    actionable `BiAdminRequired`). So the floor is paired with
    `_is_marker_fragment` over ``markers``, the calling site's own fragment
    tuple: a needle that is a strict substring of one of them is not elided
    at all. Safe in both directions, exactly as at `_redact_echoed_args` — a
    strict fragment carries no marker of its own, while a needle that IS a
    marker or CONTAINS one is still elided, which is every forge case.

    ``markers`` defaults to empty (no fragment guard) so a site that does not
    pass its tuple keeps the floor-only behaviour rather than silently
    acquiring a guard keyed to somebody else's vocabulary.

    An "only elide when the needle actually occurs" guard was considered and
    deliberately left out: `str.replace` is already a no-op when the needle is
    absent, so it is provably equivalent to this code on every input (verified
    by differential fuzz) — dead weight, and unkillable by any test.
    """
    # `str()` rather than assuming a str: `caller_text` comes straight from
    # caller args, and the jsonschema type check that would enforce `string`
    # is optional in this server (see `raise_validation_refusal`). A TypeError
    # here would escape as an *unhandled* exception from an error handler.
    needle = str(caller_text).strip().lower()
    if len(needle) < _MIN_ELIDABLE_NEEDLE:
        return reason
    # A strict fragment of one of this site's markers can only destroy
    # signal, never forge it — see `_is_marker_fragment`.
    if _is_marker_fragment(needle, markers):
        return reason
    return reason.replace(needle, " ")


# The shapes in which BI demonstrably QUOTES a caller argument back: a label,
# a colon, then the caller's text as the rest of the reason ("Not found:
# SecCam_3"). Matched as a suffix-after-colon rather than by label, because
# BI's label vocabulary is undocumented and varies by cmd.
def echoed_caller_text(reason: str, caller_text: str) -> str:
    """``caller_text`` if ``reason`` demonstrably ECHOES it, else ``""``.

    A gate in front of `_elide_caller_text` for call sites whose caller value
    has no distinguishing shape rule. Blanket elision — removing the caller's
    text from EVERY position it happens to occupy — cannot tell an echo from
    an identical word BI authored itself, and for ordinary-English fragments
    that difference is not academic:

        camera `unknown` + BI's own ``"Unknown command"``
          → blanket elision yields ``" command"``, the camconfig fallback
            fragment is gone, and `bi_get_camera_config` RAISES instead of
            degrading to the documented shallow camlist path.

    A camera legitimately named `unknown` or `invalid` is an ordinary name,
    not an attack, so that failure mode needs no adversary at all.

    So the caller's text is only treated as an echo where BI's own reply shape
    proves it is one: the text is the entire remainder after a ``":"``. That
    covers the observed echo wording (``"Not found: <short>"``) while leaving
    an incidental mid-sentence occurrence of the same word — which is BI's
    prose, not a quotation — in place to be matched.

    Tested as a SUFFIX of the whole needle, not by partitioning the reason on
    its last ``":"``: the needle may itself contain colons (a camera named
    ``invalid:cam``), and `rpartition` then compared only the tail after the
    FINAL one (``"cam"`` vs ``"invalid:cam"``), saw no echo, left the planted
    name in place and re-opened the very downgrade this gate closes. The
    separator is required to sit immediately before the needle, so what counts
    as an echo is unchanged for every colon-free name.

    Returns the needle to elide (so the caller feeds it straight to
    `_elide_caller_text`, which keeps its own `_MIN_ELIDABLE_NEEDLE` floor and
    keeps the site visible to the elidable-needle audit), or ``""`` — which
    that floor rejects, leaving the reason untouched.
    """
    needle = str(caller_text).strip().lower()
    if not needle:
        return ""
    lowered = reason.strip().lower()
    if not lowered.endswith(needle):
        return ""
    head = lowered[: len(lowered) - len(needle)]
    if head.rstrip().endswith(":"):
        return needle
    return ""


# Historical private name. Kept as an alias so the terminal-status call sites
# and their tests are undisturbed by the rename that made this shareable.
_terminal_status_candidate = bi_authored_reason


def is_terminal_status_message(message: str) -> bool:
    """True if ``message`` reports a known terminal ``data.status``.

    The canonical predicate shared by classification (`_classify_fail`, which
    matches the bare status text) and by downstream *graduation guards* in the
    tool layer (which only ever see the rendered `BiError` message, e.g.
    ``"Blue Iris cmd=export failed: Clip not BVR"``). Both sides MUST agree:
    `_classify_fail` normalises casing/whitespace before matching, so a guard
    hard-coding the literal ``"Clip not BVR"`` classified terminal but then
    RAISED on any case variant instead of returning its documented
    ``{ok:false}`` envelope.

    ANCHORED equality on the BI-authored reason inside the known wrapper
    frame, NOT a free substring search, and deliberately NOT "contains a
    terminal status anywhere". Two reasons, in order of severity:

      * ``bi_export_clip``'s ``path`` is unconstrained free text and BI echoes
        a rejected path straight back in its ``reason``. Under a substring
        match, ``path="@clip not bvr"`` turned BI's genuine *rejection*
        (``"Not found: @clip not bvr"``) into a successful graduation, so the
        caller read "BI rejected your path" as "export completed" and stopped
        polling. Any BI text that merely MENTIONS the phrase — a camera name,
        a memo, an echoed filename — had the same effect.
      * An EMBEDDED status (``"job 7: Clip not BVR"``) is treated as terminal
        on NEITHER side, which is the safe direction: `_classify_fail` already
        declines it (unclassifiable-None → one retry), so matching it here
        would have made the guard graduate something the classifier does not
        trust. Not-terminal on both sides costs at most one wasted retry on a
        wording BI has never been observed to emit; terminal-on-both would
        re-open defect 1 for every reason that happens to end in the phrase.
        BI's own graduation reply is the bare status, which this matches.

    Equality on the extracted reason (rather than ``endswith``) also rules out
    a terminal status that is only the SUFFIX of a longer word or phrase, and
    one carrying trailing punctuation — both of which a naive anchor accepts.

    ``_classify_fail`` normalises the bare status the same way (strip + lower)
    and compares against the same ``_TERMINAL_STATUSES`` frozenset, so the two
    sides cannot drift and a new entry reaches both with no second edit.

    Deliberately NOT a typed exception subclass: `type(e) is BiError` guards
    across the tool layer mean "bare = shapeable, subclass = durable failure,
    re-raise", so a new subclass would be re-raised by the very guard that
    needs to catch it.
    """
    return _terminal_status_candidate(message) in _TERMINAL_STATUSES


def _select_fail_text(data: dict[str, Any], *, for_display: bool = False) -> str | None:
    """Pick the authoritative failure text from a fail envelope's ``data``.

    Single source of truth for BOTH ``_fail_reason`` (what the human/guard
    sees) and ``_classify_fail`` (the retry verdict). They MUST agree on which
    key won: if the verdict comes from `status` while the message quotes
    `reason`, a caller matching on the message misses. ``bi_export_clip``'s
    graduation guard does exactly that (``"Clip not BVR" in str(e)``), so a
    junk non-string `reason` alongside a terminal `status` would make it raise
    instead of returning its ``{ok:false}`` envelope.

    `reason` wins when it is a usable non-empty string; otherwise `status`
    (the export queue's terminal-state channel — AGENTS.md Rule 6.5).
    Returns None when neither key yields usable text.

    ``for_display`` adds the last-resort fallback of stringifying a truthy
    NON-string `reason`. It is on for `_fail_reason` (losing the only
    diagnostic BI sent would be worse than an ugly message) and off for
    `_classify_fail`, because substring-matching a repr is not classification:
    ``{"reason": {"session": "camera offline"}}`` stringifies to
    ``"{'session': 'camera offline'}"``, which contains the bare `"session"`
    auth marker and was typed auth-class — a pointless re-login and a
    `BiAuthFailed` for a camera that was merely offline.

    The two callers still agree on which key WON, which is the property the
    export guard depends on: the fallback is reached only after both string
    branches declined, so whenever `_classify_fail` gets text at all,
    `_fail_reason` got that same text from that same key.
    """
    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    status = data.get("status")
    if isinstance(status, str) and status.strip():
        return status
    # A non-string `reason` is still the better diagnostic than nothing, but it
    # can't be classified — surface it while `_classify_fail` returns None.
    if for_display and reason:
        return str(reason)
    return None


def _fail_reason(resp: dict[str, Any]) -> str:
    """Best-effort human-readable reason from a ``result:"fail"`` envelope."""
    data = resp.get("data")
    if isinstance(data, dict):
        text = _select_fail_text(data, for_display=True)
        if text:
            return text
        # Fall back to the whole dict repr when neither key yields text —
        # whatever BI did include shouldn't vanish from the error message.
        return str(data) if data else "no reason given"
    return str(data) if data else "no reason given"


def _is_word_char(ch: str) -> bool:
    r"""True if ``ch`` is a ``\w`` character, i.e. one that ``\b`` can anchor on.

    Kept in step with `re`'s own definition (Unicode word chars plus ``_``)
    rather than restated as a character class, so an anchor is only ever
    asserted where a boundary can exist.
    """
    return ch.isalnum() or ch == "_"


def _is_auth_marker_fragment(needle: str) -> bool:
    """True if ``needle`` is a STRICT substring of some `_AUTH_FAIL_SUBSTRINGS`
    entry — i.e. a piece of BI's own auth wording that is not itself a marker.

    Such a needle cannot forge an auth verdict on its own: no marker is a
    substring of another marker, so a strict fragment of one contains no
    marker, and echoing it back verbatim adds no marker to the reason.
    Redacting it, however, CAN destroy a genuine marker BI authored. So the
    hazard is one-directional and the safe answer is to leave it alone.

    Derived from `_AUTH_FAIL_SUBSTRINGS` rather than from a hand-listed set of
    risky names, so adding a marker automatically extends the protection to
    its fragments instead of silently re-opening the shredding hole.
    """
    return any(needle in marker and needle != marker for marker in _AUTH_FAIL_SUBSTRINGS)


# Request-body keys that are NOT caller payload — the cmd name and the session
# token this client minted itself. Everything else in the body reached us from
# a tool argument, so BI may echo it back inside `reason`.
# RESERVED: these keys are exempt from redaction, so a tool payload builder
# must never be able to emit one — a caller-reachable `session=` would both
# override the minted token and plant an unredactable marker in the reason.
_NON_CALLER_BODY_KEYS = frozenset({"cmd", "session"})


def _redact_echoed_args(text: str, body: dict[str, Any] | None) -> str:
    """Blank out caller-supplied argument values echoed inside a BI reason.

    BI quotes request arguments back verbatim (``"Not found: <camera>"``), so
    every string a caller put in the request body is text the caller controls
    inside `reason`. `_classify_fail` then substring-searches that reason for
    auth markers, which made the VERDICT caller-controlled: a camera named
    ``session`` turned BI's plain "not found" into an auth-class fail, and
    auth-class means *re-login and re-POST the same cmd*. For a mutating cmd
    that is a second execution the caller asked for and a `BiAuthFailed`
    ("check your credentials") for a fault that was nothing of the kind.

    Redaction runs BEFORE matching and is deliberately unlike
    `_elide_caller_text`, whose `_MIN_ELIDABLE_NEEDLE` floor exists to protect
    a BI-authored reason from being shredded by a short needle. The two have
    OPPOSITE safe-failure directions, so they cannot share that LENGTH rule
    (the fragment guard below is a different matter — it is safe in both
    directions and both mechanisms apply it; see `_is_marker_fragment`):

    * There, over-eliding is the hazard — it destroys a real match and a
      documented fallback stops working (loud, but wrong).
    * Here, UNDER-redacting is the hazard — it grants a forged auth verdict
      and an extra mutation round-trip. Over-redacting merely loses a
      re-login the caller can retry, and only for the exact wording the
      caller themselves supplied. `_AUTH_FAIL_SUBSTRINGS` also holds needles
      shorter than that floor (``"login"``, 5), so applying it here would
      leave the shortest markers forgeable — precisely the hole.

    So every caller value is redacted at any length. The ambiguous case — BI
    text that is indistinguishable from the caller's own echoed text — is
    resolved toward "not authenticated-class", i.e. no retry authorised.

    That resolution has a known, accepted cost: when ANY caller string equals
    a marker IN FULL (``memo="session"``, ``search="unauthorized"`` — free-form
    fields with no content validation, so this needs no adversary), redaction
    consumes BI's genuine marker too and this one call raises ``BiError``
    instead of recovering its session. It is irreducible, not an oversight: at
    that point the genuine and the forged reason are byte-identical, so no rule
    can re-admit the one without re-admitting the other and reopening the
    forge. Word-anchoring already covers everything short of full equality
    (``"session cam"``, ``"Session_Room"``), and the cost is a loud, accurate,
    non-destructive failure the caller can retry — the opposite direction from
    a silent second execution of a mutating cmd.

    Redaction is WORD-ANCHORED, though, because "at any length" applied with a
    bare ``str.replace`` re-introduced the shredding hazard from the other
    side: an ordinary camera short name that is a strict SUB-word fragment of
    BI's own auth wording destroyed the marker and returned a *decided*
    non-auth verdict, silently skipping genuine session recovery. Camera `on`
    (or `io`, or `e`) against BI's real ``"invalid session"`` observed zero
    re-logins where one was due; ``"invalid session"`` is broken by 5 distinct
    single characters, ``"unauthorized"`` by 11. No minimum-length rule on
    camera names exists anywhere, so that needs no adversary either.

    Anchoring handles the SUB-WORD half of that: BI ECHOES the caller's value
    as a whole token ("Not found: <camera>"), so every forge — ``session``,
    ``login``, ``@session``, ``session expired``, ``unauthorized`` — is a
    whole word in the echo and is still removed, while a needle occurring only
    as a fragment inside a longer word is left alone. ``\b`` is asserted only
    on an END that is actually a word character: a path needle like
    ``@session`` starts with punctuation, where a leading ``\b`` would demand
    a word char before the ``@`` and fail to match at the start of a reason.

    Anchoring alone is NOT the whole guarantee, though — it only protects a
    needle that is a strict sub-WORD fragment. `_AUTH_FAIL_SUBSTRINGS` holds
    MULTI-WORD markers (``"not logged in"``, ``"not authenticated"``) whose
    constituent words are ordinary camera names, and those match as whole
    words: camera `in` against BI's genuine ``"not logged in"`` shredded the
    marker exactly as camera `on` did against ``"invalid session"``. So the
    boundary rule is paired with `_is_auth_marker_fragment`: a needle that is
    a strict substring of some marker is not redacted at all. That is safe in
    both directions because no marker is a substring of another, so a strict
    fragment of one carries no marker of its own and cannot forge a verdict —
    while a needle that IS a marker (or contains one) is still redacted, which
    is every forge case. The rule reads `_AUTH_FAIL_SUBSTRINGS` directly, so a
    marker added later cannot silently reopen the shredding hole.
    """
    if not body:
        return text
    for key, value in body.items():
        if key in _NON_CALLER_BODY_KEYS:
            continue
        if not isinstance(value, str):
            continue
        needle = value.strip().lower()
        if not needle:
            continue
        # A strict fragment of an auth marker can only destroy signal, never
        # forge it — see `_is_auth_marker_fragment`.
        if _is_auth_marker_fragment(needle):
            continue
        pattern = re.escape(needle)
        if _is_word_char(needle[0]):
            pattern = r"\b" + pattern
        if _is_word_char(needle[-1]):
            pattern = pattern + r"\b"
        text = re.sub(pattern, " ", text)
    return text


def _classify_fail(resp: dict[str, Any], body: dict[str, Any] | None = None) -> bool | None:
    """Classify a fail envelope: True = auth-class reason, False = non-auth
    reason, None = unclassifiable (missing/malformed ``data.reason``).

    ``body`` is the request body that produced ``resp``. Its caller-supplied
    values are redacted out of the reason before auth matching, so echoed
    argument text cannot manufacture the auth verdict that authorises a
    re-login + retry (see `_redact_echoed_args`). Omitting it keeps the old,
    forgeable behaviour and exists only for callers that genuinely have no
    body (tests probing the classifier itself).

    The two call sites weigh None differently:
    - retry decision: None retries (preserves the historical broad-retry
      behavior for odd/old BI replies; strict tightening risks regressions)
    - post-retry exception typing: None raises plain ``BiError`` — claiming
      "auth failed, check credentials" needs an explicit auth-class reason.
    """
    data = resp.get("data")
    if not isinstance(data, dict):
        return None
    text = _select_fail_text(data)
    if not text:
        return None
    lowered = text.strip().lower()
    # Auth markers are matched ONLY against text BI authored — never against
    # the caller's own echoed arguments.
    if any(s in _redact_echoed_args(lowered, body) for s in _AUTH_FAIL_SUBSTRINGS):
        return True
    # Known terminal states are durable end-conditions: a re-login can never
    # change the answer, so decide non-auth rather than leaving it
    # unclassifiable-None (which would retry). Deliberately narrow — an
    # unknown status keeps the None default, because a transient like "busy"
    # could plausibly clear on a retry.
    # Shared with the tool layer's graduation guard via the same predicate, so
    # the two sides cannot drift: here `text` is already the bare status (no
    # wrapper), which `is_terminal_status_message` treats as its own tail.
    if is_terminal_status_message(lowered):
        return False
    # `reason` said something we don't recognise as auth-class: a decided
    # non-auth verdict (unchanged behaviour).
    if isinstance(data.get("reason"), str) and data["reason"].strip():
        return False
    # Text came from `status` and isn't a known terminal state —
    # unclassifiable, keep the historical retry.
    return None


class BiClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if not host:
            raise BiBadRequest("BI_HOST is empty in .env")
        if not user:
            raise BiBadRequest("BI_USER is empty in .env")
        if not password:
            raise BiBadRequest("BI_PASS is empty in .env")

        self.host = host
        self.port = int(port)
        self.user = user
        self._password = password
        self.session: str | None = None
        self.login_data: dict[str, Any] | None = None

        self._http = httpx.Client(
            base_url=f"http://{self.host}:{self.port}",
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    # ----- public API --------------------------------------------------

    def login(self) -> dict[str, Any]:
        """Perform the two-step MD5 handshake. Returns the login response ``data``."""
        log.debug("Login step 1: requesting session for user=%s", self.user)
        # Deliberately _post, NOT _call_with_auth_retry: step 1's fail reply
        # is the expected handshake response, not an auth failure to classify.
        step1 = self._post({"cmd": "login"})

        # Step 1 always returns result:"fail" + session. If it returns success
        # immediately, the server is in no-LAN-password mode and we're done.
        if step1.get("result") == "success":
            self.session = step1.get("session")
            self.login_data = step1.get("data", {})
            log.debug("Login: server accepted unauthenticated session")
            return self.login_data

        sess = step1.get("session")
        if not sess:
            raise BiError("Blue Iris login step 1 did not return a session token")

        token = hashlib.md5(f"{self.user}:{sess}:{self._password}".encode()).hexdigest()
        log.debug("Login step 2: sending MD5 response")
        step2 = self._post({"cmd": "login", "session": sess, "response": token})

        if step2.get("result") != "success":
            reason = (step2.get("data") or {}).get("reason", "rejected")
            raise BiAuthFailed(f"Blue Iris rejected login: {reason}")

        self.session = sess
        self.login_data = step2.get("data", {})
        log.info("Login successful; BI version=%s", self.login_data.get("version"))
        return self.login_data

    def call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Call a BI cmd and return the full response envelope (not just `data`).

        Most callers want ``call()`` which unwraps ``data``. ``call_raw()`` is
        for cmds like ``trigger``/``ptz`` (write side) that return
        ``{result:"success"}`` with no ``data``, where the caller wants to
        verify ``result`` rather than treat absence-of-data as the response.
        """
        if not self.session:
            self.login()
        body: dict[str, Any] = {"cmd": cmd, "session": self.session, **payload}
        log.debug("Call (raw) cmd=%s", cmd)
        return self._call_with_auth_retry(cmd, body)

    def _call_with_auth_retry(self, cmd: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST a cmd body; on an auth-class fail, re-login and retry once.

        Non-auth failures (bad camera name, cmd-specific permission denial,
        ...) raise ``BiError`` immediately — a re-login round-trip cannot fix
        them. A post-retry failure raises ``BiAuthFailed`` when the second
        reason is still auth-class (so ``admin_call`` can re-tag it as
        ``BiAdminAuthFailed``), plain ``BiError`` otherwise.
        """
        resp = self._post(body)
        if resp.get("result") != "fail":
            return resp
        if _classify_fail(resp, body) is False:
            log.debug("cmd=%s failed (non-auth): %s — not retrying", cmd, _fail_reason(resp))
            raise BiError(f"Blue Iris cmd={cmd} failed: {_fail_reason(resp)}")
        log.info("cmd=%s returned auth-class fail; attempting one session re-login + retry", cmd)
        self.session = None
        self.login()
        body["session"] = self.session
        resp = self._post(body)
        if resp.get("result") == "fail":
            reason = _fail_reason(resp)
            if _classify_fail(resp, body) is True:
                raise BiAuthFailed(f"Blue Iris cmd={cmd} failed after re-login: {reason}")
            raise BiError(f"Blue Iris cmd={cmd} failed: {reason}")
        return resp

    def call(self, cmd: str, **payload: Any) -> Any:
        """Call a Blue Iris JSON cmd. Logs in lazily, retries once on session expiry."""
        if not self.session:
            self.login()

        body: dict[str, Any] = {"cmd": cmd, "session": self.session, **payload}
        log.debug("Call cmd=%s", cmd)
        resp = self._call_with_auth_retry(cmd, body)

        # Most cmds return result:"success" + data. A few return data inline.
        if "data" in resp:
            return resp["data"]
        return resp

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        """GET a non-/json endpoint (e.g. `/image/<short>`) with the session
        token attached as a query param. Returns (body_bytes, content_type).

        Re-logs in and retries once on 401/403 in case the session expired,
        matching the auth-retry behavior of ``call()``.
        """
        if not self.session:
            self.login()
        body, ctype, status = self._get_raw(path, {**params, "session": self.session})
        if status in (401, 403):
            log.info("GET %s returned %d; re-login + retry", path, status)
            self.session = None
            self.login()
            body, ctype, status = self._get_raw(path, {**params, "session": self.session})
        if status == 404:
            raise BiNotFound(f"Blue Iris HTTP 404 on {path}")
        if status >= 400:
            raise BiBadRequest(f"Blue Iris returned HTTP {status} on {path}: {body[:200]!r}")
        return body, ctype

    def _get_raw(self, path: str, params: dict[str, Any]) -> tuple[bytes, str, int]:
        try:
            r = self._http.get(path, params=params)
        except httpx.ConnectError as e:
            raise BiUnreachable(f"Cannot connect to Blue Iris at {self.host}:{self.port}: {e}") from e
        except httpx.TimeoutException as e:
            raise BiUnreachable(f"Blue Iris at {self.host}:{self.port} timed out: {e}") from e
        except httpx.HTTPError as e:
            raise BiUnreachable(f"HTTP error talking to Blue Iris: {e}") from e
        return r.content, r.headers.get("content-type", ""), r.status_code

    def close(self) -> None:
        self._http.close()

    # ----- internals ---------------------------------------------------

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self._http.post("/json", json=body)
        except httpx.ConnectError as e:
            raise BiUnreachable(f"Cannot connect to Blue Iris at {self.host}:{self.port}: {e}") from e
        except httpx.TimeoutException as e:
            raise BiUnreachable(f"Blue Iris at {self.host}:{self.port} timed out: {e}") from e
        except httpx.HTTPError as e:
            raise BiUnreachable(f"HTTP error talking to Blue Iris: {e}") from e

        if r.status_code >= 500:
            raise BiError(f"Blue Iris returned HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code == 404:
            raise BiNotFound(f"Blue Iris HTTP 404 on /json — is the web server enabled?")
        if r.status_code >= 400:
            raise BiBadRequest(f"Blue Iris returned HTTP {r.status_code}: {r.text[:200]}")

        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise BiError(f"Blue Iris returned non-JSON: {r.text[:200]}") from e

    def __enter__(self) -> "BiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class BiClients:
    """Pair of Blue Iris clients: the default read-only user, plus an optional
    admin user for cmds that BI gates behind admin rights (e.g. `camconfig`,
    `log`).

    Three configuration shapes are supported:

      1. ``BI_ADMIN_USER`` + ``BI_ADMIN_PASS`` set → ``_explicit_admin`` is
         a separate ``BiClient`` instance; ``admin`` returns it always.
      2. ``BI_USER`` is itself an admin and no admin env vars are set → the
         ``read`` client doubles as the admin client. We discover this
         lazily: the first time ``admin`` is queried we check
         ``read.login_data["admin"]``, logging in if needed.
      3. ``BI_USER`` is non-admin and no admin env vars are set → ``admin``
         resolves to ``None`` (after the lazy probe).

    Shape (2) used to be resolved eagerly in ``from_env()`` via an extra
    startup ``read.login()``. That stalled MCP initialization on slow/dead
    BI hosts and permanently cached ``admin=None`` after a single transient
    failure. The lazy approach avoids both problems: BI is only contacted
    when something actually needs auth, and a transient failure leaves the
    probe re-armed for the next call.
    """

    def __init__(self, read: BiClient, admin: BiClient | None):
        self.read = read
        # The explicit admin client (configuration shape 1). Stays None for
        # shapes 2 and 3.
        self._explicit_admin = admin

    @property
    def admin(self) -> BiClient | None:
        """The admin client, or None if no admin path is available.

        Side-effect-free lookup. If no explicit admin client was configured,
        falls back to ``read`` *only if* ``read`` has already logged in and
        the login response reports admin=true. If ``read`` hasn't logged in
        yet, returns None — callers that ACTUALLY need admin should call
        ``resolve_admin()`` instead, which will trigger a login as needed.

        This split exists so that capability checks like
        ``if client.admin is not None: try admin path else fall back`` stay
        cheap and don't initiate network I/O.
        """
        if self._explicit_admin is not None:
            return self._explicit_admin
        if self.read.login_data is not None and self.read.login_data.get("admin"):
            return self.read
        return None

    def resolve_admin(self) -> BiClient | None:
        """Like ``admin`` but actively logs in to find out.

        When no explicit admin client is configured, this forces a
        ``read.login()`` (if it hasn't happened yet) so the lazy BI_USER-as-
        admin probe can answer correctly even on a fresh process whose first
        tool call is admin-gated. Returns None only after BI has confirmed
        the user lacks admin (or if the login itself fails — caller's
        exception handler should surface that).

        Capability-check call sites should keep using ``.admin`` (cheap,
        no I/O). Admin-required call sites use this.
        """
        if self._explicit_admin is not None:
            return self._explicit_admin
        if self.read.login_data is None:
            self.read.login()
        if self.read.login_data is not None and self.read.login_data.get("admin"):
            return self.read
        return None

    def admin_or_raise(self) -> BiClient:
        admin = self.resolve_admin()
        if admin is None:
            raise BiAuthFailed(
                "This tool requires admin BI credentials. Set BI_ADMIN_USER "
                "and BI_ADMIN_PASS in bi-mcp/.env (create a dedicated admin "
                "user in Blue Iris → Settings → Users), or grant admin to the "
                "existing BI_USER."
            )
        return admin

    @contextlib.contextmanager
    def fresh_admin_session(self) -> Iterator[BiClient]:
        """Yield a throwaway admin ``BiClient`` for verify-after-write reads.

        Brand-new httpx session and BI session token, cloned from the admin
        user's credentials — never touches the shared admin singleton.
        Automatically closed on exit.

        Why this exists: BI 5.9.9.71 returns stale camconfig/camlist reads
        to the session that issued the write for ~2-3s. Defeating that
        staleness used to mean clearing the shared singleton's session token
        mid-call, which corrupted overlapping tool calls (Codex review
        2026-05-22). A throwaway client per verify call defeats staleness
        without the singleton hazard.

        Pair with :meth:`verify_call` (preferred) so auth blips during
        verification are surfaced as ``BiVerifyInconclusive`` rather than
        hard admin-auth errors.
        """
        src = self.admin_or_raise()
        fresh = BiClient(host=src.host, port=src.port, user=src.user, password=src._password)
        try:
            yield fresh
        finally:
            fresh.close()

    @staticmethod
    def verify_call(fresh: BiClient, cmd: str, **payload: Any) -> Any:
        """Run a post-write verification read through a fresh admin client.

        Converts blip-class verify-side failures into typed subclasses of
        ``BiVerifyInconclusive``:

          * ``BiAuthFailed`` → ``BiVerifyAuthBlip`` (kind=``verify_auth_blip``).
            Throwaway login could not authenticate. Causes range from
            transient (BI session pressure, parallel admin logins) to
            durable (creds rotated, account locked). Callers seeing this
            repeatedly should investigate creds rather than blind-retry.
          * ``BiUnreachable`` → ``BiVerifyUnreachable``
            (kind=``verify_unreachable``). Network blip / timeout / BI
            restart. Almost always transient.

        Rationale for distinct kinds: a verify-side auth failure deserves
        different handling than a network blip — a single boolean
        "inconclusive" flag would hide durable creds breakage behind a
        transient-looking flag. Surfacing the kind lets callers escalate
        auth-class blips on repeat without us having to do (brittle)
        stateful classification at the verify layer.

        Rationale for raising at all (rather than returning): the write
        already succeeded (verify only runs after a success reply), so
        these are not "BI is down" / "creds are wrong" failures from the
        caller's perspective — they are "write landed, post-read couldn't
        confirm" situations. Dispatchers catch and convert to
        ``verified=False`` + structured ``verify_error_kind`` in the
        response, while keeping ``ok=True`` (the write *was* accepted).

        Structural / logic errors (``BiBadRequest``, ``BiNotFound``, and
        bare ``BiError`` for malformed responses) propagate unchanged —
        those indicate real bugs in the verify path that the caller needs
        to see loudly.
        """
        try:
            return fresh.call(cmd, **payload)
        except BiAuthFailed as e:
            raise BiVerifyAuthBlip(
                f"verify read cmd={cmd} could not authenticate: {e}"
            ) from e
        except BiUnreachable as e:
            raise BiVerifyUnreachable(
                f"verify read cmd={cmd} could not reach Blue Iris: {e}"
            ) from e

    def admin_call(self, cmd: str, **payload: Any) -> Any:
        """Call an admin-gated BI cmd. Re-tags BiAuthFailed from the admin
        client as BiAdminAuthFailed so the hint points at the right env vars.

        Triggers a ``read.login()`` if no explicit admin client is configured
        and BI hasn't been contacted yet — see ``resolve_admin``.
        """
        admin = self.resolve_admin()
        assert admin is not None  # callers should pre-check via .admin
        try:
            return admin.call(cmd, **payload)
        except BiAuthFailed as e:
            raise BiAdminAuthFailed(str(e)) from e

    def admin_call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Like ``call_raw`` but routed through the admin client."""
        admin = self.resolve_admin()
        assert admin is not None
        try:
            return admin.call_raw(cmd, **payload)
        except BiAuthFailed as e:
            raise BiAdminAuthFailed(str(e)) from e

    @property
    def bi_version(self) -> str | None:
        """Connected BI version (best-effort; populated after first login)."""
        if self.read.login_data:
            return self.read.login_data.get("version")
        return None

    def admin_login(self) -> dict[str, Any]:
        """Force admin auth.

        For shape (1) — explicit admin client — logs in if needed. For
        shape (2) — read-doubles-as-admin — forces ``read.login()`` and
        returns its data only if the user actually has admin. Raises
        ``BiAdminAuthFailed`` on auth failure, ``BiAuthFailed`` on
        no-admin-available (consistent with ``admin_or_raise``).
        """
        if self._explicit_admin is not None:
            if self._explicit_admin.login_data is not None:
                return self._explicit_admin.login_data
            try:
                return self._explicit_admin.login()
            except BiAuthFailed as e:
                raise BiAdminAuthFailed(str(e)) from e
        # No explicit admin — probe `read`.
        if self.read.login_data is None:
            self.read.login()
        if not (self.read.login_data or {}).get("admin"):
            raise BiAuthFailed(
                "This tool requires admin BI credentials. The configured "
                "BI_USER does not have admin enabled. Either grant admin to "
                "that user in Blue Iris → Settings → Users, or set "
                "BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env to a separate "
                "admin user."
            )
        return self.read.login_data  # type: ignore[return-value]

    def call(self, cmd: str, **payload: Any) -> Any:
        """Delegate to the read-only client. Tools that need admin should
        call `.admin_call(...)` explicitly."""
        return self.read.call(cmd, **payload)

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        """Delegate to the read-only client's GET helper."""
        return self.read.get_bytes(path, **params)

    def call_raw(self, cmd: str, **payload: Any) -> dict[str, Any]:
        """Delegate to the read-only client's raw cmd path.

        Used by the mutation tools (``bi_trigger_camera``, ``bi_set_ptz_preset``,
        ``bi_set_profile``) which need the full response envelope
        (``{result:"success"}``) rather than the unwrapped ``data`` block —
        BI's write cmds return success markers, not data.
        """
        return self.read.call_raw(cmd, **payload)

    @property
    def login_data(self) -> dict[str, Any] | None:
        return self.read.login_data

    def login(self) -> dict[str, Any]:
        return self.read.login()

    def close(self) -> None:
        self.read.close()
        if self._explicit_admin is not None:
            self._explicit_admin.close()


def from_env() -> BiClients:
    """Build a BiClients pair from environment variables.

    Required:  BI_HOST, BI_PORT, BI_USER, BI_PASS  (read-only user)
    Optional:  BI_ADMIN_USER, BI_ADMIN_PASS        (admin user for camconfig/log)

    Does NOT contact Blue Iris. If no explicit admin creds are given,
    ``BiClients.admin`` lazily checks whether ``BI_USER`` itself has admin
    rights on first use — see the class docstring.
    """
    import os

    host = os.environ.get("BI_HOST", "")
    port = int(os.environ.get("BI_PORT", "81") or "81")

    read = BiClient(
        host=host,
        port=port,
        user=os.environ.get("BI_USER", ""),
        password=os.environ.get("BI_PASS", ""),
    )

    admin_user = os.environ.get("BI_ADMIN_USER", "").strip()
    admin_pass = os.environ.get("BI_ADMIN_PASS", "")
    # Both or neither — a half-filled admin config is almost always a typo and
    # would silently downgrade admin-gated tools to the shallow fallback.
    if bool(admin_user) != bool(admin_pass):
        which_set = "BI_ADMIN_USER" if admin_user else "BI_ADMIN_PASS"
        which_missing = "BI_ADMIN_PASS" if admin_user else "BI_ADMIN_USER"
        raise BiBadRequest(
            f"{which_set} is set in .env but {which_missing} is empty. "
            "Set both to enable admin-gated tools, or unset both to run read-only."
        )

    explicit_admin: BiClient | None = None
    if admin_user and admin_pass:
        explicit_admin = BiClient(host=host, port=port, user=admin_user, password=admin_pass)
    # Else: no explicit admin. BI is NOT contacted here — the `BI_USER as
    # admin` fallback is resolved lazily by `BiClients.admin` on first use.

    return BiClients(read=read, admin=explicit_admin)
