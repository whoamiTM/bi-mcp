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
- **Surface:** 15 read tools (always) + 6 mutating tools (when enabled).

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
| "Show me current motion sensitivity / contrast / breaktime"              | `bi_get_camera_motion_config(short="...")` — live, no stale .reg |
| "What's MQTT/archive/global-schedule state?"                             | `bi_get_sysconfig` (admin required)                      |
| "What's PTZ doing?" / available presets                                  | `bi_get_ptz_status(camera="...")`                        |
| "Recent errors in BI log"                                                | `bi_list_log(levels=[2], since="-1h")`                   |
| "Why did SecCam_3 trigger 10 min ago?"                                   | `bi_list_log(camera="SecCam_3", since="-15m", levels=[3])` |
| "Fire a test alert on camera X" (requires mutations)                     | `bi_trigger_camera(camera="...", memo="...")`            |
| "Move PTZ to preset N" (requires mutations)                              | `bi_get_ptz_status` first → `bi_set_ptz_preset(...)`     |
| "Change to night profile" (requires mutations)                           | `bi_get_status` first → `bi_set_profile(...)` → revert   |
| "Export an MP4 of this alert / clip" (requires mutations + `clipcreate`) | `bi_export_clip(mode="create", path=…, startms=…)` → poll w/ `mode="status"` |
| "Rename / hide / enable / pause / reboot camera X" (requires mutations)  | `bi_set_camera(camera="…", op="…")` — read op list from tool docstring first |

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
| `bi_get_camera_motion_config` | `camconfig` |   ✓    |           | Live motion + post-trigger settings (`setmotion` + `setpost`); read-only, no `.reg` staleness — AI thresholds NOT included |
| `bi_list_alerts`        | `alertlist`  |        |           | Recent AI/motion alerts                                        |
| `bi_get_alert_tracks`   | `tracks`     |        |           | Per-frame bounding boxes for one alert                         |
| `bi_get_clip_info`      | `clipstats`  |        |           | Forensic clip metadata                                         |
| `bi_list_clips`         | `cliplist`   |        |           | Recorded clip inventory                                        |
| `bi_get_timeline`       | `timeline`   |        |           | 24-hour activity timeline                                      |
| `bi_get_ptz_status`     | `ptz` (query)|        |           | PTZ state: raw `presets[]` + `presetnum` passthrough, plus derived `preset_map` {N→desc} and `active_preset` {num,description} |
| `bi_list_log`           | `log`        |   ✓    |           | System log entries with filters (since/camera/obj/levels/match/regex). Returns {entries, scanned, matched, warning?} |
| `bi_get_reg`            | (file)       |        |           | .reg hive parse — for what the API can't reach                 |
| `bi_get_actionset`      | (file)       |        |           | Semantic view of Alerts\\OnTrigger / OnReset (decoded)         |
| `bi_trigger_camera`     | `trigger`    |   ✓    |     ✓     | Fire a synthetic motion trigger                                |
| `bi_set_ptz_preset`     | `ptz` (cmd)  |        |     ✓     | Recall a PTZ preset (1-20)                                     |
| `bi_set_profile`        | `status` set |   ✓    |     ✓     | Switch active profile                                          |
| `bi_export_clip`        | `export`     |   ✓    |     ✓     | Async MP4/AVI/WMV export from a clip range (modes: create / status / list). Requires BI user `clipcreate=true` |
| `bi_update_record`      | `update`     |        |     ✓     | Set memo (≤35 chars) and/or flag bits (flagged/protected/archive/export_flag) on one alert or clip @record. Read-before-write captures previous_memo + previous_flags |
| `bi_set_camera`         | `camconfig`  |   ✓    |     ✓     | 10 ops: rename, hide, enable, audio, output, manrec, pause, profile+lock, reset (stream reload), reboot. All verify post-write; reset/reboot have extended verify windows. |

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

### Narrowing investigative queries with `view` + `search`

Both `bi_list_alerts` and `bi_list_clips` accept server-side filters.
Prefer them over fetching everything and filtering client-side — BI's
database can handle the predicate faster, and you stay under the default
limit of 50.

```
bi_list_alerts(camera="SecCam_3", view="cancelled", limit=10)
    → only the cancelled-by-AI alerts (false positives)
bi_list_alerts(camera="SecCam_3", view="people", search="UPS")
    → person alerts whose memo contains "UPS"
bi_list_clips(camera="SecCam_3", view="zoneb")
    → alerts that fired in Zone B (returned as alert items inside a clip
      response — see crossover note in the tool docstring)
```

Manual `view` values (per `§ alertlist` / `§ cliplist`): `all`, `new`,
`stored`, `alerts`, `aux1..aux7`, `flagged`, `export`, `archive`,
`people`, `vehicles`, `confirmed`, `canceled`. UI3 source adds:
`zonea..zoneh`, `dio`, `onvif`, `audio`, `external`, `cancelled` (note
the British spelling — BI accepts both).

Crossover quirk: passing an alert-side view to `bi_list_clips` returns
alert items (not clips); passing `flagged` to either returns mixed
results. UI3 fixed a v91 bug in handling these mixed payloads — `msec`
on a returned alert item is **alert length**, not clip length.

### The config inspection ladder

```
bi_get_camera_config(short="SecCam_3")
    → top-level: motion sense/contrast, AI zones bitmask, recording mode,
      stream paths, schedule/profile flags
        ↓ (motion/post drill-down, live, no .reg staleness)
bi_get_camera_motion_config(short="SecCam_3")
    → setmotion (sense, contrast, breaktime, maketime, ai_zones, etc.) +
      setpost (timed, timed_interval) read live from camconfig. Use this
      while tuning sensitivity in the UI — diffs against bi_get_reg
      ("Motion" subkey for profile 1; "Motion\\<n>" subject to off-by-one).
      Does NOT expose AI thresholds — those stay .reg-only.
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
(Pass 1, 2026-05-17, 11 cameras, 28 action entries), then expanded against
jaydeel's authoritative PowerShell decoders on ipcamtalk thread 85627
(2026-05-21), and closed out 2026-05-24 by diffing a throwaway clone
fixture with one row of every action kind (`cam settings/clone_seccam_10_test.reg`
in the parent repo, commit `d43b63f`). All 13 action kinds in BI 5.9.9.71
now have per-type payload decoders. The `type` integer space is sparse
(0-11, 13 used; 12 is renamed do_command; 14+ unused). Coverage now:

| Field            | Mapped values                                                                   | Still missing                          |
| ---------------- | ------------------------------------------------------------------------------- | -------------------------------------- |
| `type`           | 0-13 full map (sound, push, run, web, email, sms, phone, dio, toast, ftp, shield, schedule, do_command, wait) with per-kind payload field decode | —                                      |
| `command`        | PTZ presets 2201-2456, action sets 33203-33210, brightness/contrast/gain ranges, PTZ speed/outputs, ~60 individual codes | any code not in jaydeel's table        |
| `web_proto1`     | 0=http, 1=https, 2=mqtt (full; no TCP option exists)                            | —                                      |
| `run_action`     | 0=run_program, 1=write_file_append, 2=write_file_replace, 3=delete_file         | —                                      |
| `trig_allzones`  | 0=exact, 1=all, 2=any                                                           | —                                      |
| `profiles`       | bits 0-6 → profiles 0-6 (profile 0 = "Inactive"); legacy sentinel 46 = no profiles | —                                  |
| `trig_zones`     | bits 0-7 → zones A-H (H = Hotspot)                                              | —                                      |
| `diobits`        | bits 0-31 → DIO 1-32, decoded into `filters.dio_trigger_gate` on **every** row as a per-row trigger gate (independent from `type=7`'s `dio_number` output channel) | —                  |
| `trig_source`    | bits 1,2,3,4,5,6,14 (motion, onvif, audio, external, dio, group, ai) decoded into a list; `trig_source_raw` preserved | bit 7 (=128) observed in our exports but unnamed by jaydeel |
| `source` (type=9)| 1=specific_file, 2=group_image, 3=current_camera_image, 4=alert_media (alert-image vs alert-MP4 disambiguated via `mp4sec`/`mp4audio`) | —                                      |
| `mode` (type=13) | 3-bit OR'd bitmask: bit 0=queues_empty, bit 1=no_longer_triggered, bit 2=retriggered (empty list = wait full `breaktime` unconditionally) | —                                      |

Unmapped values fall through as `type: "unknown"` / `command_raw: <int>` /
`protocol: "unknown"` — the original dict is always preserved under `raw`, so
the tool never loses data. When adding a new mapping, edit the tables at the
top of `shapers.shape_actionset` (`_ACTION_TYPE`, `_WEB_PROTO`,
`_RUN_ACTION`, `_DOCMD_INDIVIDUAL`, `_decode_command`, etc.).

### Future extension: Watchdog action sets

The `Cameras\<short>\Watchdog\OnLoss\<N>` and `\Watchdog\OnRestore\<N>`
registry trees use the **same structure** as `Alerts\OnTrigger\<N>` — same
action-entry shape, same `type`/`command`/filter fields. `bi_get_actionset`
currently only reads the `Alerts\` tree. Extending it (or adding a sibling
tool like `bi_get_watchdog_actionset`) would reuse `_shape_action_entry`
unchanged. Not implemented yet; deferred until there's a concrete need.

### Motion key off-by-one quirk

Per jaydeel on ipcamtalk ("legacy reasons"): the `Cameras\<short>\Motion`
registry subtree numbers profiles starting at the bare key, not at `\1`:

| Registry path                | Actual profile |
| ---------------------------- | -------------- |
| `Cameras\<short>\Motion`     | profile **1**  |
| `Cameras\<short>\Motion\1`   | profile **2**  |
| `Cameras\<short>\Motion\2`   | profile **3**  |
| ...                          | ...            |

This affects `bi_get_reg(key_path="Motion\\<n>")` callers — `Motion\\3` is
actually profile 4 in BI's UI, not profile 3. The `AI\` subtree appears to
use straightforward 1-based numbering (`AI\\3` = profile 3); the off-by-one
is specific to `Motion\`. If a future task involves cross-referencing motion
zones against AI rules, this offset must be applied.

### What the BI JSON API does NOT expose (even with admin)

- **Trigger zone polygons** — use `bi_get_reg` → `Motion\\<profile>\\maskbits_*`
- **Per-class AI confidence thresholds** — use `bi_get_reg` → `AI\\<profile>\\smartconf`
- **Alert action definitions** — use `bi_get_reg` → `Alerts\\OnTrigger`
- **Per-preset alert-skip / AI-confirmation flags** — use `bi_get_reg` → `PTZ\\Presets\\<n>`
- **ONVIF event handler rules** — use `bi_get_reg` → `camevents`
- **Per-camera Dahua Smart Plan / IVS rules** — needs Dahua-side API; out of scope for bi-mcp

---

## Mutation patterns

Mutating tools (`bi_trigger_camera`, `bi_set_ptz_preset`, `bi_set_profile`,
`bi_export_clip`, `bi_update_record`, `bi_set_camera`)
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

**For `bi_set_camera`** the response also carries a `verified` field
(plus `verify_method` and, when verify couldn't complete, a
`verify_error_kind`):

| `ok`  | `verified` | Meaning | Action |
|-------|------------|---------|--------|
| true  | true       | BI accepted AND post-read confirmed the change | Done |
| true  | false      | BI accepted but post-read couldn't confirm (verify-side blip, or stream-dip not seen for reboot/reset) | Re-read state manually; do NOT blindly retry — some ops are not idempotent (`pause` is additive, `reboot`/`reset` are disruptive) |
| true  | omitted    | BI accepted; this op verifies via the response itself (no post-read needed) | Done |
| false | —          | BI rejected the write | Read `reason` and decide |

When `verified=false`, look at `verify_error_kind`:

- `verify_auth_blip` — fresh admin login failed. **If this recurs across
  calls, investigate `BI_ADMIN_USER`/`BI_ADMIN_PASS`** (rotation,
  lockout) rather than treating as transient.
- `verify_unreachable` — network blip / BI restart. Almost always
  transient; one retry is usually fine, but still re-read first to avoid
  duplicating non-idempotent side effects.

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

### Rule 6.5 — `bi_export_clip` lifecycle ends outside the export queue

Verified live 2026-05-18: a small (5s) export completes in <8s on this
install. Once BI marks the job `done`, the export record **graduates out
of the export-queue namespace** into the regular clip database. Symptoms:

  * `bi_export_clip mode="status" path=@<new_record>` starts returning
    `{'status': 'Clip not BVR'}` from BI (it's an MP4 now, not a queue
    entry).
  * `bi_export_clip mode="list"` no longer lists it.
  * `bi_get_clip_info path=@<new_record>` returns the produced MP4 with
    its real filesize, duration, and filetype like `mp4 H264 New.Clipboard`.

The right polling pattern:

```
create_resp = bi_export_clip(mode="create", ...)
new_record  = create_resp["item"]["path"]
loop:
    s = bi_export_clip(mode="status", path=new_record)
    if not s["ok"]:           # BI errored OR the record graduated
        break
    if s["item"]["status"] in ("done", "error"):
        break
    sleep / re-poll
# Whether the loop broke on error or done, verify by clip-info:
clip = bi_get_clip_info(path=new_record)
# clip.filesize tells you the export actually landed
```

Don't treat "status returned a BI error" as failure on its own — also
check `bi_get_clip_info`. The export probably succeeded.

**`raw=true` nuance**: passing `raw=true` on `mode="status"` returns the
verbatim BI payload, which means the graduation case **re-raises** the
underlying `BiError` instead of synthesizing the `{ok:false}` envelope.
The shaped path (default) is the ergonomic one; `raw=true` exists for
debugging the wire protocol and must show what BI actually returned —
which, post-graduation, is an error. If you want a clean ok/false signal,
don't pass `raw=true`.

### Rule 7 — `bi_update_record` auto-preserves `flagged` on memo-only writes

Verified live 2026-05-20 on BI 5.9.9.71: sending `update` with only
`memo` (no `flags`/`mask`) **clears the `flagged` bit** on the record as
a side effect of the BI cmd. Other named bits weren't observed to drift.

To protect curation state, `bi_update_record` **auto-preserves the
existing `flagged` bit** on memo-only writes by default. The tool
pre-reads `flags` via `clipstats`, then synthesizes a `(flags, mask)`
pair that pins the `flagged` bit to its current value before sending
`update`. The response includes `flagged_auto_preserved: true` when
this happens.

```
# Default — auto-preserve is on. flagged stays whatever it was.
bi_update_record(path=alert_path, memo="new memo")
# → response includes flagged_auto_preserved: true
```

Two opt-outs:

* Pass `preserve_flagged=false` for raw BI semantics (memo-only writes
  will clear flagged again).
* Pass an explicit flag arg (`flagged=true/false`, `protected`,
  `archive`, `export_flag`, or raw `flags`+`mask`) and the tool defers
  to your intent — no auto-preserve, no override.

The verify step also guards against silent side effects on the other
three named bits: if the caller made no flag claims AND auto-preserve
isn't active, post-write drift in `protected` / `archive` /
`export_flag` raises `BiError` rather than silently returning success.
This protects against future BI builds that might mutate additional
flag bits we haven't characterized.

### Rule 8 — `bi_set_camera` op-specific gotchas

- **output**: the BI reply echoes the *pre-write* value, not the new one. The tool re-reads `bi_get_camera_config` to verify — do not interpret the raw reply value as the post-write state.
- **audio**: toggling audio restarts the camera's stream (~1-2s reconnect lag). Don't follow immediately with a state-sensitive read.
- **profile+lock (op="profile")**: setting `profile=-1` on an *enabled* camera is silently coerced to 0 (scheduled) by BI. Only works as "hold at -1" on a *disabled* camera. Verify the post-write value rather than assuming the requested profile landed.
- **pause**: pause codes are *additive* (bitmask). Probe with `bi_get_camera_config` to see the current `pause` value before setting, or you may clear a pause set by a different caller.
- **reset**: this is a *stream reload*, NOT a counter or alert reset. Use it only when a camera feed is stuck. Tool refuses upfront if camera is offline.
- **reboot**: end-to-end ~75s on this install (10s for BI to mark offline + ~65s for the camera to return). The verify window is only 10s, so `{ok: true, verified: false}` in the response is expected and normal — BI accepted the reboot cmd, the dip just happened outside the sampling window. **Do NOT re-fire the cmd on `verified: false`** (that would mean a second hardware power cycle). Poll `bi_list_cameras` until `isOnline` cycles false→true to confirm.
- **All ops require admin**: `bi_set_camera` always routes through the admin client. `BI_ADMIN_USER` and `BI_ADMIN_PASS` must be set.

### Rule 6 — `bi_export_clip` needs the `clipcreate` user permission

The BI `export` cmd is gated on the per-user **`clipcreate`** capability
(manual § *login* reply table: "may take snapshots, start manual recording,
export/convert clips"), **not on the admin flag**. A user can have
`admin=true` and still get BI's `Access denied` here. Check
`bi_get_session()["clipcreate"]` before calling; if false, point the user
at BI → Settings → Users → \<the user\> → enable **"Create clips"**
(manual § 6605: the Create clips privilege gates "snapshots, manual video
recordings, or to crop and export video"). This applies to whichever
account `bi_export_clip` routes through (it goes admin-side, so that's
`BI_ADMIN_USER` if explicit-admin is configured, otherwise `BI_USER`).

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

### ❌ Calling `bi_list_log` without `since` for camera-scoped debugging

BI's log buffer holds thousands of entries (3.8k+ on a typical install).
`since="-15m"` — or an explicit timestamp from `bi_list_alerts` — returns a
fraction of that and avoids client-side scanning the full buffer for a
substring match. A `warning` is emitted in the response envelope when
filters run without a time bound. Clone cameras (e.g. `SecCam_11AI` cloned
from `SecCam_11`) log under their own short names; query each by its actual
name rather than expecting prefix-match magic.

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
