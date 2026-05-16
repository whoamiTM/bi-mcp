# bi-mcp — Blue Iris MCP Server

A small Python [Model Context Protocol](https://modelcontextprotocol.io) server
that lets Claude (or any MCP client) inspect a running Blue Iris NVR install in
real time. You describe an issue; Claude calls the tools it needs (`bi_status`,
`bi_camera_config`, `bi_alerts`, `bi_log`, …) and tells you what to change,
citing the Blue Iris manual.

10 read-only tools, ~700 LoC of Python, MIT-licensed.

---

## Prerequisites

- Blue Iris **5.x** with the **web server enabled** (Settings → Web server).
- A dedicated **low-privilege Blue Iris user** for this server (see
  [Security](#security)). Do **not** use your admin account.
- Python **3.10+** on the machine that runs the MCP server. The server can run
  on the Blue Iris box itself, on a separate machine on the LAN, or in WSL.
- (Recommended) [`uv`](https://github.com/astral-sh/uv) for the one-line install.

## Install

### For users (recommended)

If you have `uv`:

```bash
uvx --from git+https://github.com/whoamiTM/bi-mcp bi-mcp-server check
```

That fetches, builds, and runs the server once. Repeated runs reuse the cache.

### For developers / hackers

```bash
git clone https://github.com/whoamiTM/bi-mcp
cd bi-mcp
uv sync
uv run bi-mcp-server check
```

Or with plain pip:

```bash
git clone https://github.com/whoamiTM/bi-mcp
cd bi-mcp
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
bi-mcp-server check
```

## Configure

Copy `.env.example` to `.env` and fill it in:

```ini
BI_HOST=192.168.1.10        # LAN IP or hostname of your Blue Iris box
BI_PORT=81                  # Blue Iris web-server port
BI_USER=mcp-readonly        # the dedicated user you created in BI
BI_PASS=••••••••••          # that user's password
BI_MCP_DEBUG=0              # 1 to log to stderr + a rotating cache file
```

The server reads `.env` from the current working directory at startup. When
launched by Claude Code, "current working directory" is whatever directory
Claude Code is in — so either put `.env` there, or set the env vars in the
MCP-config `env` block (see next section).

## Register with Claude Code

Edit `~/.claude.json` (or whatever your platform's Claude Code config file is)
and add an entry under `mcpServers`:

```json
{
  "mcpServers": {
    "bi-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/whoamiTM/bi-mcp", "bi-mcp-server"],
      "env": {
        "BI_HOST": "192.168.1.10",
        "BI_PORT": "81",
        "BI_USER": "mcp-readonly",
        "BI_PASS": "••••••••••"
      }
    }
  }
}
```

Restart Claude Code. The 10 `bi_*` tools should now appear in your tool list.

## Verify it works

Three smoke tests, in order of increasing involvement:

1. **CLI connectivity check**
   ```bash
   bi-mcp-server check
   ```
   Expected: `OK — connected to Blue Iris 5.x.y.z at HOST:PORT, N cameras found`

2. **CLI tool call**
   ```bash
   bi-mcp-server bi_cameras
   bi-mcp-server bi_camera_config short=SecCam_3
   ```
   You should see shaped JSON for your cameras.

3. **MCP Inspector**
   ```bash
   npx @modelcontextprotocol/inspector uvx --from . bi-mcp-server
   ```
   Opens a browser UI where you can poke each tool individually. Useful when
   debugging schemas or response shapes.

If steps 1–3 pass, the server is good. Connect Claude Code and ask it about
your cameras.

## Tool reference

All tools are **read-only**. Pass `raw=true` on any tool to get the unshaped
Blue Iris JSON instead of the trimmed view.

| Tool | Backing BI cmd | Purpose |
|---|---|---|
| `bi_status` | `status` | Active profile, schedule, CPU/RAM/disk, uptime, DIO, warnings. |
| `bi_session_info` | `login` data | BI version, license, time zone, current user's capabilities, available profile/schedule/stream names. |
| `bi_cameras` | `camlist` | All cameras and groups: online state, trigger counts, stream health, last alert. |
| `bi_camera_config` | `camlist` filtered | Full config + current state for one camera by short name. |
| `bi_log` | `log` | Recent system log entries. Params: `level`, `limit`. **Requires admin** — see Security note. |
| `bi_alerts` | `alertlist` | Recent alerts with AI memo, object/confidence, zones, clip path. Params: `camera`, `startdate`, `enddate`, `limit`. |
| `bi_alert_tracks` | `tracks` | AI per-frame bounding boxes inside one alert. Param: `path`. |
| `bi_clip_info` | `clipstats` | Forensic detail for one clip: resolution, duration, profile/schedule/zones at trigger. Param: `path`. |
| `bi_timeline` | `timeline` | 24-hour activity buckets for a camera. Params: `camera`, `startdate`, `enddate`. |
| `bi_ptz_status` | `ptz` (query) | Current PTZ position, preset list, lock state. Param: `camera`. |

## Troubleshooting

**`unreachable`** — Cannot reach Blue Iris.
- Check `BI_HOST` and `BI_PORT` in `.env` (default port is 81).
- Confirm Blue Iris's web server is enabled: BI → Settings → Web server.
- From the same machine, try `curl -X POST -d '{"cmd":"login"}' http://HOST:PORT/json` — you should get a JSON response.

**`auth`** — Blue Iris rejected the login.
- Check `BI_USER` and `BI_PASS`.
- The user must have LAN access enabled in BI → Settings → Users.
- **Don't keep retrying with wrong creds** — Blue Iris will lock the account.

**`not_found`** — Requested camera/clip/alert doesn't exist.
- For `bi_camera_config short=…`: the value must match a camera's *short name*, not its display name. Run `bi_cameras` to see the list.

**Empty / weird responses** — pass `raw=true` to see what BI actually returned, then file an issue with the raw JSON so the shaper can be improved.

**Debug logging** — set `BI_MCP_DEBUG=1` in `.env` (or in the MCP config `env`).
Logs go to stderr and a rotating file under your platform's user-cache dir:
- Linux: `~/.cache/bi-mcp/server.log`
- macOS: `~/Library/Caches/bi-mcp/server.log`
- Windows: `%LOCALAPPDATA%\bi-mcp\server.log`

## Security

This server is designed to run **locally**, talking to a Blue Iris box on the
**same LAN**. It does not listen on a network port; it speaks stdio to one
MCP client at a time. It should not be exposed to the internet.

**Create a dedicated Blue Iris user** for it (BI → Settings → Users → +):

| Setting | Value | Why |
|---|---|---|
| Access | Local + LAN | Required for the server to authenticate. |
| Admin | unchecked | No need; server is read-only. |
| Change profile | unchecked | We don't change profiles. |
| PTZ | **checked** | Needed for `bi_ptz_status`. |
| Audio | unchecked | Not used. |
| Clips | **checked** | Needed for `bi_alerts`, `bi_clip_info`, `bi_alert_tracks`, `bi_timeline`. |
| Clip create | unchecked | We don't create or export. |
| Camera groups | (tick all you want Claude to see) | |

`.env` is gitignored. Never commit it. If you commit credentials by accident,
delete the user in Blue Iris immediately and create a new one.

### Why `bi_log` returns "Access denied" for the recommended user

Blue Iris gates the `log` JSON command behind admin. The non-admin user above
will see an `Access denied` error when calling `bi_log`. This is intentional:
the read-only contract of this server is more valuable than one tool. If you
genuinely need log access during diagnosis, paste the relevant log lines from
BI's Status window manually — or grant the MCP user admin and accept the
larger blast radius if `.env` ever leaks.

## License

MIT — see [LICENSE](LICENSE).

Contributions welcome. Open an issue on GitHub before sending a large PR so we
can agree on shape.
