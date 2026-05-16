"""Entry point: dispatch between MCP stdio server and CLI mode.

  * No args               -> run the MCP server over stdio (the Claude Code path)
  * Any args              -> hand off to the terminal CLI
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        from .server import serve_main
        return serve_main()
    from .cli import cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
