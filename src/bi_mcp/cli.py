"""Terminal CLI for bi-mcp — debugging without involving MCP.

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

# Note: `bi_mcp.tools` is imported lazily inside cli_main() after load_dotenv()
# has run — auto-discovery reads BI_MCP_ALLOW_MUTATIONS at import time, so the
# dotenv side-effects must land first.


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


def _print_help(tools_map: dict, descriptions: dict, mutations_on: bool) -> None:
    print("bi-mcp-server — Blue Iris MCP server / CLI")
    print()
    print("USAGE")
    print("  bi-mcp-server                       run the MCP server (stdio)")
    print("  bi-mcp-server check                 verify connectivity + auth")
    print("  bi-mcp-server <tool> [--key=value]  invoke a tool directly")
    print("  bi-mcp-server --list                list all available tools")
    print()
    print(f"MUTATIONS: {'enabled' if mutations_on else 'disabled'} "
          "(BI_MCP_ALLOW_MUTATIONS)")
    print()
    print("TOOLS")
    for name in sorted(tools_map):
        print(f"  {name:24}  {descriptions.get(name, '')[:72]}")


def cli_main(argv: list[str]) -> int:
    # load_dotenv MUST run before importing bi_mcp.tools — auto-discovery
    # reads BI_MCP_ALLOW_MUTATIONS at import time.
    load_dotenv()
    setup_logging()

    from .tools import TOOL_DESCRIPTIONS, TOOLS, mutations_enabled  # noqa: E402

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_help(TOOLS, TOOL_DESCRIPTIONS, mutations_enabled())
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
            if client.admin is not None:
                try:
                    admin_data = client.admin_login()
                except BiError as e:
                    print(
                        f"FAIL [{e.kind}] admin user '{client.admin.user}' login failed: {e}",
                        file=sys.stderr,
                    )
                    print(f"hint: {e.hint}", file=sys.stderr)
                    return 1
                if not admin_data.get("admin"):
                    print(
                        f"FAIL — user '{client.admin.user}' logged in, but Blue "
                        f"Iris reports admin=false for this user. Admin-gated "
                        f"tools (bi_list_log, deep bi_get_camera_config, "
                        f"bi_get_sysconfig) will not work.",
                        file=sys.stderr,
                    )
                    print(
                        f"hint: enable Admin for '{client.admin.user}' in Blue "
                        f"Iris → Settings → Users, or point BI_ADMIN_USER at a "
                        f"user that already has it.",
                        file=sys.stderr,
                    )
                    return 1
            if client.admin is None:
                admin_status = "not set (admin-gated tools disabled)"
            elif client.admin is client.read:
                admin_status = f"primary user '{client.read.user}' has admin"
            else:
                admin_status = f"separate user '{client.admin.user}'"
            mut_status = "enabled" if mutations_enabled() else "disabled"
            print(
                f"OK — connected to Blue Iris {version} at "
                f"{client.read.host}:{client.read.port} as '{client.read.user}', "
                f"{ncams} cameras found; admin: {admin_status}; mutations: {mut_status}; "
                f"tools registered: {len(TOOLS)}"
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
