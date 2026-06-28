# bi-mcp: Blue Iris MCP Server

Ask Claude about your security cameras in plain English. It looks at your
live Blue Iris install and answers, instead of guessing. What cameras are
online, why an alert fired, what the AI saw, how a preset is configured.

## What is this?

[Blue Iris](https://blueirissoftware.com/) is a Windows NVR app for IP
security cameras. It records footage, runs motion and AI detection, and
manages PTZ cameras.

The [Model Context Protocol](https://modelcontextprotocol.io) is a
standard way for AI assistants like Claude to call tools on your own
computer.

bi-mcp is an MCP server that connects the two. Claude gets a set of
read-only tools (and optional control tools) for inspecting your Blue
Iris install. You talk to Claude, Claude talks to Blue Iris.

Some example questions you can ask once it's set up:

> What cameras are offline right now?
>
> Why did the front door camera fire an alert at 3:14 PM?
>
> Show me the trigger zones on SecCam_3.
>
> What's the current PTZ preset on the pan-tilt camera?

19 read-only tools register by default. 6 control tools (rename a
camera, switch profile, recall a PTZ preset, export a clip, and a couple
more) register only when you explicitly opt in by setting
`BI_MCP_ALLOW_MUTATIONS=1`. The project is MIT-licensed.

> **Compatibility note:** bi-mcp is built and tested against Blue Iris
> **5.x** (specifically 5.9.9.71). Blue Iris 6 is out but has not been
> tested. Some tools call undocumented BI endpoints whose response shapes
> may have changed in 6.x. If you run bi-mcp against Blue Iris 6 and
> something works (or breaks), open an issue so I can update this note.

---

## Prerequisites

You need three things before installing.

First, Blue Iris 5.x running on Windows with the web server enabled (see
the compatibility note above about Blue Iris 6). In Blue Iris that's
under Settings, then Web server, then check "Enabled".

Second, Python 3.10 or newer on the machine that will run bi-mcp. To
check what you have, open a terminal and run `python --version`. If it's
missing or too old, install from
[python.org](https://www.python.org/downloads/).

Third, a Claude app. Either [Claude
Desktop](https://claude.ai/download), which most users want, or [Claude
Code](https://docs.claude.com/claude-code), which is the terminal CLI.

bi-mcp itself can run on the Blue Iris box or on any other computer on
the same LAN.

## Install

bi-mcp is a command-line tool. The recommended way to install command-line
Python tools is with `pipx`, which gives each tool its own isolated
environment so they don't interfere with each other.

Open a terminal. On Windows that's PowerShell from the Start menu, on
macOS it's Terminal from Spotlight, on Linux it's whatever terminal you
already use.

Then run one of these:

```bash
# Most common, works on macOS, Linux, and Windows
pipx install bi-mcp

# Faster alternative, if you have uv (https://docs.astral.sh/uv/)
uv tool install bi-mcp

# Try it once without installing anything permanent
uvx bi-mcp-server check
```

You can also install straight from the GitHub source if you want the
latest unreleased changes:

```bash
pipx install git+https://github.com/whoamiTM/bi-mcp
# or with uv:
uv tool install git+https://github.com/whoamiTM/bi-mcp
# or one-shot:
uvx --from git+https://github.com/whoamiTM/bi-mcp bi-mcp-server check
```

If you don't have `pipx`, install it with `python -m pip install --user
pipx` followed by `python -m pipx ensurepath`. Close and reopen your
terminal so the new command lands on your `PATH`.

After installing, confirm the command works:

```bash
bi-mcp-server --help
```

If you want to edit the source instead of installing a released version,
see [For contributors](#for-contributors-editing-the-source) further down.

## Create a Blue Iris user for bi-mcp

Don't point bi-mcp at your admin account. Create a dedicated low-privilege
user that bi-mcp will log in as. In Blue Iris, go to Settings then Users
and click the **+** button to add a user. Name it `mcp-readonly` (or
whatever you prefer) and set a password you'll paste into the config in
the next step. Set Access to "Local + LAN", leave Admin unchecked, and
check the **PTZ** and **Clips** boxes. Under Camera groups, tick the
groups you want Claude to be able to see.

The [Security](#security) section further down has the full permission
table and explains the optional admin user that a handful of tools need.

## Quickstart with Claude Code

The simplest way to register an MCP server with Claude Code is the
`claude mcp add` command. From any terminal:

```bash
claude mcp add --transport stdio -s user \
  -e BI_HOST=192.168.1.10 \
  -e BI_PORT=81 \
  -e BI_USER=mcp-readonly \
  -e BI_PASS=your-password-here \
  bi-mcp \
  -- bi-mcp-server
```

Replace `192.168.1.10` with your Blue Iris box's LAN IP, and the user
and password with the ones you just created. The `-s user` flag makes
bi-mcp available in every project, not just the current directory.

Then restart Claude Code. Run `/exit` in any active session, then launch
`claude` again. In a fresh session, ask:

> List my Blue Iris cameras.

Claude should call `bi_list_cameras` and show your camera list.

### Editing the config file by hand

If you'd rather edit the config directly, it lives at `~/.claude.json` on
every platform. Add a `bi-mcp` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "bi-mcp": {
      "command": "bi-mcp-server",
      "env": {
        "BI_HOST": "192.168.1.10",
        "BI_PORT": "81",
        "BI_USER": "mcp-readonly",
        "BI_PASS": "your-password-here"
      }
    }
  }
}
```

Same restart procedure as above.

## Quickstart with Claude Desktop

I built bi-mcp against Claude Code and haven't tested it on Claude
Desktop. The MCP protocol is the same so it should work the same, but
the instructions below are lighter than the Claude Code path. PRs
welcome if anything's off.

The friendliest way to edit the config is from inside Claude Desktop
itself. Open the Claude menu, go to Settings, click the Developer tab,
then click "Edit Config". Claude Desktop creates the file if it doesn't
exist yet and opens it in your default editor.

If you'd rather find the file on disk, it lives at:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add a `bi-mcp` entry under `mcpServers` using the same JSON shape as the
Claude Code block above.

After saving, fully quit Claude Desktop and reopen it. Closing the
window is not the same as quitting. Use the app menu's Quit option.

If you want a walkthrough that uses Claude Desktop with a different MCP
server (the official filesystem one) as a template, Anthropic's user
quickstart at
[modelcontextprotocol.io/quickstart/user](https://modelcontextprotocol.io/quickstart/user)
covers the same Settings → Developer → Edit Config flow in more detail.

## When the first run doesn't work

The most common cause is invalid JSON in the config file. Trailing
commas, mismatched quotes, that kind of thing. Paste the file through a
JSON validator if you're not sure.

The next most common cause is not actually restarting the Claude app.
For Claude Code, run `/exit` and then `claude` again. For Claude Desktop,
use the app menu's Quit option. Closing the window isn't enough.

If both of those check out, run the CLI smoke test from a terminal:

```bash
BI_HOST=192.168.1.10 BI_USER=mcp-readonly BI_PASS=... bi-mcp-server check
```

A passing run prints `OK — connected to Blue Iris 5.x.y.z at HOST:PORT,
N cameras found`. If you see that, the server itself works and the
problem is on the Claude side: config not loaded, wrong file, app not
fully restarted. If the smoke test fails, jump to
[Troubleshooting](#troubleshooting) further down.

You can also call individual tools from the CLI to confirm they return
real data:

```bash
bi-mcp-server bi_list_cameras
bi-mcp-server bi_get_camera_config --short=SecCam_3
```

And for poking at tool schemas interactively, the MCP Inspector opens a
browser UI:

```bash
npx @modelcontextprotocol/inspector uvx --from . bi-mcp-server
```

## Configuration reference

bi-mcp reads its settings from environment variables. The `env` block in
the Claude config above sets them, so you don't need a separate file.

| Variable | Required | Purpose |
|---|---|---|
| `BI_HOST` | yes | LAN IP or hostname of your Blue Iris box |
| `BI_PORT` | yes | Blue Iris web-server port (default `81`) |
| `BI_USER` | yes | The low-privilege user you created |
| `BI_PASS` | yes | That user's password |
| `BI_ADMIN_USER` | no | Optional admin user for admin-gated tools (see [Security](#security)) |
| `BI_ADMIN_PASS` | no | Admin user's password |
| `BI_MCP_ALLOW_MUTATIONS` | no | Set to `1` to enable the 6 control tools |
| `BI_MCP_DEBUG` | no | Set to `1` to log to stderr and a rotating file |

You can also put these in a `.env` file in the directory you launch the
server from, which is handy for the CLI smoke tests. The `.env.example`
file in this repo shows the format.

### For contributors (editing the source)

```bash
git clone https://github.com/whoamiTM/bi-mcp
cd bi-mcp
uv sync                       # or: python -m venv .venv && source .venv/bin/activate && pip install -e .[dev]
uv run bi-mcp-server check
```

## Tool reference

19 read tools register by default. The 6 mutating tools register only when
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
| `bi_get_camera_motion_config` | `camconfig` | ✓ | | Live `setmotion` and `setpost` subtrees: current sensitivity, contrast, breaktime, and so on, without `.reg` staleness. AI thresholds not included. |
| `bi_get_camera_snapshot` | `GET /image/<short>` | | | Current JPEG frame, returned as an MCP image block (renders inline in image-aware clients) plus base64. Useful for live coverage cross-reference and PTZ preset framing checks. |
| `bi_get_alert_image` | `alertlist` + `GET /alerts/@<record>` | | | Stored alert image (the saved frame, not a live one), resolved by camera + optional `at` time to the most-recent alert at/before it. Returns an MCP image block (renders inline) plus a metadata text block (record/time/memo) and base64; `markup=true` for the v=2 AI overlay. |
| `bi_list_alerts` | `alertlist` | | | Recent alerts with AI memo, classification, zones, clip path. |
| `bi_get_alert_tracks` | `tracks` | | | AI per-frame bounding boxes inside one alert. |
| `bi_get_clip_info` | `clipstats` | | | Forensic detail for one clip. |
| `bi_list_clips` | `cliplist` | | | Recorded clip inventory; complementary to `bi_list_alerts`. |
| `bi_get_timeline` | `timeline` | | | Activity timeline (alert/clip spans) for a camera over a window; defaults to the last 24h. |
| `bi_get_ptz_status` | `ptz` (query) | | | Current PTZ position, preset list, lock state. |
| `bi_list_log` | `log` | ✓ | | Recent BI system log entries. |
| `bi_get_reg` | (file parser) | | | Parse `.reg` camera export: trigger zones, AI thresholds, per-preset flags. |
| `bi_get_actionset` | (file parser) | | | Decoded semantic view of `Alerts\OnTrigger` / `OnReset` action rows. |
| `bi_audit_actions` | (file parser) | | | Cross-camera cohort-divergence report. Surfaces action-row outliers (values that differ from the cohort majority) for user review. Outliers may be intentional per-camera customizations, not bugs. |
| `bi_explain_alert_chain` | `clipstats` + `log` + (file) | ✓ | | Diagnose one alert: alert facts, per-row filter decode, comparator verdicts for compound/threshold/wait/cross-zone cases, and a ±2-minute log cross-reference of what BI actually did. Refuses alerts older than 24h by default (override with `max_alert_age_h`). |
| `bi_trigger_camera` | `trigger` | ✓ | ✓ | Fire a synthetic motion trigger (mutations flag). |
| `bi_set_ptz_preset` | `ptz` (cmd) | | ✓ | Recall a PTZ preset 1-20 (mutations flag). |
| `bi_set_profile` | `status` (set) | ✓ | ✓ | Switch active profile (mutations flag). |
| `bi_export_clip` | `export` | ✓ | ✓ | Async MP4/AVI/WMV export from a clip range (modes: create / status / list). Requires BI user `clipcreate=true`. |
| `bi_update_record` | `update` | | ✓ | Set memo (≤35 chars) and/or flag bits on one alert or clip @record. Auto-preserves `flagged` on memo-only writes. |
| `bi_set_camera` | `camconfig` | ✓ | ✓ | 10 ops: rename, hide, enable, audio, output, manrec, pause, profile+lock, reset, reboot. All verify post-write. |

### Enabling mutating tools

Set `BI_MCP_ALLOW_MUTATIONS=1` in `.env` (or in the MCP config `env` block).
With the flag off, the mutating tools are not registered at all, so the
MCP tool list stays clean. Read [AGENTS.md § Mutation patterns](AGENTS.md) before
flipping the flag.

### `bi_get_reg` and the `.reg` parser

`bi_get_reg` parses Blue Iris's binary `.reg` camera exports to surface
settings the JSON API doesn't expose (trigger zone polygons, per-class AI
confidence thresholds, per-preset alert-skip flags, ONVIF event handlers,
alert action definitions). It expects:

- A `cam settings/` directory in the launch CWD with `<short>.reg` exports,
  OR `BI_MCP_REG_DIR` pointing at the directory.

Parsing is in-process. `python-registry` ships as a normal `bi-mcp`
dependency, so the install is single-step.

The `BI_MCP_REG_DIR` default resolves relative to the current working
directory at call time, not the install location, so it works correctly
whether bi-mcp is run from an editable checkout, a wheel, or via `uvx`.
Camera short names are validated (`[A-Za-z0-9_-]+`) before being composed
into a path, so a malformed name can't escape the configured directory.

Re-export a camera from BI any time you tune settings (right-click camera →
Camera settings → Copy/import → Export). Files older than 7 days trigger a
staleness warning in the tool's response.

## Troubleshooting

**`unreachable`**: cannot reach Blue Iris.
- Check `BI_HOST` and `BI_PORT` in `.env` (default port is 81).
- Confirm Blue Iris's web server is enabled: BI → Settings → Web server.
- From the same machine, try `curl -X POST -d '{"cmd":"login"}' http://HOST:PORT/json`. You should get a JSON response.

**`auth`**: Blue Iris rejected the login.
- Check `BI_USER` and `BI_PASS`.
- The user must have LAN access enabled in BI → Settings → Users.
- Don't keep retrying with wrong credentials. Blue Iris will lock the account.

**`not_found`**: requested camera, clip, or alert doesn't exist.
- For `bi_get_camera_config short=…`, the value must match a camera's *short name*, not its display name. Run `bi_list_cameras` to see the list.

**Empty or weird responses**: pass `raw=true` to see what BI actually returned, then file an issue with the raw JSON so the shaper can be improved.

**Debug logging**: set `BI_MCP_DEBUG=1` in `.env` (or in the MCP config `env`).
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

If you work from a clone of this repo, `.env` is gitignored and you should
never commit it. If credentials end up in a commit by accident, delete the
Blue Iris user immediately and create a new one.

### Admin-gated tools and the two-user setup

Blue Iris gates several JSON cmds behind admin, so the recommended pattern is
a **two-user setup**:

- `BI_USER` and `BI_PASS`: the low-privilege account with PTZ and Clips access,
  used for all read tools that don't need admin.
- `BI_ADMIN_USER` and `BI_ADMIN_PASS`: a dedicated admin account, used only
  for the admin-gated cmds (marked ✓ in the *Admin?* column of the tool table
  above). Covers `bi_get_sysconfig`, `bi_list_log`, the deep path of
  `bi_get_camera_config`, `bi_get_camera_motion_config`, `bi_trigger_camera`,
  `bi_set_profile`, `bi_export_clip`, and `bi_set_camera` (all ops).

If only the read user is configured, admin-gated tools degrade gracefully
(deep `bi_get_camera_config` falls back to the shallow `camlist` view; other
admin-gated tools raise a clear `admin_required` error). See [AGENTS.md
§ Mutation patterns](AGENTS.md) before enabling the mutating tools.

## License

MIT. See [LICENSE](LICENSE).

Contributions welcome. Open an issue on GitHub before sending a large PR so we
can agree on shape.
