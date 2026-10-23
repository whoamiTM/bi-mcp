"""Tests for bi_explain_alert_chain's clip-path-vs-alert-path guard.

Record shapes are verbatim from BI 5.9.9.71 `clipstats` (SecCam_12,
2026-10-19):
  * `.jpg` still alert — the camera doesn't record in the active profile, so
    the alert has no BVR: alertmsec 0, no memo (no AI), filetype "... Alerts".
  * `.bvr` recorded alert — alertmsec > 0, filetype "... Alerts".
  * Parent clip — alertmsec 0, no memo, filetype "... New". Must be rejected.

The `.jpg` still alert and the clip look alike in clipstats, so the guard asks
`alertlist` (a ±1 s window at the record's date) whether the path is a listed
alert. `filetype`'s last token is a renamable folder name, so it must not
decide either way.
"""

from __future__ import annotations

from typing import Any

import pytest

from bi_mcp.errors import BiBadRequest, BiError
from bi_mcp.tools import tools_explain_alert
from bi_mcp.tools.tools_explain_alert import _tool_explain_alert_chain


class _PastGuard(Exception):
    """Raised by the stubbed .reg parse — the first step after the guard."""


class _StubClient:
    def __init__(self, clipstats: dict[str, Any], *, listed: bool) -> None:
        self._clipstats = clipstats
        self._listed = listed
        self.alertlist_calls: list[dict[str, Any]] = []

    def resolve_admin(self) -> object:
        return object()

    def call(self, cmd: str, **payload: Any) -> Any:
        if cmd == "alertlist":
            self.alertlist_calls.append(payload)
            return [{"path": self._clipstats["path"]}] if self._listed else []
        assert cmd == "clipstats"
        return self._clipstats


_BASE = {"camera": "SecCam_12", "date": 1792455630, "camprofile": 1}

_JPG_STILL_ALERT = {
    **_BASE, "path": "@12856749679.jpg", "alertmsec": 0, "triggeroffset": 0,
    "msec": 0, "flags": 196608, "filesize": "Snapshot", "filetype": "jpg H264 Alerts",
}
_BVR_ALERT = {
    **_BASE, "path": "@12854272515.bvr", "alertmsec": 12539, "triggeroffset": 3457,
    "flags": 196608, "filetype": "bvr H264 Alerts",
}
_PARENT_CLIP = {
    **_BASE, "path": "@12854262184.bvr", "alertmsec": 0, "flags": 256,
    "filetype": "bvr H264 New",
}


@pytest.fixture(autouse=True)
def _stop_after_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise _PastGuard

    monkeypatch.setattr(tools_explain_alert.reg_mod, "parse_reg", _raise)


# Users can rename folders, and filetype's last token is the folder name.
_JPG_STILL_ALERT_RENAMED_FOLDER = {**_JPG_STILL_ALERT, "filetype": "jpg H264 Stills"}
_PARENT_CLIP_IN_ALERTS_NAMED_FOLDER = {**_PARENT_CLIP, "filetype": "bvr H264 RecordedAlerts"}


@pytest.mark.parametrize(
    "record",
    [_JPG_STILL_ALERT, _JPG_STILL_ALERT_RENAMED_FOLDER],
    ids=["jpg-still", "jpg-still-renamed-folder"],
)
def test_listed_still_alert_passes_the_guard(record: dict[str, Any]) -> None:
    client = _StubClient(record, listed=True)
    with pytest.raises(_PastGuard):
        _tool_explain_alert_chain(
            client, {"camera": "SecCam_12", "path": record["path"]}  # type: ignore[arg-type]
        )
    assert client.alertlist_calls == [
        {"camera": "SecCam_12", "startdate": 1792455629, "enddate": 1792455631}
    ]


def test_recorded_alert_passes_without_alertlist_lookup() -> None:
    client = _StubClient(_BVR_ALERT, listed=False)
    with pytest.raises(_PastGuard):
        _tool_explain_alert_chain(
            client, {"camera": "SecCam_12", "path": _BVR_ALERT["path"]}  # type: ignore[arg-type]
        )
    assert client.alertlist_calls == []


@pytest.mark.parametrize(
    "record",
    [_PARENT_CLIP, _PARENT_CLIP_IN_ALERTS_NAMED_FOLDER],
    ids=["clip", "clip-in-alerts-named-folder"],
)
def test_unlisted_clip_is_rejected(record: dict[str, Any]) -> None:
    with pytest.raises(BiBadRequest, match="clip-level record"):
        _tool_explain_alert_chain(
            _StubClient(record, listed=False), {"camera": "SecCam_12", "path": record["path"]}  # type: ignore[arg-type]
        )


class _ListedAsClient(_StubClient):
    """alertlist returns a canned payload instead of a list built from the record."""

    def __init__(self, clipstats: dict[str, Any], alertlist_reply: Any) -> None:
        super().__init__(clipstats, listed=False)
        self._reply = alertlist_reply

    def call(self, cmd: str, **payload: Any) -> Any:
        if cmd == "alertlist":
            self.alertlist_calls.append(payload)
            return self._reply
        return super().call(cmd, **payload)


def test_still_alert_passes_with_extensionless_path() -> None:
    # clipstats accepts '@<digits>' and replies with the canonical
    # '@<digits>.jpg' path (live-verified 2026-10-19); alertlist lists the
    # canonical form, so membership must compare against clipstats' path.
    client = _StubClient(_JPG_STILL_ALERT, listed=True)
    with pytest.raises(_PastGuard):
        _tool_explain_alert_chain(
            client, {"camera": "SecCam_12", "path": "@12856749679"}  # type: ignore[arg-type]
        )


def test_missing_date_reports_epoch_error_not_clip_error() -> None:
    record = {k: v for k, v in _JPG_STILL_ALERT.items() if k != "date"}
    with pytest.raises(BiBadRequest, match="alert epoch"):
        _tool_explain_alert_chain(
            _StubClient(record, listed=True), {"camera": "SecCam_12", "path": record["path"]}  # type: ignore[arg-type]
        )


def test_non_list_alertlist_reply_is_surfaced() -> None:
    client = _ListedAsClient(_JPG_STILL_ALERT, {"unexpected": 1})
    with pytest.raises(BiError, match="unexpected payload"):
        _tool_explain_alert_chain(
            client, {"camera": "SecCam_12", "path": _JPG_STILL_ALERT["path"]}  # type: ignore[arg-type]
        )


def test_bvr_alert_with_parent_clip_path_in_clipstats_passes() -> None:
    # For a BVR-backed alert clipstats replies with the PARENT clip's path
    # (live-verified on SecCam_3), so the caller's own path must also count.
    record = {**_JPG_STILL_ALERT, "path": "@12854262184.bvr"}  # parent clip id
    client = _ListedAsClient(record, [{"path": "@12854272515.bvr", "clip": "@12854262184.bvr"}])
    with pytest.raises(_PastGuard):
        _tool_explain_alert_chain(
            client, {"camera": "SecCam_12", "path": "@12854272515.bvr"}  # type: ignore[arg-type]
        )
