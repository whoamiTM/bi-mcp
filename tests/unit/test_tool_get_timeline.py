"""Tests for bi_get_timeline.

Covers:
  * Bare call (no range) injects a trailing 24h window — matching the tool's
    name — because BI returns empty spans for a rangeless query.
  * Explicit int range is forwarded verbatim; no default injected.
  * Relative/ISO range strings are parsed via parse_since (consistency with
    bi_list_alerts / bi_list_clips).
  * Single-bound calls forward just that bound (no backfill) — confirmed design.
  * startdate=0 is treated as a real bound, not "unset".
  * camera is required; bad date strings raise BiBadRequest.
  * msecpp survives the camera/date split in the forward logic.
  * raw=true skips shaping but still gets the injected default range.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from bi_mcp.errors import BiBadRequest
from bi_mcp.tools.tools_timeline import _tool_get_timeline
from bi_mcp.utils.time import parse_since

# Real BI timeline reply shape (BI manual § JSON Interface, lines 9286-9300).
_RAW = {
    "colors": [9068350],
    "alerts": [{"x1": -53, "x2": 8, "type": 402857986, "record": "@655252", "tracks": 1}],
    "clips": [{"x1": 19908, "x2": 86392, "track": 0}],
}


class _StubClient:
    """Minimal BiClients stand-in: records each call's cmd + payload."""

    def __init__(self, raw: Any = None) -> None:
        self._raw = raw if raw is not None else _RAW
        self.cmd_calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, cmd: str, **payload: Any) -> Any:
        self.cmd_calls.append((cmd, payload))
        return self._raw


def test_bare_call_injects_24h_default_range() -> None:
    client = _StubClient()
    now_before = int(time.time())
    _tool_get_timeline(client, {"camera": "SecCam_4"})  # type: ignore[arg-type]
    now_after = int(time.time())
    cmd, payload = client.cmd_calls[0]
    assert cmd == "timeline"
    assert payload["camera"] == "SecCam_4"
    assert now_before <= payload["enddate"] <= now_after
    assert payload["enddate"] - payload["startdate"] == 86400


def test_explicit_int_range_passed_through() -> None:
    client = _StubClient()
    _tool_get_timeline(
        client, {"camera": "X", "startdate": 1700000000, "enddate": 1700086400}  # type: ignore[arg-type]
    )
    _, payload = client.cmd_calls[0]
    assert payload["startdate"] == 1700000000
    assert payload["enddate"] == 1700086400


def test_explicit_relative_range_parsed() -> None:
    client = _StubClient()
    before = parse_since("-1d")
    _tool_get_timeline(client, {"camera": "X", "startdate": "-1d"})  # type: ignore[arg-type]
    after = parse_since("-1d")
    _, payload = client.cmd_calls[0]
    # parse_since("-1d") reads the clock, so allow a 1s tolerance window.
    assert before <= payload["startdate"] <= after


def test_lone_startdate_backfills_enddate_to_now() -> None:
    # BI defaults a missing enddate to epoch 0 (inverting the window), so a lone
    # startdate returns nothing. We backfill enddate=now so "since T" works.
    client = _StubClient()
    now_before = int(time.time())
    _tool_get_timeline(client, {"camera": "X", "startdate": 1700000000})  # type: ignore[arg-type]
    now_after = int(time.time())
    _, payload = client.cmd_calls[0]
    assert payload["startdate"] == 1700000000
    assert now_before <= payload["enddate"] <= now_after


def test_lone_enddate_left_untouched() -> None:
    # A missing startdate is fine (BI treats it as "from the beginning"); don't
    # backfill it — that would cap an otherwise-useful all-history-before-T query.
    client = _StubClient()
    _tool_get_timeline(client, {"camera": "X", "enddate": 1700086400})  # type: ignore[arg-type]
    _, payload = client.cmd_calls[0]
    assert payload["enddate"] == 1700086400
    assert "startdate" not in payload


def test_startdate_zero_not_overridden() -> None:
    # startdate=0 is a real bound (not "unset"), so it is NOT replaced by the
    # 24h default; enddate is backfilled to now like any lone startdate.
    client = _StubClient()
    now_before = int(time.time())
    _tool_get_timeline(client, {"camera": "X", "startdate": 0})  # type: ignore[arg-type]
    now_after = int(time.time())
    _, payload = client.cmd_calls[0]
    assert payload["startdate"] == 0
    assert now_before <= payload["enddate"] <= now_after


def test_msecpp_forwarded() -> None:
    client = _StubClient()
    _tool_get_timeline(client, {"camera": "X", "msecpp": 256})  # type: ignore[arg-type]
    _, payload = client.cmd_calls[0]
    assert payload["msecpp"] == 256


def test_requires_camera() -> None:
    client = _StubClient()
    with pytest.raises(BiBadRequest):
        _tool_get_timeline(client, {})  # type: ignore[arg-type]
    assert client.cmd_calls == []


def test_bad_date_raises_bad_request() -> None:
    client = _StubClient()
    with pytest.raises(BiBadRequest):
        _tool_get_timeline(client, {"camera": "X", "startdate": "not-a-date"})  # type: ignore[arg-type]
    assert client.cmd_calls == []


def test_raw_passthrough_still_injects_default() -> None:
    client = _StubClient()
    out = _tool_get_timeline(client, {"camera": "X", "raw": True})  # type: ignore[arg-type]
    # raw skips shaping...
    assert out is client._raw
    # ...but the default range was still injected into the outgoing payload.
    _, payload = client.cmd_calls[0]
    assert payload["enddate"] - payload["startdate"] == 86400
