# bi-mcp — Agent Operating Manual

This document is the canonical operating manual for an LLM working through
`bi-mcp`. Tool routing decisions, error handling, mutation safety, and
the gap between what the BI JSON API exposes and what it doesn't all live
here. Read this first when entering a fresh session.

---

## Server identity

- **Wraps:** Blue Iris NVR's HTTP/JSON API + the undocumented `camconfig`
  cmd, plus a `.reg` export parser for what the API can't reach.
- **Tested against:** Blue Iris **5.9.9.71** (x64) on Windows 10.
- **Mode:** stdio MCP server. Read-only by default; mutating tools register
  only when `BI_MCP_ALLOW_MUTATIONS=1`.
- **Surface:** 13 read tools (always) + 3 mutating tools (when enabled).

---

## Quick-start decision table

| If the user asks…                                                        | Call first…                                              |
| ------------------------------------------------------------------------ | -------------------------------------------------------- |
| "What's BI doing right now?" / current profile / disk / DIO              | `bi_get_status`                                          |
| "What cameras are online?" / spotter overview                            | `bi_list_cameras`                                        |
| "What recent alerts on camera X?"                                        | `bi_list_alerts(camera="...")`                           |
| "What recorded between A and B?"                                         | `bi_list_clips(camera="...", startdate=…, enddate=…)`    |
| "Why did this specific alert fire?"                                      | `bi_list_alerts` → `bi_get_alert_tracks` + `bi_get_clip_info` |
| "Show me trigger zones / AI threshold for camera X"                      | `bi_get_reg(camera="...")` — NOT `bi_get_camera_config`  |
| "What's MQTT/archive/global-schedule state?"                             | `bi_get_sysconfig` (admin required)                      |
| "What's PTZ doing?" / available presets                                  | `bi_get_ptz_status(camera="...")`                        |
| "Recent errors in BI log"                                                | `bi_list_log(level=2)`                                    |
| "Fire a test alert on camera X" (requires mutations)                     | `bi_trigger_camera(camera="...", memo="...")`            |
| "Move PTZ to preset N" (requires mutations)                              | `bi_get_ptz_status` first → `bi_set_ptz_preset(...)`     |
| "Change to night profile" (requires mutations)                           | `bi_get_status` first → `bi_set_profile(...)` → revert   |

For static facts (camera → IP, role, friendly name), do **not** call
`bi_list_cameras` — those are cached in user-level memory at
`project_camera_roster.md`. Same for spotter→PTZ-preset map
(`project_spotter_ptz_map.md`).

---

## Tool inventory

| Name                    | BI Cmd       | Admin? | Mutating? | Purpose                                                        |
| ----------------------- | ------------ | :----: | :-------: | -------------------------------------------------------------- |
| `bi_get_status`         | `status`     |        |           | System state snapshot                                          |
| `bi_get_session`        | `login`      |        |           | Version, capabilities, available profiles/schedules            |
| `bi_get_sysconfig`      | `sysconfig`  |   ✓    |           | Archive/schedule/manrecsec + any DIO/MQTT inline               |
| `bi_list_cameras`       | `camlist`    |        |           | All cameras + groups                                           |
| `bi_get_camera_config`  | `camconfig`/`camlist` | (deep needs admin) | | Per-camera config (deep w/ admin, shallow without)        |
| `bi_list_alerts`        | `alertlist`  |        |           | Recent AI/motion alerts                                        |
| `bi_get_alert_tracks`   | `tracks`     |        |           | Per-frame bounding boxes for one alert                         |
| `bi_get_clip_info`      | `clipstats`  |        |           | Forensic clip metadata                                         |
| `bi_list_clips`         | `cliplist`   |        |           | Recorded clip inventory                                        |
| `bi_get_timeline`       | `timeline`   |        |           | 24-hour activity timeline                                      |
| `bi_get_ptz_status`     | `ptz` (query)|        |           | PTZ position + preset list                                     |
| `bi_list_log`           | `log`        |   ✓    |           | System log entries                                             |
| `bi_get_reg`            | (file)       |        |           | .reg hive parse — for what the API can't reach                 |
| `bi_get_actionset`      | (file)       |        |           | Semantic view of Alerts\\OnTrigger / OnReset (decoded)         |
| `bi_trigger_camera`     | `trigger`    |   ✓    |     ✓     | Fire a synthetic motion trigger                                |
| `bi_set_ptz_preset`     | `ptz` (cmd)  |        |     ✓     | Recall a PTZ preset (1-20)                                     |
| `bi_set_profile`        | `status` set |   ✓    |     ✓     | Switch active profile                                          |

Every tool accepts `raw=true` — returns the underlying BI payload verbatim
instead of the shaped view. Use it when shaping might be hiding a field
you need, or when a future BI build's response shape doesn't match the
shaper.

---

## Read patterns

### The alert investigation chain

```
bi_list_alerts(camera="SecCam_3", limit=20)
    → returns alerts[] with `path` (alert id) and `clip` (clip id)
bi_get_alert_tracks(path=<alerts[i].path>)
    → per-frame bounding boxes for one alert
bi_get_clip_info(path=<alerts[i].clip>)
    → resolution, duration, AI/profile/schedule/zones active at trigger time
```

`alerts[].offset` is **milliseconds into the parent clip**, not a Unix
timestamp. Don't ISO-format it.

### The config inspection ladder

```
bi_get_camera_config(short="SecCam_3")
    → top-level: motion sense/contrast, AI zones bitmask, recording mode,
      stream paths, schedule/profile flags
        ↓ (when the question goes deeper)
bi_get_reg(camera="SecCam_3", key_path="AI\\3")
    → per-class confidence thresholds, smartlabels, smartzones bitmask,
      autotrack settings, etc.

bi_get_reg(camera="SecCam_3", key_path="Motion\\3")
    → trigger zone polygons (maskbits_*), object size limits, AI trigger flag

bi_get_reg(camera="SecCam_3", key_path="PTZ\\Presets")
    → per-preset noalerts (alert-skip), bconfirm (AI override), trigger zone

bi_get_reg(camera="SecCam_3", key_path="camevents")
    → ONVIF event handler rules (Dahua IVS → BI trigger bindings)

bi_get_reg(camera="SecCam_3", key_path="Alerts\\OnTrigger")
    → action set definitions (raw — integer-coded fields)

bi_get_actionset(camera="SecCam_3")          ← prefer this over the line above
    → same data, but with type/command/protocol decoded to readable strings,
      profiles/zones bitmasks expanded to lists, and the original dict kept
      under raw[] for fields the decoder doesn't yet cover
```

If a `.reg` file is older than 7 days, `bi_get_reg` (and `bi_get_actionset`)
return `meta.stale: true`. Prompt the user to re-export the camera before
quoting values from a stale file.

### Action-set decoder coverage

`bi_get_actionset` decoder tables were built empirically from this install
(Pass 1, 2026-05-17, 11 cameras, 28 action entries). Coverage is partial:

| Field         | Mapped values                          | Pass 2 needed for                     |
| ------------- | -------------------------------------- | ------------------------------------- |
| `type`        | 3 (web_or_mqtt), 12 (do_command)       | Sound, Push, Email, SMS, Phone, Run Program, DIO, Popup, FTP, Shield, Schedule, Wait |
| `command`     | 2200-2299 (PTZ preset)                 | snapshot, profile change, /admin?, etc. (manual lists ~30 do-commands) |
| `web_proto1`  | 2 (MQTT)                               | HTTP-GET / HTTP-POST / TCP            |
| `profiles`    | full (bits 1-7 → profiles 1-7)         | —                                     |
| `trig_zones`  | full (bits 0-7 → zones A-H)            | —                                     |
| `trig_source` | passed through raw (bits 1,2,3,7,14 observed) | full bit-position map (toggle each "Trigger sources" checkbox in UI) |

Unmapped values fall through as `type: "unknown"` / `command_raw: <int>` /
`protocol: "unknown"` — the original dict is always preserved under `raw`, so
the tool never loses data. When adding a new mapping, edit the tables at the
top of `shapers.shape_actionset` (`_ACTION_TYPE`, `_WEB_PROTO`, `_decode_command`).

### What the BI JSON API does NOT expose (even with admin)

- **Trigger zone polygons** — use `bi_get_reg` → `Motion\\<profile>\\maskbits_*`
- **Per-class AI confidence thresholds** — use `bi_get_reg` → `AI\\<profile>\\smartconf`
- **Alert action definitions** — use `bi_get_reg` → `Alerts\\OnTrigger`
- **Per-preset alert-skip / AI-confirmation flags** — use `bi_get_reg` → `PTZ\\Presets\\<n>`
- **ONVIF event handler rules** — use `bi_get_reg` → `camevents`
- **Per-camera Dahua Smart Plan / IVS rules** — needs Dahua-side API; out of scope for bi-mcp

---

## Mutation patterns

Mutating tools (`bi_trigger_camera`, `bi_set_ptz_preset`, `bi_set_profile`)
are registered only when `BI_MCP_ALLOW_MUTATIONS=1`. The rules below apply
whenever you reach for any of them.

### Rule 1 — Read before write

Always confirm state before mutating. PTZ presets, profiles, and triggers
are easy to fire on the wrong target.

```
✅  bi_get_ptz_status(camera="SecCam_11")  → confirm preset 5 exists
    bi_set_ptz_preset(camera="SecCam_11", preset=5)

❌  bi_set_ptz_preset(camera="SecCam_11", preset=5)  → "did the user mean preset 5?"
```

### Rule 2 — Verify after write

Re-read the state to confirm the action landed. Don't claim success
based on the cmd-envelope `ok: true` alone.

```
bi_set_ptz_preset(camera="SecCam_11", preset=5)  → {ok: true, ...}
bi_get_ptz_status(camera="SecCam_11")            → confirm position
```

For `bi_trigger_camera`:

```
bi_trigger_camera(camera="SecCam_3", memo="test-low-light")
bi_list_alerts(camera="SecCam_3", limit=1)  → memo should appear
```

### Rule 3 — Revert global state before turn end

`bi_set_profile` affects the **whole BI install**. If you flip the
profile to verify behavior, flip it back before the turn ends — unless
the user explicitly asked for a persistent change. The tool's response
includes `previous_profile` to make this easy.

```
status = bi_get_status()
prev_profile = status["profile"]
prev_lock    = status["lock"]
bi_set_profile(profile=2)
… do the verification …
bi_set_profile(profile=prev_profile)
# if you also toggled hold via profile=-1 mid-flow, revert that with another -1
```

`bi_set_profile` accepts:
- Profile number `0-7` — switch to that profile (requires it to differ from
  the current one; same-profile calls are rejected because BI interprets
  them as "engage schedule hold")
- Profile name — resolved against `bi_get_session().profiles[]`
- `-1` — toggle schedule hold/run state (`status["lock"]`)

The tool does a mandatory `bi_get_status` pre-read to capture
`previous_profile` and `previous_lock`, and a mandatory post-read to verify
the requested change actually landed. The return value includes both
`{profile, lock, previous_profile, previous_lock}` so the agent can revert
either axis. Always revert before turn end unless the user asked for a
persistent change.

### Rule 4 — Don't loop `bi_trigger_camera`

Each call creates a real alert in the user's database. Fire once,
observe the result, move on. A retry loop is a bug.

### Rule 5 — Default to refusing destructive requests

If the user asks for something destructive that's beyond mutation —
e.g. "delete all alerts from yesterday" — the answer is that bi-mcp
deliberately doesn't expose `delalert`/`delclip`/`moveclip`. Point them
at the BI UI.

---

## Error taxonomy

Typed exceptions raised by tool dispatch surface as JSON via
`BiError.to_dict()`. Each carries a `kind` discriminator and a `hint`.
The mapping to `ErrorCode`:

| `BiError` kind       | `ErrorCode`           | When                                            | What to do                                            |
| -------------------- | --------------------- | ----------------------------------------------- | ----------------------------------------------------- |
| `unreachable`        | `BI_UNREACHABLE`      | BI host down / port wrong                       | Surface to user; check BI_HOST/BI_PORT                |
| `auth`               | `AUTH_FAILED`         | Read-user login rejected                        | Surface; check BI_USER/BI_PASS                        |
| `admin_auth`         | `ADMIN_AUTH_FAILED`   | Admin-user login rejected                       | Surface; check BI_ADMIN_USER/BI_ADMIN_PASS            |
| `admin_required`     | `ADMIN_REQUIRED`      | Admin cmd called with no admin client           | Surface; tell user to set admin creds                 |
| `not_found`          | `CAMERA_NOT_FOUND` etc| Camera/alert/clip doesn't exist                 | Don't retry — fix the input                           |
| `bad_request`        | `VALIDATION_FAILED`   | Tool arg shape wrong                            | Don't retry — fix the call                            |
| `mutations_disabled` | `MUTATIONS_DISABLED`  | Mutation invoked w/ flag off (defensive)        | Surface; tell user to set the env flag                |
| `stale_reg`          | `STALE_REG`           | .reg file >7 days old                           | Surface as a warning; recommend re-export             |
| `bi_error`           | `BI_ERROR`            | Anything else from BI                           | Surface verbatim; log for diagnosis                   |

**Retry rules:** transient session-expiry is handled inside `BiClient.call`
(one auto re-login + retry). Beyond that, the agent should NOT retry —
all the typed errors above are durable failures that need a different
input or a config fix.

---

## Naming convention

`bi_<verb>_<noun>`. Canonical verbs:

- `get_` — single object, by id (`bi_get_status`, `bi_get_camera_config`,
  `bi_get_alert_tracks`)
- `list_` — collection (`bi_list_cameras`, `bi_list_alerts`,
  `bi_list_clips`, `bi_list_log`)
- `set_` — mutate state (`bi_set_profile`, `bi_set_ptz_preset`)
- `trigger_` — fire-and-forget action (`bi_trigger_camera`)

When adding a new tool, pick one of these verbs. If none fit, update this
section in the same PR.

---

## Anti-patterns

### ❌ Re-fetching static facts

```
bi_list_cameras()  → walk for IP / role / friendly-name of SecCam_3
```

Those are in `project_camera_roster.md` and don't change without an
explicit user action. Use `bi_list_cameras` for state (online, FPS,
bitrate, alert counts), not identity.

### ❌ Calling `bi_get_camera_config` for trigger zones / AI thresholds

`camconfig` exposes about 15 top-level fields. Trigger zones, per-class
confidence thresholds, and per-preset flags all live in the `.reg`
hive. Use `bi_get_reg` with the appropriate `key_path`.

### ❌ Sending `button=-1` to query PTZ status

The `ptz` cmd treats *any* `button` value as a write. Omit `button`
entirely to query state — that's what `bi_get_ptz_status` does. Never
pass `button` from outside `bi_set_ptz_preset`.

### ❌ Converting `offset` to a datetime

In both `bi_list_alerts` and `bi_get_clip_info`, `offset` is
**milliseconds within the parent clip**, NOT a Unix timestamp. The
shaper deliberately doesn't ISO-format it.

### ❌ Looping `bi_trigger_camera` to "make sure" a config change works

One call. If it didn't land, fix the config — don't pile up alerts.

### ❌ Flipping profiles and forgetting to revert

`bi_set_profile` is global. If you're A/B testing, capture
`previous_profile` from the response and flip back before turn end.

---

## Adding a new tool

1. Decide what BI cmd it wraps (`BlueIris_Manual.md` § *JSON Interface*).
2. Pick a name in `bi_<verb>_<noun>` form. Update this file's
   tool-inventory table.
3. Create or extend `src/bi_mcp/tools/tools_<domain>.py`. Match the
   docstring + decorator + register style in `tools_status.py`.
4. Add a shaper in `shapers.py` if the response needs trimming.
5. If the cmd is admin-gated, route through `client.admin_call(...)`
   and raise `BiAdminRequired` when admin isn't configured.
6. If the cmd mutates state, put the tool in `tools_mutations.py`
   (auto-skipped without `BI_MCP_ALLOW_MUTATIONS=1`) and set
   `destructiveHint=true` in annotations.
7. `bi-mcp-server check` should pass; `bi-mcp-server <new_tool> --…`
   should return shaped data.

---

## Version handling

bi-mcp doesn't pin a BI version or refuse to connect to mismatched
builds. The connected version is logged at startup; the `raw=true`
escape hatch on every tool is the graceful-degradation path if a
response shape changes in a future build. If something breaks on a new
BI version, fix it forward — don't add compat shims.
