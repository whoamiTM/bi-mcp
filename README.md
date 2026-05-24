# bi-mcp — Blue Iris MCP Server

A small Python [Model Context Protocol](https://modelcontextprotocol.io) server
that lets Claude (or any MCP client) inspect — and optionally drive — a running
Blue Iris NVR install in real time. You describe an issue; Claude calls the
tools it needs (`bi_get_status`, `bi_get_camera_config`, `bi_list_alerts`,
`bi_list_log`, `bi_get_reg`, …) and tells you what to change, citing the Blue
Iris manual.

**16 read-only tools** by default, plus **6 mutating tools**
(`bi_trigger_camera`, `bi_set_ptz_preset`, `bi_set_profile`, `bi_export_clip`,
`bi_update_record`, `bi_set_camera`) that register only when
`BI_MCP_ALLOW_MUTATIONS=1`. MIT-licensed.

For agents (LLMs working through this server): read [AGENTS.md](AGENTS.md) —
that's the canonical operating manual with tool routing, mutation safety
rules, and the gap between what the BI JSON API exposes and what the `.reg`
parser is for.

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

Restart Claude Code. The `bi_*` tools should now appear in your tool list (16
read tools by default, 22 if `BI_MCP_ALLOW_MUTATIONS=1`).

## Verify it works

Three smoke tests, in order of increasing involvement:

1. **CLI connectivity check**
   ```bash
   bi-mcp-server check
   ```
   Expected: `OK — connected to Blue Iris 5.x.y.z at HOST:PORT, N cameras found`

2. **CLI tool call**
   ```bash
   bi-mcp-server bi_list_cameras
   bi-mcp-server bi_get_camera_config short=SecCam_3
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

## Breaking change: tool rename (v0.2.0)

The 10 read tools shipped in v0.1.0 were renamed to a consistent
`bi_<verb>_<noun>` convention in v0.2.0. **No aliases are kept** — old
callers will see `unknown tool` errors. Update any saved prompts, MCP
client tool-name caches, or shell scripts accordingly:

| Old name (v0.1.0)    | New name (v0.2.0)        |
|----------------------|--------------------------|
| `bi_status`          | `bi_get_status`          |
| `bi_session_info`    | `bi_get_session`         |
| `bi_cameras`         | `bi_list_cameras`        |
| `bi_camera_config`   | `bi_get_camera_config`   |
| `bi_log`             | `bi_list_log`            |
| `bi_alerts`          | `bi_list_alerts`         |
| `bi_alert_tracks`    | `bi_get_alert_tracks`    |
| `bi_clip_info`       | `bi_get_clip_info`       |
| `bi_timeline`        | `bi_get_timeline`        |
| `bi_ptz_status`      | `bi_get_ptz_status`      |

The rename is deliberate — naming consistency across the now-22-tool surface
matters more than one-time backward compatibility for a pre-1.0 server. If
that tradeoff doesn't work for your install, stay on v0.1.0.

## Tool reference

16 read tools register by default. The 6 mutating tools register only when
`BI_MCP_ALLOW_MUTATIONS=1`. Pass `raw=true` on any tool to get the unshaped
Blue Iris JSON.

For the canonical reference with routing tables, error taxonomy, and
mutation-safety rules, see [AGENTS.md](AGENTS.md).

| Tool | Backing BI cmd | Admin? | Mutating? | Purpose |
|---|---|:--:|:--:|---|
| `bi_get_status` | `status` | | | Active profile, schedule, CPU/RAM/disk, uptime, DIO, warnings. |
| `bi_get_session` | `login` data | | | BI version/capabilities, available profiles/schedules/streams. |
| `bi_get_sysconfig` | `sysconfig` | ✓ | | Archive/schedule/manrecsec + any DIO/MQTT inline. |
| `bi_list_cameras` | `camlist` | | | All cameras and groups: online state, trigger counts, stream health. |
| `bi_get_camera_config` | `camconfig`/`camlist` | (deep) | | Per-camera config (deep w/ admin, shallow w/o). |
| `bi_get_camera_motion_config` | `camconfig` | ✓ | | Live `setmotion` + `setpost` subtrees — current sensitivity/contrast/breaktime/etc. without `.reg` staleness. AI thresholds NOT included. |
| `bi_list_alerts` | `alertlist` | | | Recent alerts with AI memo, classification, zones, clip path. |
| `bi_get_alert_tracks` | `tracks` | | | AI per-frame bounding boxes inside one alert. |
| `bi_get_clip_info` | `clipstats` | | | Forensic detail for one clip. |
| `bi_list_clips` | `cliplist` | | | Recorded clip inventory; complementary to `bi_list_alerts`. |
| `bi_get_timeline` | `timeline` | | | 24-hour activity buckets for a camera. |
| `bi_get_ptz_status` | `ptz` (query) | | | Current PTZ position, preset list, lock state. |
| `bi_list_log` | `log` | ✓ | | Recent BI system log entries. |
| `bi_get_reg` | (file parser) | | | Parse `.reg` camera export — trigger zones, AI thresholds, per-preset flags. |
| `bi_get_actionset` | (file parser) | | | Decoded semantic view of `Alerts\OnTrigger` / `OnReset` action rows. |
| `bi_audit_actions` | (file parser) | | | Cross-camera drift report — flags action-row fields that deviate from the cohort majority. |
| `bi_trigger_camera` | `trigger` | ✓ | ✓ | Fire a synthetic motion trigger (mutations flag). |
| `bi_set_ptz_preset` | `ptz` (cmd) | | ✓ | Recall a PTZ preset 1-20 (mutations flag). |
| `bi_set_profile` | `status` (set) | ✓ | ✓ | Switch active profile (mutations flag). |
| `bi_export_clip` | `export` | ✓ | ✓ | Async MP4/AVI/WMV export from a clip range (modes: create / status / list). Requires BI user `clipcreate=true`. |
| `bi_update_record` | `update` | | ✓ | Set memo (≤35 chars) and/or flag bits on one alert or clip @record. Auto-preserves `flagged` on memo-only writes. |
| `bi_set_camera` | `camconfig` | ✓ | ✓ | 10 ops: rename, hide, enable, audio, output, manrec, pause, profile+lock, reset, reboot. All verify post-write. |

### Enabling mutating tools

Set `BI_MCP_ALLOW_MUTATIONS=1` in `.env` (or in the MCP config `env` block).
With the flag off, the mutating tools are not registered at all — the MCP
tool list stays clean. Read [AGENTS.md § Mutation patterns](AGENTS.md) before
flipping the flag.

### `bi_get_reg` and the `.reg` parser

`bi_get_reg` parses Blue Iris's binary `.reg` camera exports to surface
settings the JSON API doesn't expose (trigger zone polygons, per-class AI
confidence thresholds, per-preset alert-skip flags, ONVIF event handlers,
alert action definitions). It expects:

- A `.reg-venv/` directory in the launch CWD with `python-registry` installed,
  OR `BI_MCP_REG_VENV_PYTHON` pointing at any Python interpreter that has it.
  The default probe is platform-aware: on POSIX it looks for
  `.reg-venv/bin/python3`, on Windows for `.reg-venv\Scripts\python.exe`.
- A `cam settings/` directory in the launch CWD with `<short>.reg` exports,
  OR `BI_MCP_REG_DIR` pointing at the directory.

To create the venv:
- POSIX: `python3 -m venv .reg-venv && .reg-venv/bin/pip install python-registry`
- Windows: `python -m venv .reg-venv && .reg-venv\Scripts\pip install python-registry`

Both defaults resolve relative to the current working directory at call time,
not the install location — so they work correctly whether bi-mcp is run from
an editable checkout, a wheel, or via `uvx`. Camera short names are validated
(`[A-Za-z0-9_-]+`) before being composed into a path, so a malformed name
can't escape the configured directory.

Re-export a camera from BI any time you tune settings (right-click camera →
Camera settings → Copy/import → Export). Files older than 7 days trigger a
staleness warning in the tool's response.

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
- For `bi_get_camera_config short=…`: the value must match a camera's *short name*, not its display name. Run `bi_list_cameras` to see the list.

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

**Create a dedicated low-privilege Blue Iris user** for the read tools
(BI → Settings → Users → +):

| Setting | Value | Why |
|---|---|---|
| Access | Local + LAN | Required for the server to authenticate. |
| Admin | unchecked | Read tools don't need it; admin-gated reads use a separate user (below). |
| Change profile | unchecked | `bi_set_profile` routes through the admin user. |
| PTZ | **checked** | Needed for `bi_get_ptz_status` and `bi_set_ptz_preset`. |
| Audio | unchecked | Not used. |
| Clips | **checked** | Needed for `bi_list_alerts`, `bi_get_clip_info`, `bi_get_alert_tracks`, `bi_get_timeline`, `bi_list_clips`. |
| Clip create | **checked** *only* if you enable `bi_export_clip` | Required by the BI `export` cmd. Leave unchecked otherwise. |
| Camera groups | (tick all you want Claude to see) | |

`.env` is gitignored. Never commit it. If you commit credentials by accident,
delete the user in Blue Iris immediately and create a new one.

### Admin-gated tools and the two-user setup

Blue Iris gates several JSON cmds behind admin, so the recommended pattern is
a **two-user setup**:

- `BI_USER` / `BI_PASS` — low-privilege account (PTZ + Clips), used for all
  read tools that don't need admin.
- `BI_ADMIN_USER` / `BI_ADMIN_PASS` — a dedicated admin account, used only
  for the admin-gated cmds (marked ✓ in the *Admin?* column of the tool table
  above). Covers `bi_get_sysconfig`, `bi_list_log`, the deep path of
  `bi_get_camera_config`, `bi_get_camera_motion_config`, `bi_trigger_camera`,
  `bi_set_profile`, `bi_export_clip`, and `bi_set_camera` (all ops).

If only the read user is configured, admin-gated tools degrade gracefully
(deep `bi_get_camera_config` falls back to the shallow `camlist` view; other
admin-gated tools raise a clear `admin_required` error). See [AGENTS.md
§ Mutation patterns](AGENTS.md) before enabling the mutating tools.

## License

MIT — see [LICENSE](LICENSE).

Contributions welcome. Open an issue on GitHub before sending a large PR so we
can agree on shape.
