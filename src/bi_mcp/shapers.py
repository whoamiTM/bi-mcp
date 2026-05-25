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


def shape_motion_config(raw: Any) -> dict[str, Any]:
    """Shape `camconfig` for `bi_get_camera_motion_config`.

    Surfaces the two nested config subtrees BI exposes on the wire (`setmotion`
    and `setpost`) under stable names, with verbatim raw twins for callers that
    need to escape the shaping.

    Empirical recon 2026-05-23 (BI 5.9.9.71) across PTZ master, fixed cam, and
    clone confirmed:
      * `camconfig` exposes exactly `setmotion` + `setpost`. There is no
        `setai`. AI thresholds remain `bi_get_reg(key_path="AI\\<n>")`-only.
      * All 12 `setmotion` keys present on every camera topology — no
        clone-inherit branch needed, no PTZ-vs-fixed shape branch needed.
      * Wire-side `setmotion.usemask` ↔ `.reg Motion.mask`. Type coercion for
        showmotion/shadows/luminance: int (.reg) → bool (wire). Shaper does NOT
        normalize either — wire shape is preserved verbatim so the raw twin
        and the shaped view always agree.

    The tool layer (`_tool_get_camera_motion_config`) enforces the invariant
    that `raw` is a dict containing both `setmotion` and `setpost` as dicts
    before calling this shaper — so we don't re-validate here. Callers who
    invoke the shaper directly outside that tool path must enforce the same
    invariant themselves or accept that malformed input may produce an
    incomplete-looking but structurally-valid response.
    """
    motion = raw["setmotion"]
    post = raw["setpost"]
    return _drop_empty({
        "motion": dict(motion),
        "post": dict(post),
        "motion_raw": dict(motion),
        "post_raw": dict(post),
        "_source": "camconfig",
    })


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
    """Shape the `log` response — array of log entries.

    Per-entry fields (empirical, BI 5.9.9.71): ``date`` (UTC sec), ``level``,
    ``obj``, ``msg``, optional ``count`` (a string in the raw payload).
    """
    if not isinstance(raw, list):
        return [{"raw": raw}]
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        shaped = _drop_empty(_replace_ts(entry, ("date",)))
        if "count" in shaped:
            try:
                shaped["count"] = int(shaped["count"])
            except (TypeError, ValueError):
                pass
        out.append(shaped)
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
      verified: explicit verification status, surfaced in the response as
          a separate field from ``ok``. None = verify wasn't run (or
          succeeded on a non-stream-dip op, where reaching the shaper at
          all means verify passed). True = post-write read confirmed the
          change. False = post-write read could not confirm (verify-side
          blip per ``BiVerifyInconclusive``, or stream-dip not observed
          within the sampling window). **``verified`` does NOT affect
          ``ok``.** ``ok`` reflects BI's write-acceptance only; callers
          must check ``verified`` (and ``verify_error_kind`` when False)
          to know whether the change is *confirmed* landed. This split
          exists because some ops (`pause` is additive, `reboot`/`reset`
          are disruptive) are not safely retryable, so an inconclusive
          verify must not look like a retry-triggering failure.
      extras: op-specific extras merged into the response (e.g. for pause:
          ``{"seconds_remaining": int, "is_paused": bool}``).
    """
    if not isinstance(raw, dict):
        return {"raw": raw, "ok": False, "op": op, "camera": camera}
    write_accepted = raw.get("result") != "fail"
    # `ok` reflects BI's write acceptance only. Verification certainty lives
    # entirely in `verified` (and `verify_error_kind` when False). This is
    # deliberate: see docstring above. Codex adversarial review 2026-05-23
    # flagged the old `ok = accepted AND verified` shape as a duplicate-
    # action hazard for non-idempotent ops (pause additive, reboot/reset
    # disruptive) under transient verify outages.
    ok = write_accepted
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


# Per-profile pages where BI exposes the "Sync this profile with profile 1"
# checkbox at the top of the tab. When that box is checked, BI either omits
# the `<Page>\<N>` subkey entirely OR writes it with `sync: 1, camsync: ""`
# — in both cases the live config for profile N is whatever `<Page>\1` (or
# `<Page>` with no number, for Motion's off-by-one quirk) contains.
#
# When the box is unchecked, BI writes `sync: 0` and the subkey's fields
# become the live config for profile N.
#
# Cross-camera sync (a different mechanism) lives on profile 1 with
# `camsync: "<other_cam>"`; that is NOT a passthrough and is not flagged.
#
# Motion uses an off-by-one numbering quirk ("legacy reasons", per
# reg.py docstring): `Motion` (no number) is profile 1, `Motion\1` is
# profile 2, etc. The other pages use straight 1:1 indexing. So for
# Motion we annotate from `Motion\1` upward; for the others, from
# `<Page>\2` upward.
_PROFILE_SYNC_PAGES = ("Alerts", "Motion", "AI", "Clips", "Watchdog")
_PROFILE_SYNC_MIN_N: dict[str, int] = {
    "Motion": 1,  # Motion\1 == profile 2
    # Other pages default to 2 (handled below).
}


def _is_profile_sync_passthrough(key: str, val: Any) -> bool:
    """Return True iff `<key, val>` represents a sync-with-profile-1
    passthrough (profile >= 2 under a per-profile page, with `sync == 1`
    and empty `camsync`).

    See `_annotate_profile_sync_passthroughs` for the encoding context.
    """
    if not isinstance(val, dict):
        return False
    parts = key.split("\\")
    if len(parts) != 2:
        return False
    page, tail = parts
    if page not in _PROFILE_SYNC_PAGES:
        return False
    if not tail.isdigit():
        return False
    if int(tail) < _PROFILE_SYNC_MIN_N.get(page, 2):
        return False
    return val.get("sync") == 1 and val.get("camsync", "") == ""


def _annotate_profile_sync_passthroughs(parsed: dict[str, Any]) -> dict[str, Any]:
    """Return a new outer dict where passthrough subkey values are
    shallow-copied with `_synced_with: "profile_1"` added. Non-passthrough
    values are reused by reference — the input dict and its inner dicts
    are NOT mutated, so callers can safely reuse `parsed` afterward.

    A passthrough is identified by `sync == 1` AND empty `camsync` on a
    profile-N subkey for N >= 2 (where Motion's off-by-one quirk means
    N >= 1 under the `Motion` page). The real config for these profiles
    lives in `<Page>\\1` (or in `Motion` with no number, for the Motion
    quirk). Subkeys ABSENT from the hive are also passthroughs (BI's
    default state for new profiles), but we can't annotate what isn't
    there.
    """
    out: dict[str, Any] = {}
    for key, val in parsed.items():
        if _is_profile_sync_passthrough(key, val):
            copy = dict(val)
            copy["_synced_with"] = "profile_1"
            out[key] = copy
        else:
            out[key] = val
    return out


def shape_reg(parsed: dict[str, Any], camera_short: str, mtime_age_days: float) -> dict[str, Any]:
    """Shape the parsed .reg hive output.

    ``parsed`` is the dict produced by ``reg.py::parse_reg`` (keyed by hive
    subpath). We attach a top-level ``meta`` block with the camera name, the
    file mtime age in days, and a ``stale`` flag for the warning path.

    Profile-N (N>=2) subkeys under Alerts/Motion/AI/Clips/Watchdog get a
    `_synced_with: "profile_1"` marker when they're sync-with-profile-1
    passthroughs, so callers don't misread their stale field values as the
    live config. The input ``parsed`` dict is not mutated — passthrough
    entries are shallow-copied before annotation. See
    `_annotate_profile_sync_passthroughs`.
    """
    return _drop_empty(
        {
            "camera": camera_short,
            "meta": {
                "mtime_age_days": round(mtime_age_days, 2),
                "stale": mtime_age_days > 7.0,
            },
            "data": _annotate_profile_sync_passthroughs(parsed),
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
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_WEB_PROTO: dict[int, str] = {
    0: "http",
    1: "https",
    2: "mqtt",
}

# run_action enum (Alerts\OnTrigger\N\run_action) for type=2 actions.
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_RUN_ACTION: dict[int, str] = {
    0: "run_program",
    1: "write_file_append",
    2: "write_file_replace",
    3: "delete_file",
}

# trig_allzones enum — zone-match mode for the trig_zones bitmask.
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_TRIG_ALLZONES: dict[int, str] = {
    0: "exact",  # "=" in BI UI
    1: "all",
    2: "any",
}

# trig_source bitmask — which trigger sources fire this action.
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21). Bit 7 observed in our
# exports (e.g. 132 = 4|128, 16514 = 2|128|16384) but unnamed by jaydeel —
# keep `trig_source_raw` alongside the decoded list until identified.
_TRIG_SOURCE_BITS: dict[int, str] = {
    1: "motion",
    2: "onvif",
    3: "audio",
    4: "external",
    5: "dio",
    6: "group",
    14: "ai",
}


def _decode_bitmask(mask: int, labels: list[str]) -> list[str]:
    """Decode a bitmask integer into a list of label strings."""
    if not isinstance(mask, int):
        return []
    return [lbl for i, lbl in enumerate(labels) if mask & (1 << i)]


def _decode_bit_dict(mask: int, bits: dict[int, str]) -> list[str]:
    """Decode a bitmask using a sparse {bit_index: label} map (for non-contiguous bits)."""
    if not isinstance(mask, int):
        return []
    return [bits[b] for b in sorted(bits) if mask & (1 << b)]


# profiles: bits 0-6 = profiles 0-6. Profile 0 is BI's real "Inactive" profile.
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_PROFILE_LABELS = [str(n) for n in range(7)]

# Legacy sentinel: profiles=46 (0x2E) means "no profiles selected".
_PROFILES_NONE_SENTINEL = 46

# trig_zones: bits 0-7 = zones A-H. Per UI3 source (ui3.js ~6114), zone H is
# the "Hotspot" zone in the BI UI — labelled distinctly from regular zones
# A-G. We keep the letter for stability and call out the Hotspot meaning
# in the AGENTS.md decoder table.
_ZONE_LABELS = list("ABCDEFGH")

# diobits: bits 0-31 = DIO 1-32 (1-indexed in BI UI).
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_DIO_LABELS = [str(n) for n in range(1, 33)]


# Individual `command` codes for type=12 (Do Command) actions.
# Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
_DOCMD_INDIVIDUAL: dict[int, str] = {
    1: "admin_request",
    58491: "camera_restart",
    58508: "camera_trigger",
    58473: "camera_snapshot",
    32796: "camera_enable",
    32797: "camera_disable",
    32798: "camera_toggle_enable",
    58662: "camera_show",
    58663: "camera_hide",
    58664: "dio_1_on", 58665: "dio_2_on", 58666: "dio_3_on",
    58674: "dio_1_off", 58675: "dio_2_off",
    58684: "focus_far", 58685: "focus_near",
    58754: "iris_open", 58755: "iris_close",
    58720: "ir_leds_on", 58721: "ir_leds_off", 58722: "ir_leds_auto",
    58686: "pan_left", 58687: "pan_right",
    58688: "tilt_up", 58689: "tilt_down",
    58690: "ptz_home",
    58691: "zoom_in", 58692: "zoom_out",
    59060: "ptz_speed_increase", 59061: "ptz_speed_decrease",
    32945: "patrol_on", 32946: "patrol_off",
    58725: "ptz_preset_previous",
    58694: "mode_50hz", 58695: "mode_60hz", 58696: "mode_outdoor",
    32930: "overlay_toggle",
    32890: "pause_indefinite",
    32891: "pause_reset",
    32892: "pause_add_30s",
    32893: "pause_add_5m", 32901: "pause_add_15m", 32894: "pause_add_30m",
    32895: "pause_add_1h", 32896: "pause_add_2h", 32897: "pause_add_3h",
    32898: "pause_add_5h", 32899: "pause_add_10h",
    58500: "record_toggle",
    58723: "reboot_camera", 58782: "reboot_pc",
    58970: "shutter_1_6", 58971: "shutter_1_12", 58972: "shutter_1_30",
    58973: "shutter_1_60", 58974: "shutter_1_90",
    58976: "shutter_1_200", 58977: "shutter_1_250", 58978: "shutter_1_500",
    58979: "shutter_1_1000", 58980: "shutter_1_2000", 58981: "shutter_1_4000",
    58752: "wiper_off", 58753: "wiper_on",
}


def _decode_command(cmd: int) -> dict[str, Any]:
    """Decode the `command` integer in a type=12 (Do Command) action.

    Source: jaydeel ipcamtalk thread 85627 (2026-05-21).
    """
    if not isinstance(cmd, int):
        return {"command_raw": cmd}
    if 2201 <= cmd <= 2456:
        return {"command": "ptz_preset", "preset": cmd - 2200}
    if 33203 <= cmd <= 33210:
        return {"command": "action_set", "set": cmd - 33202}
    if 58697 <= cmd <= 58712:
        return {"command": "brightness", "value": cmd - 58697}
    if 58713 <= cmd <= 58719:
        return {"command": "contrast", "value": cmd - 58713}
    if 58985 <= cmd <= 58995:
        return {"command": "gain", "percent": (cmd - 58985) * 10}
    if 59050 <= cmd <= 59059:
        return {"command": "ptz_speed", "value": cmd - 59049}
    if 59027 <= cmd <= 59034:
        return {"command": "set_output", "output": cmd - 59026}
    if 59035 <= cmd <= 59042:
        return {"command": "reset_output", "output": cmd - 59034}
    if cmd in _DOCMD_INDIVIDUAL:
        return {"command": _DOCMD_INDIVIDUAL[cmd]}
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
        p = raw["profiles"]
        if isinstance(p, int) and p == _PROFILES_NONE_SENTINEL:
            filters["profiles"] = []
        else:
            filters["profiles"] = _decode_bitmask(p, _PROFILE_LABELS)
    if "trig_zones" in raw:
        filters["zones"] = _decode_bitmask(raw["trig_zones"], _ZONE_LABELS)
    if "trig_allzones" in raw:
        mode_int = raw["trig_allzones"]
        mode = _TRIG_ALLZONES.get(mode_int)
        if mode:
            filters["zones_match"] = mode
        else:
            filters["zones_match_raw"] = mode_int
    if raw.get("trig_object"):
        # comma-separated, lowercase for stability (some entries use "Person")
        filters["objects"] = [s.strip().lower() for s in str(raw["trig_object"]).split(",") if s.strip()]
    if raw.get("trig_skip"):
        filters["skip"] = raw["trig_skip"]
    if "trig_source" in raw:
        ts = raw["trig_source"]
        decoded = _decode_bit_dict(ts, _TRIG_SOURCE_BITS)
        # Only emit the decoded list when at least one bit decoded — empty
        # would be ambiguous with "no sources set". Bit 7 (=128) is observed
        # but unnamed; raw is always preserved for audit.
        if decoded:
            filters["trig_source"] = decoded
        filters["trig_source_raw"] = ts
    # diobits is a universal per-row DIO trigger-gate bitmask (channel N = bit
    # N-1). Distinct from type=7's dio_number (output channel the row drives).
    if "diobits" in raw:
        db = raw["diobits"]
        if isinstance(db, int) and db != 0:
            filters["dio_trigger_gate"] = _decode_bitmask(db, _DIO_LABELS)
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

    elif kind == "run":
        ra_int = raw.get("run_action")
        if ra_int is not None:
            sub = _RUN_ACTION.get(ra_int)
            if sub:
                out["run_action"] = sub
            else:
                out["run_action_raw"] = ra_int

    elif kind == "dio":
        # type=7 output channel is dio_number (single int, 1-based). Legacy
        # fallback to diobits for older exports that lacked the field.
        if "dio_number" in raw:
            out["dio_output"] = raw["dio_number"]
        elif "diobits" in raw:
            out["dio_outputs"] = _decode_bitmask(raw["diobits"], _DIO_LABELS)
        if raw.get("dio_time_ms") not in (None, 0):
            out["pulse_ms"] = raw["dio_time_ms"]
        if raw.get("camdio"):
            out["use_camera_dio"] = True

    elif kind == "sound":
        for src, dst in [
            ("sound_path", "path"),
            ("sound_volume", "volume"),
            ("sound_devname", "device_name"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]
        if raw.get("sound_camera"):
            out["play_on_camera"] = True
        if raw.get("sound_device"):
            out["play_on_device"] = True
        if raw.get("sound_loop"):
            out["loop"] = True
            if raw.get("sound_looptime"):
                out["loop_seconds"] = raw["sound_looptime"]
        if raw.get("remote"):
            out["execute_on_remote"] = True

    elif kind == "push":
        for src, dst in [
            ("title", "title"),
            ("text", "body"),
            ("tags", "device_tag"),
            ("push_sound", "sound"),
            ("push_imagegroup", "image_group"),
            ("volume", "volume"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]
        for flag in ("richpush", "richgif", "critical", "camgroup"):
            if raw.get(flag):
                out[flag] = True

    elif kind == "email":
        for src, dst in [
            ("email_recipient", "to"),
            ("email_subject", "subject"),
            ("email_body", "body"),
            ("email_server", "server"),
            ("email_imagegroup", "image_group"),
            ("email_mp4sec", "mp4_seconds"),
            ("email_waitsec", "wait_seconds"),
            ("emailquality", "image_quality"),
            ("emailscale", "image_scale_pct"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]
        if raw.get("email_2images"):
            out["include_before_after"] = True
        if raw.get("email_alertimage"):
            out["include_alert_image"] = True
        if raw.get("email_images"):
            out["include_images"] = True
        if raw.get("mp4audio"):
            out["mp4_audio"] = True

    elif kind == "sms":
        for src, dst in [
            ("sms_to", "to"),
            ("sms_text", "body"),
            ("sms_subject", "subject"),
            ("sms_carrier", "carrier"),
            ("sms_gateway", "gateway"),
            ("sms_server", "server"),
            ("sms_imagegroup", "image_group"),
            ("sms_quality", "image_quality"),
            ("sms_scale", "image_scale_pct"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]

    elif kind == "phone":
        for src, dst in [
            ("phone_number", "number"),
            ("phone_number2", "backup_number"),
            ("phone_soundpath", "sound_path"),
            ("phone_soundname", "sound_profile"),
            ("phone_retries", "retries"),
            ("phone_duration", "max_seconds"),
        ]:
            if raw.get(src) not in (None, "", 0):
                out[dst] = raw[src]
        if raw.get("phone_audio"):
            out["audio_enabled"] = True

    elif kind == "toast":
        if raw.get("text"):
            out["body"] = raw["text"]
        if raw.get("image"):
            out["include_hero_image"] = True

    elif kind == "ftp":  # Save JPEG/MP4 (local and/or FTP)
        # source enum: 1=specific_file, 2=group_image, 3=current_camera_image,
        # 4=alert_media. UI options "alert image" and "alert MP4" both write
        # source=4; mp4sec/mp4audio presence distinguishes them.
        src_int = raw.get("source")
        src_map = {1: "specific_file", 2: "group_image",
                   3: "current_camera_image", 4: "alert_media"}
        if src_int in src_map:
            out["source"] = src_map[src_int]
            if src_int == 4:
                if raw.get("mp4sec"):
                    out["alert_media_kind"] = "mp4"
                    out["mp4_seconds"] = raw["mp4sec"]
                    if raw.get("mp4audio"):
                        out["mp4_audio"] = True
                else:
                    out["alert_media_kind"] = "image"
                if raw.get("markup"):
                    out["include_ai_markup"] = True
        elif src_int is not None:
            out["source_raw"] = src_int
        if raw.get("local"):
            out["save_local"] = True
        if raw.get("ftp"):
            out["save_ftp"] = True
        for src, dst in [
            ("localfolder", "local_folder"),
            ("ftpserver", "ftp_server"),
            ("ftpfolder", "ftp_folder"),
            ("remote", "filename_template"),
            ("filename", "filename_override"),
            ("groupname", "group_name"),
            ("camname", "target_camera"),
        ]:
            if raw.get(src) not in (None, "", "(default)"):
                out[dst] = raw[src]

    elif kind == "shield":
        out["shield"] = bool(raw.get("shield", 0))

    elif kind == "schedule":
        if raw.get("bschedule"):
            # BI writes `schedule` as RegSZ (schedule name like "Day_Night").
            out["set_schedule"] = raw.get("schedule", "")
        if raw.get("bprofile"):
            out["set_profile"] = raw.get("profile")
        if raw.get("plock"):
            out["lock_profile"] = True

    elif kind == "wait":
        if raw.get("breaktime") is not None:
            out["max_wait_ms"] = raw["breaktime"]
        mode_int = raw.get("mode")
        if isinstance(mode_int, int):
            conds = []
            if mode_int & 1:
                conds.append("queues_empty")
            if mode_int & 2:
                conds.append("no_longer_triggered")
            if mode_int & 4:
                conds.append("retriggered")
            # empty list = wait full max_wait_ms unconditionally
            out["continue_when"] = conds
        for flag, dst in [
            ("cutclip", "cut_clip"),
            ("cancelq", "cancel_previous_actions"),
            ("reqtrigger", "cancel_if_no_longer_triggered"),
            ("newtime", "adopt_current_time"),
            ("newsources", "adopt_new_sources"),
        ]:
            if raw.get(flag):
                out[dst] = True
        if raw.get("crossing"):
            out["zone_crossing"] = raw["crossing"]

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
            "decoder_coverage": "high",
            "decoder_note": (
                "all 13 type codes (0-13, sparse) labeled with per-type "
                "payload decode (sound/push/run/web/email/sms/phone/dio/"
                "toast/save/shield/schedule/do_command/wait); full filter "
                "decode (profiles, trig_zones, trig_allzones, trig_source, "
                "diobits-as-trigger-gate); save (type=9) source enum with "
                "alert-image/MP4 disambiguation; wait (type=13) continue-"
                "when bitmask. Known gap: bit 7 of trig_source (preserved "
                "as trig_source_raw). See bi-mcp/AGENTS.md for the full "
                "table."
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
