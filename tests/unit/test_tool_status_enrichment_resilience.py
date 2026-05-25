"""Regression test for bi_get_status enrichment resilience (Codex finding 2).

A successful `status` call must not be discarded when the lazy `login()`
probe for `profile_name` resolution fails (auth blip, session glitch,
admin creds rotated mid-process). Enrichment is best-effort: the shaped
status comes back, just without the `profile_name` field.
"""

from __future__ import annotations

from typing import Any

import pytest

from bi_mcp.tools.tools_status import _tool_get_status


class _StubClient:
    """Minimal BiClients stand-in: just the surface _tool_get_status touches."""

    def __init__(
        self,
        *,
        status_payload: dict[str, Any],
        login_data: dict[str, Any] | None = None,
        login_raises: Exception | None = None,
    ) -> None:
        self._status_payload = status_payload
        self.login_data = login_data
        self._login_raises = login_raises
        self.login_calls = 0

    def call(self, cmd: str, **_: Any) -> dict[str, Any]:
        assert cmd == "status"
        return self._status_payload

    def login(self) -> dict[str, Any]:
        self.login_calls += 1
        if self._login_raises is not None:
            raise self._login_raises
        # If login is allowed to succeed, populate login_data the way the real
        # client does on a successful handshake.
        self.login_data = {"profiles": ["Inactive", "Active/Day", "Armed/Away"]}
        return self.login_data


def test_status_returns_profile_name_when_login_succeeds() -> None:
    client = _StubClient(status_payload={"profile": 1, "cpu": 30})
    out = _tool_get_status(client, {})  # type: ignore[arg-type]
    assert out["profile"] == 1
    assert out["profile_name"] == "Active/Day"
    assert client.login_calls == 1


def test_status_uses_cached_login_data_without_calling_login() -> None:
    cached = {"profiles": ["Inactive", "Active/Day"]}
    client = _StubClient(status_payload={"profile": 1}, login_data=cached)
    out = _tool_get_status(client, {})  # type: ignore[arg-type]
    assert out["profile_name"] == "Active/Day"
    assert client.login_calls == 0, "should not re-login when login_data is cached"


def test_status_survives_login_failure_without_profile_name() -> None:
    client = _StubClient(
        status_payload={"profile": 1, "cpu": 26},
        login_raises=RuntimeError("auth blip"),
    )
    out = _tool_get_status(client, {})  # type: ignore[arg-type]
    # Status data must survive intact.
    assert out["profile"] == 1
    assert out["cpu"] == 26
    # Enrichment failed → profile_name omitted, not the whole call.
    assert "profile_name" not in out


def test_status_survives_login_data_missing_profiles_key() -> None:
    client = _StubClient(
        status_payload={"profile": 1},
        login_data={"system name": "Blue Iris"},  # no `profiles` key
    )
    out = _tool_get_status(client, {})  # type: ignore[arg-type]
    assert out["profile"] == 1
    assert "profile_name" not in out


def test_raw_passthrough_skips_enrichment_entirely() -> None:
    client = _StubClient(
        status_payload={"profile": 1, "raw_field": "x"},
        login_raises=RuntimeError("login must not be called"),
    )
    out = _tool_get_status(client, {"raw": True})  # type: ignore[arg-type]
    assert out == {"profile": 1, "raw_field": "x"}
    assert client.login_calls == 0
