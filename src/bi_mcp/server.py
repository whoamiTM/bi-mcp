"""MCP (stdio) server exposing bi-mcp tools to Claude Code.

Tools are auto-discovered from ``bi_mcp.tools.tools_<domain>`` modules via
``tools/registry.py``. Each tool is registered with a name, description,
JSON Schema for arguments, and MCP safety annotations.

The dispatch fn is sync (Blue Iris's HTTP API isn't streaming-friendly);
we offload to a thread so we don't block the asyncio loop.

Supports both MCP SDK generations. The 1.x low-level API registered handlers
with ``@server.list_tools()`` / ``@server.call_tool()`` decorators; 2.x
(2026-07-28) removed those in favour of ``on_list_tools=`` / ``on_call_tool=``
constructor kwargs taking a request context and returning Result models.
``_serve`` detects which is available at runtime and wires up accordingly, so
one codebase works across the break. See `_register_handlers`.

Three behaviours are matched deliberately rather than by accident: an unhandled
tool exception is caught in `dispatch_tool` only on 2.x (1.x's decorator does
that for us, and catching it there too would downgrade isError to False),
arguments are validated against `inputSchema` in `dispatch_tool` on every SDK
that does not validate them itself — 2.x, and 1.2-1.9, whose decorator predates
the feature (see `_sdk_validates_input`) — and tool-level failures are flagged
`isError` on BOTH generations. 2.x reads the `errored` out-param into its
`CallToolResult`; 1.x has no such channel, so the shim raises into its
decorator instead (`_ShimToolError` for handled BiErrors,
`_ShimValidationError` for shim-side input-validation refusals). Either way a
call that failed reports as failed, so a client sees one contract regardless of
which SDK resolved.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from .client import BiClients, from_env
from .errors import BiError
from .logging_setup import get_logger, setup_logging

# Marker key a tool sets on its result dict to request an MCP image block.
# Value: {"data": <base64 str>, "mimeType": <str>}. The dispatcher splits it
# into an ImageContent block (rendered inline by clients like Claude Desktop)
# plus a TextContent block carrying the remaining metadata fields.
_MCP_IMAGE_KEY = "_mcp_image"

# Module-scope logger: `_dispatch_tool` runs outside `_serve`'s local `log`.
log = get_logger()


def _mark_error(errored: list[bool] | None) -> None:
    """Flip the caller's error out-param, if it supplied one."""
    if errored is not None:
        errored[:] = [True]


class _ShimValidationError(Exception):
    """A shim-side input-validation refusal, raised so 1.x flags it isError.

    WHY RAISE INSTEAD OF RETURN: the `errored` out-param only reaches the wire
    on the 2.x path (`_on_call_tool` reads it into `CallToolResult.isError`).
    The 1.x wrapper in `_register_handlers` calls `dispatch_tool(name, args)`
    with no out-param, so a *returned* refusal looked to the decorator like a
    normal result and shipped as isError=False — a rejected call reported as a
    SUCCESS whose text happened to read "Input validation error: ...". Raising
    is the only channel 1.x has: BOTH decorator vintages wrap the handler in
    `except Exception -> CallToolResult(text=str(e), isError=True)`.

    WHY NOT RETURN A `CallToolResult`: mcp 1.10+ honours one verbatim, but
    1.2-1.9 — the exact SDKs this fixes — do `content=list(results)` on it,
    which mangles the model. Raising is the one mechanism both vintages share.

    `str()` is the fully rendered wire text (prefix included), so the decorator
    reproduces the message byte-for-byte: nothing is re-derived on the 1.x side
    that could drift from the 2.x side.
    """


def _validation_refusal(
    text: str,
    errored: list[bool] | None,
    *,
    raise_refusal: bool,
) -> list[TextContent | ImageContent]:
    """Single exit for every shim-side validation refusal.

    Deliberately the ONLY place that chooses raise-vs-return, so the two SDK
    channels cannot drift: every refusal path calls this, and adding a new one
    inherits the correct behaviour for free. Always marks `errored` (2.x reads
    it); additionally RAISES where the shim validates on a 1.x SDK, whose
    decorator is the only thing that can set isError there.
    """
    _mark_error(errored)
    if raise_refusal:
        raise _ShimValidationError(text)
    return [TextContent(type="text", text=text)]


class _ShimToolError(Exception):
    """A handled `BiError`, raised so 1.x flags it isError like 2.x does.

    Same channel and same reason as `_ShimValidationError`: the `errored`
    out-param only reaches the wire on 2.x, so a *returned* error payload
    shipped as isError=False and a failed call was indistinguishable from a
    successful one. Raising is the mechanism BOTH decorator vintages share
    (1.2-1.9 mangle a returned `CallToolResult`; only 1.10+ honour it).

    `str()` is the fully rendered JSON payload, so the decorator's
    `except Exception -> CallToolResult(text=str(e), isError=True)` reproduces
    byte-for-byte what the 2.x path puts in its content block — the two
    generations differ in nothing but which layer sets the flag.
    """


def _tool_failure(
    text: str,
    errored: list[bool] | None,
    *,
    raise_failure: bool,
) -> list[TextContent | ImageContent]:
    """Single exit for a handled `BiError`, mirroring `_validation_refusal`.

    Always marks `errored` (2.x reads it); additionally RAISES on 1.x, whose
    decorator is the only thing that can set isError there.
    """
    _mark_error(errored)
    if raise_failure:
        raise _ShimToolError(text)
    return [TextContent(type="text", text=text)]


def _sdk_generation() -> str:
    """Return "1x" if the low-level decorator API is present, else "2x".

    MCP SDK 2.0 (2026-07-28) removed ``Server.list_tools`` /
    ``Server.call_tool`` decorators in favour of constructor kwargs. Probe the
    class rather than the package version so a future SDK that restores or
    re-removes the API is handled by what it actually exposes.
    """
    return "1x" if hasattr(Server, "list_tools") else "2x"


def _sdk_validates_input() -> bool:
    """Whether the installed SDK validates tool arguments for us.

    Probes for the FEATURE, not the generation. The decorator API and
    decorator-side validation are two different things that arrived four years
    apart: `@call_tool()` exists from 1.2, but `validate_input: bool = True`
    (the kwarg that turns on the pre-handler `jsonschema.validate`) only landed
    in mcp 1.10. `pyproject.toml` now pins ``mcp>=2.1.1,<3``, so 1.2-1.9 are no
    longer installs packaging selects — but the 1.x branch is retained for
    environments that already have such an SDK, where the decorator is present
    and validates NOTHING. Gating on the generation string called those "the
    SDK handles it" and let a typo'd payload through to `bi_set_camera`, which
    reboots real cameras. Probing the feature keeps that closed on any SDK,
    pinned or pre-existing.

    The kwarg's own presence in `Server.call_tool`'s signature is the feature's
    marker, so inspect that. Absent `Server.call_tool` entirely (2.x) there is
    no decorator and nothing validates either.

    We additionally require the parameter to DEFAULT to True, because that
    default is what `_register_handlers` actually gets: it calls
    ``server.call_tool()`` with no arguments. An SDK exposing the kwarg but
    defaulting it False would validate nothing despite advertising the feature,
    and we must validate ourselves there. (No such SDK is known to exist —
    1.10 through 1.27.1 all default True — so this arm is untested against a
    real release and defends by construction.)
    """
    call_tool = getattr(Server, "call_tool", None)
    if call_tool is None:
        return False
    try:
        param = inspect.signature(call_tool).parameters.get("validate_input")
    except (TypeError, ValueError):  # pragma: no cover - unsignaturable callable
        # Can't introspect => can't prove the SDK validates. Fail CLOSED and
        # validate ourselves; the cost is a duplicate check, not a live reboot.
        return False
    return param is not None and param.default is True


def _validate_input_for_sdk() -> bool:
    """Whether `_dispatch_tool` must validate arguments against inputSchema.

    True on any SDK that does not validate for us — 2.x (the `on_call_tool=`
    kwarg path has no equivalent) and 1.2-1.9 (decorator present, validation
    absent). False only where the decorator does it, so we never
    double-validate and never change its message.

    This matters because `bi_set_camera` (the only tool declaring
    ``additionalProperties: false``) silently ignores unrecognised keys, so an
    unvalidated typo'd extra property is dropped while the remaining valid op
    — reboot, reset — still executes on real hardware. Module-scope, like
    `_reraise_unhandled_for_sdk`, so the policy is reachable from tests
    without a live BI connection.
    """
    return not _sdk_validates_input()


def _reraise_unhandled_for_sdk() -> bool:
    """Whether `_dispatch_tool` should let a bare exception propagate.

    True only on 1.x, whose `@call_tool()` decorator catches it and yields
    isError=True; on 2.x nothing would catch it and it would surface as a
    JSON-RPC protocol error. Module-scope (rather than inline in `_serve`)
    so this one-line policy is reachable from tests — `_serve` itself needs
    a live BI connection.
    """
    return _sdk_generation() == "1x"


def _server_version() -> str:
    """bi-mcp's own version string for the MCP `serverInfo` block.

    Kept total: a version is cosmetic metadata and must never be the reason a
    server fails to start, so any import/attribute problem degrades to "" —
    exactly what 2.x would have used anyway.
    """
    try:
        from . import __version__
    except Exception:  # noqa: BLE001 # pragma: no cover - defensive
        return ""
    return __version__ if isinstance(__version__, str) else ""


def _register_handlers(
    server: "Server",
    build_tool_list: Callable[[], list[Tool]],
    dispatch_tool: Callable[[str, dict[str, Any] | None], Awaitable[list[Any]]],
) -> None:
    """Wire tool handlers onto a 1.x Server via its decorator API.

    2.x servers get their handlers at construction time instead (see
    `_build_server`); this is a no-op there.
    """
    if _sdk_generation() != "1x":
        return

    @server.list_tools()  # type: ignore[attr-defined]
    async def _list_tools() -> list[Tool]:
        return build_tool_list()

    @server.call_tool()  # type: ignore[attr-defined]
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[Any]:
        return await dispatch_tool(name, arguments)


def _build_server(
    build_tool_list: Callable[[], list[Tool]],
    dispatch_tool: Callable[[str, dict[str, Any] | None], Awaitable[list[Any]]],
) -> "Server":
    """Construct the Server for whichever SDK generation is installed.

    On 2.x the handlers are constructor kwargs and take a
    ``ServerRequestContext`` + params, returning ``ListToolsResult`` /
    ``CallToolResult`` instead of bare lists.
    """
    if _sdk_generation() == "1x":
        return Server("bi-mcp")

    from mcp.types import CallToolResult, ListToolsResult

    async def _on_list_tools(_ctx: Any, _params: Any = None) -> Any:
        return ListToolsResult(tools=build_tool_list())

    async def _on_call_tool(_ctx: Any, params: Any) -> Any:
        errored: list[bool] = []
        blocks = await dispatch_tool(params.name, params.arguments, errored)
        # `isError` marks a TOOL-level failure (as opposed to a protocol
        # error), letting a client tell "BI said no" from "the call worked"
        # without parsing the JSON payload.
        #
        # The two generations agree here: 1.x reaches the same flag by raising
        # `_ShimToolError` into its decorator (see `_tool_failure`), which is
        # the only channel it has. Only the mechanism differs — a failed call
        # reports as failed on both.
        return CallToolResult(content=blocks, isError=bool(errored))

    # 2.x's `version` defaults to "" and, unlike 1.x, is NOT backfilled — a
    # client's `initialize` reply advertised an empty serverVersion. Send
    # bi-mcp's OWN version rather than the SDK's: 1.x's backfill is the SDK
    # version ("1.27.1"), which tells a client nothing about which bi-mcp it is
    # talking to, and the serverInfo block is named for the server. Sourced
    # from `bi_mcp.__version__` rather than `importlib.metadata`, which reads
    # installed dist metadata and goes stale against an editable checkout
    # (measured here: metadata said 0.1.0 while the tree was 0.3.1).
    return Server(
        "bi-mcp",
        version=_server_version(),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


async def _dispatch_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    tools: Mapping[str, Any],
    client: Any,
    errored: list[bool] | None = None,
    on_first_success: Callable[[], None] | None = None,
    reraise_unhandled: bool = False,
    schemas: Mapping[str, Any] | None = None,
    raise_validation_refusal: bool = False,
    raise_tool_failure: bool = False,
) -> list[TextContent | ImageContent]:
    """Run one tool and render its result as MCP content blocks.

    Module-scope (not a closure inside `_serve`) so unit tests can drive it
    directly — the `except Exception` branch below is the highest-value line
    in the SDK port and was previously unreachable from tests.

    Handled `BiError`s always become an error content block and are always
    flagged isError on both generations — `raise_tool_failure` picks the
    channel (raise on 1.x, `errored` out-param on 2.x). A bare (non-
    `BiError`) exception is re-raised when `reraise_unhandled` is set and
    otherwise becomes an error block — see the branch's comment for why the
    two SDK generations want opposite treatment. `on_first_success` fires
    after the first successful call (used for the lazy BI-version log).

    `schemas` is passed only when the SDK does not validate for us (see
    `_validate_input_for_sdk`); when set, arguments are checked against the
    tool's advertised `inputSchema` before dispatch, matching what a
    validating 1.x decorator does for us.

    `raise_validation_refusal` routes those refusals out as
    `_ShimValidationError` instead of as content — set on the 1.x path, where
    raising is the only way to reach isError=True. See `_validation_refusal`.

    `raise_tool_failure` does the same for handled `BiError`s — and for the
    unknown-tool refusal, which the SDK forwards here rather than rejecting
    itself — via `_ShimToolError`. Set on ALL of 1.x (not just the
    non-validating vintages `raise_validation_refusal` covers): the tool has
    already run by then, so whether the decorator validates input is
    irrelevant to flagging its failure. See `_tool_failure`.
    """
    args = arguments or {}
    log.debug("MCP call tool=%s", name)
    if name not in tools:
        # Reachable: the low-level SDK forwards a call for an UNLISTED tool
        # straight to this handler rather than rejecting it itself.
        #
        # Routed through `_tool_failure` for the same reason a handled
        # `BiError` is: `_mark_error` alone only reaches the wire on 2.x, so
        # on 1.x this refusal shipped as isError=False — a rejected call
        # indistinguishable from a success whose payload happens to carry an
        # "error" key. `raise_tool_failure` picks the channel, so both
        # generations agree here exactly as they do below.
        payload = {"error": f"unknown tool: {name}", "kind": "bad_request"}
        return _tool_failure(
            json.dumps(payload),
            errored,
            raise_failure=raise_tool_failure,
        )
    if schemas is not None:
        # Set only when the SDK doesn't validate for us (see
        # `_sdk_validates_input`). The tool must NOT run if its arguments
        # don't validate:
        # `bi_set_camera` reboots/resets real cameras and silently ignores
        # keys it doesn't recognise, so an unvalidated typo would drop the
        # typo'd key and still execute the remaining op. Message text and
        # isError are matched to 1.x's decorator so both generations produce
        # the same wire result.
        schema = schemas.get(name)
        if schema is not None:
            try:
                import jsonschema  # direct dependency; see pyproject.toml
            except ImportError as e:  # pragma: no cover - direct dep
                # FAIL CLOSED. jsonschema is a DIRECT dependency of bi-mcp, so
                # this is near-unreachable — but if the validator is ever
                # missing we must refuse the call, never skip validation and
                # dispatch anyway: this guard is what stops a typo'd key from
                # reaching `bi_set_camera`, which reboots real cameras and
                # silently ignores keys it doesn't recognise.
                #
                # NOT inherited from `mcp`: that only started requiring
                # jsonschema at 1.10.0, and while this project's floor was
                # 1.2.0 relying on the SDK's transitive dep made every tool
                # call on a valid `mcp>=1.2,<1.10` install hit exactly this
                # branch. The floor is now 2.1.1, which does pull jsonschema —
                # but we declare it directly rather than depend on the SDK
                # continuing to.
                log.error("tool=%s cannot validate input: %s", name, e)
                return _validation_refusal(
                    f"Input validation error: {e}",
                    errored,
                    raise_refusal=raise_validation_refusal,
                )

            try:
                jsonschema.validate(instance=args, schema=schema)
            except jsonschema.ValidationError as e:
                log.info("tool=%s input validation failed: %s", name, e.message)
                return _validation_refusal(
                    f"Input validation error: {e.message}",
                    errored,
                    raise_refusal=raise_validation_refusal,
                )
            except jsonschema.SchemaError as e:
                # A MALFORMED SCHEMA — our bug, not the caller's. `validate()`
                # calls `check_schema()` internally and raises this, and it is
                # NOT a `ValidationError` subclass (they share only the private
                # `jsonschema.exceptions._Error` base), so without this branch
                # it escapes `_dispatch_tool` entirely. It is also raised
                # BEFORE the `try` below, so `reraise_unhandled` never sees it
                # either — on 2.x that means a JSON-RPC PROTOCOL error, which
                # breaks the "nothing escapes as a protocol error" contract.
                #
                # Caught separately rather than widening to `_Error` (private,
                # and would swallow future siblings unexamined) and rendered
                # WITHOUT the "Input validation error: " prefix, because that
                # is exactly what 1.x does: its decorator's `except
                # jsonschema.ValidationError` misses this too, so it falls to
                # the outer `except Exception -> _make_error_result(str(e))`.
                # Same class of outcome (fail closed, tool never runs), but a
                # deliberately different message so a server bug is not
                # misread as caller error.
                log.error("tool=%s has a malformed inputSchema: %s", name, e)
                return _validation_refusal(
                    str(e), errored, raise_refusal=raise_validation_refusal
                )
            except Exception as e:  # noqa: BLE001
                # BACKSTOP — deliberately a catch-all, not another named type.
                # `jsonschema.validate()` raises outside its own taxonomy in
                # several ways, and no list of types closes the hole:
                #   * `_WrappedReferencingError` (unresolvable/remote/bad-pointer
                #     `$ref`) derives from `referencing`'s `Unresolvable`, NOT
                #     from `jsonschema.exceptions._Error`;
                #   * `RecursionError` (self- or mutually-recursive `$ref`) and
                #     `TypeError` (a scalar where a schema was expected) are
                #     builtins, outside any jsonschema hierarchy entirely.
                # Two prior rounds were spent extending the type list; the bug
                # is the ESCAPE, not the enumeration, so this catches by
                # position instead. It must stay LAST so the two named branches
                # above keep their distinct wording.
                #
                # Cannot be folded into the `try` below by moving validation
                # there: that block honours `reraise_unhandled`, and a schema
                # failure must fail closed on BOTH generations (see
                # `test_malformed_schema_does_not_escape_under_the_1x_reraise_policy`).
                #
                # Rendered as bare `str(e)` with no "Input validation error: "
                # prefix, matching 1.x — its decorator validates inside the
                # same `try` as the tool call, so anything that is not a
                # `ValidationError` lands in its outer
                # `except Exception -> _make_error_result(str(e))`.
                log.error(
                    "tool=%s input validation raised %s: %s",
                    name, type(e).__name__, e,
                )
                return _validation_refusal(
                    str(e), errored, raise_refusal=raise_validation_refusal
                )
    try:
        result = await asyncio.to_thread(tools[name], client, args)
        if on_first_success is not None:
            on_first_success()
        return _result_to_content(result)
    except BiError as e:
        log.info("tool=%s failed: kind=%s msg=%s", name, e.kind, e)
        return _tool_failure(
            json.dumps(e.to_dict()),
            errored,
            raise_failure=raise_tool_failure,
        )
    except Exception as e:  # noqa: BLE001
        # Tools raise bare ValueError for arg validation (see tools_log).
        # The two generations need OPPOSITE handling here:
        #
        # 1.x (`reraise_unhandled=True`): its @call_tool decorator wraps the
        # handler in `except Exception -> _make_error_result(str(e))`, which
        # yields isError=True. Swallowing the exception here would rob the
        # decorator of that and downgrade a real failure to isError=False.
        # So let it propagate and keep 1.x's historical wire behaviour.
        #
        # 2.x: the constructor-kwarg path has no such safety net, so an
        # escaping exception becomes a JSON-RPC *protocol* error instead of a
        # tool error. Catch it and report it as a tool error via `errored`.
        log.info("tool=%s raised %s: %s", name, type(e).__name__, e)
        if reraise_unhandled:
            raise
        _mark_error(errored)
        return [TextContent(type="text", text=str(e))]


def _result_to_content(result: Any) -> list[Any]:
    """Convert a tool's return value into MCP content blocks.

    Default: one JSON TextContent block. If the result is a dict carrying the
    `_mcp_image` marker, emit an ImageContent block (so the image renders in
    image-aware clients) followed by a TextContent block of the other fields.
    """
    if isinstance(result, dict) and _MCP_IMAGE_KEY in result:
        img = result[_MCP_IMAGE_KEY]
        meta = {k: v for k, v in result.items() if k != _MCP_IMAGE_KEY}
        return [
            ImageContent(type="image", data=img["data"], mimeType=img["mimeType"]),
            TextContent(type="text", text=json.dumps(meta, indent=2, default=str)),
        ]
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _serve() -> None:
    # Load .env BEFORE importing bi_mcp.tools — the tools package runs
    # auto-discovery at import time and reads BI_MCP_ALLOW_MUTATIONS to decide
    # whether to register the mutation module.
    load_dotenv()
    setup_logging()
    # `log` is module-scope (see top of file) so `_dispatch_tool` shares it;
    # setup_logging() configures that same named logger in place.

    from .tools import (  # noqa: E402 — deliberate post-load_dotenv import
        TOOL_ANNOTATIONS,
        TOOL_DESCRIPTIONS,
        TOOL_SCHEMAS,
        TOOLS,
        mutations_enabled,
    )

    client: BiClients = from_env()
    log.info(
        "bi-mcp server starting; BI endpoint=%s:%s admin=%s mutations=%s tools=%d",
        client.read.host,
        client.read.port,
        "yes" if client.admin is not None else "no",
        "enabled" if mutations_enabled() else "disabled",
        len(TOOLS),
    )
    log.debug("MCP SDK generation detected: %s", _sdk_generation())
    # BI version is logged lazily on the first successful tool call — see
    # `dispatch_tool` below. Logging in eagerly at startup would block the MCP
    # `initialize` handshake on BI being reachable, which turns a transient
    # BI outage into a broken server startup rather than a per-tool failure.
    version_logged = False

    def build_tool_list() -> list[Tool]:
        tools_out: list[Tool] = []
        for name in TOOLS:
            kwargs: dict[str, Any] = {
                "name": name,
                "description": TOOL_DESCRIPTIONS.get(name, ""),
                "inputSchema": TOOL_SCHEMAS.get(name, {"type": "object", "additionalProperties": True}),
            }
            annotations = TOOL_ANNOTATIONS.get(name)
            if annotations:
                # The MCP `Tool` model accepts an `annotations` kwarg on
                # recent versions of the SDK. Older versions either:
                #   * raise TypeError if the constructor signature rejects
                #     unknown kwargs, or
                #   * raise pydantic.ValidationError if the model is in
                #     extra='forbid' mode and doesn't define `annotations`.
                # We catch both broadly so `list_tools()` always returns a
                # valid tool list — losing the annotation hint is better
                # than blanking the entire tool surface.
                try:
                    tools_out.append(Tool(**kwargs, annotations=annotations))
                    continue
                except Exception as e:  # noqa: BLE001
                    log.debug(
                        "Tool model rejected `annotations` kwarg for %s: %s; "
                        "falling back to annotation-free Tool().",
                        name,
                        e,
                    )
            tools_out.append(Tool(**kwargs))
        return tools_out

    async def dispatch_tool(
        name: str,
        arguments: dict[str, Any] | None,
        errored: list[bool] | None = None,
    ) -> list[TextContent | ImageContent]:
        """Bind the module-scope dispatcher to this server's tools + client."""
        nonlocal version_logged

        def _log_version_once() -> None:
            nonlocal version_logged
            if not version_logged and client.bi_version:
                # First successful call has populated login_data; log the BI
                # version once and never again for this process.
                log.info("Connected to Blue Iris version=%s", client.bi_version)
                version_logged = True

        return await _dispatch_tool(
            name,
            arguments,
            tools=TOOLS,
            client=client,
            errored=errored,
            on_first_success=_log_version_once,
            reraise_unhandled=_reraise_unhandled_for_sdk(),
            # None where the SDK's own decorator already validated. See
            # `_validate_input_for_sdk`.
            schemas=TOOL_SCHEMAS if _validate_input_for_sdk() else None,
            # Derived from the same two policies that pick `schemas` and
            # `reraise_unhandled`, so it cannot be set for a combination that
            # does not exist: raise only where WE validate AND the decorator
            # is there to catch (1.x). On 2.x `errored` already carries the
            # flag and raising would become a JSON-RPC protocol error.
            raise_validation_refusal=(
                _validate_input_for_sdk() and _reraise_unhandled_for_sdk()
            ),
            # Every 1.x, validating or not: by the time a BiError exists the
            # tool has already run, so the decorator's input validation has no
            # bearing on it. Same probe as `reraise_unhandled` — the decorator
            # is what turns a raise into isError there.
            raise_tool_failure=_reraise_unhandled_for_sdk(),
        )

    server: Server = _build_server(build_tool_list, dispatch_tool)
    _register_handlers(server, build_tool_list, dispatch_tool)

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve_main() -> int:
    try:
        asyncio.run(_serve())
        return 0
    except KeyboardInterrupt:
        return 0
