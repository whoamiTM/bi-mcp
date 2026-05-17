---
name: bi-troubleshoot
description: Structured walk for "camera X is missing alerts" or "alerts are firing wrong on camera X" — read alert pattern, drill into config (BI then .reg), form one hypothesis, test it (optional bi_trigger_camera), verify.
disable-model-invocation: true
---

# bi-troubleshoot — alert pipeline diagnosis

Use this when the user reports an alert problem on a specific camera —
missing alerts, spurious alerts, wrong classification, alerts at the wrong
times of day. Do NOT use for connectivity / PTZ / recording issues; those
need different surfaces.

## Step 1 — Confirm the symptom in data

```
bi_list_alerts(camera="<short>", limit=50)
```

Look at the time distribution, AI memo, and zone hits. Don't trust the
user's framing alone — they may have miscounted, or the issue may be
different from how they described it.

If they gave you a time range:

```
bi_list_alerts(camera="<short>", startdate=<unix>, enddate=<unix>, limit=200)
```

If you need clip context (was it recording? what resolution?):

```
bi_list_clips(camera="<short>", view="alerts", limit=20)
```

## Step 2 — Read top-level config

```
bi_get_camera_config(short="<short>")
```

Note `sense`, `contrast`, `recmode`, `aizones`, profile/schedule flags.
The `_note` field tells you whether you got the admin (deep) or fallback
(shallow) view.

## Step 3 — Drill into what camconfig doesn't expose

Pick the area that matches the symptom:

| Symptom                                | `bi_get_reg` key_path                                |
| -------------------------------------- | ---------------------------------------------------- |
| Wrong AI classification / thresholds   | `AI\\<profile>` (smartconf, smartlabels, smartzones) |
| Trigger zones look wrong               | `Motion\\<profile>` (maskbits_*, objmaxpercent10)    |
| Alerts not firing during PTZ preset    | `PTZ\\Presets` (noalerts flag per preset)            |
| Dahua IVS not reaching BI              | `camevents` (ONVIF event handlers)                   |
| Wrong action (no email/webhook)        | `Alerts\\OnTrigger`                                  |

If `bi_get_reg` returns `meta.stale: true`, stop and ask the user to
re-export the camera before continuing — stale data will lead you astray.

## Step 4 — Form ONE hypothesis

Write it down in user-visible text. Examples:

- "AI confidence is 70 in profile 3; person events at night are scoring
  60-65 because of low contrast — they're below threshold."
- "Preset 5 has noalerts=1 set, so alerts during that preset are
  deliberately suppressed. Either the preset is wrong or the noalerts
  flag is."

Only one hypothesis. If you have multiple, pick the one you can falsify
fastest.

## Step 5 — Test the hypothesis (mutations path)

If `BI_MCP_ALLOW_MUTATIONS` is enabled, you can close the loop:

```
bi_trigger_camera(camera="<short>", memo="diagnose-<symptom>")
bi_list_alerts(camera="<short>", limit=1)
```

The memo should appear on the new alert. If the alert didn't fire, or
fired with a different classification than you expected, your hypothesis
is wrong — go back to step 2.

If mutations are disabled, ask the user to wave at the camera (or
otherwise generate motion) and re-run step 1.

## Step 6 — Report

User-visible answer should include:

- One-line root cause
- The specific setting (registry key, BI menu path) that's responsible
- A concrete change to make (don't apply it yourself — the user owns
  the BI UI)
- Citation: which AGENTS.md / BlueIris_Manual.md section confirms the
  diagnosis

## Anti-patterns

- ❌ Walking all the .reg subkeys "in case something looks wrong" —
  pick the one matching the symptom
- ❌ Forming multiple hypotheses and asking the user which to pursue —
  pick one, test it, iterate
- ❌ Looping `bi_trigger_camera` — one call, observe, move on
- ❌ Re-fetching `bi_list_cameras` for identity facts already in
  `project_camera_roster.md`
