"""bi_set_profile / bi_set_camera verify-after-write contract (audit D1-D4, D6).

Once BI accepted a write, verify trouble must surface as ``verified=False`` +
``verify_error_kind`` with the revert data intact — never as an exception.
Stub clients only; nothing here talks to Blue Iris.
"""

from __future__ import annotations

import contextlib

import pytest

from bi_mcp.errors import (
    BiAdminAuthFailed,
    BiBadRequest,
    BiError,
    BiNotFound,
    BiUnreachable,
    BiVerifyUnreachable,
)
from bi_mcp.tools import tools_mutations as tm


@pytest.fixture(autouse=True)
def _mutations_on(monkeypatch):
    monkeypatch.setenv("BI_MCP_ALLOW_MUTATIONS", "1")
    monkeypatch.setattr("time.sleep", lambda _s: None)


class _Stub:
    """Minimal BiClients stand-in: `call` serves the pre-read, `verify_call`
    serves post-reads (or raises), writes are recorded."""

    def __init__(self, pre=None, post=None, admin_call_exc=None):
        self.pre = pre
        self.post = post
        self.admin_call_exc = admin_call_exc
        self.writes: list[tuple[str, dict]] = []
        self.login_data = {"profiles": ["Inactive", "Day", "Night"]}

    def resolve_admin(self):
        return object()

    def call(self, cmd, **kw):
        return self.pre

    def admin_call(self, cmd, **kw):
        if self.admin_call_exc is not None:
            raise self.admin_call_exc
        return self.pre

    def admin_call_raw(self, cmd, **kw):
        self.writes.append((cmd, kw))
        return {"result": "success", "data": {}}

    @contextlib.contextmanager
    def fresh_admin_session(self):
        yield None

    def verify_call(self, _fresh, cmd, **kw):
        if isinstance(self.post, Exception):
            raise self.post
        return self.post


# --- D1: post-read failure after an accepted write ------------------------

def test_set_profile_verify_unreachable_keeps_ok_and_revert_data() -> None:
    st = _Stub(pre={"profile": 1, "lock": 0}, post=BiVerifyUnreachable("timeout"))
    out = tm._tool_set_profile(st, {"profile": 2})
    assert st.writes == [("status", {"profile": 2})]
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_unreachable"
    assert out["previous_profile"] == 1
    assert out["previous_lock"] == 0


def test_set_profile_mismatch_reported_not_raised() -> None:
    st = _Stub(pre={"profile": 1, "lock": 0}, post={"profile": 1, "lock": 1})
    out = tm._tool_set_profile(st, {"profile": 2})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"
    assert out["previous_profile"] == 1


def test_set_profile_clean_verify() -> None:
    st = _Stub(pre={"profile": 1, "lock": "0"}, post={"profile": 2, "lock": "0"})
    out = tm._tool_set_profile(st, {"profile": 2})
    assert out["verified"] is True
    assert "verify_error_kind" not in out
    assert out["profile"] == 2


# --- D2: -1 toggle --------------------------------------------------------

def test_set_profile_toggle_stale_lock_is_unverified_not_failed() -> None:
    st = _Stub(pre={"profile": 1, "lock": 0}, post={"profile": 1, "lock": 0})
    out = tm._tool_set_profile(st, {"profile": -1})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"
    assert out["previous_lock"] == 0


def test_set_profile_toggle_refused_before_write_without_pre_lock() -> None:
    st = _Stub(pre={"profile": 1}, post={"profile": 1, "lock": 1})
    with pytest.raises(BiError, match="Nothing was sent"):
        tm._tool_set_profile(st, {"profile": -1})
    assert st.writes == []


# --- D4: isdigit() passes but int() fails ---------------------------------

@pytest.mark.parametrize("bad", ["--1", "²"])
def test_set_profile_bad_numeric_string_is_bad_request(bad) -> None:
    with pytest.raises(BiBadRequest):
        tm._tool_set_profile(_Stub(), {"profile": bad})


# --- D3: typed pre-read errors keep their type ----------------------------

@pytest.mark.parametrize("exc", [BiAdminAuthFailed("bad pass"), BiUnreachable("down")])
def test_set_camera_preread_typed_error_propagates(exc) -> None:
    st = _Stub(admin_call_exc=exc)
    with pytest.raises(type(exc)):
        tm._tool_set_camera(st, {"camera": "SecCam_1", "audio": True})
    assert st.writes == []


# --- D6: sticky lock=2 after lock=false ------------------------------------

def test_set_camera_lock_false_sticky_two_reported_not_raised() -> None:
    st = _Stub(pre={"profile": 5, "lock": 1}, post={"profile": 5, "lock": 2})
    out = tm._tool_set_camera(st, {"camera": "X", "profile": 5, "lock": False})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"
    assert out["previous"] == {"profile": 5, "lock": 1}


# --- RD1: any verify-side BiError after an accepted write ------------------

@pytest.mark.parametrize(
    "exc",
    [BiError("Blue Iris returned HTTP 503: busy"), BiError("Blue Iris returned non-JSON: <html>"),
     BiNotFound("Blue Iris HTTP 404 on /json")],
)
def test_set_profile_structural_verify_error_keeps_revert_data(exc) -> None:
    st = _Stub(pre={"profile": 1, "lock": 0}, post=exc)
    out = tm._tool_set_profile(st, {"profile": 2})
    assert st.writes == [("status", {"profile": 2})]
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_error"
    assert str(exc) in out["verify_error"]
    assert out["previous_profile"] == 1
    assert out["previous_lock"] == 0


def test_set_camera_structural_verify_error_keeps_previous() -> None:
    st = _Stub(pre={"audio": False}, post=BiError("Blue Iris returned HTTP 503: busy"))
    out = tm._tool_set_camera(st, {"camera": "X", "audio": True})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_error"
    assert out["previous"] is False


# --- Fresh review F2: genuine mismatches after an accepted write ----------

@pytest.mark.parametrize(
    "args, pre, post",
    [
        ({"audio": True}, {"audio": False}, {"audio": False}),
        ({"profile": 5}, {"profile": 3, "lock": 0}, {"profile": 3, "lock": 0}),
        ({"profile": -1}, {"profile": 3, "lock": 0}, {"profile": 1, "lock": 0}),
        ({"profile": 5, "lock": True}, {"profile": 3, "lock": 0}, {"profile": 5, "lock": 0}),
    ],
)
def test_set_camera_camconfig_mismatch_reported_not_raised(args, pre, post) -> None:
    st = _Stub(pre=pre, post=post)
    out = tm._tool_set_camera(st, {"camera": "X", **args})
    assert len(st.writes) == 1
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"
    assert out["previous"] is not None


def test_set_camera_camlist_mismatch_reported_not_raised() -> None:
    row = {"optionValue": "X", "optionDisplay": "Old", "hidden": False, "isManRec": False}
    st = _Stub(pre=[row], post=[row])
    out = tm._tool_set_camera(st, {"camera": "X", "rename": "New"})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"
    assert out["previous"] == "Old"


def test_set_camera_reset_without_dip_reported_not_raised() -> None:
    row = {"optionValue": "X", "isOnline": True}
    st = _Stub(pre=[row], post=[row])
    out = tm._tool_set_camera(st, {"camera": "X", "reset": True})
    assert out["ok"] is True
    assert out["verified"] is False
    assert out["verify_error_kind"] == "verify_mismatch"


@pytest.mark.parametrize(
    "new_name",
    ["Other", "other ", "Y", "y"],
    ids=["display", "display-case-space", "short", "short-case"],
)
def test_set_camera_rename_collision_refused_before_write(new_name) -> None:
    # Live 2026-10-22: BI accepted a rename onto another camera's name and
    # that camera's registry key was wiped. Refuse before anything is sent.
    rows = [
        {"optionValue": "X", "optionDisplay": "Mine"},
        {"optionValue": "Y", "optionDisplay": "Other"},
    ]
    st = _Stub(pre=rows, post=rows)
    with pytest.raises(BiBadRequest, match="collides"):
        tm._tool_set_camera(st, {"camera": "X", "rename": new_name})
    assert st.writes == []


def test_set_camera_rename_to_own_short_name_allowed() -> None:
    rows = [
        {"optionValue": "X", "optionDisplay": "Mine"},
        {"optionValue": "Y", "optionDisplay": "Other"},
    ]
    post = [{"optionValue": "X", "optionDisplay": "X"}, rows[1]]
    st = _Stub(pre=rows, post=post)
    out = tm._tool_set_camera(st, {"camera": "X", "rename": "X"})
    assert len(st.writes) == 1
    assert out["ok"] is True


@pytest.mark.parametrize("pre", [None, {"bad": 1}], ids=["none", "dict"])
def test_set_camera_rename_refused_when_camlist_unreadable(pre) -> None:
    # The collision check needs the camera list; without it, refuse (fail closed).
    st = _Stub(pre=pre, post=pre)
    with pytest.raises(BiBadRequest):
        tm._tool_set_camera(st, {"camera": "X", "rename": "Other"})
    assert st.writes == []


def test_set_camera_rename_blank_refused() -> None:
    rows = [{"optionValue": "X", "optionDisplay": "Mine"}]
    st = _Stub(pre=rows, post=rows)
    with pytest.raises(BiBadRequest):
        tm._tool_set_camera(st, {"camera": "X", "rename": "   "})
    assert st.writes == []
