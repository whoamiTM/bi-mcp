"""MCP (stdio) server exposing the 10 Blue Iris tools to Claude Code.

The server is registered as one MCP tool per entry in ``tools.TOOLS``.
Each tool:

  * reads ``arguments`` (a dict of named params)
  * calls the dispatch function with a shared ``BiClient``
  * returns either the shaped result (JSON-encoded) or a structured error
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
from .tools import TOOL_DESCRIPTIONS, TOOLS


def _input_schema_for(name: str) -> dict[str, Any]:
    """Best-effort JSON Schema for each tool's arguments.

    Schemas are intentionally permissive — extra keys are allowed so callers
    (Claude) don't get stuck if a parameter name changes.
    """
    common = {
        "raw": {
            "type": "boolean",
            "description": "If true, return the raw Blue Iris JSON instead of the shaped view.",
        },
    }
    schemas: dict[str, dict[str, Any]] = {
        "bi_status": {"type": "object", "properties": {**common}, "additionalProperties": True},
        "bi_session_info": {"type": "object", "properties": {**common}, "additionalProperties": True},
        "bi_cameras": {
            "type": "object",
            "properties": {
                **common,
                "limit": {"type": "integer", "description": "Cap number of cameras returned."},
            },
            "additionalProperties": True,
        },
        "bi_camera_config": {
            "type": "object",
            "properties": {
                **common,
                "short": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
            },
            "required": ["short"],
            "additionalProperties": True,
        },
        "bi_log": {
            "type": "object",
            "properties": {
                **common,
                "level": {"type": "integer", "description": "0=info, 1=warning, 2=error."},
                "limit": {"type": "integer", "description": "Max entries (default 100)."},
            },
            "additionalProperties": True,
        },
        "bi_alerts": {
            "type": "object",
            "properties": {
                **common,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (required). Use 'Index' for all cameras.",
                },
                "startdate": {"type": "integer", "description": "Unix epoch start."},
                "enddate": {"type": "integer", "description": "Unix epoch end."},
                "view": {
                    "type": "string",
                    "description": "Database view filter, e.g. 'people', 'vehicles', 'flagged'.",
                },
                "search": {"type": "string", "description": "Memo substring filter."},
                "limit": {"type": "integer", "description": "Max alerts (default 50)."},
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        "bi_alert_tracks": {
            "type": "object",
            "properties": {
                **common,
                "path": {"type": "string", "description": "Alert path/identifier from bi_alerts."},
            },
            "required": ["path"],
            "additionalProperties": True,
        },
        "bi_clip_info": {
            "type": "object",
            "properties": {
                **common,
                "path": {"type": "string", "description": "Clip path/identifier from bi_alerts."},
            },
            "required": ["path"],
            "additionalProperties": True,
        },
        "bi_timeline": {
            "type": "object",
            "properties": {
                **common,
                "camera": {"type": "string", "description": "Camera short name. Required."},
                "startdate": {"type": "integer", "description": "Unix epoch start."},
                "enddate": {"type": "integer", "description": "Unix epoch end."},
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        "bi_ptz_status": {
            "type": "object",
            "properties": {
                **common,
                "camera": {"type": "string", "description": "PTZ camera short name. Required."},
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
    }
    return schemas.get(name, {"type": "object", "additionalProperties": True})


async def _serve() -> None:
    load_dotenv()
    setup_logging()
    log = get_logger()

    server: Server = Server("bi-mcp")
    client: BiClients = from_env()
    log.info(
        "bi-mcp server starting; BI endpoint=%s:%s admin=%s",
        client.read.host,
        client.read.port,
        "yes" if client.admin is not None else "no",
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description=TOOL_DESCRIPTIONS.get(name, ""),
                inputSchema=_input_schema_for(name),
            )
            for name in TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        args = arguments or {}
        log.debug("MCP call tool=%s", name)
        if name not in TOOLS:
            payload = {"error": f"unknown tool: {name}", "kind": "bad_request"}
            return [TextContent(type="text", text=json.dumps(payload))]
        try:
            # The dispatch function is sync; offload so we don't block the loop.
            result = await asyncio.to_thread(TOOLS[name], client, args)
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
