"""Response shapers: trim and normalise raw Blue Iris JSON for Claude.

Each shaper takes the raw ``data`` block returned by ``BiClient.call()`` and
returns a shaped dict/list with:

  * Unix epoch timestamps converted to ISO 8601 strings
  * Empty arrays / null fields dropped
  * Long lists capped by a default limit (overridable per-call)
  * Field names left as Blue Iris reports them (no renaming) — keeps the
    shaped output 1:1 mappable back to the manual's response tables

When a caller passes ``raw=True`` at the tool layer, these functions are
skipped and the raw response is returned verbatim.

Reference: ``BlueIris_Manual.md`` § *JSON Interface* (line 8353+).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(epoch: Any) -> str | None:
    """Convert a Unix epoch (int/float seconds) to an ISO 8601 UTC string."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None, '', [], or {}. Keeps 0 and False."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _replace_ts(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out = dict(d)
    for k in keys:
        if k in out:
            iso = _iso(out[k])
            if iso is not None:
                out[k] = iso
    return out


# ---------------------------------------------------------------------------
# per-tool shapers
# ---------------------------------------------------------------------------


def shape_session_info(login_data: dict[str, Any]) -> dict[str, Any]:
    """Shape the `login` response data."""
    if not isinstance(login_data, dict):
        return {"raw": login_data}
    keep = (
        "system name", "version", "license", "support", "tzone",
        "admin", "changeprofile", "ptz", "audio", "clips", "clipcreate",
        "dio", "timelimits",
        "profiles", "schedules", "streams",
    )
    out = {k: login_data[k] for k in keep if k in login_data}
    return _drop_empty(out)


def shape_status(raw: Any) -> dict[str, Any]:
    """Shape the `status` response — system state snapshot."""
    if not isinstance(raw, dict):
        return {"raw": raw}
    # Most fields are useful; just convert any epoch timestamps we recognise.
    return _replace_ts(raw, ("warnings", "lastupdate"))


def shape_camlist(raw: Any, limit: int | None = None) -> list[dict[str, Any]]:
    """Shape the `camlist` response — array of cameras."""
    if not isinstance(raw, list):
        return [{"raw": raw}]
    out: list[dict[str, Any]] = []
    for cam in raw:
        if not isinstance(cam, dict):
            continue
        shaped = _replace_ts(cam, ("lastalertutc", "newalertsutc"))
        out.append(_drop_empty(shaped))
    if limit is not None:
        out = out[: max(0, int(limit))]
    return out


def shape_camera_config_deep(raw: Any) -> Any:
    """Shape the `camconfig` response — a single camera's config dict.

    `camconfig` is undocumented in the BI manual but works on 5.9.9.71. The
    response is a flat dict with a few nested sub-dicts (`setmotion`,
    `setpost`, etc.). We don't enumerate every key — BI may add fields over
    time — we just drop empties recursively and ISO-ify recognised timestamps.
    """
    if not isinstance(raw, dict):
        return {"raw": raw}
    ts_keys = ("lastalertutc", "newalertsutc", "utc", "lastupdate")

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            cleaned = {}
            for k, v in node.items():
                walked = _walk(v)
                if walked in (None, "", [], {}):
                    continue
                cleaned[k] = walked
            return _replace_ts(cleaned, ts_keys)
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    return _walk(raw)


def shape_camera_config(raw: Any, short_name: str) -> dict[str, Any] | None:
    """From a `camlist` response, pick the single camera matching short_name."""
    if not isinstance(raw, list):
        return None
    for cam in raw:
        if isinstance(cam, dict) and (
            cam.get("optionValue") == short_name
            or cam.get("shortName") == short_name
            or cam.get("name") == short_name
        ):
            return _drop_empty(_replace_ts(cam, ("lastalertutc", "newalertsutc")))
    return None


def shape_log(raw: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Shape the `log` response — array of log entries."""
    if not isinstance(raw, list):
        return [{"raw": raw}]
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(_drop_empty(_replace_ts(entry, ("utc", "time"))))
    return out[: max(0, int(limit))]


def shape_alerts(raw: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Shape the `alertlist` response — array of alerts.

    Per manual: `date` is the alert UTC epoch (seconds). `offset` is the
    millisecond offset within the parent clip — NOT a timestamp.
    """
    if not isinstance(raw, list):
        return [{"raw": raw}]
    out: list[dict[str, Any]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        out.append(_drop_empty(_replace_ts(a, ("utc", "date"))))
    return out[: max(0, int(limit))]


def shape_alert_tracks(raw: Any) -> Any:
    """Shape the `tracks` response — AI object tracks for an alert."""
    if isinstance(raw, list):
        return [
            _drop_empty(_replace_ts(t, ("utc",))) if isinstance(t, dict) else t
            for t in raw
        ]
    if isinstance(raw, dict):
        return _drop_empty(_replace_ts(raw, ("utc",)))
    return raw


def shape_clip_info(raw: Any) -> dict[str, Any]:
    """Shape the `clipstats` response — forensic detail for one clip/alert.

    Per manual: `offset` is the millisecond offset within the parent clip —
    NOT a timestamp. Don't run it through `_replace_ts`.
    """
    if not isinstance(raw, dict):
        return {"raw": raw}
    return _drop_empty(_replace_ts(raw, ("utc", "date")))


def shape_timeline(raw: Any) -> Any:
    """Shape the `timeline` response — 24h activity per camera.

    The raw response is highly variable (per BI version). For v1 we drop empty
    fields and convert recognised epochs; future versions can decode the bucket
    array if it becomes a usability problem.
    """
    if isinstance(raw, list):
        return [
            _drop_empty(_replace_ts(t, ("utc", "from", "to"))) if isinstance(t, dict) else t
            for t in raw
        ]
    if isinstance(raw, dict):
        return _drop_empty(_replace_ts(raw, ("utc", "from", "to")))
    return raw


def shape_ptz_status(raw: Any) -> Any:
    """Shape the `ptz` query response — position + preset list.

    Additive shaping: every field BI returns is preserved under its original
    name. We add two derived fields alongside for ergonomics:

    * ``preset_map`` → ``{"N": description, ...}`` map keyed by preset
      number as a string, with empty / placeholder descriptions
      ("", "(undefined)") dropped. Per UI3 source (ui3.js ~7160), the
      original ``presets`` array is **1-indexed by position** and can be
      either an array of strings (``presets[0]`` = preset 1's description)
      or an array of ``{num, desc}`` objects.
    * ``active_preset`` → ``{"num": presetnum, "description": "..."}``
      when ``presetnum`` is set (>0) and resolvable from the preset map.

    The original ``presetnum`` (int) and ``presets`` (array) fields are
    left untouched so any caller that already reads them keeps working.
    """
    if not isinstance(raw, dict):
        return raw

    out: dict[str, Any] = dict(raw)

    presets_in = raw.get("presets")
    preset_map: dict[int, str] = {}
    if isinstance(presets_in, list):
        for i, entry in enumerate(presets_in):
            if isinstance(entry, str):
                desc, num = entry, i + 1
            elif isinstance(entry, dict):
                num = entry.get("num")
                desc = entry.get("desc", "")
                try:
                    num = int(num) if num is not None else i + 1
                except (TypeError, ValueError):
                    num = i + 1
            else:
                continue
            if desc in (None, "", "(undefined)"):
                continue
            preset_map[num] = desc
    if preset_map:
        out["preset_map"] = {str(k): v for k, v in sorted(preset_map.items())}

    presetnum = raw.get("presetnum")
    if isinstance(presetnum, int) and presetnum > 0:
        active: dict[str, Any] = {"num": presetnum}
        if presetnum in preset_map:
            active["description"] = preset_map[presetnum]
        out["active_preset"] = active

    return _drop_empty(out)


# ---------------------------------------------------------------------------
# new shapers (Phase 1+2)
# ---------------------------------------------------------------------------


def shape_cliplist(raw: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Shape the `cliplist` response — array of clips.

    Per manual § *cliplist*: each entry has ``camera``, ``path``, ``offset``
    (ms inside parent clip — NOT a timestamp), ``clip`` (record id), ``date``
    (UTC seconds), ``color``, ``flags`` (int bitmask: 2=flagged, 4=protected,
    64=archive, 512=export), ``res`` (resolution string), etc. We ISO-ify the
    ``date`` and ``utc`` epochs but leave ``offset`` numeric.
    """
    if not isinstance(raw, list):
        return [{"raw": raw}]
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        out.append(_drop_empty(_replace_ts(c, ("utc", "date"))))
    return out[: max(0, int(limit))]


def shape_sysconfig(raw: Any) -> dict[str, Any]:
    """Shape the `sysconfig` response.

    Per manual § *sysconfig*: read-side returns ``archive`` (bool, FTP backup
    enable), ``schedule`` (bool, global-schedule use), ``manrecsec`` (manual
    record time limit in seconds, 0=unlimited). BI builds may include additional
    fields (MQTT state, DIO output state) inline. We drop empties; everything
    else passes through.
    """
    if not isinstance(raw, dict):
        return {"raw": raw}
    return _drop_empty(raw)


def shape_trigger_result(raw: Any) -> dict[str, Any]:
    """Shape the `trigger` response envelope (not the alert that follows).

    BI returns ``{result:"success"}`` on a fired trigger. We surface a
    consistent ``{ok: True}`` so callers can check truthiness, and pass
    through any ``data`` BI included.
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False}
    ok = raw.get("result") == "success"
    out: dict[str, Any] = {"ok": ok}
    data = raw.get("data")
    if data:
        out["data"] = data
    if not ok:
        reason = (raw.get("data") or {}).get("reason") if isinstance(raw.get("data"), dict) else None
        out["reason"] = reason or raw.get("result") or "unknown"
    return out


def shape_ptz_command_result(raw: Any) -> dict[str, Any]:
    """Shape a `ptz` write-side response (e.g. preset recall).

    Same envelope as `trigger`: ``{result:"success"}``. We surface ``{ok: True}``.
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False}
    ok = raw.get("result") == "success"
    out: dict[str, Any] = {"ok": ok}
    if not ok:
        data = raw.get("data")
        reason = data.get("reason") if isinstance(data, dict) else None
        out["reason"] = reason or raw.get("result") or "unknown"
    return out


def shape_profile_set_result(raw: Any, previous_profile: Any = None) -> dict[str, Any]:
    """Shape the `status` (set-profile mode) response.

    BI's ``status`` cmd returns the full status payload after the set; we
    surface the new active profile + the prior profile so a caller can revert.
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False}
    data = raw.get("data") if "data" in raw else raw
    ok = raw.get("result") != "fail"
    out: dict[str, Any] = {"ok": ok}
    if isinstance(data, dict) and "profile" in data:
        out["profile"] = data["profile"]
    if previous_profile is not None:
        out["previous_profile"] = previous_profile
    if not ok:
        reason = (data or {}).get("reason") if isinstance(data, dict) else None
        out["reason"] = reason or "unknown"
    return out


def _shape_export_item(item: dict[str, Any]) -> dict[str, Any]:
    """Shape one export-queue entry.

    BI returns per-item fields (manual § *export* reply table): ``path``,
    ``status`` (queued/active/error/done), ``msec``, ``progress`` (0-100 when
    active), ``uri`` (relative to the New folder; full URL is /clips/{uri}),
    ``utc`` (source clip start, epoch seconds), ``error`` (when status=error),
    ``filesize`` (formatted when status=done). The manual spells the size
    field with a typo (``lesize``) in the docs table; we tolerate both names.
    """
    if not isinstance(item, dict):
        return {"raw": item}
    out = dict(item)
    if "utc" in out:
        iso = _iso(out["utc"])
        if iso is not None:
            out["utc"] = iso
    # Manual table prints "lesize" (a documentation typo from "filesize");
    # real BI builds return "filesize". Normalize either way.
    if "lesize" in out and "filesize" not in out:
        out["filesize"] = out.pop("lesize")
    return _drop_empty(out)


def shape_export_result(raw: Any) -> dict[str, Any]:
    """Shape the `export` cmd reply.

    Three reply shapes from one cmd (manual § *export*):

      * **create / single-status** — a single export-item dict with
        ``path``, ``status``, etc. Returned wrapped in BI's standard
        ``{result, data}`` envelope.
      * **queue list** (no ``path`` sent) — an array of export-item dicts
        under ``data``.
      * **failure** — ``{result: "fail", data: {reason: "..."}}``.

    We surface ``ok`` plus either ``item`` (single) or ``items`` (list).
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False}
    ok = raw.get("result") != "fail"
    out: dict[str, Any] = {"ok": ok}
    data = raw.get("data") if "data" in raw else raw
    if not ok:
        reason = data.get("reason") if isinstance(data, dict) else None
        out["reason"] = reason or raw.get("result") or "unknown"
        return out
    if isinstance(data, list):
        out["items"] = [_shape_export_item(it) for it in data if isinstance(it, dict)]
    elif isinstance(data, dict):
        out["item"] = _shape_export_item(data)
    return out


# Flag bit definitions for the BI `update` cmd (manual § *update*).
# Mirrored in tools_mutations._FLAG_BITS so the tool layer can build flags/mask
# without importing the shaper.
UPDATE_FLAG_BITS: dict[str, int] = {
    "flagged": 2,
    "protected": 4,
    "archive": 64,
    "export_flag": 512,
}


def _decode_update_flags(flags: Any) -> dict[str, bool] | None:
    """Decode an integer ``flags`` field into the named bits we expose.

    Returns ``None`` if ``flags`` isn't an int. Unknown bits are not surfaced
    here — the raw integer remains in the response under ``flags``.
    """
    if not isinstance(flags, bool) and isinstance(flags, int):
        return {name: bool(flags & bit) for name, bit in UPDATE_FLAG_BITS.items()}
    return None


def shape_update_record_result(
    raw: Any,
    *,
    previous_memo: Any = None,
    previous_flags: Any = None,
) -> dict[str, Any]:
    """Shape the `update` cmd reply for ``bi_update_record``.

    The BI ``update`` cmd returns a ``{result, data}`` envelope. ``data``
    echoes the post-update record fields (typically ``memo`` and ``flags``).
    We surface:

      * ``ok`` — derived from ``result``.
      * ``memo`` / ``flags`` — post-update values (when BI echoed them).
      * ``flags_decoded`` — named-bit view of ``flags`` (flagged/protected/
        archive/export_flag) for ergonomic inspection.
      * ``previous_memo`` / ``previous_flags`` / ``previous_flags_decoded`` —
        captured by the tool's read-before-write so callers can revert.
      * ``reason`` — on failure.
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False}
    ok = raw.get("result") != "fail"
    data = raw.get("data") if "data" in raw else raw
    out: dict[str, Any] = {"ok": ok}
    if not ok:
        reason = data.get("reason") if isinstance(data, dict) else None
        out["reason"] = reason or raw.get("result") or "unknown"
        return out
    if isinstance(data, dict):
        if "memo" in data:
            out["memo"] = data["memo"]
        if "flags" in data:
            out["flags"] = data["flags"]
            decoded = _decode_update_flags(data["flags"])
            if decoded is not None:
                out["flags_decoded"] = decoded
    if previous_memo is not None:
        out["previous_memo"] = previous_memo
    if previous_flags is not None:
        out["previous_flags"] = previous_flags
        prev_decoded = _decode_update_flags(previous_flags)
        if prev_decoded is not None:
            out["previous_flags_decoded"] = prev_decoded
    return out


def shape_camera_set_result(
    raw: Any,
    *,
    op: str,
    camera: str,
    previous: Any = None,
    new: Any = None,
    verify_method: str | None = None,
    verified: bool | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape the response from ``bi_set_camera`` (wraps BI ``camconfig`` set-half).

    Pattern mirrors :func:`shape_update_record_result`: the tool runs a
    pre-read, write, post-read flow and hands this shaper everything needed
    for an ergonomic response. The shaper itself does no I/O.

    Args:
      raw: the raw BI ``camconfig`` set reply (``{result, session, data}``).
      op: the op name from the tool layer (``"rename"``, ``"hide"``,
          ``"enable"``, ``"audio"``, ``"output"``, ``"manrec"``, ``"pause"``,
          ``"profile_lock"``, ``"reset"``, ``"reboot"``).
      camera: the target camera short name.
      previous: the pre-write value (op-specific shape; e.g. a string for
          rename, a bool for hide, ``{"profile": int, "lock": int}`` for
          profile_lock).
      new: the verified post-write value (same shape as ``previous``).
      verify_method: one of ``"camconfig"`` (write reply or post-camconfig
          read), ``"camlist"`` (post-camlist read), ``"isOnline_dip"``
          (reset/reboot — confirmed via stream-transition observation), or
          ``None`` (fire-and-forget; e.g. reboot when verify is skipped).
      verified: explicit verification status. When ``None`` (default), ``ok``
          derives from BI's ``result`` only (i.e. the write was accepted).
          When ``True``, ``ok`` is True iff BI accepted AND verify confirmed.
          When ``False``, ``ok`` is forced to False — used by `reboot` when
          the dispatcher couldn't observe the offline transition within its
          sampling window (the write was accepted but the effect is unproven).
          ``verified`` is also surfaced in the response so callers can
          distinguish "write accepted but unproven" from "write accepted and
          confirmed" without parsing the rest.
      extras: op-specific extras merged into the response (e.g. for pause:
          ``{"seconds_remaining": int, "is_paused": bool}``).
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False, "op": op, "camera": camera}
    write_accepted = raw.get("result") != "fail"
    # ok = (BI accepted the write) AND (if a verify outcome was supplied, it succeeded)
    ok = write_accepted and (verified is not False)
    out: dict[str, Any] = {"ok": ok, "op": op, "camera": camera}
    if not write_accepted:
        data = raw.get("data") if "data" in raw else raw
        reason = data.get("reason") if isinstance(data, dict) else None
        out["reason"] = reason or raw.get("result") or "unknown"
        return out
    if verified is not None:
        out["verified"] = verified
    if previous is not None:
        out["previous"] = previous
    if new is not None:
        out["new"] = new
    if verify_method is not None:
        out["verify_method"] = verify_method
    if extras:
        out.update(extras)
    return out


def shape_reg(parsed: dict[str, Any], camera_short: str, mtime_age_days: float) -> dict[str, Any]:
    """Shape the parsed .reg hive output.

    ``parsed`` is the dict produced by ``reg.py::parse_reg`` (keyed by hive
    subpath). We attach a top-level ``meta`` block with the camera name, the
    file mtime age in days, and a ``stale`` flag for the warning path.
    """
    return _drop_empty(
        {
            "camera": camera_short,
            "meta": {
                "mtime_age_days": round(mtime_age_days, 2),
                "stale": mtime_age_days > 7.0,
            },
            "data": parsed,
        }
    )


# ---------------------------------------------------------------------------
# action set decoding (Alerts\OnTrigger\N, Alerts\OnReset\N)
# ---------------------------------------------------------------------------
#
# The decoder tables below were derived empirically from the user's 11-camera
# install (Pass 1, 2026-05-17). Coverage is partial: only the action kinds the
# user has configured are mapped. Unknown codes fall through to type="unknown"
# with the raw integer preserved, so the tool degrades gracefully.

# type codes (Alerts\OnTrigger\N\type). Full 0-13 map confirmed by user
# 2026-05-17. The kind labels are known; per-type payload field names beyond
# type=3 (web/mqtt) and type=12 (do_command) still need Pass 2 mapping.
_ACTION_TYPE: dict[int, str] = {
    0: "sound",
    1: "push",
    2: "run",
    3: "web_or_mqtt",  # disambiguated via web_proto1
    4: "email",
    5: "sms",
    6: "phone",
    7: "dio",
    8: "toast",
    9: "ftp",
    10: "shield",
    11: "schedule",
    12: "do_command",
    13: "wait",
}

# web_proto1 enum (Alerts\OnTrigger\N\web_proto1) for type=3 actions.
# Only value 2 (MQTT) observed.
_WEB_PROTO: dict[int, str] = {
    2: "mqtt",
}


def _decode_bitmask(mask: int, labels: list[str]) -> list[str]:
    """Decode a bitmask integer into a list of label strings."""
    if not isinstance(mask, int):
        return []
    return [lbl for i, lbl in enumerate(labels) if mask & (1 << i)]


# profiles: bits 1-7 = profiles 1-7. Bit 0 is unused (or "inactive").
_PROFILE_LABELS = ["inactive"] + [str(n) for n in range(1, 8)]

# trig_zones: bits 0-7 = zones A-H. Per UI3 source (ui3.js ~6114), zone H is
# the "Hotspot" zone in the BI UI — labelled distinctly from regular zones
# A-G. We keep the letter for stability and call out the Hotspot meaning
# in the AGENTS.md decoder table.
_ZONE_LABELS = list("ABCDEFGH")


def _decode_command(cmd: int) -> dict[str, Any]:
    """Decode the `command` integer in a type=12 (Do Command) action.

    Only the 2200+N "Call PTZ preset N" family is mapped (Pass 1 observation).
    Other Do Command codes need empirical mapping via Pass 2.
    """
    if not isinstance(cmd, int):
        return {"command_raw": cmd}
    if 2200 <= cmd <= 2299:
        return {"command": "ptz_preset", "preset": cmd - 2200}
    return {"command_raw": cmd}


def _shape_action_entry(idx: int, raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one Alerts\\OnTrigger\\N entry into a semantic action dict."""
    t_int = raw.get("type")
    kind = _ACTION_TYPE.get(t_int, "unknown")

    out: dict[str, Any] = {
        "index": idx,
        "type": kind,
        "enabled": bool(raw.get("enabled", 0)),
        "description": raw.get("descript") or None,
    }
    if kind == "unknown":
        out["type_raw"] = t_int

    # filters block (common across all action types)
    filters: dict[str, Any] = {}
    if "profiles" in raw:
        filters["profiles"] = _decode_bitmask(raw["profiles"], _PROFILE_LABELS)
    if "trig_zones" in raw:
        filters["zones"] = _decode_bitmask(raw["trig_zones"], _ZONE_LABELS)
    if raw.get("trig_allzones"):
        filters["zones_all_required"] = True
    if raw.get("trig_object"):
        # comma-separated, lowercase for stability (some entries use "Person")
        filters["objects"] = [s.strip().lower() for s in str(raw["trig_object"]).split(",") if s.strip()]
    if raw.get("trig_skip"):
        filters["skip"] = raw["trig_skip"]
    # trig_source bitmask is only partially decoded — pass through raw int.
    if "trig_source" in raw:
        filters["trig_source_raw"] = raw["trig_source"]
    if filters:
        out["filters"] = filters

    # type-specific fields
    if kind == "web_or_mqtt":
        proto_int = raw.get("web_proto1")
        out["protocol"] = _WEB_PROTO.get(proto_int, "unknown")
        if out["protocol"] == "unknown" and proto_int is not None:
            out["protocol_raw"] = proto_int
        for src, dst in [
            ("web_path", "path"),
            ("web_params", "payload"),
            ("web_headers", "headers"),
            ("web_attempts", "attempts"),
            ("web_timeout", "timeout_s"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]
        if raw.get("mqtt_retain"):
            out["mqtt_retain"] = True

    elif kind == "do_command":
        out.update(_decode_command(raw.get("command")))
        if raw.get("args"):
            out["args"] = raw["args"]
        if raw.get("camname") and raw["camname"] != "(default)":
            out["target_camera"] = raw["camname"]
        if raw.get("remote"):
            out["execute_on_remote"] = True

    # always preserve raw for debugging / unmapped fields
    out["raw"] = raw
    return out


def shape_actionset(
    parsed: dict[str, Any],
    camera_short: str,
    mtime_age_days: float,
    hook: str,
) -> dict[str, Any]:
    """Shape the Alerts hive into semantic on_trigger / on_reset action lists.

    ``hook`` is "on_trigger", "on_reset", or "both" — controls which hook lists
    are included in the output. ``parsed`` is the full Alerts subtree from
    ``reg.parse_reg(short, key_path="Alerts")``.
    """
    def collect(hook_name: str) -> dict[str, Any] | None:
        # OnTrigger / OnReset header key (e.g. {"enabled": 1, "count": 3})
        header_key = f"Alerts\\{hook_name}"
        header = parsed.get(header_key) or {}
        # Numeric children
        actions: list[dict[str, Any]] = []
        prefix = f"Alerts\\{hook_name}\\"
        for k, v in parsed.items():
            if k.startswith(prefix):
                tail = k[len(prefix):]
                if tail.isdigit():
                    actions.append(_shape_action_entry(int(tail), v))
        if not actions and not header:
            return None
        actions.sort(key=lambda a: a["index"])
        return {
            "enabled": bool(header.get("enabled", 0)),
            "declared_count": header.get("count"),
            "actions": actions,
        }

    out: dict[str, Any] = {
        "camera": camera_short,
        "meta": {
            "mtime_age_days": round(mtime_age_days, 2),
            "stale": mtime_age_days > 7.0,
            "decoder_coverage": "partial",
            "decoder_note": (
                "all 14 type codes (0-13) are labeled (sound/push/run/web/"
                "email/sms/phone/dio/toast/ftp/shield/schedule/do_command/"
                "wait). Payload-field decoding is complete for type=3 (web/"
                "mqtt via web_proto1) and type=12 (do_command, command "
                "2200-2299 = ptz_preset); other types pass raw payload "
                "fields through. See bi-mcp/AGENTS.md for the full table."
            ),
        },
    }
    if hook in ("on_trigger", "both"):
        block = collect("OnTrigger")
        if block is not None:
            out["on_trigger"] = block
    if hook in ("on_reset", "both"):
        block = collect("OnReset")
        if block is not None:
            out["on_reset"] = block
    return _drop_empty(out)
