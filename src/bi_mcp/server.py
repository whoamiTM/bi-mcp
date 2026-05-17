"""MCP (stdio) server exposing bi-mcp tools to Claude Code.

Tools are auto-discovered from ``bi_mcp.tools.tools_<domain>`` modules via
``tools/registry.py``. Each tool is registered with a name, description,
JSON Schema for arguments, and MCP safety annotations.

The dispatch fn is sync (Blue Iris's HTTP API isn't streaming-friendly);
we offload to a thread so we don't block the asyncio loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import BiClients, from_env
from .errors import BiError
from .logging_setup import get_logger, setup_logging


async def _serve() -> None:
    # Load .env BEFORE importing bi_mcp.tools — the tools package runs
    # auto-discovery at import time and reads BI_MCP_ALLOW_MUTATIONS to decide
    # whether to register the mutation module.
    load_dotenv()
    setup_logging()
    log = get_logger()

    from .tools import (  # noqa: E402 — deliberate post-load_dotenv import
        TOOL_ANNOTATIONS,
        TOOL_DESCRIPTIONS,
        TOOL_SCHEMAS,
        TOOLS,
        mutations_enabled,
    )

    server: Server = Server("bi-mcp")
    client: BiClients = from_env()
    log.info(
        "bi-mcp server starting; BI endpoint=%s:%s admin=%s mutations=%s tools=%d",
        client.read.host,
        client.read.port,
        "yes" if client.admin is not None else "no",
        "enabled" if mutations_enabled() else "disabled",
        len(TOOLS),
    )
    # BI version is logged lazily on the first successful tool call — see
    # `call_tool` below. Logging in eagerly at startup would block the MCP
    # `initialize` handshake on BI being reachable, which turns a transient
    # BI outage into a broken server startup rather than a per-tool failure.
    version_logged = False

    @server.list_tools()
    async def list_tools() -> list[Tool]:
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

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        nonlocal version_logged
        args = arguments or {}
        log.debug("MCP call tool=%s", name)
        if name not in TOOLS:
            payload = {"error": f"unknown tool: {name}", "kind": "bad_request"}
            return [TextContent(type="text", text=json.dumps(payload))]
        try:
            result = await asyncio.to_thread(TOOLS[name], client, args)
            if not version_logged and client.bi_version:
                # First successful call has populated login_data; log the BI
                # version once and never again for this process.
                log.info("Connected to Blue Iris version=%s", client.bi_version)
                version_logged = True
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
        except BiError as e:
            log.info("tool=%s failed: kind=%s msg=%s", name, e.kind, e)
            return [TextContent(type="text", text=json.dumps(e.to_dict()))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def serve_main() -> int:
    try:
        asyncio.run(_serve())
        return 0
    except KeyboardInterrupt:
        return 0
