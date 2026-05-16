"""Terminal CLI for bi-mcp — used for debugging without involving MCP.

Usage:
    bi-mcp-server check                       # verify connectivity + auth
    bi-mcp-server <tool_name> [--key=value]   # invoke any tool directly
    bi-mcp-server --list                      # list all tools

The same dispatch table that powers the MCP server is used here, so behaviour
matches one-to-one with what Claude sees.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from dotenv import load_dotenv

from .client import from_env
from .errors import BiError
from .logging_setup import setup_logging
from .tools import TOOLS, TOOL_DESCRIPTIONS


def _parse_kv_args(argv: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tok in argv:
        if tok.startswith("--"):
            tok = tok[2:]
        if "=" not in tok:
            out[tok] = True
            continue
        k, v = tok.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def _print_help() -> None:
    print("bi-mcp-server — Blue Iris MCP server / CLI")
    print()
    print("USAGE")
    print("  bi-mcp-server                       run the MCP server (stdio)")
    print("  bi-mcp-server check                 verify connectivity + auth")
    print("  bi-mcp-server <tool> [--key=value]  invoke a tool directly")
    print("  bi-mcp-server --list                list all available tools")
    print()
    print("TOOLS")
    for name in sorted(TOOLS):
        print(f"  {name:20}  {TOOL_DESCRIPTIONS.get(name, '')}")


def cli_main(argv: list[str]) -> int:
    load_dotenv()
    setup_logging()

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    if argv[0] == "--list":
        for name in sorted(TOOLS):
            print(name)
        return 0

    if argv[0] == "check":
        try:
            client = from_env()
            data = client.login()
            version = data.get("version", "unknown")
            cams = client.call("camlist")
            ncams = len(cams) if isinstance(cams, list) else 0
            print(
                f"OK — connected to Blue Iris {version} at {client.host}:{client.port}, "
                f"{ncams} cameras found"
            )
            return 0
        except BiError as e:
            print(f"FAIL [{e.kind}] {e}", file=sys.stderr)
            print(f"hint: {e.hint}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"FAIL [unexpected] {type(e).__name__}: {e}", file=sys.stderr)
            return 2

    tool_name = argv[0]
    if tool_name not in TOOLS:
        print(f"unknown tool: {tool_name}", file=sys.stderr)
        print("run with --list to see available tools", file=sys.stderr)
        return 1

    args = _parse_kv_args(argv[1:])
    try:
        client = from_env()
        result = TOOLS[tool_name](client, args)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except BiError as e:
        print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def main() -> int:
    return cli_main(sys.argv[1:])
