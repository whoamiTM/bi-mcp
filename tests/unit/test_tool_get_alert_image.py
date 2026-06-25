"""Tests for bi_get_alert_image.

Covers:
  * Happy path: camera resolves via alertlist to the most-recent alert; its
    @record is fetched from /alerts/ and round-trips to base64 with record,
    time, and memo echoed back.
  * Defensive ordering: when the alertlist comes back NOT newest-first, the
    tool still picks the latest by timestamp (it doesn't trust array position).
  * `.bvr` suffix and `@` prefix on the alert path are stripped before the
    /alerts/@<record> fetch.
  * `at` time arg is parsed and forwarded as `enddate` to alertlist.
  * `markup` toggles the v=2 query param.
  * No alerts -> BiNotFound, no image fetch issued.
  * camera validation rejects path-traversal shapes before any HTTP call.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from bi_mcp.errors import BiBadRequest, BiError, BiNotFound
from bi_mcp.tools.tools_alert_image import _tool_get_alert_image


_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"x" * 50 + b"\xff\xd9"


class _StubClient:
    """Minimal BiClients stand-in: alertlist via call(), image via get_bytes()."""

    def __init__(self, *, alerts: list[dict[str, Any]], body: bytes = _JPEG) -> None:
        self._alerts = alerts
        self._body = body
        self.cmd_calls: list[tuple[str, dict[str, Any]]] = []
        self.byte_calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, cmd: str, **payload: Any) -> Any:
        self.cmd_calls.append((cmd, payload))
        return self._alerts

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        self.byte_calls.append((path, params))
        return self._body, "image/jpeg"


def test_resolves_camera_to_most_recent_alert_image() -> None:
    client = _StubClient(
        alerts=[
            {"path": "@6461778619.bvr", "date": "2026-06-25T21:16:51+00:00", "memo": "person"},
        ]
    )
    out = _tool_get_alert_image(client, {"camera": "SecCam_11AI"})  # type: ignore[arg-type]
    assert out["camera"] == "SecCam_11AI"
    assert out["alert_record"] == "@6461778619"
    assert out["alert_time"] == "2026-06-25T21:16:51+00:00"
    assert out["memo"] == "person"
    assert out["content_type"] == "image/jpeg"
    assert base64.b64decode(out["image_base64"]) == _JPEG
    # MCP image marker the dispatcher splits into an image block.
    assert out["_mcp_image"] == {
        "data": out["image_base64"],
        "mimeType": "image/jpeg",
    }
    # alertlist resolution, then /alerts/@record (suffix + prefix stripped).
    assert client.cmd_calls == [("alertlist", {"camera": "SecCam_11AI"})]
    assert client.byte_calls == [("/alerts/@6461778619", {})]


def test_picks_latest_by_timestamp_not_array_position() -> None:
    # alertlist returns oldest-first here; tool must still pick the latest.
    client = _StubClient(
        alerts=[
            {"path": "@100.bvr", "date": "2026-06-25T21:13:00+00:00", "memo": "old"},
            {"path": "@200.bvr", "date": "2026-06-25T21:16:00+00:00", "memo": "new"},
        ]
    )
    out = _tool_get_alert_image(client, {"camera": "SecCam_4"})  # type: ignore[arg-type]
    assert out["alert_record"] == "@200"
    assert out["memo"] == "new"
    assert client.byte_calls == [("/alerts/@200", {})]


def test_at_arg_forwarded_as_enddate() -> None:
    client = _StubClient(
        alerts=[{"path": "@6461704713.bvr", "date": "2026-06-25T21:14:17+00:00", "memo": "car"}]
    )
    _tool_get_alert_image(
        client, {"camera": "SecCam_4", "at": "2026-06-25T21:15:00Z"}  # type: ignore[arg-type]
    )
    cmd, payload = client.cmd_calls[0]
    assert cmd == "alertlist"
    assert payload["camera"] == "SecCam_4"
    # `at` is parsed through the same parse_since the alerts tool uses, then
    # forwarded as `enddate` (the at/before-time resolution).
    from bi_mcp.utils.time import parse_since

    assert payload["enddate"] == parse_since("2026-06-25T21:15:00Z")


def test_markup_toggles_v2_param() -> None:
    client = _StubClient(
        alerts=[{"path": "@42.bvr", "date": "2026-06-25T21:00:00+00:00", "memo": "person"}]
    )
    out = _tool_get_alert_image(client, {"camera": "SecCam_4", "markup": True})  # type: ignore[arg-type]
    assert out["markup"] is True
    assert client.byte_calls == [("/alerts/@42", {"v": 2})]


def test_no_alerts_raises_notfound_without_fetching() -> None:
    client = _StubClient(alerts=[])
    with pytest.raises(BiNotFound):
        _tool_get_alert_image(client, {"camera": "SecCam_4"})  # type: ignore[arg-type]
    assert client.byte_calls == [], "must not fetch an image when no alert resolved"


def test_sentinel_raw_shape_surfaces_upstream_error() -> None:
    # shape_alerts wraps a non-list BI response (error/login challenge) as a
    # single [{"raw": <payload>}] item. The tool must surface that, not fall
    # through to a misleading "no path record" BiNotFound.
    client = _StubClient(alerts=[{"raw": {"result": "fail", "data": "login"}}])
    with pytest.raises(BiError) as exc:
        _tool_get_alert_image(client, {"camera": "SecCam_4"})  # type: ignore[arg-type]
    assert "unexpected payload" in str(exc.value)
    # BiNotFound is itself a BiError subclass; assert it's the base type, not NotFound.
    assert not isinstance(exc.value, (BiNotFound, BiBadRequest))
    assert client.byte_calls == [], "must not fetch an image on a malformed payload"


def test_requires_camera_arg() -> None:
    client = _StubClient(alerts=[])
    with pytest.raises(BiBadRequest):
        _tool_get_alert_image(client, {})  # type: ignore[arg-type]
    assert client.cmd_calls == []


@pytest.mark.parametrize(
    "bad_name",
    ["../mjpg/SecCam_3/video.mjpg", "SecCam_3/x", "SecCam 3", "SecCam_3?session=stolen", ""],
)
def test_rejects_non_shortname_camera(bad_name: str) -> None:
    client = _StubClient(alerts=[])
    with pytest.raises(BiBadRequest):
        _tool_get_alert_image(client, {"camera": bad_name})  # type: ignore[arg-type]
    assert client.cmd_calls == [], "validation must reject before any alertlist call"


@pytest.mark.parametrize("index_name", ["Index", "index", "INDEX"])
def test_rejects_index_all_cameras_selector(index_name: str) -> None:
    # 'Index' passes the short-name regex but resolves a cross-camera alert;
    # an image must come from one named camera, so reject it before querying BI.
    client = _StubClient(alerts=[{"path": "@1.bvr", "date": "2026-06-25T21:00:00+00:00"}])
    with pytest.raises(BiBadRequest):
        _tool_get_alert_image(client, {"camera": index_name})  # type: ignore[arg-type]
    assert client.cmd_calls == [], "must reject Index before any alertlist call"
