"""Tests for the MCP SDK 1.x/2.x compatibility shim in server.py.

SDK 2.0 (2026-07-28) removed the low-level ``@server.list_tools()`` /
``@server.call_tool()`` decorators in favour of ``on_list_tools=`` /
``on_call_tool=`` constructor kwargs. `server.py` supports both by probing
for the decorator at runtime.

`pyproject.toml` now pins ``mcp>=2.1.1,<3``, so 2.x is what a fresh install
gets and the 1.x branch is the one the pin no longer reaches. It is retained
for environments that already have a 1.x SDK. These tests fake each generation
rather than requiring both SDKs installed, so whichever branch the local SDK
cannot exercise is still covered wherever the suite runs.

What these do NOT cover: a real 2.x wire handshake. That was verified
manually against mcp 2.1.1 + a live Blue Iris (initialize / tools/list /
tools/call / unknown-tool) before the pin was lifted.
"""

from __future__ import annotations

import builtins
import asyncio
import json
from typing import Any
from unittest import mock

import pytest

from bi_mcp import server as srv


class _FakeServer1x:
    """Stand-in for the 1.x low-level Server: decorator registration."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs
        self.registered: dict[str, Any] = {}

    def list_tools(self):
        def deco(fn):
            self.registered["list_tools"] = fn
            return fn
        return deco

    def call_tool(self):
        def deco(fn):
            self.registered["call_tool"] = fn
            return fn
        return deco


class _FakeServer2x:
    """Stand-in for the 2.x Server: handlers arrive as constructor kwargs."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs


def _sample_tool_list() -> list[Any]:
    from mcp.types import Tool
    return [Tool(name="bi_probe", description="d", inputSchema={"type": "object"})]


async def _sample_dispatch(
    name: str, arguments: dict[str, Any] | None, errored: list[bool] | None = None
) -> list[Any]:
    from mcp.types import TextContent
    return [TextContent(type="text", text=f"called:{name}:{sorted((arguments or {}).items())}")]


async def _failing_dispatch(
    name: str, arguments: dict[str, Any] | None, errored: list[bool] | None = None
) -> list[Any]:
    """Stand-in for a tool failure: content block + the error flag set."""
    from mcp.types import TextContent
    srv._mark_error(errored)
    return [TextContent(type="text", text='{"error": "boom"}')]


# ---------------------------------------------------------------------------
# Generation probe
# ---------------------------------------------------------------------------


def test_sdk_generation_reports_1x_when_decorator_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "Server", _FakeServer1x)
    assert srv._sdk_generation() == "1x"


def test_sdk_generation_reports_2x_when_decorator_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    assert srv._sdk_generation() == "2x"


def test_sdk_generation_matches_installed_sdk() -> None:
    """Sanity-check the probe against whatever SDK is actually installed.

    Guards against the probe silently inverting: whichever generation is
    installed, the decorator's presence must agree with the verdict.
    """
    from mcp.server import Server as RealServer
    expected = "1x" if hasattr(RealServer, "list_tools") else "2x"
    assert srv._sdk_generation() == expected


# ---------------------------------------------------------------------------
# 1.x path: decorators, no constructor kwargs
# ---------------------------------------------------------------------------


def test_1x_builds_bare_server_and_registers_via_decorators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "Server", _FakeServer1x)
    s = srv._build_server(_sample_tool_list, _sample_dispatch)

    assert s.kwargs == {}, "1.x Server must not receive on_* handler kwargs"

    srv._register_handlers(s, _sample_tool_list, _sample_dispatch)
    assert set(s.registered) == {"list_tools", "call_tool"}

    tools = asyncio.run(s.registered["list_tools"]())
    assert [t.name for t in tools] == ["bi_probe"]

    blocks = asyncio.run(s.registered["call_tool"]("bi_probe", {"a": 1}))
    assert "called:bi_probe" in blocks[0].text


# ---------------------------------------------------------------------------
# 2.x path: constructor kwargs returning Result models
# ---------------------------------------------------------------------------


def test_2x_passes_handlers_as_constructor_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    s = srv._build_server(_sample_tool_list, _sample_dispatch)
    # `version` joined the handlers: 2.x defaults it to "" and, unlike 1.x,
    # never backfills, so it must be passed explicitly.
    assert set(s.kwargs) == {"on_list_tools", "on_call_tool", "version"}


def test_2x_register_handlers_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handlers are already wired at construction; re-registering must not fail."""
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    s = srv._build_server(_sample_tool_list, _sample_dispatch)
    srv._register_handlers(s, _sample_tool_list, _sample_dispatch)  # must not raise
    assert not hasattr(s, "registered")


@pytest.mark.skipif(
    not hasattr(pytest.importorskip("mcp.types"), "ListToolsResult"),
    reason="SDK too old to expose ListToolsResult",
)
def test_2x_handlers_wrap_returns_in_result_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2.x handlers must return ListToolsResult / CallToolResult.

    This is the shape difference that made the un-ported server crash: 1.x
    handlers returned bare lists, 2.x wants Result models.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    s = srv._build_server(_sample_tool_list, _sample_dispatch)

    lt = asyncio.run(s.kwargs["on_list_tools"](None, None))
    assert [t.name for t in lt.tools] == ["bi_probe"]

    class _Params:
        name = "bi_probe"
        arguments = {"b": 2}

    ct = asyncio.run(s.kwargs["on_call_tool"](None, _Params()))
    assert "called:bi_probe" in ct.content[0].text
    # Field is `isError` on 1.x and `is_error` on 2.x; the shim never sets it,
    # so just assert the default is falsy under whichever SDK is installed.
    assert not getattr(ct, "is_error", getattr(ct, "isError", False))


def test_2x_call_tool_forwards_none_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """`arguments` is optional on the wire; None must reach the dispatcher intact."""
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    seen: dict[str, Any] = {}

    async def _capture(
        name: str, arguments: dict[str, Any] | None, errored: list[bool] | None = None
    ) -> list[Any]:
        seen["name"], seen["arguments"] = name, arguments
        from mcp.types import TextContent
        return [TextContent(type="text", text="ok")]

    s = srv._build_server(_sample_tool_list, _capture)

    class _Params:
        name = "bi_probe"
        arguments = None

    asyncio.run(s.kwargs["on_call_tool"](None, _Params()))
    assert seen == {"name": "bi_probe", "arguments": None}


# ---------------------------------------------------------------------------
# isError propagation on the 2.x path
# ---------------------------------------------------------------------------


def test_2x_sets_is_error_when_dispatch_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool failure must surface as isError=True, not a successful call.

    1.x only set the flag when the handler raised; `dispatch_tool` handles
    BiError itself and returns a content block, so without threading the flag
    through, every BI failure (unreachable, auth, camera not found) would look
    like success to a client that keys off isError.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    s = srv._build_server(_sample_tool_list, _failing_dispatch)

    class _Params:
        name = "bi_probe"
        arguments = {}

    ct = asyncio.run(s.kwargs["on_call_tool"](None, _Params()))
    flag = getattr(ct, "is_error", None)
    if flag is None:
        flag = getattr(ct, "isError", None)
    assert flag is True


def test_2x_clears_is_error_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path must NOT be flagged — otherwise the flag is meaningless."""
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    s = srv._build_server(_sample_tool_list, _sample_dispatch)

    class _Params:
        name = "bi_probe"
        arguments = {}

    ct = asyncio.run(s.kwargs["on_call_tool"](None, _Params()))
    flag = getattr(ct, "is_error", None)
    if flag is None:
        flag = getattr(ct, "isError", None)
    assert flag is False


def test_mark_error_is_a_noop_without_an_out_param() -> None:
    """`errored=None` (the 1.x path) must not raise — the flag is optional."""
    srv._mark_error(None)  # must not raise


def test_mark_error_is_idempotent() -> None:
    """Two failures in one call must not append a second entry."""
    slot: list[bool] = []
    srv._mark_error(slot)
    srv._mark_error(slot)
    assert slot == [True]


# ---------------------------------------------------------------------------
# _dispatch_tool — the module-scope dispatcher
#
# These branches were unreachable while `dispatch_tool` was a closure inside
# `_serve()`: a review found that disabling the `except Exception` catch left
# the whole suite green. `except Exception` is the highest-value line in the
# SDK port (without it a bare ValueError from a tool escapes as a JSON-RPC
# PROTOCOL error on 2.x instead of a tool error), so it needs real coverage.
# ---------------------------------------------------------------------------


def _dispatch(tools: dict[str, Any], name: str, args: dict[str, Any] | None = None):
    """Run _dispatch_tool, returning (blocks, errored_flag)."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(name, args, tools=tools, client=object(), errored=errored)
    )
    return blocks, bool(errored)


def test_dispatch_unknown_tool_marks_error_and_does_not_raise() -> None:
    blocks, errored = _dispatch({}, "bi_nope")
    assert errored is True
    payload = json.loads(blocks[0].text)
    assert payload == {"error": "unknown tool: bi_nope", "kind": "bad_request"}


def test_dispatch_catches_bare_valueerror_instead_of_propagating() -> None:
    """A tool raising ValueError must become a tool error, never propagate.

    Tools do raise bare ValueError for arg validation (tools_log has six such
    paths). 1.x's decorator caught these for us; the 2.x kwarg path does not,
    so `_dispatch_tool` must. If this regresses, `bi_list_log(level=2)` comes
    back as a JSON-RPC protocol error on 2.x.
    """
    def _boom(_client, _args):
        raise ValueError("the 'level' arg was renamed to 'levels' (list)")

    blocks, errored = _dispatch({"bi_boom": _boom}, "bi_boom", {})
    assert errored is True
    assert "renamed to 'levels'" in blocks[0].text


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad arg"), TypeError("wrong type"), KeyError("missing"),
     RuntimeError("boom"), ZeroDivisionError("div")],
)
def test_dispatch_catches_every_exception_type(exc: Exception) -> None:
    """The catch must be broad — any tool bug becomes a tool error, not a crash."""
    def _boom(_client, _args):
        raise exc

    from mcp.types import TextContent

    blocks, errored = _dispatch({"bi_boom": _boom}, "bi_boom", {})
    assert errored is True
    assert isinstance(blocks[0], TextContent)
    assert str(exc) in blocks[0].text, "the exception message must reach the client"


def test_dispatch_bierror_is_reported_as_structured_payload() -> None:
    """BiError keeps its structured to_dict() shape, unlike a bare exception."""
    from bi_mcp.errors import BiNotFound

    def _boom(_client, _args):
        raise BiNotFound("no such camera")

    blocks, errored = _dispatch({"bi_boom": _boom}, "bi_boom", {})
    assert errored is True
    payload = json.loads(blocks[0].text)
    assert payload.get("kind") and "no such camera" in payload.get("error", "")


def test_dispatch_success_leaves_error_flag_clear() -> None:
    blocks, errored = _dispatch({"bi_ok": lambda _c, _a: {"n": 1}}, "bi_ok", {})
    assert errored is False
    assert json.loads(blocks[0].text) == {"n": 1}


def test_dispatch_passes_arguments_through_and_defaults_none_to_empty() -> None:
    seen: dict[str, Any] = {}

    def _capture(_client, args):
        seen.update(args or {"__was_none__": True})
        return {"ok": True}

    _dispatch({"bi_x": _capture}, "bi_x", {"a": 1})
    assert seen == {"a": 1}
    seen.clear()
    _dispatch({"bi_x": _capture}, "bi_x", None)
    assert seen == {"__was_none__": True}, "None arguments must become {}"


def test_dispatch_on_first_success_fires_only_on_success() -> None:
    calls: list[int] = []

    def _ok(_client, _args):
        return {}

    def _bad(_client, _args):
        raise ValueError("nope")

    asyncio.run(srv._dispatch_tool("bi_ok", {}, tools={"bi_ok": _ok},
                                   client=object(), on_first_success=lambda: calls.append(1)))
    assert calls == [1]
    asyncio.run(srv._dispatch_tool("bi_bad", {}, tools={"bi_bad": _bad},
                                   client=object(), on_first_success=lambda: calls.append(1)))
    assert calls == [1], "must not fire on a failed call"


def test_dispatch_works_without_an_errored_out_param() -> None:
    """The 1.x path passes no out-param; `_mark_error(None)` must not raise."""
    blocks = asyncio.run(
        srv._dispatch_tool("bi_nope", {}, tools={}, client=object())
    )
    assert "unknown tool" in blocks[0].text


# ---------------------------------------------------------------------------
# reraise_unhandled — the 1.x/2.x split for BARE (non-BiError) exceptions
#
# Regression guard. `_dispatch_tool` was originally an unconditional catch,
# which silently downgraded 1.x's wire behaviour: the @call_tool decorator
# wraps raising handlers via `except Exception -> _make_error_result(...)`
# (isError=True), so swallowing the exception first made every bare
# ValueError — tools_log alone raises six — come back isError=False.
# Measured on mcp 1.27.1 + live Blue Iris: HEAD isError=True, regressed
# tree isError=False.
#
# Only BARE exceptions are affected. Handled BiError keeps its documented
# generation divergence (2.x True / 1.x False) — see `_build_server`.
# ---------------------------------------------------------------------------


def test_dispatch_reraises_bare_exception_when_asked() -> None:
    """1.x path: the exception must reach the decorator, which sets isError."""
    def _boom(_client, _args):
        raise ValueError("bad arg")

    errored: list[bool] = []
    with pytest.raises(ValueError, match="bad arg"):
        asyncio.run(
            srv._dispatch_tool(
                "bi_boom", {}, tools={"bi_boom": _boom}, client=object(),
                errored=errored, reraise_unhandled=True,
            )
        )


def test_dispatch_default_still_swallows_bare_exception() -> None:
    """2.x path (the default): no decorator safety net, so it must NOT raise."""
    def _boom(_client, _args):
        raise ValueError("bad arg")

    blocks, errored = _dispatch({"bi_boom": _boom}, "bi_boom", {})
    assert errored is True
    assert "bad arg" in blocks[0].text


def test_dispatch_reraise_does_not_affect_bierror() -> None:
    """BiError stays handled under reraise — the split is bare-exceptions only.

    If this ever starts raising, the deliberate BiError divergence documented
    in `_build_server` has been broken.
    """
    from bi_mcp.errors import BiNotFound

    def _boom(_client, _args):
        raise BiNotFound("no such camera")

    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_boom", {}, tools={"bi_boom": _boom}, client=object(),
            errored=errored, reraise_unhandled=True,
        )
    )
    assert bool(errored) is True
    payload = json.loads(blocks[0].text)
    assert payload["kind"] == "not_found"


def test_dispatch_reraise_does_not_affect_unknown_tool() -> None:
    """Unknown tool is a returned error block, never a raise, on both paths."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_nope", {}, tools={}, client=object(),
            errored=errored, reraise_unhandled=True,
        )
    )
    assert bool(errored) is True
    assert json.loads(blocks[0].text)["error"] == "unknown tool: bi_nope"


def test_dispatch_reraise_does_not_affect_success() -> None:
    """The happy path is untouched by the flag."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_ok", {}, tools={"bi_ok": lambda _c, _a: {"n": 1}}, client=object(),
            errored=errored, reraise_unhandled=True,
        )
    )
    assert bool(errored) is False
    assert json.loads(blocks[0].text) == {"n": 1}


def test_1x_call_tool_lets_bare_exception_reach_the_decorator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on the registration seam: the 1.x wrapper must not absorb it.

    The decorator the real SDK applies is what turns this into isError=True,
    so `_register_handlers`' wrapper has to let it through.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1x)

    async def _raising_dispatch(
        name: str, arguments: dict[str, Any] | None, errored: list[bool] | None = None
    ) -> list[Any]:
        raise ValueError("bad arg")

    s = srv._build_server(_sample_tool_list, _raising_dispatch)
    srv._register_handlers(s, _sample_tool_list, _raising_dispatch)

    with pytest.raises(ValueError, match="bad arg"):
        asyncio.run(s.registered["call_tool"]("bi_probe", {}))


def test_reraise_policy_is_true_on_1x_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the generation->policy mapping in BOTH directions.

    Asserting only the 1.x side would let the mapping be inverted silently:
    an inverted wiring re-breaks 1.x (isError=False) AND turns every bare
    ValueError on 2.x into a JSON-RPC protocol error.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1x)
    assert srv._reraise_unhandled_for_sdk() is True
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    assert srv._reraise_unhandled_for_sdk() is False


# ---------------------------------------------------------------------------
# _serve's wiring of the policy into the dispatcher
#
# The two ends above are each pinned (`_reraise_unhandled_for_sdk` by
# `test_reraise_policy_is_true_on_1x_only`, the parameter by
# `test_dispatch_reraises_bare_exception_when_asked`) but nothing asserted the
# JOIN: the single `reraise_unhandled=_reraise_unhandled_for_sdk()` line in
# `_serve`'s `dispatch_tool` closure. Mutating it to a literal left the whole
# suite green while regressing the 1.x wire (bare ValueError isError True->
# False). These tests drive the REAL `_serve` — stubbing only its I/O edges
# (`from_env`, the transport, `Server.run`) — and capture what the closure
# actually forwards.
# ---------------------------------------------------------------------------


class _StubTransport:
    """Async-CM stand-in for `stdio_server()`: yields two dummy streams."""

    async def __aenter__(self) -> tuple[Any, Any]:
        return (object(), object())

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _run_serve_capturing_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Run the real `_serve()` and return the calls its closure forwarded.

    Each entry is the kwargs dict `_dispatch_tool` was called with. `_serve`
    is stubbed only at its edges: `from_env` (would need BI creds),
    `stdio_server` (would take over the process's stdio) and the server's
    `run` (would block forever serving). Everything between — including the
    wiring line under test — is the production code path.
    """
    calls: list[dict[str, Any]] = []

    async def _recorder(name: str, arguments: Any, **kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        from mcp.types import TextContent
        return [TextContent(type="text", text="recorded")]

    class _FakeClient:
        # `_serve` logs these at startup; nothing here contacts Blue Iris.
        class read:
            host = "127.0.0.1"
            port = 81
        admin = None
        bi_version = None

    captured: dict[str, Any] = {}

    real_build_server = srv._build_server

    def _capture_build_server(build_tool_list: Any, dispatch_tool: Any) -> Any:
        captured["dispatch_tool"] = dispatch_tool
        s = real_build_server(build_tool_list, dispatch_tool)
        # Neutralise the blocking serve loop; the handshake is not under test.
        # `create_initialization_options` is stubbed too so the generation
        # fakes (which are not full Servers) can stand in for a real one.
        monkeypatch.setattr(s, "run", _noop_run, raising=False)
        monkeypatch.setattr(
            s, "create_initialization_options", lambda *a, **kw: None, raising=False
        )
        return s

    async def _noop_run(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(srv, "from_env", lambda: _FakeClient())
    monkeypatch.setattr(srv, "stdio_server", lambda: _StubTransport())
    monkeypatch.setattr(srv, "_build_server", _capture_build_server)
    monkeypatch.setattr(srv, "_dispatch_tool", _recorder)

    asyncio.run(srv._serve())

    assert "dispatch_tool" in captured, "_serve never built a server"
    asyncio.run(captured["dispatch_tool"]("bi_probe", {"a": 1}))
    return calls


def test_serve_forwards_the_sdk_reraise_policy_into_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_serve` must pass `_reraise_unhandled_for_sdk()`, not a literal.

    Compared against the policy fn's live result rather than a hardcoded
    True, so an inverted wiring (`reraise_unhandled=not ...`, or a pinned
    `True`/`False`) fails here too — a pinned literal is exactly the mutation
    that survived the rest of the suite.
    """
    calls = _run_serve_capturing_dispatch(monkeypatch)

    assert len(calls) == 1
    assert "reraise_unhandled" in calls[0], (
        "_serve stopped passing reraise_unhandled at all — the dispatcher's "
        "default (False) would then silently regress the 1.x wire"
    )
    assert calls[0]["reraise_unhandled"] is srv._reraise_unhandled_for_sdk()


@pytest.mark.parametrize("fake_server", [_FakeServer1x, _FakeServer2x])
def test_serve_forwards_the_policy_under_both_sdk_generations(
    monkeypatch: pytest.MonkeyPatch, fake_server: type
) -> None:
    """Both generations, whichever SDK is actually installed.

    Faking `Server` swings `_sdk_generation()` and therefore the expected
    value, so this catches a wiring pinned to either literal: under 1.x the
    expected value is True, under 2.x it is False.
    """
    monkeypatch.setattr(srv, "Server", fake_server)
    expected = fake_server is _FakeServer1x

    calls = _run_serve_capturing_dispatch(monkeypatch)

    assert srv._reraise_unhandled_for_sdk() is expected, "generation probe fake failed"
    assert calls[0]["reraise_unhandled"] is expected


# ---------------------------------------------------------------------------
# Input validation against inputSchema
#
# 1.x's `@call_tool()` decorator defaults to `validate_input=True` and runs
# `jsonschema.validate` against the advertised schema BEFORE the handler. The
# 2.x `on_call_tool=` kwarg path has no equivalent, so arguments reached tools
# unchecked. That is not cosmetic: `bi_set_camera` is the only tool declaring
# `additionalProperties: false` AND it is the mutating tool (reboot/reset/...),
# and its `_pick_op()` silently ignores keys it doesn't recognise. Measured on
# mcp 2.1.1 with a spy tool, payload {"camera":..,"reboot":true,"reboto":false}:
# the reboot op DISPATCHED, isError=False. On 1.x the same payload was refused.
# ---------------------------------------------------------------------------


# The shape that matters: one required string + a boolean op, no extras.
_STRICT_SCHEMA = {
    "type": "object",
    "properties": {"camera": {"type": "string"}, "reboot": {"type": "boolean"}},
    "required": ["camera"],
    "additionalProperties": False,
}


def _dispatch_validating(
    args: dict[str, Any] | None,
    schema: dict[str, Any] | None = None,
    *,
    tool_name: str = "bi_set_camera",
):
    """Dispatch a spy tool WITH schemas wired, as `_serve` does on 2.x.

    Returns (blocks, errored_flag, dispatched) where `dispatched` records the
    args the tool actually received — empty means the tool never ran.
    """
    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            tool_name,
            args,
            tools={"bi_set_camera": _spy},
            client=object(),
            errored=errored,
            schemas={"bi_set_camera": schema if schema is not None else _STRICT_SCHEMA},
        )
    )
    return blocks, bool(errored), dispatched


def test_validation_blocks_unknown_property_and_does_not_run_the_tool() -> None:
    """The exact reproduction: a typo'd op must not reach a mutating tool.

    `dispatched` is the load-bearing assertion — asserting only isError would
    pass even if the tool ran and THEN got flagged, which is the dangerous
    outcome (the camera is already rebooting).
    """
    blocks, errored, dispatched = _dispatch_validating(
        {"camera": "SecCam_11", "reboot": True, "reboto": False}
    )
    assert dispatched == [], "the tool MUST NOT run when arguments fail validation"
    assert errored is True
    assert "Input validation error" in blocks[0].text
    assert "reboto" in blocks[0].text, "the offending property must be named"


def test_validation_message_matches_the_1x_sdk_wording() -> None:
    """Both generations must produce the same text for the same failure.

    Computed from jsonschema itself rather than hardcoded, so this tracks the
    SDK's own `f"Input validation error: {e.message}"` construction instead of
    freezing a literal that could drift from what 1.x emits.
    """
    import jsonschema

    payload = {"camera": "SecCam_11", "reboot": True, "reboto": False}
    try:
        jsonschema.validate(instance=payload, schema=_STRICT_SCHEMA)
    except jsonschema.ValidationError as e:
        expected = f"Input validation error: {e.message}"
    else:  # pragma: no cover - the payload is invalid by construction
        pytest.fail("the sample payload must not validate")

    blocks, _, _ = _dispatch_validating(payload)
    assert blocks[0].text == expected


def test_validation_rejects_wrong_type_and_missing_required() -> None:
    """Not just additionalProperties — the whole schema is enforced."""
    _, errored, dispatched = _dispatch_validating({"camera": 123})
    assert (errored, dispatched) == (True, []), "wrong type must be refused"

    _, errored, dispatched = _dispatch_validating({"reboot": True})
    assert (errored, dispatched) == (True, []), "missing required must be refused"


def test_validation_lets_valid_arguments_through() -> None:
    """The guard must not become a wall: a valid payload still reaches the tool.

    Without this, inverting the validation branch (refuse everything) would
    leave the failure-side tests above green.
    """
    blocks, errored, dispatched = _dispatch_validating({"camera": "SecCam_11", "reboot": True})
    assert dispatched == [{"camera": "SecCam_11", "reboot": True}]
    assert errored is False
    assert json.loads(blocks[0].text) == {"ok": True}


def test_validation_treats_none_arguments_as_empty_object() -> None:
    """`arguments` is optional on the wire; None validates as {}.

    With `required: ["camera"]` that must FAIL rather than crash the validator
    on a None instance.
    """
    _, errored, dispatched = _dispatch_validating(None)
    assert dispatched == []
    assert errored is True

    # ...and against a permissive schema, None must sail through as {}.
    blocks, errored, dispatched = _dispatch_validating(
        None, schema={"type": "object", "additionalProperties": True}
    )
    assert dispatched == [{}]
    assert errored is False


def test_validation_is_skipped_when_no_schemas_are_supplied() -> None:
    """The 1.x path passes schemas=None — nothing may be validated there.

    This is what keeps 1.x from double-validating (and from emitting our
    message instead of the SDK's).
    """
    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_set_camera",
            {"camera": "SecCam_11", "reboto": False},
            tools={"bi_set_camera": _spy},
            client=object(),
            errored=errored,
            schemas=None,
        )
    )
    assert dispatched == [{"camera": "SecCam_11", "reboto": False}]
    assert bool(errored) is False
    assert json.loads(blocks[0].text) == {"ok": True}


def test_validation_tolerates_a_tool_with_no_registered_schema() -> None:
    """A tool missing from `schemas` must still dispatch, not blow up."""
    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    asyncio.run(
        srv._dispatch_tool(
            "bi_other",
            {"anything": 1},
            tools={"bi_other": _spy},
            client=object(),
            errored=errored,
            schemas={},  # no entry for bi_other
        )
    )
    assert dispatched == [{"anything": 1}]
    assert bool(errored) is False


def test_validation_does_not_change_the_unknown_tool_path() -> None:
    """Unknown tool stays a bad_request block, even with schemas wired."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_nope", {"x": 1}, tools={}, client=object(),
            errored=errored, schemas={"bi_nope": _STRICT_SCHEMA},
        )
    )
    assert bool(errored) is True
    assert json.loads(blocks[0].text) == {
        "error": "unknown tool: bi_nope", "kind": "bad_request"
    }


def test_validation_policy_tracks_the_capability_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the capability->policy mapping in BOTH directions.

    Inverted, this would double-validate where the SDK already does (replacing
    the SDK's message with ours) and leave the non-validating SDKs unguarded —
    the original defect. Keyed on the CAPABILITY, not the generation:
    `_FakeServer1x` has the decorator but no `validate_input`, i.e. the
    mcp 1.2-1.9 shape, which must be validated by us.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xValidating)
    assert srv._validate_input_for_sdk() is False
    for fake in (_FakeServer1x, _FakeServer2x):
        monkeypatch.setattr(srv, "Server", fake)
        assert srv._validate_input_for_sdk() is True


def test_the_two_policies_key_off_different_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They are deliberately NOT inverses of each other any more.

    `_reraise_unhandled_for_sdk` is a GENERATION question (does the decorator
    catch escaping exceptions?), while `_validate_input_for_sdk` is a
    CAPABILITY question (does the decorator validate?). mcp 1.2-1.9 answers
    yes to the first and no to the second — the case that made the old
    "exact inverse" invariant wrong. Guards against either being collapsed
    back into the other.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1x)  # 1.x, non-validating
    assert srv._reraise_unhandled_for_sdk() is True
    assert srv._validate_input_for_sdk() is True, "both True on mcp 1.2-1.9"

    monkeypatch.setattr(srv, "Server", _FakeServer1xValidating)  # >=1.10
    assert srv._reraise_unhandled_for_sdk() is True
    assert srv._validate_input_for_sdk() is False

    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    assert srv._reraise_unhandled_for_sdk() is False
    assert srv._validate_input_for_sdk() is True


def test_serve_forwards_tool_schemas_only_where_we_must_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring line in `_serve`: schemas iff the SDK doesn't validate.

    The SDK fake swings the expected value, so a literal pinned either way
    fails here. Compared against `_validate_input_for_sdk()` rather than a
    hardcoded expectation, and the schema mapping is checked for real content
    so `schemas={}` (which validates nothing) cannot pass as "wired".
    """
    from bi_mcp.tools import TOOL_SCHEMAS

    for fake in (_FakeServer1x, _FakeServer1xValidating, _FakeServer2x):
        monkeypatch.setattr(srv, "Server", fake)
        calls = _run_serve_capturing_dispatch(monkeypatch)
        assert len(calls) == 1
        assert "schemas" in calls[0], "_serve stopped passing schemas at all"
        got = calls[0]["schemas"]
        if srv._validate_input_for_sdk():
            assert got is TOOL_SCHEMAS, "2.x must forward the real schema registry"
            assert got, "the schema registry must not be empty"
        else:
            assert got is None, "1.x must not validate (the SDK already does)"


# ---------------------------------------------------------------------------
# Malformed schema (`jsonschema.SchemaError`)
#
# `jsonschema.validate()` calls `check_schema()` internally and raises
# `SchemaError` for a MALFORMED schema. It is NOT a `ValidationError`
# subclass, so `except jsonschema.ValidationError` misses it — and because the
# validation block sits BEFORE `_dispatch_tool`'s `try`, `reraise_unhandled`
# never sees it either. On 2.x that escape became a JSON-RPC *protocol* error,
# breaking the "nothing escapes as a protocol error" contract. Latent today
# (all shipped schemas pass `check_schema`), armed by any future schema edit.
# ---------------------------------------------------------------------------

_MALFORMED_SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "not_a_real_type"}},
}


def test_the_malformed_schema_fixture_really_is_malformed() -> None:
    """Positive control for the tests below.

    Without this, a fixture that quietly became VALID would turn every
    "malformed schema is handled" assertion into a vacuous pass — they would
    all be exercising the ordinary success path instead.
    """
    import jsonschema

    with pytest.raises(jsonschema.SchemaError):
        jsonschema.validate(instance={"x": 1}, schema=_MALFORMED_SCHEMA)

    # ...and it must be invisible to the ValidationError branch, which is the
    # whole reason the defect existed.
    assert not issubclass(jsonschema.SchemaError, jsonschema.ValidationError)


def test_malformed_schema_does_not_escape_the_dispatcher() -> None:
    """The defect itself: a `SchemaError` must not propagate out.

    Uses `reraise_unhandled=False` (the 2.x policy), where an escape becomes a
    protocol error rather than a tool error.
    """
    blocks, errored, dispatched = _dispatch_validating(
        {"x": 1}, schema=_MALFORMED_SCHEMA
    )
    assert dispatched == [], "the tool MUST NOT run when its schema is malformed"
    assert errored is True
    assert "not_a_real_type" in blocks[0].text


def test_malformed_schema_does_not_escape_under_the_1x_reraise_policy() -> None:
    """Also pinned with `reraise_unhandled=True`.

    `SchemaError` is raised BEFORE the `try` that honours that flag, so the
    flag must make no difference here. Asserting only the 2.x policy would
    miss a fix mistakenly written inside that `try`.
    """
    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_set_camera",
            {"x": 1},
            tools={"bi_set_camera": _spy},
            client=object(),
            errored=errored,
            reraise_unhandled=True,
            schemas={"bi_set_camera": _MALFORMED_SCHEMA},
        )
    )
    assert dispatched == []
    assert bool(errored) is True
    assert "not_a_real_type" in blocks[0].text


def test_malformed_schema_message_matches_the_1x_sdk_wording() -> None:
    """Both generations must render a `SchemaError` identically.

    1.x's decorator catches only `ValidationError`, so a `SchemaError` falls
    to its outer `except Exception -> _make_error_result(str(e))` — bare
    `str(e)`, with NO "Input validation error: " prefix (unlike a genuine
    ValidationError). Computed from jsonschema rather than hardcoded so this
    tracks the SDK's construction instead of freezing a literal.
    """
    import jsonschema

    try:
        jsonschema.validate(instance={"x": 1}, schema=_MALFORMED_SCHEMA)
    except jsonschema.SchemaError as e:
        expected = str(e)
    else:  # pragma: no cover - the schema is malformed by construction
        pytest.fail("the sample schema must not pass check_schema")

    blocks, _, _ = _dispatch_validating({"x": 1}, schema=_MALFORMED_SCHEMA)
    assert blocks[0].text == expected
    assert not blocks[0].text.startswith("Input validation error: "), (
        "a malformed schema is a SERVER bug, not caller error — 1.x renders it "
        "via the unprefixed str(e) path and 2.x must agree"
    )


def test_missing_jsonschema_fails_closed() -> None:
    """A missing validator must REFUSE the call, never skip validation.

    Fail-open here would hand an unvalidated payload to `bi_set_camera`, whose
    `_pick_op()` silently drops unrecognised keys — so a typo'd `reboto` would
    become a real reboot. Simulated by making the function-local
    `import jsonschema` raise.
    """
    import builtins

    real_import = builtins.__import__

    def _no_jsonschema(name: str, *a: Any, **kw: Any) -> Any:
        if name == "jsonschema":
            raise ImportError("No module named 'jsonschema'")
        return real_import(name, *a, **kw)

    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    with mock.patch.object(builtins, "__import__", _no_jsonschema):
        blocks = asyncio.run(
            srv._dispatch_tool(
                "bi_set_camera",
                # A payload that is VALID against the schema, so only a
                # fail-closed guard can stop it. A merely-invalid payload
                # would be refused for the wrong reason and prove nothing.
                {"camera": "SecCam_11", "reboot": True},
                tools={"bi_set_camera": _spy},
                client=object(),
                errored=errored,
                schemas={"bi_set_camera": _STRICT_SCHEMA},
            )
        )
    assert dispatched == [], "must not dispatch when the validator is unavailable"
    assert bool(errored) is True
    assert "validation error" in blocks[0].text.lower()


# ---------------------------------------------------------------------------
# Escapes OUTSIDE the jsonschema taxonomy (the backstop `except Exception`)
#
# `jsonschema.validate()` raises several things that no list of jsonschema
# types catches, because they are not jsonschema types at all:
#   * `_WrappedReferencingError` — an unresolvable `$ref` (dangling pointer,
#     bad fragment, unknown URN, or a REMOTE ref the validator refuses to
#     fetch). Derives from `referencing`'s `Unresolvable`, NOT from
#     `jsonschema.exceptions._Error`.
#   * `RecursionError` — a self- or mutually-recursive `$ref`.
#   * `TypeError` — a scalar (`42`) where a schema was expected.
# All are raised BEFORE the `try` that honours `reraise_unhandled`, so on 2.x
# they escaped `_dispatch_tool` as JSON-RPC PROTOCOL errors. Measured on real
# mcp 2.1.1 before the fix, via the production `on_call_tool` handler.
#
# Latent today (no shipped schema uses `$ref`), armed by the first one that
# does. Two earlier rounds were spent extending the caught-type list; these
# tests pin the ESCAPE, which is the actual contract.
# ---------------------------------------------------------------------------

_ESCAPING_SCHEMAS = {
    # (label, schema) -> each raised a non-jsonschema exception pre-fix.
    "dangling_local_ref": {
        "type": "object",
        "properties": {"camera": {"$ref": "#/$defs/missing"}},
    },
    "bad_pointer_fragment": {
        "type": "object",
        "properties": {"camera": {"$ref": "#/$defs/a/b/c/d"}},
    },
    "remote_ref": {"$ref": "https://example.invalid/schema"},
    "unknown_urn_ref": {"$ref": "urn:nope:not-registered"},
    "recursive_root_ref": {"$ref": "#"},
    "self_recursive_defs": {"$defs": {"x": {"$ref": "#/$defs/x"}}, "$ref": "#/$defs/x"},
    "mutually_recursive_defs": {
        "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
        "$ref": "#/$defs/a",
    },
    "scalar_schema": 42,
}


@pytest.mark.parametrize("label", sorted(_ESCAPING_SCHEMAS))
def test_these_schemas_really_do_escape_the_jsonschema_taxonomy(label: str) -> None:
    """POSITIVE CONTROL for the regression tests below.

    Each fixture must raise something that is NEITHER `ValidationError` NOR
    `SchemaError` — otherwise the "does not escape" tests below would be
    exercising the two pre-existing named branches and passing vacuously,
    proving nothing about the backstop.
    """
    import jsonschema

    with pytest.raises(BaseException) as exc:
        jsonschema.validate(instance={"camera": "SecCam_11"},
                            schema=_ESCAPING_SCHEMAS[label])
    e = exc.value
    assert not isinstance(e, (jsonschema.ValidationError, jsonschema.SchemaError)), (
        f"{label} is caught by an existing named branch — it no longer exercises "
        "the backstop, so it cannot prove the escape is closed"
    )


@pytest.mark.parametrize("label", sorted(_ESCAPING_SCHEMAS))
def test_non_jsonschema_exception_does_not_escape_the_dispatcher(label: str) -> None:
    """The defect: none of these may propagate out of `_dispatch_tool`.

    An escape here is a JSON-RPC protocol error on 2.x. `dispatched` is the
    load-bearing half — the tool must never run on a schema we cannot even
    evaluate.
    """
    blocks, errored, dispatched = _dispatch_validating(
        {"camera": "SecCam_11"}, schema=_ESCAPING_SCHEMAS[label]
    )
    assert dispatched == [], "the tool MUST NOT run when its schema cannot be evaluated"
    assert errored is True
    assert blocks[0].text, "an error block must carry a message"


@pytest.mark.parametrize("label", sorted(_ESCAPING_SCHEMAS))
def test_non_jsonschema_exception_is_contained_under_the_1x_reraise_policy(
    label: str,
) -> None:
    """Also pinned with `reraise_unhandled=True`.

    These are raised BEFORE the `try` that honours the flag, so it must make
    no difference. This is what forbids "fix" (a) — moving validation inside
    that `try` would re-raise here instead of failing closed.
    """
    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_set_camera",
            {"camera": "SecCam_11"},
            tools={"bi_set_camera": _spy},
            client=object(),
            errored=errored,
            reraise_unhandled=True,
            schemas={"bi_set_camera": _ESCAPING_SCHEMAS[label]},
        )
    )
    assert dispatched == []
    assert bool(errored) is True
    assert blocks[0].text


def test_backstop_renders_bare_str_without_the_validation_prefix() -> None:
    """Wording parity with 1.x for a non-`ValidationError` failure.

    1.x validates inside the same `try` as the tool call, so anything that is
    not a `ValidationError` lands in its outer
    `except Exception -> _make_error_result(str(e))` — bare `str(e)`, no
    prefix. Computed from jsonschema rather than hardcoded.
    """
    import jsonschema

    schema = _ESCAPING_SCHEMAS["dangling_local_ref"]
    try:
        jsonschema.validate(instance={"camera": "SecCam_11"}, schema=schema)
    except BaseException as e:  # noqa: BLE001 - that's the point
        expected = str(e)
    else:  # pragma: no cover - the schema is unresolvable by construction
        pytest.fail("the sample schema must not validate")

    blocks, _, _ = _dispatch_validating({"camera": "SecCam_11"}, schema=schema)
    assert blocks[0].text == expected
    assert not blocks[0].text.startswith("Input validation error: "), (
        "an unevaluatable schema is a SERVER bug, not caller error — 1.x "
        "renders it via the unprefixed str(e) path and 2.x must agree"
    )


def test_backstop_does_not_swallow_valid_arguments() -> None:
    """The backstop must not become a wall.

    Widening to `except Exception` around the whole validation block could
    catch-and-refuse everything; a valid payload must still reach the tool.
    """
    blocks, errored, dispatched = _dispatch_validating(
        {"camera": "SecCam_11", "reboot": True}
    )
    assert dispatched == [{"camera": "SecCam_11", "reboot": True}]
    assert errored is False
    assert json.loads(blocks[0].text) == {"ok": True}


def test_backstop_preserves_the_validationerror_prefix() -> None:
    """The backstop must stay LAST — the named branches keep their wording.

    If `except Exception` were placed before `except ValidationError`, a
    genuine caller error would lose its "Input validation error: " prefix.
    """
    blocks, errored, dispatched = _dispatch_validating(
        {"camera": "SecCam_11", "reboot": True, "reboto": False}
    )
    assert dispatched == []
    assert errored is True
    assert blocks[0].text.startswith("Input validation error: ")
    assert "reboto" in blocks[0].text


# ---------------------------------------------------------------------------
# Capability probe: "has the decorator" != "the decorator validates"
#
# The policy used to be `_sdk_generation() == "2x"`, which asserted that any
# 1.x SDK validates for us. It doesn't: `@call_tool()` exists from 1.2, but
# decorator-side validation (`validate_input: bool = True`) only landed in
# 1.10. `pyproject.toml` now pins `mcp>=2.1.1,<3`, so 1.2-1.9 are no longer
# installs packaging selects — but the 1.x branch is retained for
# environments that already have such an SDK, where nobody validated and a
# typo'd payload reached `bi_set_camera` — which reboots real cameras and
# silently drops keys it doesn't recognise.
#
# These fake the SDK shapes rather than installing old SDKs (no network, and
# 1.2-1.9 cannot be obtained here). They are a SIMULATION of the old signature,
# verified against the real ones by `test_capability_probe_agrees_with_the_
# installed_sdk` below.
# ---------------------------------------------------------------------------


async def _dispatch_as_serve_would(
    name: str,
    args: dict[str, Any] | None,
    *,
    tools: dict[str, Any],
    schemas: dict[str, Any] | None = None,
    errored: list[bool] | None = None,
):
    """Call `_dispatch_tool` with EXACTLY the kwargs `_serve` computes.

    Every SDK-dependent kwarg is derived from the policy fns, never hardcoded.
    A harness that forgets one (`schemas=`, `reraise_unhandled=`,
    `raise_validation_refusal=`, and now `raise_tool_failure=`) silently
    exercises the parameter's stub default instead of production wiring —
    which is how the isError bug went unnoticed: the returned-blocks
    assertions passed while the real 1.x wire said isError=False. Keep this
    the ONE construction site in this module.
    """
    validates = srv._validate_input_for_sdk()
    reraise = srv._reraise_unhandled_for_sdk()
    return await srv._dispatch_tool(
        name,
        args,
        tools=tools,
        client=object(),
        errored=errored,
        reraise_unhandled=reraise,
        schemas=schemas if validates else None,
        raise_validation_refusal=validates and reraise,
        raise_tool_failure=reraise,
    )


def _wire_text_1x(dispatch, tool_list, name, args, *, sdk_validates: bool):
    """Run one call through the REAL installed 1.x decorator; return (isError, text).

    This is the only assertion that sees what a client sees. `sdk_validates`
    picks the decorator vintage being modelled: False is the pre-1.10 shape
    (mcp 1.2-1.9 had no `validate_input` kwarg at all), which we reproduce on
    the installed decorator by passing `validate_input=False` where the kwarg
    exists and calling it bare where it does not.
    """
    from mcp.server import Server as RealServer
    from mcp.types import CallToolRequest, CallToolRequestParams

    s = RealServer("t")

    @s.list_tools()
    async def _lt():
        return tool_list

    try:
        deco = s.call_tool(validate_input=sdk_validates)
    except TypeError:  # pragma: no cover - only on a pre-1.10 SDK
        deco = s.call_tool()

    @deco
    async def _ct(n, a):
        return await dispatch(n, a)

    handler = s.request_handlers[CallToolRequest]
    res = asyncio.run(
        handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name=name, arguments=args),
            )
        )
    )
    r = getattr(res, "root", res)
    flag = getattr(r, "isError", getattr(r, "is_error", False))
    return bool(flag), r.content[0].text


class _FakeServer1xNoValidation:
    """mcp 1.2-1.9: decorators present, `call_tool()` takes no validate_input."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def list_tools(self):
        def deco(fn):
            return fn
        return deco

    def call_tool(self):
        def deco(fn):
            return fn
        return deco


class _FakeServer1xValidatingDefaultFalse:
    """Hypothetical: exposes the kwarg but defaults it OFF.

    `_register_handlers` calls `server.call_tool()` with no arguments, so the
    DEFAULT is what we get. Presence of the kwarg alone would be a false
    positive here. No real SDK is known to ship this shape.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def list_tools(self):
        def deco(fn):
            return fn
        return deco

    def call_tool(self, *, validate_input: bool = False):
        def deco(fn):
            return fn
        return deco


class _FakeServer1xValidating:
    """mcp >=1.10: the decorator validates for us (kwarg defaults True)."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs

    def list_tools(self):
        def deco(fn):
            return fn
        return deco

    def call_tool(self, *, validate_input: bool = True):
        def deco(fn):
            return fn
        return deco


def test_capability_probe_agrees_with_the_installed_sdk() -> None:
    """Positive control against whichever REAL SDK is installed.

    Without this the fakes could all drift from reality together and every
    assertion below would still pass. Recomputes the ground truth from the
    real `Server` independently of `_sdk_validates_input`'s implementation.
    """
    import inspect as _inspect

    from mcp.server import Server as RealServer

    call_tool = getattr(RealServer, "call_tool", None)
    if call_tool is None:
        expected = False  # 2.x: no decorator at all
    else:
        p = _inspect.signature(call_tool).parameters.get("validate_input")
        expected = p is not None and p.default is True
    assert srv._sdk_validates_input() is expected


@pytest.mark.parametrize(
    ("fake", "sdk_validates"),
    [
        (_FakeServer1xNoValidation, False),
        (_FakeServer1xValidatingDefaultFalse, False),
        (_FakeServer1xValidating, True),
        (_FakeServer2x, False),
    ],
)
def test_probe_reads_the_capability_not_the_generation(
    monkeypatch: pytest.MonkeyPatch, fake: Any, sdk_validates: bool
) -> None:
    """Three of these four are 1.x; they must NOT share one verdict.

    This is the assertion the old `_sdk_generation() == "2x"` policy fails:
    it returns the same answer for all three 1.x shapes.
    """
    monkeypatch.setattr(srv, "Server", fake)
    assert srv._sdk_validates_input() is sdk_validates
    assert srv._validate_input_for_sdk() is (not sdk_validates)


def test_old_1x_sdk_gets_validation_and_the_tool_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression: the typo'd reboot payload on a simulated mcp 1.2-1.9.

    Drives `_serve`'s real wiring expression rather than hardcoding
    `schemas=`, so a fix that only changed the policy fn while leaving the
    call site wrong still fails. `dispatched` is the load-bearing assertion:
    isError alone would pass even if the camera had already rebooted.

    On this SDK the refusal leaves as `_ShimValidationError` (the only channel
    that reaches isError=True through a 1.x decorator), so the text is read off
    the exception rather than off returned blocks. `_wire_text_1x` below covers
    what the decorator actually renders from it.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    assert srv._sdk_generation() == "1x", "the fake must still look like 1.x"

    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    errored: list[bool] = []
    with pytest.raises(srv._ShimValidationError) as excinfo:
        asyncio.run(
            _dispatch_as_serve_would(
                "bi_set_camera",
                {"camera": "SecCam_11", "reboot": True, "reboto": False},
                tools={"bi_set_camera": _spy},
                schemas={"bi_set_camera": _STRICT_SCHEMA},
                errored=errored,
            )
        )
    text = str(excinfo.value)
    assert dispatched == [], "an SDK that does not validate must not run the tool"
    assert bool(errored) is True
    assert text.startswith("Input validation error: ")
    assert "reboto" in text


def test_validating_1x_sdk_is_not_double_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the contract: don't validate where the SDK does.

    Counted rather than asserted on the policy flag, so it measures the
    dispatcher's actual behaviour. A VALID payload is used deliberately: an
    invalid one short-circuits at the first validating layer and would show
    a count of 1 whether or not the second layer exists.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xValidating)
    assert srv._validate_input_for_sdk() is False

    import jsonschema

    calls: list[Any] = []
    real_validate = jsonschema.validate

    def _counting(*a: Any, **k: Any):
        calls.append(k.get("schema"))
        return real_validate(*a, **k)

    monkeypatch.setattr(jsonschema, "validate", _counting)

    dispatched: list[dict[str, Any]] = []

    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    asyncio.run(
        _dispatch_as_serve_would(
            "bi_set_camera",
            {"camera": "SecCam_11", "reboot": True},
            tools={"bi_set_camera": _spy},
            schemas={"bi_set_camera": _STRICT_SCHEMA},
        )
    )
    assert dispatched, "a valid payload must still reach the tool"
    assert calls == [], "the dispatcher must not validate when the SDK already does"


def test_counting_validate_instrument_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the counter used above.

    A monkeypatch that silently failed to take effect would make the
    "calls == []" assertion pass vacuously. Force the validating branch on and
    prove the same instrument does record a call.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    assert srv._validate_input_for_sdk() is True

    import jsonschema

    calls: list[Any] = []
    real_validate = jsonschema.validate

    def _counting(*a: Any, **k: Any):
        calls.append(k.get("schema"))
        return real_validate(*a, **k)

    monkeypatch.setattr(jsonschema, "validate", _counting)

    asyncio.run(
        srv._dispatch_tool(
            "bi_set_camera",
            {"camera": "SecCam_11", "reboot": True},
            tools={"bi_set_camera": lambda _c, _a: {"ok": True}},
            client=object(),
            schemas={"bi_set_camera": _STRICT_SCHEMA}
            if srv._validate_input_for_sdk()
            else None,
        )
    )
    assert len(calls) == 1, "the counter must observe the dispatcher's own validate"


def test_unintrospectable_call_tool_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Can't prove the SDK validates => validate ourselves.

    A builtin/C-implemented `call_tool` raises from `inspect.signature`. The
    safe answer is "we validate": a duplicate check costs a wasted call, a
    skipped one costs a live camera reboot.
    """

    class _Opaque:
        def __init__(self, name: str, **kwargs: Any) -> None:
            self.name = name

        def list_tools(self):
            def deco(fn):
                return fn
            return deco

        # `type` is a builtin whose signature genuinely cannot be built:
        # `inspect.signature(type)` raises ValueError. The probe must not
        # propagate that, and must not read the failure as "SDK validates".
        call_tool = type

    # Positive control: without this the assertions below could pass via the
    # ordinary `param is None` path, leaving the except branch untested — a
    # mutant that returned True there would survive (it did, once).
    with pytest.raises(ValueError):
        __import__("inspect").signature(_Opaque.call_tool)

    monkeypatch.setattr(srv, "Server", _Opaque)
    assert srv._sdk_validates_input() is False
    assert srv._validate_input_for_sdk() is True


def test_serve_forwards_schemas_on_a_non_validating_1x_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_serve`'s wiring line, not just the policy fn.

    The prior harness bug in this work was a test that built its own
    dispatcher and never forwarded `schemas=`, so it tested its own stub.
    This drives `_serve` and reads what it actually passed.
    """
    from bi_mcp.tools import TOOL_SCHEMAS

    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    calls = _run_serve_capturing_dispatch(monkeypatch)
    assert len(calls) == 1
    got = calls[0]["schemas"]
    assert got is TOOL_SCHEMAS, "a non-validating 1.x SDK must get the real schemas"
    assert got, "the schema registry must not be empty"


# ---------------------------------------------------------------------------
# Server version advertised in `initialize`
#
# 1.x backfills a `version=None` to the SDK's own version; 2.x defaults it to
# "" and backfills nothing, so `create_initialization_options().server_version`
# measured '' on mcp 2.1.1. We send bi-mcp's own version instead: the
# serverInfo block names THIS server, and the SDK version tells a client
# nothing about which bi-mcp it is talking to.
# ---------------------------------------------------------------------------


def test_server_version_is_bi_mcps_own_version() -> None:
    """Sourced from the package, not hardcoded, so it can't drift."""
    import bi_mcp

    assert srv._server_version() == bi_mcp.__version__
    assert srv._server_version(), "the advertised version must not be empty"


def test_dunder_version_matches_pyproject_version() -> None:
    """`__version__` and pyproject's `version` are two independent literals.

    `_server_version()` reads `__version__`, so a release that bumps only
    pyproject ships a wheel advertising the PREVIOUS version in `serverInfo`
    — and every test still passes, because the test above compares
    `_server_version()` against the very source it reads from. This is the
    only check that pins the two literals to each other.
    """
    import re
    from pathlib import Path

    import bi_mcp

    # tomllib is stdlib only on 3.11+, but `requires-python` is >=3.10 — so
    # fall back to tomli, then skip honestly rather than ERRORing on 3.10.
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 only
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            pytest.skip("needs tomllib (py3.11+) or tomli to parse pyproject.toml")

    pyproject = Path(srv.__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip(f"pyproject.toml not found at {pyproject} (installed, not a source tree)")

    parsed = tomllib.loads(pyproject.read_text()).get("project")
    if not isinstance(parsed, dict):
        pytest.skip(f"{pyproject} has no [project] table")

    # Guard the path derivation: `parents[2]` assumes this repo's src-layout.
    # Under a flat layout it would resolve to some OTHER project's pyproject
    # and compare against an unrelated version, passing or failing for the
    # wrong reason. Assert we found OUR file before trusting what's in it.
    # Compare PEP 503-normalised, so a cosmetic rename (`bi_mcp`, `Bi-MCP`)
    # doesn't skip past real drift — the guard is for "wrong file", not
    # "right file, spelled differently".
    found = parsed.get("name")
    normalised = re.sub(r"[-_.]+", "-", found).lower() if isinstance(found, str) else None
    if normalised != "bi-mcp":
        pytest.skip(f"{pyproject} is not bi-mcp's (found {found!r})")

    declared = parsed.get("version")
    if declared is None:
        # `dynamic = ["version"]` or a malformed table: there is no literal
        # here to compare against, so there is nothing to pin. Skip rather
        # than KeyError — an ERROR is the failure mode this test exists to
        # avoid, not to introduce.
        pytest.skip(f"{pyproject} declares no static project.version")
    assert declared == bi_mcp.__version__, (
        f"pyproject version {declared!r} != bi_mcp.__version__ "
        f"{bi_mcp.__version__!r} — bump both, or serverInfo advertises the "
        "wrong release"
    )
    # Guard the guard: if the file were ever parsed into something that made
    # `declared` falsy, the equality above could pass vacuously.
    assert re.match(r"^\d+\.\d+", declared), f"unexpected version literal {declared!r}"


def test_2x_server_is_constructed_with_a_non_empty_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect: 2.x's `Server(...)` was built with no `version=` at all."""
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    server = srv._build_server(_sample_tool_list, _sample_dispatch)
    assert "version" in server.kwargs, "2.x must pass version= explicitly"
    assert server.kwargs["version"] == srv._server_version()
    assert server.kwargs["version"], "2.x advertised an empty server version"


def test_server_version_degrades_to_empty_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cosmetic metadata must never stop the server from starting.

    Simulates a package whose `__version__` is missing/not a string.
    """
    import bi_mcp

    monkeypatch.setattr(bi_mcp, "__version__", None)
    assert srv._server_version() == ""


def test_1x_server_construction_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surgical: 1.x still gets a bare `Server("bi-mcp")`.

    It backfills the version itself, and passing one would change what every
    already-connected 1.x client sees in `initialize`.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1x)
    server = srv._build_server(_sample_tool_list, _sample_dispatch)
    assert server.kwargs == {}, "1.x must keep its bare construction"


# ---------------------------------------------------------------------------
# THE isError REGRESSION: a shim-side refusal must not ship as a "success"
#
# On mcp 1.2-1.9 the shim (not the SDK) validates, but `_register_handlers`'
# 1.x wrapper calls `dispatch_tool(name, arguments)` with NO `errored`
# out-param — so `_mark_error` was a no-op there, `_dispatch_tool` returned
# content normally, and the decorator emitted isError=False. Measured on the
# real 1.27.1 decorator with `validate_input=False` (a faithful pre-1.10
# stand-in) and again on a real mcp 1.9.0: a REJECTED call reached the client
# as a SUCCESS whose text happened to read "Input validation error: ...".
#
# The fix raises `_ShimValidationError`, the one channel both decorator
# vintages honour: 1.2-1.9 and 1.10+ alike wrap the handler in
# `except Exception -> CallToolResult(text=str(e), isError=True)`. (Returning
# a `CallToolResult` is NOT viable: 1.10+ honours one, but 1.2-1.9 does
# `content=list(results)` on it and mangles the model.)
#
# These assert on what the DECORATOR renders, not on returned blocks — the
# returned-blocks assertions above all passed while the wire was wrong.
# ---------------------------------------------------------------------------


# These drive the REAL installed decorator, so they need a 1.x SDK present.
# SKIPPED — never silently passed — where there is none: a vacuous green on
# 2.x would be worse than a gap, since this is the suite that proves the
# pre-1.10 wire. The `errored`/policy-level tests below have no such
# requirement and run everywhere.
_needs_1x_decorator = pytest.mark.skipif(
    not hasattr(__import__("mcp.server", fromlist=["Server"]).Server, "list_tools"),
    reason="needs a 1.x SDK: these assert on the real decorator's wire result",
)


def _strict_tool_list() -> list[Any]:
    from mcp.types import Tool
    return [Tool(name="bi_set_camera", description="d", inputSchema=_STRICT_SCHEMA)]


def _spy_dispatch(schemas: dict[str, Any] | None, dispatched: list[Any]):
    """A `_serve`-shaped dispatch closure over a SPY tool.

    SAFETY: the real `bi_set_camera` reboots hardware; it is never imported
    here. The spy also proves the tool did not run.
    """
    def _spy(_client, a):
        dispatched.append(dict(a))
        return {"ok": True}

    async def _dispatch(name: str, args: dict[str, Any] | None, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools={"bi_set_camera": _spy},
            schemas=schemas, errored=errored,
        )
    return _dispatch


@_needs_1x_decorator
def test_shim_refusal_reaches_the_1x_wire_as_iserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, at the wire: pre-1.10 must report isError=True, text intact.

    Note the two loads this carries: `isError` is the regression, and
    `dispatched == []` is the safety invariant (a flag set AFTER the camera
    rebooted would be the worse outcome, and asserting isError alone would
    not notice).
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    assert srv._validate_input_for_sdk() is True, "this SDK must not validate for us"

    dispatched: list[Any] = []
    payload = {"camera": "SecCam_11", "reboot": True, "reboto": False}
    is_error, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": _STRICT_SCHEMA}, dispatched),
        _strict_tool_list(), "bi_set_camera", payload, sdk_validates=False,
    )

    assert dispatched == [], "the tool MUST NOT run when arguments fail validation"
    assert is_error is True, (
        "a refused call reported isError=False is indistinguishable from a "
        "success to any client that keys off the flag"
    )
    # The prefix must SURVIVE the trip through the decorator: it is baked into
    # the exception's message, so the decorator's `str(e)` reproduces it
    # verbatim rather than re-deriving anything.
    assert text.startswith("Input validation error: ")
    assert "reboto" in text


@_needs_1x_decorator
def test_shim_refusal_wire_text_is_byte_identical_to_the_2x_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flagging the error must not have changed the message.

    Computed from jsonschema itself, so it tracks the SDK's own
    `f"Input validation error: {e.message}"` wording rather than freezing a
    literal.
    """
    import jsonschema

    payload = {"camera": "SecCam_11", "reboot": True, "reboto": False}
    try:
        jsonschema.validate(instance=payload, schema=_STRICT_SCHEMA)
    except jsonschema.ValidationError as e:
        expected = f"Input validation error: {e.message}"
    else:  # pragma: no cover - invalid by construction
        pytest.fail("the sample payload must not validate")

    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    _, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": _STRICT_SCHEMA}, []),
        _strict_tool_list(), "bi_set_camera", payload, sdk_validates=False,
    )
    assert text == expected


@_needs_1x_decorator
@pytest.mark.skipif(
    not srv._sdk_validates_input(),
    reason="needs mcp>=1.10: models the decorator that validates for us, and "
           "a pre-1.10 decorator cannot be made to do that",
)
def test_shim_refusal_does_not_disturb_the_validating_1x_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mcp 1.10+: the SDK validates, the shim does not, nothing changed.

    The refusal comes from the DECORATOR here (isError=True has always been
    right on this path); the point is that the shim neither double-validates
    nor injects its own exception.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xValidating)
    assert srv._validate_input_for_sdk() is False

    dispatched: list[Any] = []
    is_error, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": _STRICT_SCHEMA}, dispatched),
        _strict_tool_list(), "bi_set_camera",
        {"camera": "SecCam_11", "reboot": True, "reboto": False},
        sdk_validates=True,
    )
    assert dispatched == []
    assert is_error is True
    assert text.startswith("Input validation error: ")


@_needs_1x_decorator
def test_valid_payload_still_dispatches_and_is_not_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not become a wall.

    Without this, making `_validation_refusal` raise unconditionally — refuse
    everything, always isError=True — would leave every failure-side assertion
    above green.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    dispatched: list[Any] = []
    is_error, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": _STRICT_SCHEMA}, dispatched),
        _strict_tool_list(), "bi_set_camera",
        {"camera": "SecCam_11", "reboot": True}, sdk_validates=False,
    )
    assert dispatched == [{"camera": "SecCam_11", "reboot": True}]
    assert is_error is False
    assert json.loads(text) == {"ok": True}


@pytest.mark.parametrize(
    "label,schema",
    [
        # `properties.camera` must be a schema; an int is not one -> SchemaError.
        ("schema_error", {"type": "object", "properties": {"camera": {"type": 12345}}}),
        # Unresolvable `$ref` -> escapes the jsonschema taxonomy -> backstop.
        (
            "backstop",
            {
                "type": "object",
                "properties": {"camera": {"$ref": "https://nope.invalid/x#/y"}},
            },
        ),
    ],
)
@_needs_1x_decorator
def test_other_shim_side_failure_paths_are_flagged_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, label: str, schema: dict[str, Any]
) -> None:
    """SchemaError and the terminal backstop share the refusal channel.

    A VALID-shaped payload is used deliberately: these are OUR bugs, not the
    caller's, so the refusal must happen regardless of what was sent — that is
    what "fail closed" means here, and `dispatched == []` is the assertion
    that measures it.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    dispatched: list[Any] = []
    from mcp.types import Tool

    is_error, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": schema}, dispatched),
        [Tool(name="bi_set_camera", description="d", inputSchema=_STRICT_SCHEMA)],
        "bi_set_camera", {"camera": "SecCam_11", "reboot": True},
        sdk_validates=False,
    )
    assert dispatched == [], f"{label}: must fail CLOSED, the tool must not run"
    assert is_error is True, f"{label}: a server-side refusal is still a refusal"
    # Both render bare `str(e)`, deliberately WITHOUT the caller-error prefix,
    # so a server bug is not misread as bad input. See the branch comments.
    assert not text.startswith("Input validation error: ")
    assert text, f"{label}: the message must not be empty"


@_needs_1x_decorator
def test_missing_jsonschema_is_flagged_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-closed guard must also reach the wire as isError=True.

    NOT hypothetical on this path: a real mcp 1.9.0 install ships NO
    jsonschema (it became a dependency later), so a supported pre-1.10 install
    can genuinely land here. A VALID payload is used — the guard must refuse
    everything when it cannot validate, or it is not a guard.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer1xNoValidation)
    dispatched: list[Any] = []

    real_import = builtins.__import__

    def _no_jsonschema(name: str, *a: Any, **k: Any):
        if name == "jsonschema":
            raise ImportError("No module named 'jsonschema'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_jsonschema)
    is_error, text = _wire_text_1x(
        _spy_dispatch({"bi_set_camera": _STRICT_SCHEMA}, dispatched),
        _strict_tool_list(), "bi_set_camera",
        {"camera": "SecCam_11", "reboot": True}, sdk_validates=False,
    )
    assert dispatched == [], "no validator => refuse, never dispatch anyway"
    assert is_error is True
    assert "Input validation error" in text


def test_refusal_channel_is_derived_from_the_two_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raise ONLY where we validate AND a decorator is there to catch.

    Pins the wiring expression in `_serve` against every generation, so an
    edit to either policy fn cannot silently re-open the hole (or, worse, turn
    a 2.x refusal into a JSON-RPC protocol error by raising with nothing to
    catch it).
    """
    for fake, validates, reraise in [
        (_FakeServer1xNoValidation, True, True),   # mcp 1.2-1.9
        (_FakeServer1xValidating, False, True),    # mcp 1.10+
        (_FakeServer2x, True, False),              # mcp 2.x
    ]:
        monkeypatch.setattr(srv, "Server", fake)
        assert srv._validate_input_for_sdk() is validates, fake.__name__
        assert srv._reraise_unhandled_for_sdk() is reraise, fake.__name__


def test_2x_refusal_returns_content_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.x must keep using the `errored` out-param, not the exception.

    Raising there would escape `on_call_tool` as a JSON-RPC PROTOCOL error,
    breaking the "nothing escapes as a protocol error" contract.
    """
    monkeypatch.setattr(srv, "Server", _FakeServer2x)
    assert srv._validate_input_for_sdk() is True
    assert srv._reraise_unhandled_for_sdk() is False

    dispatched: list[Any] = []
    errored: list[bool] = []
    blocks = asyncio.run(
        _dispatch_as_serve_would(
            "bi_set_camera",
            {"camera": "SecCam_11", "reboot": True, "reboto": False},
            tools={"bi_set_camera": lambda _c, a: dispatched.append(dict(a))},
            schemas={"bi_set_camera": _STRICT_SCHEMA},
            errored=errored,
        )
    )
    assert dispatched == []
    assert bool(errored) is True, "2.x reads isError off this flag"
    assert blocks[0].text.startswith("Input validation error: ")


@pytest.mark.parametrize(
    "fake_server,expected",
    [
        (_FakeServer1xNoValidation, True),   # mcp 1.2-1.9: we validate, it catches
        (_FakeServer1xValidating, False),    # mcp 1.10+: the SDK validates
        (_FakeServer2x, False),              # mcp 2.x: `errored` carries the flag
    ],
)
def test_serve_wires_the_refusal_channel_from_the_policies(
    monkeypatch: pytest.MonkeyPatch, fake_server: type, expected: bool
) -> None:
    """`_serve` itself must pass the derived flag, not a hardcoded constant.

    Reads the kwargs `_serve`'s dispatch closure actually forwards. Without
    this, a fix that taught `_dispatch_tool` to raise while leaving `_serve`'s
    call site at the `False` default would leave every wire test above green
    (they build their own dispatch closure) and the real server still broken.

    PARAMETRIZED OVER ALL THREE GENERATIONS on purpose, and against a
    hardcoded `expected` rather than re-deriving it from the policy fns.
    Checking only the installed SDK cannot kill a pinned literal: on mcp
    1.10+/2.x the correct answer IS False, so `raise_validation_refusal=False`
    matches — and `= _validate_input_for_sdk()` (dropping the generation half,
    which would raise on 2.x and turn a refusal into a JSON-RPC protocol
    error) matches too. Only the 1.2-1.9 row separates them. Re-deriving the
    expectation would likewise let an identical bug in both places agree.
    """
    monkeypatch.setattr(srv, "Server", fake_server)
    calls = _run_serve_capturing_dispatch(monkeypatch)
    assert calls, "the harness must have captured a dispatch call"
    assert calls[0].get("raise_validation_refusal") is expected


# ---------------------------------------------------------------------------
# The validator must be a DECLARED dependency, not one inherited from `mcp`
#
# `test_missing_jsonschema_fails_closed` proves the shim refuses the call when
# jsonschema is absent. That is the right behaviour for an anomaly — but it is
# a catastrophic default for a SUPPORTED install: the server starts, lists
# tools, and refuses every single tool call.
#
# `mcp` only began requiring jsonschema at 1.10.0 (verified against PyPI
# metadata for 1.2.0, 1.9.0 and 1.10.0), so while the floor was 1.2.0 the
# transitive dependency left the whole `mcp>=1.2,<1.10` range with a server
# that could not call a tool. The floor is now >=2.1.1, which does pull
# jsonschema — but the shim still imports it directly and fails closed, so the
# declaration stays rather than being borrowed from the SDK.
# ---------------------------------------------------------------------------


def _declared_dependencies() -> list[str]:
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[2]
    with open(root / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def test_jsonschema_is_a_direct_dependency() -> None:
    """The shim imports jsonschema for every known tool call, so it must be
    declared here rather than borrowed from whatever `mcp` happens to pull."""
    deps = _declared_dependencies()
    assert any(d.split(">=")[0].split("[")[0].strip() == "jsonschema" for d in deps), (
        "server.py's compat shim imports `jsonschema` for every known tool "
        "call and FAILS CLOSED when it is missing, so it must be a direct "
        f"dependency. Declared dependencies: {deps}"
    )


def test_jsonschema_dependency_is_declared_independently_of_the_mcp_floor() -> None:
    """The floor is now >=2.1.1, which DOES pull jsonschema transitively, so
    the direct declaration is no longer the only thing supplying it.

    It stays load-bearing anyway: 2.x does not self-validate, so the shim
    imports jsonschema itself for every known tool call and fails closed
    without it. Borrowing it from the SDK would leave our validation hostage
    to a transitive dep the SDK is free to drop. This asserts the floor is
    what the comment above claims, so that reasoning is checked rather than
    assumed.
    """
    deps = _declared_dependencies()
    mcp_spec = next(d for d in deps if d.startswith("mcp"))
    assert ">=2.1.1" in mcp_spec, (
        "mcp floor changed — re-check the jsonschema reasoning above against "
        f"the new floor's own dependencies. Spec: {mcp_spec!r}"
    )


# ---------------------------------------------------------------------------
# THE GENERATION FORK: a handled BiError must report isError=True on BOTH SDKs
#
# Previously the 2.x path read `errored` into `CallToolResult.isError` while
# the 1.x path returned the error payload as ordinary content, so the decorator
# shipped isError=False. A client therefore saw DIFFERENT error semantics
# depending on which SDK happened to resolve: on 1.x every "BI said no" —
# camera not found, auth refused, BI unreachable — arrived indistinguishable
# from a success whose JSON happened to contain an "error" key.
#
# Unified on isError=True: a call that failed reports as failed. 1.x reaches
# the flag by raising `_ShimToolError` into its decorator, the same channel
# `_ShimValidationError` uses and the only one both decorator vintages share
# (1.2-1.9 mangle a returned `CallToolResult`; only 1.10+ honour it).
# ---------------------------------------------------------------------------


def _bierror_tools() -> dict[str, Any]:
    """A tool that raises a handled BiError, as a real BI failure would."""
    from bi_mcp.errors import BiNotFound

    def _fail(_client, _args):
        raise BiNotFound("camera not found")

    return {"bi_boom": _fail}


def _bierror_tool_list() -> list[Any]:
    from mcp.types import Tool
    return [Tool(name="bi_boom", description="d", inputSchema={"type": "object"})]


def test_bierror_is_flagged_on_the_2x_policy() -> None:
    """2.x carries the flag in the `errored` out-param, without raising."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_boom", {}, tools=_bierror_tools(), client=object(),
            errored=errored, raise_tool_failure=False,
        )
    )
    assert bool(errored) is True, "2.x reads isError off this flag"
    assert json.loads(blocks[0].text)["kind"] == "not_found"


def test_bierror_raises_the_shim_error_on_the_1x_policy() -> None:
    """1.x has no out-param channel, so the failure must leave as a raise.

    The payload must survive intact: the decorator renders `str(exc)` into the
    content block, so anything re-derived here would drift from the 2.x text.
    """
    errored: list[bool] = []
    with pytest.raises(srv._ShimToolError) as excinfo:
        asyncio.run(
            srv._dispatch_tool(
                "bi_boom", {}, tools=_bierror_tools(), client=object(),
                errored=errored, raise_tool_failure=True,
            )
        )
    assert json.loads(str(excinfo.value))["kind"] == "not_found"
    assert bool(errored) is True, "the flag is set even on the raising path"


def test_bierror_text_is_byte_identical_across_the_two_channels() -> None:
    """Same failure, same wire text — only the flag's carrier differs.

    Guards the drift this whole shim exists to prevent: two generations must
    not describe one BI failure two different ways.
    """
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_boom", {}, tools=_bierror_tools(), client=object(),
            errored=errored, raise_tool_failure=False,
        )
    )
    with pytest.raises(srv._ShimToolError) as excinfo:
        asyncio.run(
            srv._dispatch_tool(
                "bi_boom", {}, tools=_bierror_tools(), client=object(),
                raise_tool_failure=True,
            )
        )
    assert blocks[0].text == str(excinfo.value)


@_needs_1x_decorator
def test_bierror_reaches_the_1x_wire_as_iserror() -> None:
    """The fork, at the wire: what a client on a 1.x SDK actually receives.

    This is the assertion that would have caught the divergence. The
    `errored`-level tests above cannot see it — the 1.x wrapper passes no
    out-param, so the flag is set on a list nobody reads and only the
    decorator's own result tells the truth.
    """
    async def _dispatch(name, args, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools=_bierror_tools(), errored=errored,
        )

    is_error, text = _wire_text_1x(
        _dispatch, _bierror_tool_list(), "bi_boom", {}, sdk_validates=True,
    )
    assert is_error is True, (
        "a failed tool call reported isError=False is indistinguishable from "
        "a success whose payload happens to contain an 'error' key"
    )
    from bi_mcp.errors import BiNotFound
    assert json.loads(text) == BiNotFound("camera not found").to_dict()


@_needs_1x_decorator
def test_bierror_wire_flag_matches_the_pre_1_10_decorator_too() -> None:
    """The raise channel must work on BOTH decorator vintages.

    A returned `CallToolResult(isError=True)` would pass on 1.10+ and silently
    regress on 1.2-1.9, which do `content=list(results)` on it. Modelling the
    pre-1.10 shape here is what makes the choice of channel load-bearing.
    """
    async def _dispatch(name, args, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools=_bierror_tools(), errored=errored,
        )

    is_error, text = _wire_text_1x(
        _dispatch, _bierror_tool_list(), "bi_boom", {}, sdk_validates=False,
    )
    assert is_error is True
    assert json.loads(text)["error"] == "camera not found"


@_needs_1x_decorator
def test_success_is_still_not_flagged_on_the_1x_wire() -> None:
    """The unification must not become a wall.

    Without this, making `_tool_failure` raise unconditionally — flag
    everything, always isError=True — would leave every assertion above green.
    """
    async def _dispatch(name, args, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools={"bi_ok": lambda _c, _a: {"n": 1}}, errored=errored,
        )

    from mcp.types import Tool
    is_error, text = _wire_text_1x(
        _dispatch,
        [Tool(name="bi_ok", description="d", inputSchema={"type": "object"})],
        "bi_ok", {}, sdk_validates=True,
    )
    assert is_error is False
    assert json.loads(text) == {"n": 1}


@pytest.mark.parametrize(
    "fake_server,expected",
    [
        (_FakeServer1xNoValidation, True),   # mcp 1.2-1.9: the decorator catches
        (_FakeServer1xValidating, True),     # mcp 1.10+: still needs the raise
        (_FakeServer2x, False),              # mcp 2.x: `errored` carries the flag
    ],
)
def test_serve_wires_the_tool_failure_channel_from_the_generation(
    monkeypatch: pytest.MonkeyPatch, fake_server: type, expected: bool
) -> None:
    """`_serve` itself must pass the derived flag, not a hardcoded constant.

    Reads the kwargs `_serve`'s dispatch closure actually forwards, so a fix
    that taught `_dispatch_tool` to raise while leaving `_serve`'s call site
    at the `False` default cannot pass — the wire tests above build their own
    dispatch closure and would stay green with the real server still forked.

    The 1.10+ row is the load-bearing one: it separates the correct
    `_reraise_unhandled_for_sdk()` wiring from a plausible-looking
    `_validate_input_for_sdk() and ...`, which would silently leave every
    validating 1.x SDK — the common case — reporting BiErrors as successes.
    """
    monkeypatch.setattr(srv, "Server", fake_server)
    calls = _run_serve_capturing_dispatch(monkeypatch)
    assert calls, "the harness must have captured a dispatch call"
    assert calls[0].get("raise_tool_failure") is expected


# ---------------------------------------------------------------------------
# THE SAME FORK, ONE BRANCH LATER: the UNKNOWN-TOOL refusal
#
# The isError unification above covered handled `BiError`s but missed the
# unknown-tool branch, which kept the pre-fix shape: `_mark_error(errored)`
# plus a returned block. On 2.x that is isError=True; on 1.x there is no
# out-param, so the block shipped as isError=False — a refused call reported
# as a success whose payload happens to contain an "error" key, exactly the
# divergence the unification closed everywhere else.
#
# The branch IS reachable: the low-level SDK forwards a call naming an
# unlisted tool to the handler rather than rejecting it itself, which is what
# the wire tests below drive.
#
# Fixed by routing it through the SAME `_tool_failure` exit the BiError branch
# uses, so `raise_tool_failure` picks the channel and no second mechanism
# exists to drift.
# ---------------------------------------------------------------------------


def test_unknown_tool_is_flagged_on_the_2x_policy() -> None:
    """2.x is unchanged: the flag rides the `errored` out-param."""
    errored: list[bool] = []
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_nope", {}, tools={}, client=object(),
            errored=errored, raise_tool_failure=False,
        )
    )
    assert bool(errored) is True, "2.x reads isError off this flag"
    assert json.loads(blocks[0].text) == {
        "error": "unknown tool: bi_nope", "kind": "bad_request"
    }


def test_unknown_tool_raises_the_shim_error_on_the_1x_policy() -> None:
    """1.x has no out-param channel, so the refusal must leave as a raise."""
    errored: list[bool] = []
    with pytest.raises(srv._ShimToolError) as excinfo:
        asyncio.run(
            srv._dispatch_tool(
                "bi_nope", {}, tools={}, client=object(),
                errored=errored, raise_tool_failure=True,
            )
        )
    assert json.loads(str(excinfo.value)) == {
        "error": "unknown tool: bi_nope", "kind": "bad_request"
    }
    assert bool(errored) is True, "the flag is set even on the raising path"


def test_unknown_tool_text_is_byte_identical_across_the_two_channels() -> None:
    """Same refusal, same wire text — only the flag's carrier differs."""
    blocks = asyncio.run(
        srv._dispatch_tool(
            "bi_nope", {}, tools={}, client=object(), raise_tool_failure=False,
        )
    )
    with pytest.raises(srv._ShimToolError) as excinfo:
        asyncio.run(
            srv._dispatch_tool(
                "bi_nope", {}, tools={}, client=object(), raise_tool_failure=True,
            )
        )
    assert blocks[0].text == str(excinfo.value)


@_needs_1x_decorator
@pytest.mark.parametrize("sdk_validates", [True, False])
def test_unknown_tool_reaches_the_1x_wire_as_iserror(sdk_validates: bool) -> None:
    """The fix, at the wire: what a client on a 1.x SDK actually receives.

    Both decorator vintages, because the raise channel is the only one they
    share (1.2-1.9 mangle a returned `CallToolResult`; only 1.10+ honour it).
    The `errored`-level tests above cannot see this — the 1.x wrapper passes
    no out-param, so the flag is set on a list nobody reads.
    """
    async def _dispatch(name, args, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools={"bi_ok": lambda _c, _a: {"n": 1}}, errored=errored,
        )

    from mcp.types import Tool
    is_error, text = _wire_text_1x(
        _dispatch,
        [Tool(name="bi_ok", description="d", inputSchema={"type": "object"})],
        "bi_nope", {}, sdk_validates=sdk_validates,
    )
    assert is_error is True, (
        "an unknown-tool refusal reported isError=False is indistinguishable "
        "from a success whose payload happens to contain an 'error' key"
    )
    assert json.loads(text) == {
        "error": "unknown tool: bi_nope", "kind": "bad_request"
    }


@_needs_1x_decorator
def test_the_unknown_tool_wire_flag_matches_a_known_tool_failure() -> None:
    """The two branches must agree, which is the whole point of one exit.

    A client cannot be told "this failed" for a BI error and "this succeeded"
    for a refusal to dispatch at all.
    """
    async def _dispatch(name, args, errored=None):
        return await _dispatch_as_serve_would(
            name, args, tools=_bierror_tools(), errored=errored,
        )

    bi_flag, _ = _wire_text_1x(
        _dispatch, _bierror_tool_list(), "bi_boom", {}, sdk_validates=True,
    )
    unknown_flag, _ = _wire_text_1x(
        _dispatch, _bierror_tool_list(), "bi_nope", {}, sdk_validates=True,
    )
    assert unknown_flag is bi_flag is True
