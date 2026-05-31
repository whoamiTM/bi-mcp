"""Tests for bi_get_camera_snapshot.

Covers:
  * Happy path: bytes from the BI HTTP helper round-trip to base64 with
    correct content_type and size.
  * Short-name validation rejects path-traversal or stream-endpoint
    redirection before any HTTP call goes out (e.g. `../mjpg/X/video.mjpg`
    would otherwise normalize past `/image/` and buffer a stream).
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from bi_mcp.errors import BiBadRequest
from bi_mcp.tools.tools_snapshot import _tool_get_camera_snapshot


class _StubClient:
    """Minimal BiClients stand-in: only the surface the tool touches."""

    def __init__(self, *, body: bytes = b"", content_type: str = "image/jpeg") -> None:
        self._body = body
        self._content_type = content_type
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_bytes(self, path: str, **params: Any) -> tuple[bytes, str]:
        self.calls.append((path, params))
        return self._body, self._content_type


def test_snapshot_returns_base64_encoded_body() -> None:
    raw = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"x" * 100 + b"\xff\xd9"
    client = _StubClient(body=raw, content_type="image/jpeg")
    out = _tool_get_camera_snapshot(client, {"camera": "SecCam_3"})  # type: ignore[arg-type]
    assert out["camera"] == "SecCam_3"
    assert out["content_type"] == "image/jpeg"
    assert out["size_bytes"] == len(raw)
    assert base64.b64decode(out["image_base64"]) == raw
    assert client.calls == [("/image/SecCam_3", {})]


def test_snapshot_requires_camera_arg() -> None:
    client = _StubClient()
    with pytest.raises(BiBadRequest):
        _tool_get_camera_snapshot(client, {})  # type: ignore[arg-type]
    assert client.calls == [], "no HTTP call should be issued when arg is missing"


@pytest.mark.parametrize(
    "bad_name",
    [
        "../mjpg/SecCam_3/video.mjpg",
        "SecCam_3/video.mjpg",
        "SecCam 3",
        "SecCam_3?session=stolen",
        "",
    ],
)
def test_snapshot_rejects_non_shortname_input(bad_name: str) -> None:
    client = _StubClient()
    with pytest.raises(BiBadRequest):
        _tool_get_camera_snapshot(client, {"camera": bad_name})  # type: ignore[arg-type]
    assert client.calls == [], (
        "validation must reject before any HTTP call; otherwise httpx normalizes the "
        "path away from /image/ and buffers whatever the redirected endpoint returns"
    )


def test_snapshot_accepts_short_aliases() -> None:
    client = _StubClient(body=b"x")
    _tool_get_camera_snapshot(client, {"short": "SecCam_3"})  # type: ignore[arg-type]
    _tool_get_camera_snapshot(client, {"short_name": "SecCam_3"})  # type: ignore[arg-type]
    assert [c[0] for c in client.calls] == ["/image/SecCam_3", "/image/SecCam_3"]
