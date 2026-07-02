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
