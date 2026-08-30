"""Auth-failure discrimination in BiClient's retry path.

BI returns ``result:"fail"`` both for expired sessions AND for legitimate
cmd failures (bad camera name, permission denied on a specific cmd, ...).
The client used to re-login + retry on ANY fail, burning a session
round-trip on non-auth failures and letting cmd-specific errors surface
as auth-looking ones after the pointless retry.

The fix (sourced from ha-blueiris's reason taxonomy,
``api/blue_iris_api.py:275-288``): classify the fail ``reason`` first.

  * auth-class reason (or missing/malformed reason — defensive default)
    → re-login + retry once, as before
  * non-auth reason → raise ``BiError`` immediately, no retry
  * post-retry failure → ``BiAuthFailed`` only when the second reason is
    EXPLICITLY auth-class; plain ``BiError`` for non-auth AND for
    unclassifiable reasons — claiming "check your credentials" needs an
    explicit auth-class reason, and ``admin_call``'s re-tag to
    ``BiAdminAuthFailed`` must never swallow a legitimate cmd failure

Deviation from the ha-blueiris whitelist (found in adversarial review
2026-07-01): "access denied" is NOT auth-class here. On BI 5.9.9.71 it
means per-cmd capability gating on a valid session (``tracks`` even for
admin, ``export`` without clipcreate), and ``bi_update_record``'s
read→admin graduation requires it to surface as a bare ``BiError``.
"""

from __future__ import annotations

import pytest

from bi_mcp.client import BiClient, BiClients
from bi_mcp.errors import (
    BiAdminAuthFailed,
    BiAuthFailed,
    BiError,
    BiVerifyAuthBlip,
)


def _client(monkeypatch, post_responses: list[dict]) -> tuple[BiClient, dict]:
    """A BiClient with a canned _post script and a spy on login().

    ``post_responses`` are consumed one per ``_post`` call. ``login`` does
    not consume a response — it just refreshes the session token
    (``sess2`` on first re-login). Bodies sent to ``_post`` are recorded
    in ``calls["bodies"]``.
    """
    c = BiClient(host="test", port=81, user="u", password="p")
    calls = {"post": 0, "login": 0, "bodies": []}

    def fake_post(self, body):
        calls["post"] += 1
        calls["bodies"].append(dict(body))
        return post_responses.pop(0)

    def fake_login(self):
        calls["login"] += 1
        self.session = f"sess{calls['login'] + 1}"
        self.login_data = {"admin": False}
        return self.login_data

    monkeypatch.setattr(BiClient, "_post", fake_post)
    monkeypatch.setattr(BiClient, "login", fake_login)
    c.session = "sess1"  # pretend already logged in
    return c, calls


def _counts(calls: dict) -> dict:
    return {"post": calls["post"], "login": calls["login"]}


_FAIL_NON_AUTH = {"result": "fail", "data": {"reason": "camera not found"}}
_FAIL_AUTH = {"result": "fail", "data": {"reason": "invalid session"}}
_OK = {"result": "success", "data": {"answer": 42}}


@pytest.mark.parametrize("method", ["call", "call_raw"])
def test_success_first_attempt_no_retry(monkeypatch, method) -> None:
    """The hot path: first POST succeeds — exactly one POST, zero logins."""
    c, calls = _client(monkeypatch, [dict(_OK)])
    result = getattr(c, method)("status")
    assert result == (_OK["data"] if method == "call" else _OK)
    assert _counts(calls) == {"post": 1, "login": 0}


@pytest.mark.parametrize("method", ["call", "call_raw"])
def test_lazy_login_on_first_use(monkeypatch, method) -> None:
    """A fresh client (no session) logs in before the first POST."""
    c, calls = _client(monkeypatch, [dict(_OK)])
    c.session = None
    getattr(c, method)("status")
    assert _counts(calls) == {"post": 1, "login": 1}
    assert calls["bodies"][0]["session"] == "sess2"  # not None on the wire


@pytest.mark.parametrize("method", ["call", "call_raw"])
def test_non_auth_fail_raises_immediately_without_relogin(monkeypatch, method) -> None:
    """A cmd-specific failure must NOT trigger a re-login round-trip."""
    c, calls = _client(monkeypatch, [dict(_FAIL_NON_AUTH)])
    with pytest.raises(BiError) as exc_info:
        getattr(c, method)("camconfig", camera="NoSuchCam")
    assert not isinstance(exc_info.value, BiAuthFailed)
    assert "camera not found" in str(exc_info.value)
    assert _counts(calls) == {"post": 1, "login": 0}


def test_access_denied_is_not_auth_class(monkeypatch) -> None:
    """"Access denied" on BI 5.9.9.71 is per-cmd capability gating on a
    VALID session — no retry, and the exception must be exactly ``BiError``
    (not a subclass): ``bi_update_record``'s read→admin graduation guard is
    ``type(e) is BiError`` and dies on ``BiAuthFailed``."""
    fail = {"result": "fail", "data": {"reason": "Access denied"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as exc_info:
        c.call_raw("update")
    assert type(exc_info.value) is BiError
    assert "Access denied" in str(exc_info.value)
    assert _counts(calls) == {"post": 1, "login": 0}


@pytest.mark.parametrize("method", ["call", "call_raw"])
def test_auth_class_fail_relogs_and_retries(monkeypatch, method) -> None:
    """Expired-session shape: fail(auth reason) → re-login → retry succeeds,
    and the retried POST carries the REFRESHED session token."""
    c, calls = _client(monkeypatch, [dict(_FAIL_AUTH), dict(_OK)])
    result = getattr(c, method)("status")
    assert result == (_OK["data"] if method == "call" else _OK)
    assert _counts(calls) == {"post": 2, "login": 1}
    assert calls["bodies"][0]["session"] == "sess1"
    assert calls["bodies"][1]["session"] == "sess2"  # kills the stale-token mutant


@pytest.mark.parametrize(
    "resp",
    [
        {"result": "fail"},                          # no data at all
        {"result": "fail", "data": {}},              # data without reason
        {"result": "fail", "data": "Access denied"}, # data is a bare string
        {"result": "fail", "data": {"reason": 42}},  # non-string reason (guard must not crash)
    ],
)
def test_missing_or_malformed_reason_still_retries(monkeypatch, resp) -> None:
    """Defensive default on the RETRY decision: an unclassifiable fail keeps
    the broad-retry behavior — strict tightening here risks regressions on
    old/odd BI replies."""
    c, calls = _client(monkeypatch, [dict(resp), dict(_OK)])
    assert c.call("status") == _OK["data"]
    assert _counts(calls) == {"post": 2, "login": 1}


def test_auth_fail_twice_raises_biauthfailed(monkeypatch) -> None:
    """Both attempts explicitly auth-class → BiAuthFailed, so admin_call can
    re-tag it as BiAdminAuthFailed and the tool error surfaces as AUTH_FAILED."""
    c, calls = _client(monkeypatch, [dict(_FAIL_AUTH), dict(_FAIL_AUTH)])
    with pytest.raises(BiAuthFailed) as exc_info:
        c.call("status")
    assert "failed after re-login" in str(exc_info.value)
    assert "invalid session" in str(exc_info.value)
    assert _counts(calls) == {"post": 2, "login": 1}


def test_reasonless_double_fail_raises_plain_bierror(monkeypatch) -> None:
    """Unclassifiable on BOTH attempts: the defensive default retries, but the
    post-retry exception must be plain BiError — "check your credentials"
    needs an explicit auth-class reason."""
    c, calls = _client(monkeypatch, [{"result": "fail"}, {"result": "fail"}])
    with pytest.raises(BiError) as exc_info:
        c.call("ptz")
    assert not isinstance(exc_info.value, BiAuthFailed)
    assert _counts(calls) == {"post": 2, "login": 1}


def test_retry_then_non_auth_fail_raises_plain_bierror(monkeypatch) -> None:
    """Auth blip, successful re-login, then a legitimate cmd failure: the
    second reason is what matters — must be BiError, not BiAuthFailed."""
    c, calls = _client(monkeypatch, [dict(_FAIL_AUTH), dict(_FAIL_NON_AUTH)])
    with pytest.raises(BiError) as exc_info:
        c.call("status")
    assert not isinstance(exc_info.value, BiAuthFailed)
    assert "camera not found" in str(exc_info.value)
    assert _counts(calls) == {"post": 2, "login": 1}


def test_login_raising_mid_retry_propagates(monkeypatch) -> None:
    """The real login() can itself raise (rotated creds, lockout). That
    BiAuthFailed must propagate from the retry path, not be swallowed."""
    c, calls = _client(monkeypatch, [dict(_FAIL_AUTH)])

    def failing_login(self):
        calls["login"] += 1
        raise BiAuthFailed("Blue Iris rejected login: bad password")

    monkeypatch.setattr(BiClient, "login", failing_login)
    with pytest.raises(BiAuthFailed, match="rejected login"):
        c.call("status")
    assert _counts(calls) == {"post": 1, "login": 1}


@pytest.mark.parametrize(
    "reason",
    [
        "invalid session",
        "UNAUTHORIZED",
        "authorization required",
        "not logged in",
        "please login first",
        "authentication failure",
        "not authenticated",
        # BI documents no `data.reason` vocabulary, so the real expiry
        # wording is unknown — any reason mentioning a session is auth-class.
        # Before this widened, each of these classified NON-auth (a *decided*
        # verdict that skips the retry entirely, worse than unclassifiable).
        "session expired",
        "expired session",
        "session timeout",
        "bad session",
        "session invalid",
        "no session",
        "session not found",
        "Session Expired",
    ],
)
def test_auth_reason_taxonomy(monkeypatch, reason) -> None:
    """Every reason in the whitelist classifies as auth-class
    (case-insensitive substring match) and takes the retry path."""
    fail = {"result": "fail", "data": {"reason": reason}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call("status") == _OK["data"]
    assert calls["login"] == 1


def test_fail_reason_preserves_dict_without_reason_key(monkeypatch) -> None:
    """A data dict lacking "reason" must not vanish from the message —
    whatever BI included is the only diagnostic there is. (Reason-less ⇒
    unclassifiable ⇒ retried once, hence two scripted fails.) The exception
    must be PLAIN BiError: unclassifiable-within-a-dict is the `return None`
    branch of _classify_fail and must never type as auth-class."""
    fail = {"result": "fail", "data": {"status": "busy", "code": 7}}
    c, calls = _client(monkeypatch, [dict(fail), dict(fail)])
    with pytest.raises(BiError, match="busy") as exc_info:
        c.call("status")
    assert not isinstance(exc_info.value, BiAuthFailed)  # kills None→True mutant
    assert _counts(calls) == {"post": 2, "login": 1}


def test_bare_string_data_double_fail_preserves_message(monkeypatch) -> None:
    """Non-dict data (bare string) is unclassifiable: retried once, then plain
    BiError whose message preserves the string BI sent."""
    fail = {"result": "fail", "data": "Clip not BVR"}
    c, calls = _client(monkeypatch, [dict(fail), dict(fail)])
    with pytest.raises(BiError, match="Clip not BVR") as exc_info:
        c.call("export")
    assert type(exc_info.value) is BiError
    assert _counts(calls) == {"post": 2, "login": 1}


# ---------------------------------------------------------------------------
# Downstream exception contract — the re-tags the typing decision exists for
# ---------------------------------------------------------------------------


def test_admin_call_retags_biauthfailed_as_admin(monkeypatch) -> None:
    """admin_call converts the client's BiAuthFailed into BiAdminAuthFailed
    so the error hint points at BI_ADMIN_USER/BI_ADMIN_PASS."""
    read = BiClient(host="test", port=81, user="u", password="p")
    admin = BiClient(host="test", port=81, user="a", password="p")
    monkeypatch.setattr(
        BiClient, "call",
        lambda self, cmd, **kw: (_ for _ in ()).throw(BiAuthFailed("failed after re-login: invalid session")),
    )
    pair = BiClients(read=read, admin=admin)
    with pytest.raises(BiAdminAuthFailed):
        pair.admin_call("camconfig", camera="SecCam_3")


def test_verify_call_converts_auth_and_propagates_plain_bierror(monkeypatch) -> None:
    """verify_call: BiAuthFailed → BiVerifyAuthBlip (inconclusive verify),
    but a plain BiError — e.g. a capability denial — propagates loudly."""
    fresh = BiClient(host="test", port=81, user="a", password="p")

    monkeypatch.setattr(
        BiClient, "call",
        lambda self, cmd, **kw: (_ for _ in ()).throw(BiAuthFailed("failed after re-login: invalid session")),
    )
    with pytest.raises(BiVerifyAuthBlip):
        BiClients.verify_call(fresh, "camconfig", camera="SecCam_3")

    monkeypatch.setattr(
        BiClient, "call",
        lambda self, cmd, **kw: (_ for _ in ()).throw(BiError("Blue Iris cmd=camconfig failed: Access denied")),
    )
    with pytest.raises(BiError) as exc_info:
        BiClients.verify_call(fresh, "camconfig", camera="SecCam_3")
    assert type(exc_info.value) is BiError


# ---------------------------------------------------------------------------
# `status` as a fail-reason channel — the export queue's terminal states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "non_auth_reason",
    ["access denied", "Access denied", "not authorized", "camera not found"],
)
def test_carveouts_stay_non_auth_after_session_widening(monkeypatch, non_auth_reason) -> None:
    """Widening to a bare "session" substring must not capture the carve-outs.

    `bi_update_record`'s read→admin graduation and `bi_export_clip`'s
    capability handling both key off these surfacing as a BARE ``BiError``
    with no retry. If any starts classifying auth-class, those guards break.
    """
    fail = {"result": "fail", "data": {"reason": non_auth_reason}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as ei:
        c.call("status")
    assert type(ei.value) is BiError, "must stay a bare BiError, not a typed subclass"
    assert _counts(calls) == {"post": 1, "login": 0}, "must not re-login"


def test_export_graduation_status_key_is_decided_non_auth(monkeypatch) -> None:
    """A graduated export poll must NOT burn a re-login round-trip.

    Once an export job finishes it leaves the queue and BI answers
    ``{"result":"fail","data":{"status":"Clip not BVR"}}`` — note the key is
    `status`, not `reason` (AGENTS.md Rule 6.5). Reading only `reason` made
    this unclassifiable-None, which takes the retry path: session cleared,
    full re-login, re-POST, then raise. Terminal polls are the common case,
    so that was a wasted handshake every time.
    """
    fail = {"result": "fail", "data": {"status": "Clip not BVR"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as ei:
        c.call_raw("export", path="@123")
    assert _counts(calls) == {"post": 1, "login": 0}, "no re-login on a terminal poll"
    # tools_mutations.py's graduation guard needs BOTH of these to hold.
    assert type(ei.value) is BiError
    assert "Clip not BVR" in str(ei.value)


def test_unknown_status_stays_unclassifiable_and_retries(monkeypatch) -> None:
    """Only KNOWN terminal statuses short-circuit; anything else keeps the
    historical unclassifiable-None default (retry once).

    A transient status like "busy" could plausibly clear on a retry, so
    widening to every `status` value would trade one wasted round-trip for a
    lost recovery. `_TERMINAL_STATUSES` is the explicit opt-in list.
    """
    fail = {"result": "fail", "data": {"status": "busy", "code": 7}}
    c, calls = _client(monkeypatch, [dict(fail), dict(fail)])
    with pytest.raises(BiError, match="busy"):
        c.call("status")
    assert _counts(calls) == {"post": 2, "login": 1}, "unknown status must still retry once"


def test_reason_key_wins_over_status_when_both_present(monkeypatch) -> None:
    """`reason` is the documented channel; `status` is only consulted when
    `reason` is missing or unusable."""
    fail = {"result": "fail", "data": {"reason": "invalid session", "status": "Clip not BVR"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call("status") == _OK["data"]
    assert calls["login"] == 1, "auth-class reason must win over a terminal status"


# ---------------------------------------------------------------------------
# `_fail_reason` and `_classify_fail` must agree on which key won
# ---------------------------------------------------------------------------


def test_message_and_verdict_agree_on_non_string_reason(monkeypatch) -> None:
    """A junk non-string `reason` beside a terminal `status` must not split them.

    `_fail_reason` picking `reason` while `_classify_fail` picks `status` makes
    the exception message disagree with the verdict that produced it. That
    breaks any caller matching on the message — `bi_export_clip`'s graduation
    guard is exactly such a caller (`"Clip not BVR" in str(e)`), so it would
    re-raise instead of returning its {ok:false} envelope.
    """
    fail = {"result": "fail", "data": {"reason": 42, "status": "Clip not BVR"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as ei:
        c.call_raw("export", path="@1")
    assert "Clip not BVR" in str(ei.value), "message must quote the key the verdict used"
    assert type(ei.value) is BiError
    assert _counts(calls) == {"post": 1, "login": 0}


@pytest.mark.parametrize(
    "status",
    ["Clip not BVR", "CLIP NOT BVR", " Clip not BVR ", "clip not bvr"],
)
def test_terminal_status_matched_case_and_whitespace_insensitively(monkeypatch, status) -> None:
    """BI's exact casing/padding is unknown, so normalise before matching."""
    fail = {"result": "fail", "data": {"status": status}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError):
        c.call_raw("export", path="@1")
    assert _counts(calls) == {"post": 1, "login": 0}, "terminal status must not retry"


def test_auth_reason_beats_terminal_status(monkeypatch) -> None:
    """`reason` is authoritative when usable — an auth-class one still retries."""
    fail = {"result": "fail", "data": {"reason": "session expired", "status": "Clip not BVR"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call("status") == _OK["data"]
    assert calls["login"] == 1


# ---------------------------------------------------------------------------
# A non-string `reason` must NOT be substring-matched for classification
#
# `_select_fail_text` stringified a truthy non-string `reason` and returned it
# to BOTH callers, so `_classify_fail` substring-searched a Python repr for
# auth markers. `{"reason": {"session": "camera offline"}}` renders as
# "{'session': 'camera offline'}", which contains the bare "session" marker:
# an offline camera was typed auth-class, burning a re-login and raising
# BiAuthFailed (BiAdminAuthFailed via admin_call) instead of a plain BiError.
# `_select_fail_text`'s own docstring already promised the opposite.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        {"session": "camera offline"},   # repr contains "session"
        ["login failed for peer"],       # repr contains "login"
        {"authorization": None},         # repr contains "authorization"
    ],
)
def test_non_string_reason_is_not_classified_from_its_repr(monkeypatch, reason) -> None:
    """The repr may contain an auth marker; that is not evidence of auth failure.

    Asserts on the OBSERVABLE consequences (no re-login, plain BiError), not
    on `_classify_fail` directly, so it fails if the classification is right
    but the retry path stops honouring it.
    """
    fail = {"result": "fail", "data": {"reason": reason}}
    # Two identical fails: unclassifiable-None keeps the documented broad
    # retry, so the SECOND reply is what decides the exception type. Auth-class
    # would raise BiAuthFailed there; unclassifiable must raise plain BiError.
    c, calls = _client(monkeypatch, [fail, dict(fail)])
    with pytest.raises(BiError) as ei:
        c.call("status")
    assert type(ei.value) is BiError, "a repr-matched marker must not type as auth"
    assert not isinstance(ei.value, (BiAuthFailed, BiAdminAuthFailed))


@pytest.mark.parametrize(
    "reason",
    [{"session": "camera offline"}, ["login failed for peer"], 42],
)
def test_non_string_reason_still_surfaces_in_the_message(monkeypatch, reason) -> None:
    """Not classifying it must not mean discarding it.

    It is the only diagnostic BI sent; losing it would trade a wrong verdict
    for a blind one. Positive control on the fixtures above: they really do
    carry an auth marker in their repr, so the test above is not vacuous.
    """
    fail = {"result": "fail", "data": {"reason": reason}}
    c, _calls = _client(monkeypatch, [fail, dict(fail)])
    with pytest.raises(BiError) as ei:
        c.call("status")
    # Pin the EXACT text, not a substring. `_fail_reason` has a last-resort
    # `str(data)` fallback that also contains `str(reason)`, so a substring
    # assertion passes even when the reason-specific branch is gone — the
    # whole-dict repr would read "{'reason': 42}" instead of "42".
    from bi_mcp.client import _fail_reason

    assert _fail_reason(fail) == str(reason), "the reason itself must be the message"
    assert str(reason) in str(ei.value), "and it must reach the raised error"


def test_the_repr_fixtures_really_do_contain_auth_markers() -> None:
    """Positive control: without this the test above could pass vacuously.

    If these reprs stopped containing an auth substring, "not classified as
    auth" would be true for the wrong reason and the regression would be
    undetectable.
    """
    from bi_mcp.client import _AUTH_FAIL_SUBSTRINGS

    for reason in ({"session": "camera offline"}, ["login failed for peer"]):
        lowered = str(reason).lower()
        assert any(s in lowered for s in _AUTH_FAIL_SUBSTRINGS)


def test_string_reason_classification_is_unaffected(monkeypatch) -> None:
    """The fix must be surgical: a genuine string reason still classifies.

    Guards against "fix" by simply deleting the fallback's classification
    everywhere, which would also break real auth detection.
    """
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call("status") == _OK["data"]
    assert calls["login"] == 1, "a real string auth reason must still retry"


# ---------------------------------------------------------------------------
# Caller-controlled request arguments must not manufacture the AUTH verdict
#
# `_classify_fail` substring-searches BI's `reason` for auth markers, and BI
# quotes request arguments straight back into that reason ("Not found:
# <camera>"). So every string the caller put in the request body was text the
# caller controlled inside the classifier's own haystack.
#
# Auth-class does not merely mislabel the failure — it AUTHORISES a re-login
# and a re-POST of the same cmd. For a mutating cmd (`trigger`, `update`,
# `ptz`) that is a second execution the caller asked for, reported back as
# BiAuthFailed / BiAdminAuthFailed: "check your credentials" for a fault that
# was nothing of the kind.
#
# The fix redacts caller-supplied body values before matching. It is
# deliberately NOT gated on `_MIN_ELIDABLE_NEEDLE` — that floor protects a
# BI-authored reason from being shredded, whereas here under-redacting is the
# hazard, and `_AUTH_FAIL_SUBSTRINGS` holds needles below it ("login", 5).
# ---------------------------------------------------------------------------


# (payload key, planted value) pairs whose value carries an auth marker BI
# then echoes back inside a genuine, unrelated NON-auth rejection.
PLANTED_AUTH_ARGS = [
    ("camera", "session"),
    ("camera", "Session"),            # casing must not launder it
    ("camera", "login"),              # 5 chars — below the elision floor
    ("camera", "my-session-cam"),     # marker embedded in a plausible name
    ("path", "@session"),
    ("memo", "session expired"),
    ("path", "@not authenticated"),
    ("memo", "unauthorized"),
]


@pytest.mark.parametrize("key,value", PLANTED_AUTH_ARGS)
def test_planted_arg_does_not_force_a_relogin(monkeypatch, key, value) -> None:
    """BI said "not found". The caller's own argument must not turn that into
    an auth-class verdict, a re-login, and a second execution of the cmd.

    Asserts on the OBSERVABLE consequences — POST count, login count, and the
    exception type — rather than on `_classify_fail`, so it fails if the
    verdict is right but the retry path stops honouring it.
    """
    fail = {"result": "fail", "data": {"reason": f"Not found: {value}"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as ei:
        c.call_raw("trigger", **{key: value})
    assert _counts(calls) == {"post": 1, "login": 0}, (
        f"{key}={value!r} steered the classifier: BI's reason was a plain "
        "'Not found', so there must be exactly one POST and no re-login "
        f"(got {calls['post']} POSTs, {calls['login']} logins)"
    )
    assert type(ei.value) is BiError, "must not be typed as an auth failure"
    assert not isinstance(ei.value, (BiAuthFailed, BiAdminAuthFailed))


@pytest.mark.parametrize("key,value", PLANTED_AUTH_ARGS)
def test_planted_arg_control_is_not_vacuous(monkeypatch, key, value) -> None:
    """Positive control for the test above.

    Each planted reason must REALLY be auth-class when the caller's argument
    is not there to be redacted — otherwise "no re-login" would be true for
    the wrong reason (an already-non-auth reason) and the regression would be
    undetectable. Same reason text, same cmd, but the body carries no
    caller-controlled string, so redaction has nothing to remove.
    """
    fail = {"result": "fail", "data": {"reason": f"Not found: {value}"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger") == _OK
    assert calls["login"] == 1, (
        f"reason 'Not found: {value}' must be auth-class with no caller arg "
        "to redact — otherwise the planted-arg test proves nothing"
    )


def test_genuine_auth_failure_still_retries_with_args_present(monkeypatch) -> None:
    """Surgical: redaction must not cost real session recovery. A genuine
    BI-authored auth reason still retries when the body carries ordinary
    arguments that appear nowhere in it."""
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger", camera="SecCam_3", memo="test") == _OK
    assert calls["login"] == 1, "a real auth reason must still trigger recovery"


def test_redaction_ignores_non_caller_body_keys(monkeypatch) -> None:
    """`cmd` and `session` are ours, not the caller's, and must never be
    redacted — the session TOKEN in particular could otherwise blank out
    arbitrary text if it happened to collide."""
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    # `session` is injected into every body by call_raw; "session" is also the
    # auth marker here. Redacting that key would destroy the genuine verdict.
    assert c.call_raw("status") == _OK
    assert calls["login"] == 1


def test_non_string_arg_values_are_skipped(monkeypatch) -> None:
    """Ints/bools/None in the body cannot be echoed as text needles and must
    not raise from inside the classifier."""
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("ptz", camera="SecCam_11", button=5, flag=True, x=None) == _OK
    assert calls["login"] == 1


def test_empty_string_arg_does_not_shred_the_reason(monkeypatch) -> None:
    """An empty/whitespace value must be skipped, not fed to `str.replace`:
    `"".replace` interleaves the replacement between every character and
    would destroy a genuine auth reason (the false-NEGATIVE direction)."""
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger", camera="SecCam_3", memo="", note="   ") == _OK
    assert calls["login"] == 1, "an empty arg must not shred the auth reason"


# ---------------------------------------------------------------------------
# ...and redaction must not SHRED BI's own auth wording (the other direction)
#
# Redacting every caller value "at any length" with a bare `str.replace` is
# unanchored, so an ordinary camera SHORT NAME that happens to be a sub-word
# fragment of BI's genuine auth reason deleted the marker: `_classify_fail`
# then returned False — a *decided* non-auth verdict — and the re-login was
# skipped entirely. The session never recovered and the caller got a plain
# BiError blaming the camera rather than the expired session.
#
# `"invalid session"` is broken by 5 distinct single characters (e i n o s),
# `"unauthorized"` by 11, `"not authenticated"` by 10, and NOTHING in this
# codebase enforces a minimum length on a camera short name — so this needs
# no adversary at all, just a short or unluckily-spelled name.
#
# The fix anchors redaction on word boundaries: BI echoes a caller value as a
# whole token, so every forge above is still removed, while every fragment
# below is now left in place.
# ---------------------------------------------------------------------------


# (camera short name, GENUINE BI-authored auth reason). Each name is a strict
# SUB-WORD fragment of the reason — an ordinary name, not an attack.
SUBWORD_CAMERA_NAMES = [
    ("on", "invalid session"),
    ("io", "invalid session"),
    ("e", "invalid session"),
    ("log", "not logged in"),
    ("auth", "authorization"),
    ("on", "session expired"),
    ("s", "unauthorized"),
    ("i", "not authenticated"),
    ("thor", "unauthorized"),
    ("cat", "not authenticated"),
    # Whole WORDS of the multi-word markers. Word-boundary anchoring does not
    # save these — they match as whole tokens — so they shredded the marker
    # and skipped session recovery exactly like the sub-word names above.
    # Guarded now by the marker-fragment rule, not by the boundary rule.
    ("in", "not logged in"),
    ("not", "not logged in"),
    ("not", "not authenticated"),
    ("logged", "not logged in"),
    ("authenticated", "not authenticated"),
]


@pytest.mark.parametrize("short,reason", SUBWORD_CAMERA_NAMES)
def test_subword_camera_name_does_not_shred_a_genuine_auth_reason(
    monkeypatch, short, reason
) -> None:
    """BI really did say the session is bad. A camera whose name is a
    fragment of that wording must not delete the marker and cancel recovery.

    Asserts on the OBSERVABLE consequence — exactly one re-login and a
    successful retry — rather than on `_classify_fail`, so it fails if the
    verdict is right but the retry path stops honouring it.
    """
    fail = {"result": "fail", "data": {"reason": reason}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger", camera=short) == _OK
    assert _counts(calls) == {"post": 2, "login": 1}, (
        f"camera {short!r} is a sub-word fragment of BI's genuine {reason!r}: "
        "redaction shredded the auth marker, so the expired session was never "
        f"recovered (got {calls['post']} POSTs, {calls['login']} logins)"
    )


@pytest.mark.parametrize("short,reason", SUBWORD_CAMERA_NAMES)
def test_subword_shredding_control_is_not_vacuous(monkeypatch, short, reason) -> None:
    """Positive control: each reason above must be auth-class on its own.

    Without this, "one re-login" could be true because the reason was never
    auth-class in the first place, and the regression would be undetectable.
    """
    fail = {"result": "fail", "data": {"reason": reason}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger") == _OK
    assert calls["login"] == 1, (
        f"reason {reason!r} must be auth-class with no camera arg present — "
        "otherwise the shredding test proves nothing"
    )


def test_subword_fragment_is_not_redacted_but_whole_word_still_is() -> None:
    """Unit-level statement of the boundary rule the two directions share.

    A caller value BI echoes as a whole token is removed; the same value
    occurring only as a sub-word fragment of BI's prose is left alone.
    """
    from bi_mcp.client import _redact_echoed_args

    # whole-word echo -> redacted (forge stays closed)
    assert "session" not in _redact_echoed_args(
        "not found: session", {"cmd": "trigger", "camera": "session"}
    )
    # sub-word fragment of BI's own wording -> untouched (shredding fixed)
    assert "invalid session" == _redact_echoed_args(
        "invalid session", {"cmd": "trigger", "camera": "on"}
    )
    # punctuation-led needle still anchors correctly at the start of a reason
    assert "session" not in _redact_echoed_args(
        "@session not found", {"cmd": "update", "path": "@session"}
    )


def test_no_marker_word_can_shred_its_own_marker() -> None:
    """Structural invariant over the ACTUAL contents of
    `_AUTH_FAIL_SUBSTRINGS`, so adding a marker cannot silently regress.

    The shredding hazard is not the sub-word case specifically — it is any
    caller value that is a piece of BI's own auth wording. Derive the risky
    names from the markers themselves (every whole word, and every marker
    prefix/suffix) and assert redaction leaves the marker intact for each.

    A hand-listed param set cannot express this: the residual regression was
    exactly a marker word ("in", "logged", "not", "authenticated") that no
    one had thought to list. If someone adds e.g. "session expired" or "not
    authorized" to `_AUTH_FAIL_SUBSTRINGS`, this test starts covering its
    words on its own.
    """
    from bi_mcp.client import _AUTH_FAIL_SUBSTRINGS, _redact_echoed_args

    checked = 0
    for marker in _AUTH_FAIL_SUBSTRINGS:
        candidates = set(marker.split())
        # plus every strict prefix/suffix, covering the sub-word direction
        candidates |= {marker[:i] for i in range(1, len(marker))}
        candidates |= {marker[i:] for i in range(1, len(marker))}
        for name in candidates:
            name = name.strip()
            if not name or name == marker:
                continue
            checked += 1
            redacted = _redact_echoed_args(marker, {"cmd": "trigger", "camera": name})
            assert marker in redacted, (
                f"camera {name!r} is a fragment of the genuine auth marker "
                f"{marker!r}; redaction destroyed it ({redacted!r}), so "
                "_classify_fail would return a decided NON-auth verdict and "
                "skip session recovery"
            )
    assert checked > 0, "invariant scanned nothing — _AUTH_FAIL_SUBSTRINGS empty?"


def test_marker_words_still_redacted_when_they_carry_a_whole_marker() -> None:
    """The other direction of the same rule: skipping redaction is scoped to
    STRICT fragments. A caller value that IS a marker, or contains one, is
    still redacted — otherwise the forge reopens.
    """
    from bi_mcp.client import _AUTH_FAIL_SUBSTRINGS, _redact_echoed_args

    for marker in _AUTH_FAIL_SUBSTRINGS:
        for value in (marker, f"cam-{marker}-1"):
            redacted = _redact_echoed_args(
                f"not found: {value}", {"cmd": "trigger", "camera": value}
            )
            assert marker not in redacted, (
                f"caller value {value!r} carries the marker {marker!r} into "
                "BI's plain 'not found' and was not redacted — that forges an "
                "auth verdict and authorises a re-login + second execution"
            )


# ---------------------------------------------------------------------------
# The residual: a caller value equal IN FULL to a marker (characterization)
#
# Word-anchoring and the marker-fragment rule between them cover everything
# except exact equality. When a caller string IS a marker, the genuine reason
# and the forged one are byte-identical, so the two directions below take the
# SAME input to the SAME outcome — by construction, not by omission. The
# ambiguity is resolved toward "no retry authorised": the forge stays closed
# and the cost is one call that surfaces BiError instead of recovering.
#
# `memo` (and `search`) carry this in practice, not camera names: they are
# free-form text with no content validation, so an agent that writes a memo
# or searches alerts for the word "session" lands here without adversary.
# ---------------------------------------------------------------------------


def test_full_marker_caller_value_suppresses_genuine_recovery(monkeypatch) -> None:
    """The documented COST. BI really did say the session is bad, but the
    caller's own memo is that exact wording, so redaction consumes the marker
    and this call raises instead of recovering.

    Loud, accurate and non-destructive — the caller can retry — and the
    opposite direction from a silent second execution of a mutating cmd.
    """
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError):
        c.call_raw("trigger", camera="SecCam_3", memo="session")
    assert _counts(calls) == {"post": 1, "login": 0}, (
        "memo='session' is byte-identical to BI's own marker, so the "
        "ambiguity resolves to 'no retry authorised' — one POST, no re-login "
        f"(got {calls['post']} POSTs, {calls['login']} logins)"
    )


def test_full_marker_caller_value_still_closes_the_forge(monkeypatch) -> None:
    """The BENEFIT, on the same input. BI's reason is a plain 'not found'
    carrying the caller's own memo; that must not manufacture an auth verdict,
    a re-login, and a second execution of a mutating cmd.
    """
    fail = {"result": "fail", "data": {"reason": "Not found: session"}}
    c, calls = _client(monkeypatch, [fail])
    with pytest.raises(BiError) as ei:
        c.call_raw("trigger", camera="SecCam_3", memo="session")
    assert _counts(calls) == {"post": 1, "login": 0}, (
        "memo='session' echoed into a plain 'not found' must not forge an "
        f"auth verdict (got {calls['post']} POSTs, {calls['login']} logins)"
    )
    assert type(ei.value) is BiError
    assert not isinstance(ei.value, (BiAuthFailed, BiAdminAuthFailed))


def test_full_marker_residual_control_is_not_vacuous(monkeypatch) -> None:
    """Positive control for both tests above: change ONLY the memo to an
    ordinary word and the identical genuine reason must recover normally.

    Without this, "no re-login" would also hold if redaction silently stopped
    running, or if 'invalid session' were not auth-class at all.
    """
    fail = {"result": "fail", "data": {"reason": "invalid session"}}
    c, calls = _client(monkeypatch, [fail, dict(_OK)])
    assert c.call_raw("trigger", camera="SecCam_3", memo="car") == _OK
    assert _counts(calls) == {"post": 2, "login": 1}, (
        "with an ordinary memo the same genuine auth reason must recover — "
        "otherwise the residual tests above prove nothing"
    )
