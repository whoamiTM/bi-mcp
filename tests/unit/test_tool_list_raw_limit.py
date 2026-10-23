"""raw=true list tools honour an explicit ``limit``.

bi_list_alerts / bi_list_clips / bi_list_log applied ``limit`` only inside the
shaper, so ``raw=true, limit=15`` returned BI's whole reply (409 KB for a 24 h
alert window). The raw path now truncates the list when ``limit`` is passed;
entries stay verbatim, and raw without ``limit`` is still the full payload.
"""

from __future__ import annotations

from typing import Any

from bi_mcp.tools.tools_alerts import _tool_list_alerts
from bi_mcp.tools.tools_clips import _tool_list_clips
from bi_mcp.tools.tools_log import _tool_list_log

_ROWS = [{"date": 1792467301 - i, "msg": f"m{i}", "level": 0, "obj": "App"} for i in range(30)]


class _StubClient:
    def call(self, cmd: str, **payload: Any) -> Any:
        return list(_ROWS)

    admin_call = call

    def resolve_admin(self) -> Any:
        return object()


def test_alerts_raw_honours_limit() -> None:
    out = _tool_list_alerts(_StubClient(), {"camera": "Index", "raw": True, "limit": 15})  # type: ignore[arg-type]
    assert out == _ROWS[:15]


def test_clips_raw_honours_limit() -> None:
    out = _tool_list_clips(_StubClient(), {"camera": "Index", "raw": True, "limit": 5})  # type: ignore[arg-type]
    assert out == _ROWS[:5]


def test_log_raw_honours_limit() -> None:
    out = _tool_list_log(_StubClient(), {"raw": True, "limit": 3})  # type: ignore[arg-type]
    assert out == _ROWS[:3]


def test_raw_without_limit_is_full_payload() -> None:
    out = _tool_list_alerts(_StubClient(), {"camera": "Index", "raw": True})  # type: ignore[arg-type]
    assert out == _ROWS


# limit=null means "not given" (default cap shaped, full payload raw); a
# non-integer limit is a BiBadRequest, not a bare TypeError/ValueError.

import pytest  # noqa: E402

from bi_mcp.errors import BiBadRequest  # noqa: E402


@pytest.mark.parametrize("tool,args", [
    (_tool_list_alerts, {"camera": "Index"}),
    (_tool_list_clips, {"camera": "Index"}),
    (_tool_list_log, {}),
])
def test_null_limit_means_default(tool: Any, args: dict) -> None:
    tool(_StubClient(), {**args, "limit": None})  # shaped path: no TypeError
    raw = tool(_StubClient(), {**args, "limit": None, "raw": True})
    assert raw == _ROWS


@pytest.mark.parametrize("tool,args", [
    (_tool_list_alerts, {"camera": "Index"}),
    (_tool_list_clips, {"camera": "Index"}),
    (_tool_list_log, {}),
])
def test_non_integer_limit_is_bad_request(tool: Any, args: dict) -> None:
    with pytest.raises(BiBadRequest):
        tool(_StubClient(), {**args, "limit": "lots"})
