"""Explain why a specific BI alert fired or suppressed each action row.

Given a live alert from ``bi_list_alerts``, walks the camera's decoded
action set (via ``shape_actionset``) and emits per-row evidence:

- The alert's facts (memo, zones, profile-at-trigger, preset-at-trigger,
  trigger source) — pulled from ``bi_get_clip_info``.
- Each row's decoded filter block, presented as raw facts the caller
  can compare against alert facts. Simple filters (object-in-list,
  profile-membership, zone-presence, source-bit) are left for the agent
  to reason over — the data is in the row, the alert is in the envelope.
- Comparator verdicts ONLY for the complex cases where agent reasoning
  is error-prone:
  * Compound predicates (``P1+person``, ``car+licenseplate``)
  * Confidence thresholds (``person:80``)
  * Cross-zone sequencing (``zones_match=cross``)
  * Wait-row downstream gating (``type=13`` with ``mode`` bitmask)
- Cross-reference against the BI log over a ±2-minute window around
  the alert timestamp: every ``MQTT: Publish``, ``Email``, ``SMS``,
  ``FTP``, ``CopyFile``, and ``AI: Alert canceled`` line attributed to
  the camera. Lets the caller see what BI *actually* did, not just what
  the row config implies.

Per AGENTS.md Rule 5.5: this tool surfaces facts, it does not pick
target values. "FIRED vs SUPPRESSED" verdicts here describe BI's
observed/derivable behavior, not a recommendation about what should
happen.

Admin-gated: the log cross-reference goes through ``bi_list_log``'s
admin path.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .. import reg as reg_mod
from .. import shapers
from ..client import BiClients
from ..errors import BiAdminRequired, BiBadRequest, BiError
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA

# ±2-minute log window around the alert timestamp. Most action results
# (MQTT publish, email send, AI cancel) land within seconds; wait-row
# downstream actions may fire after motion ends. 2 minutes covers the
# normal case without dragging in unrelated events.
_LOG_WINDOW_SEC = 120

# BI's `log` cmd only accepts ``aftertime`` (no ``totime``, no per-camera
# filter — manual § log). All trimming is client-side, so an old alert
# would pull the entire global log slice since then. Default refuse if
# the alert is older than this; caller can override with
# ``max_alert_age_h``. 24h covers the "why didn't my push fire?"
# diagnostic workflow without exposing the global-log explosion risk.
_DEFAULT_MAX_ALERT_AGE_H = 24

# Hard cap on log entries returned from BI before we walk them. Even
# within the age guard, a multi-camera install can produce thousands
# of entries per hour; refuse and tell the caller to narrow the window
# rather than burn memory.
_LOG_ENTRY_HARD_CAP = 20_000

# Patterns we recognise in log msg fields as "an action result".
# Each tuple is (kind, regex). The regex captures the action's target
# (topic/recipient/path) where applicable.
_ACTION_LOG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("mqtt", re.compile(r"^MQTT:\s*(?P<status>Publish OK|Publish failed)(?:\s+to\s+(?P<target>\S+))?", re.I)),
    ("email", re.compile(r"^Email:\s*(?P<status>.+)$", re.I)),
    ("sms", re.compile(r"^SMS:\s*(?P<status>.+)$", re.I)),
    ("ftp", re.compile(r"^FTP:\s*(?P<status>.+)$", re.I)),
    ("save", re.compile(r"^CopyFile:\s*(?P<status>.+?)(?:\s+(?P<target>\S+))?$", re.I)),
    ("ai_cancel", re.compile(r"^AI:\s*Alert canceled\s*(?:\[(?P<reason>[^\]]+)\])?", re.I)),
    ("preset_skip", re.compile(r"^Alert skipped\s*\(PTZ preset (?P<preset>\d+)\)", re.I)),
    ("sound", re.compile(r"^Sound:\s*(?P<status>.+)$", re.I)),
    ("run", re.compile(r"^Run:\s*(?P<status>.+)$", re.I)),
    ("push", re.compile(r"^Push:\s*(?P<status>.+)$", re.I)),
]

# Compound-predicate syntax (per BI manual § Conditions): a + joins
# AI labels and/or PTZ presets that must all be present. Optional
# `:N` suffix sets a confidence threshold for the whole combination.
# Examples: "P1+person", "car+licenseplate:80", "person:80".
_COMPOUND_RE = re.compile(
    r"^(?P<terms>[A-Za-z0-9_+]+)(?::(?P<threshold>\d+))?$"
)


def _parse_memo_objects(memo: str | None) -> dict[str, int]:
    """Parse a BI alert memo into {object_name_lower: confidence_pct}.

    BI memos look like ``"person:73%,car:65%"`` or ``"car:71%"``. The
    confidences are integer percentages; we strip the % and return a
    dict for fast lookup by the comparator.
    """
    if not memo or not isinstance(memo, str):
        return {}
    out: dict[str, int] = {}
    for chunk in memo.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            name, _, conf = chunk.partition(":")
            try:
                pct = int(conf.rstrip("%").strip())
            except ValueError:
                pct = 0
            out[name.strip().lower()] = pct
        else:
            out[chunk.lower()] = 0
    return out


def _evaluate_compound_predicate(
    pred: str,
    memo_objects: dict[str, int],
    active_preset: int | None,
) -> dict[str, Any]:
    """Evaluate one compound predicate term against the alert.

    Returns ``{matched: bool, components: [...], threshold: int|None,
    reason: str}``. ``components`` lists each part of the predicate and
    whether it matched, so the caller can see exactly which leg of a
    ``car+licenseplate:80`` predicate failed.

    Returns ``{compound: False, ...}`` for single-term predicates so the
    caller knows the agent should handle simple in-list matching itself.
    """
    if "+" not in pred and ":" not in pred:
        return {"compound": False, "predicate": pred}

    m = _COMPOUND_RE.match(pred.strip())
    if not m:
        return {
            "compound": True,
            "predicate": pred,
            "matched": None,
            "reason": "unparseable predicate; manual review required",
        }
    terms = m.group("terms").split("+")
    threshold_s = m.group("threshold")
    threshold = int(threshold_s) if threshold_s else None

    components: list[dict[str, Any]] = []
    all_matched = True
    for term in terms:
        if not term:
            continue
        if term.startswith("P") and term[1:].isdigit():
            preset_num = int(term[1:])
            matched = active_preset == preset_num
            components.append(
                {
                    "term": term,
                    "kind": "ptz_preset",
                    "required_preset": preset_num,
                    "active_preset": active_preset,
                    "matched": matched,
                }
            )
        else:
            obj = term.lower()
            present = obj in memo_objects
            confidence = memo_objects.get(obj, 0)
            comp: dict[str, Any] = {
                "term": term,
                "kind": "ai_object",
                "present_in_memo": present,
                "memo_confidence_pct": confidence if present else None,
                "matched": present,
            }
            components.append(comp)
            matched = present
        if not matched:
            all_matched = False

    result: dict[str, Any] = {
        "compound": True,
        "predicate": pred,
        "components": components,
    }
    # Confidence threshold only meaningful if all terms matched. Per BI
    # manual: the threshold applies to the combination, so the lowest
    # component confidence is what gets compared.
    if threshold is not None:
        ai_confs = [
            c["memo_confidence_pct"]
            for c in components
            if c["kind"] == "ai_object" and c["memo_confidence_pct"] is not None
        ]
        if ai_confs and all_matched:
            min_conf = min(ai_confs)
            threshold_met = min_conf >= threshold
            result["threshold_pct"] = threshold
            result["min_component_confidence_pct"] = min_conf
            result["threshold_met"] = threshold_met
            result["matched"] = threshold_met
            if not threshold_met:
                result["reason"] = (
                    f"all terms present but min confidence {min_conf}% "
                    f"< threshold {threshold}%"
                )
            else:
                result["matched"] = True
        else:
            result["matched"] = all_matched
            if not all_matched:
                result["reason"] = (
                    "one or more terms missing from alert; threshold not evaluated"
                )
    else:
        result["matched"] = all_matched
        if not all_matched:
            missing = [c["term"] for c in components if not c["matched"]]
            result["reason"] = f"missing required terms: {', '.join(missing)}"

    return result


def _evaluate_object_filter(
    filter_name: str,
    raw_value: str | None,
    memo_objects: dict[str, int],
    active_preset: int | None,
) -> dict[str, Any] | None:
    """For trig_object / trig_skip — break the comma-list into terms and
    return a comparator verdict ONLY for terms that need one (compound
    or threshold). Single bare terms are left for the caller to reason
    over from ``filters.objects`` / ``filters.skip``.
    """
    if not raw_value:
        return None
    terms = [t.strip() for t in str(raw_value).split(",") if t.strip()]
    complex_results: list[dict[str, Any]] = []
    for term in terms:
        if "+" in term or ":" in term:
            complex_results.append(
                _evaluate_compound_predicate(term, memo_objects, active_preset)
            )
    if not complex_results:
        return None
    return {
        "filter": filter_name,
        "complex_terms": complex_results,
        "note": (
            "Verdicts here cover compound (X+Y) and threshold (X:80) "
            "predicates only. Bare-name terms are listed in "
            "filters.objects/filters.skip — match them against alert.memo "
            "directly."
        ),
    }


def _evaluate_cross_zones(
    row_zones: list[str],
    alert_zones_raw: int,
    log_zone_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """For zones_match=cross — was the required zone sequence actually
    crossed during this alert?

    BI's cross-zone semantics (per manual § Trigger sources and zones):
    requires the listed zones to be entered in order during the alert.
    Alert metadata only gives us the final ``zones`` bitmask; the
    *sequence* lives in the log as ``New trigger source/zones: …``
    entries. We surface what's available and mark undetermined when
    the sequence is unreconstructable.
    """
    if not row_zones:
        return {"matched": None, "reason": "row has no required zones"}
    # Decode alert_zones_raw bitmask to letters for comparison.
    # Bit 0 = A, 1 = B, ... per the existing shaper convention.
    present_zones: list[str] = []
    for i, letter in enumerate("ABCDEFGH"):
        if alert_zones_raw & (1 << i):
            present_zones.append(letter)
    all_present = all(z in present_zones for z in row_zones)
    if not all_present:
        missing = [z for z in row_zones if z not in present_zones]
        return {
            "matched": False,
            "reason": f"cross-zone requires {row_zones} but alert zones were {present_zones}; missing {missing}",
            "alert_zones": present_zones,
            "required_zones_in_order": row_zones,
        }
    # All present; sequence check needs log evidence.
    if not log_zone_events:
        return {
            "matched": None,
            "reason": (
                "all required zones present in alert, but cross-zone "
                "ordering can't be confirmed from log (no zone-update "
                "entries found in window)"
            ),
            "alert_zones": present_zones,
            "required_zones_in_order": row_zones,
        }
    # Walk log_zone_events in chronological order, find the required
    # zones in the listed order. A simplistic check: each required zone
    # must appear (with the row's letter) at or after the previous one.
    cursor = 0
    seen_indices: list[int] = []
    for i, ev in enumerate(log_zone_events):
        if cursor >= len(row_zones):
            break
        if row_zones[cursor] in ev.get("zones", []):
            seen_indices.append(i)
            cursor += 1
    matched = cursor >= len(row_zones)
    return {
        "matched": matched,
        "required_zones_in_order": row_zones,
        "alert_zones": present_zones,
        "log_zone_events": log_zone_events,
        "reason": (
            "all required zones appeared in order during alert"
            if matched
            else f"only {cursor}/{len(row_zones)} required zones appeared in order"
        ),
    }


def _evaluate_wait_row(
    row: dict[str, Any],
    alert_ended: bool | None,
    retrigger_in_window: bool,
) -> dict[str, Any]:
    """For type=13 wait rows, judge whether the wait condition was met
    during this alert.

    Wait-row conditions come from the shaped row's ``continue_when``
    field — a list emitted by ``shape_actionset`` containing some
    subset of ``("queues_empty", "no_longer_triggered", "retriggered")``.
    Empty list means the row waits the full breaktime unconditionally.

    Fallback: if ``continue_when`` is missing (older shaper, or a row
    where decoding failed), read ``row["raw"]["mode"]`` directly and
    decode the bitmask here. Schema reference: bit 1 = queues_empty,
    bit 2 = no_longer_triggered, bit 4 = retriggered.
    """
    conditions: list[str] | None = row.get("continue_when")
    raw_mode = (row.get("raw") or {}).get("mode")

    if conditions is None:
        # Shaper didn't emit continue_when (older shaper, or non-int
        # raw mode). Decode here so wait-row evaluation still works.
        if not isinstance(raw_mode, int):
            return {
                "released": None,
                "reason": (
                    "wait row missing both shaped 'continue_when' and "
                    "raw 'mode' fields; cannot evaluate"
                ),
            }
        conditions = []
        if raw_mode & 1:
            conditions.append("queues_empty")
        if raw_mode & 2:
            conditions.append("no_longer_triggered")
        if raw_mode & 4:
            conditions.append("retriggered")

    # Evaluate each condition we can derive.
    derivations: dict[str, Any] = {}
    if "queues_empty" in conditions:
        # Post-hoc, parallel queues are essentially always drained by
        # the time we analyse. Treat as met.
        derivations["queues_empty"] = {"met": True, "basis": "post-hoc analysis"}
    if "no_longer_triggered" in conditions:
        if alert_ended is True:
            derivations["no_longer_triggered"] = {
                "met": True,
                "basis": (
                    "a subsequent Triggered (non-Retriggered) entry for "
                    "this camera appears after the alert timestamp; the "
                    "prior alert is necessarily over"
                ),
            }
        else:
            # alert_ended is None — no camera-scoped evidence either way.
            # Don't claim met=False; the alert may have ended without a
            # follow-up trigger in our log window. Honest answer is
            # undetermined.
            derivations["no_longer_triggered"] = {
                "met": None,
                "basis": (
                    "no camera-scoped end-of-alert evidence in log "
                    "window; alert may still be active OR may have "
                    "ended without a follow-up trigger"
                ),
            }
    if "retriggered" in conditions:
        # If we saw a retrigger in the window we can claim met=True.
        # If we didn't, we genuinely can't tell — a retrigger outside
        # the ±2min window would still satisfy BI's wait condition.
        # Honest answer is "undetermined" rather than a false "no".
        if retrigger_in_window:
            derivations["retriggered"] = {
                "met": True,
                "basis": "log shows retrigger entry within window",
            }
        else:
            derivations["retriggered"] = {
                "met": None,
                "basis": (
                    "no retrigger found in log window; may have happened "
                    "outside ±2min — caller should re-query log with a "
                    "wider window if certainty matters"
                ),
            }

    # mode=0 means "no condition bits set" — per the schema reference,
    # the row waits the full breaktime unconditionally. We have no way
    # to derive a breaktime-elapsed signal from the ±2min log window, so
    # the honest answer is undetermined, NOT released. (Without this
    # guard, all([]) below would return True and falsely report
    # released.)
    if not conditions:
        return {
            "mode_raw": raw_mode,
            "wait_conditions": [],
            "max_wait_ms": row.get("max_wait_ms"),
            "derivations": {},
            "released": None,
            "reason": (
                "mode=0 — wait runs full breaktime unconditionally; "
                "breaktime is not derivable from the log window, so "
                "downstream rows' status is undetermined"
            ),
        }

    # The wait releases when ALL of its conditions are met.
    all_met = all(d.get("met") for d in derivations.values())
    any_undetermined = any(d.get("met") is None for d in derivations.values())

    if any_undetermined:
        released: bool | None = None
        reason = "one or more conditions undetermined; downstream rows' status unknown"
    elif all_met:
        released = True
        reason = "all wait conditions met; downstream rows ran"
    else:
        released = False
        unmet = [k for k, v in derivations.items() if not v.get("met")]
        reason = f"wait blocked by unmet condition(s): {', '.join(unmet)}"

    return {
        "mode_raw": raw_mode,
        "wait_conditions": conditions,
        "max_wait_ms": row.get("max_wait_ms"),
        "derivations": derivations,
        "released": released,
        "reason": reason,
    }


def _classify_log_line(msg: str) -> dict[str, Any] | None:
    """Return a structured classification of a log msg if it matches a
    known action-result pattern; otherwise None."""
    for kind, pat in _ACTION_LOG_PATTERNS:
        m = pat.match(msg)
        if m:
            return {"action_kind": kind, **{k: v for k, v in m.groupdict().items() if v}}
    return None


def _extract_zone_events(log_entries: list[dict[str, Any]], camera: str) -> list[dict[str, Any]]:
    """Pull zone-update lines from the log window into a chronological list
    of {date, zones: [letters]}."""
    out: list[dict[str, Any]] = []
    pat = re.compile(r"New trigger source/zones:\s*(?P<zones>[A-Za-z0-9_,]+)")
    for e in log_entries:
        if e.get("obj") != camera:
            continue
        m = pat.match(e.get("msg", ""))
        if not m:
            continue
        parts = [p.strip() for p in m.group("zones").split(",")]
        zone_letters = [
            p.split("_")[-1] for p in parts
            if p.startswith("Motion_") or (len(p.split("_")[-1]) == 1)
        ]
        out.append({"date": e.get("date"), "zones": zone_letters})
    return out


def _build_row_explanation(
    row: dict[str, Any],
    alert_facts: dict[str, Any],
    log_zone_events: list[dict[str, Any]],
    alert_ended: bool | None,
    retrigger_in_window: bool,
) -> dict[str, Any]:
    """Produce one explanation entry for one action row."""
    kind = row.get("type", "unknown")
    filters = row.get("filters", {}) or {}
    raw = row.get("raw", {}) or {}

    out: dict[str, Any] = {
        "index": row.get("index"),
        "type": kind,
        "enabled": row.get("enabled"),
        "description": row.get("description"),
        "filters": filters,
    }

    if kind == "unknown":
        out["verdict"] = "UNKNOWN"
        out["reason"] = (
            "Action kind not decoded by this version of the shaper; raw row "
            "passed through for manual inspection."
        )
        out["raw_row"] = raw
        return out

    if not row.get("enabled"):
        out["verdict"] = "SUPPRESSED"
        out["reason"] = "row is disabled (enabled=false)"
        return out

    # Wait-row handling — emits WAIT_GATE verdict; the caller uses its
    # released field when judging downstream rows.
    if kind == "wait":
        wait_info = _evaluate_wait_row(row, alert_ended, retrigger_in_window)
        out["verdict"] = "WAIT_GATE"
        out["wait"] = wait_info
        return out

    # Complex-predicate verdicts (compound + threshold) for object/skip
    # filters. Bare-name verdicts are NOT emitted — agent reads
    # filters.objects/filters.skip directly.
    complex_findings: list[dict[str, Any]] = []
    obj_eval = _evaluate_object_filter(
        "trig_object",
        raw.get("trig_object"),
        alert_facts["memo_objects"],
        alert_facts.get("preset_at_trigger"),
    )
    if obj_eval:
        complex_findings.append(obj_eval)
    skip_eval = _evaluate_object_filter(
        "trig_skip",
        raw.get("trig_skip"),
        alert_facts["memo_objects"],
        alert_facts.get("preset_at_trigger"),
    )
    if skip_eval:
        complex_findings.append(skip_eval)
    if complex_findings:
        out["complex_predicate_evaluations"] = complex_findings

    # Cross-zone evaluation when row uses zones_match=cross.
    if filters.get("zones_match") == "cross" and filters.get("zones"):
        out["cross_zone_evaluation"] = _evaluate_cross_zones(
            filters["zones"],
            alert_facts.get("zones_raw", 0),
            log_zone_events,
        )

    # Verdict is left as None when only simple-filter data is in play —
    # the agent reads `filters` and `alert.memo`/`profile`/etc. to
    # render the final FIRED/SUPPRESSED call. Per AGENTS.md Rule 5.5,
    # the tool surfaces evidence; it doesn't override the simple cases.
    out["verdict"] = None
    out["verdict_note"] = (
        "Simple-filter verdict left to caller. Compare alert facts in "
        "the envelope (memo, profile_at_trigger, zones, trigger_source) "
        "against this row's `filters` block. Comparator verdicts for "
        "complex cases are under `complex_predicate_evaluations`, "
        "`cross_zone_evaluation`, and (for wait rows) `wait`."
    )
    return out


def _build_alert_facts(
    clip_info: dict[str, Any],
) -> dict[str, Any]:
    """Distill the clip-info response into the facts the comparator and
    the agent both need.
    """
    memo = clip_info.get("memo")
    return {
        "path": clip_info.get("path"),
        "camera": clip_info.get("camera"),
        "date": clip_info.get("date"),
        "memo": memo,
        "memo_objects": _parse_memo_objects(memo),
        "zones_raw": clip_info.get("zones", 0),
        "profile_at_trigger": clip_info.get("camprofile"),
        "preset_at_trigger": clip_info.get("campreset"),
        "alert_msec": clip_info.get("alertmsec"),
        "clip_msec": clip_info.get("msec"),
        "trigger_offset_ms": clip_info.get("triggeroffset"),
    }


def _has_retrigger(log_entries: list[dict[str, Any]], camera: str) -> bool:
    """Did a 'Retriggered' line for this camera appear in the log window?"""
    for e in log_entries:
        if e.get("obj") == camera and str(e.get("msg", "")).startswith("Retriggered"):
            return True
    return False


def _alert_appears_ended(
    log_entries: list[dict[str, Any]],
    camera: str,
    alert_ts: int,
) -> bool | None:
    """Decide whether the target alert appears to have ended, using
    ONLY camera-scoped log evidence.

    Returns:
      True  — a new ``Triggered`` (non-Retriggered) entry for THIS
              camera appears after the alert timestamp; the prior
              alert is necessarily over.
      None  — no camera-scoped evidence either way; status undetermined.

    Earlier versions returned True any time *any* log line appeared
    after the alert ts. On a busy install (multiple cameras logging
    constantly) that's almost always true regardless of whether the
    target alert had ended, biasing wait-row "untriggered" evaluation
    toward false positives. Caller treats None as undetermined and
    surfaces that uncertainty in the wait-row verdict.
    """
    for e in log_entries:
        d = e.get("_epoch")
        if not isinstance(d, (int, float)):
            continue
        if d <= alert_ts:
            continue
        if e.get("obj") != camera:
            continue
        msg = str(e.get("msg", ""))
        # A NEW Triggered line for this camera proves the prior alert
        # is over. Retriggered keeps the same alert active, so doesn't.
        if "Triggered" in msg and "Retriggered" not in msg:
            return True
    return None


@log_tool_usage("bi_explain_alert_chain")
def _tool_explain_alert_chain(client: BiClients, args: dict) -> Any:
    camera = args.get("camera") or args.get("short")
    path = args.get("path") or args.get("alert_path")
    if not camera:
        raise BiBadRequest("bi_explain_alert_chain requires a 'camera' argument")
    if not path:
        raise BiBadRequest(
            "bi_explain_alert_chain requires a 'path' argument (alert path "
            "from bi_list_alerts, e.g. '@4473131744.bvr')"
        )

    if client.resolve_admin() is None:
        raise BiAdminRequired(
            "bi_explain_alert_chain requires admin Blue Iris credentials for "
            "the log cross-reference. Set BI_ADMIN_USER/BI_ADMIN_PASS, or "
            "grant admin to BI_USER."
        )

    # 1) Fetch alert facts via clipstats. clip_info gives us profile,
    #    preset, zones, memo at trigger time — the source of truth for
    #    what the alert "looked like" to BI.
    clip_raw = client.call("clipstats", path=path)
    clip_info = shapers.shape_clip_info(clip_raw)
    if not clip_info or "camera" not in clip_info:
        raise BiBadRequest(
            f"bi_explain_alert_chain: clip-info lookup for path={path!r} "
            "returned no usable data. Confirm the path is valid (from "
            "bi_list_alerts)."
        )
    if clip_info.get("camera") != camera:
        raise BiBadRequest(
            f"bi_explain_alert_chain: path {path!r} belongs to camera "
            f"{clip_info.get('camera')!r}, not {camera!r}. Pass the matching "
            "camera short name."
        )

    # Guard against clip-path-instead-of-alert-path. BI's clipstats
    # accepts both, but returns alert-specific fields only for an alert
    # path. A clip path comes back with ``alertmsec: 0`` and ``memo:
    # None``, and the tool would silently analyze the wrong epoch and
    # report "everything's normal" — strictly worse than an error.
    # Detect by alertmsec=0 (real alerts always have non-zero alert
    # length) AND no memo (real alerts almost always have a memo on an
    # AI-equipped install). The bi_list_alerts response shape exposes
    # ``path`` (alert id) and ``clip`` (parent clip id); the alert path
    # is what's wanted.
    if (
        not clip_info.get("alertmsec")
        and not clip_info.get("memo")
    ):
        raise BiBadRequest(
            f"bi_explain_alert_chain: path {path!r} resolved to a clip-"
            f"level record (alertmsec=0, no memo), not an alert. From "
            f"bi_list_alerts, pass the alert's `path` field (e.g. the "
            f"alert id), NOT the alert's `clip` field (the parent clip). "
            f"Multiple alerts can share one clip, so the clip path "
            f"can't identify which alert to analyze."
        )

    alert_facts = _build_alert_facts(clip_info)

    # 2) Pull the camera's decoded action set.
    parsed, age_days = reg_mod.parse_reg(camera, key_path="Alerts")
    shaped_actions = shapers.shape_actionset(
        parsed, camera_short=camera, mtime_age_days=age_days, hook="on_trigger"
    )

    # 3) Log cross-reference: ±_LOG_WINDOW_SEC around the alert.
    #    clipstats returns ``date`` as a UTC epoch int in the raw
    #    payload; the shaper converts it to ISO. We need the epoch for
    #    arithmetic, so go to the raw payload.
    alert_ts: int | None = None
    if isinstance(clip_raw, dict):
        raw_date = clip_raw.get("date")
        if isinstance(raw_date, (int, float)):
            alert_ts = int(raw_date)
    if alert_ts is None:
        raise BiBadRequest(
            "bi_explain_alert_chain: couldn't determine alert epoch from "
            "clipstats response. Pass a more recent alert path."
        )

    # Age guard: BI's log cmd has no upper-time bound and no camera
    # filter, so querying with aftertime=<old_ts> would pull the entire
    # global log since the alert. Refuse if the alert is older than
    # max_alert_age_h hours; caller can raise the bound explicitly with
    # the arg if they really need a forensic look at an old alert.
    max_age_h = args.get("max_alert_age_h", _DEFAULT_MAX_ALERT_AGE_H)
    if not isinstance(max_age_h, (int, float)) or max_age_h <= 0:
        raise BiBadRequest(
            "'max_alert_age_h' must be a positive number (hours)"
        )
    age_h = (int(time.time()) - alert_ts) / 3600.0
    if age_h > max_age_h:
        raise BiBadRequest(
            f"bi_explain_alert_chain: alert is {age_h:.1f}h old, exceeds "
            f"max_alert_age_h={max_age_h}. BI's log cmd has no upper-time "
            f"bound, so an older alert would pull the entire global log "
            f"since then. To proceed anyway, pass "
            f"max_alert_age_h={int(age_h) + 1} (be aware the response "
            f"may be slow or hit the {_LOG_ENTRY_HARD_CAP}-entry hard cap)."
        )

    log_after = alert_ts - _LOG_WINDOW_SEC
    log_raw = client.admin_call("log", aftertime=log_after)
    if isinstance(log_raw, list) and len(log_raw) > _LOG_ENTRY_HARD_CAP:
        raise BiBadRequest(
            f"bi_explain_alert_chain: BI returned {len(log_raw)} log "
            f"entries (over the {_LOG_ENTRY_HARD_CAP} hard cap). The "
            f"alert is within the age guard but the log is unexpectedly "
            f"dense; investigate manually via bi_list_log with a tighter "
            f"since= window."
        )

    # raw=true escape hatch (per AGENTS.md tool contract): return the
    # three underlying source payloads verbatim, before any shaping,
    # comparator evaluation, or log classification. Lets a caller
    # debug at the wire level when the shaped output looks wrong.
    if args.get("raw"):
        log_raw_window = log_raw
        if isinstance(log_raw, list):
            log_raw_window = [
                e
                for e in log_raw
                if isinstance(e, dict)
                and isinstance(e.get("date"), (int, float))
                and e["date"] <= alert_ts + _LOG_WINDOW_SEC
            ]
        return {
            "camera": camera,
            "path": path,
            "alert_epoch": alert_ts,
            "window_sec": _LOG_WINDOW_SEC,
            "clipstats_raw": clip_raw,
            "actionset_parsed": parsed,
            "actionset_mtime_age_days": age_days,
            "log_raw_window": log_raw_window,
        }

    # Walk the RAW log entries: they carry ``date`` as an epoch int,
    # which shape_log converts to ISO. We want both — epoch for window
    # math, the ISO/shaped form for output.
    log_window: list[dict[str, Any]] = []
    if isinstance(log_raw, list):
        for raw_entry in log_raw:
            if not isinstance(raw_entry, dict):
                continue
            ts = raw_entry.get("date")
            if not isinstance(ts, (int, float)):
                continue
            if ts > alert_ts + _LOG_WINDOW_SEC:
                continue
            shaped_entry = shapers.shape_log([raw_entry], limit=1)
            if not shaped_entry:
                continue
            entry = shaped_entry[0]
            entry["_epoch"] = int(ts)
            log_window.append(entry)

    # Classify recognised action-result lines for this camera.
    observed_actions: list[dict[str, Any]] = []
    for e in log_window:
        if e.get("obj") != camera:
            continue
        cls = _classify_log_line(str(e.get("msg", "")))
        if cls:
            observed_actions.append(
                {
                    "date": e.get("date"),
                    "epoch_offset_from_alert_sec": int(e["_epoch"]) - int(alert_ts),
                    "level": e.get("level"),
                    "msg": e.get("msg"),
                    **cls,
                }
            )

    zone_events = _extract_zone_events(log_window, camera)
    retrigger_in_window = _has_retrigger(log_window, camera)
    alert_ended = _alert_appears_ended(log_window, camera, int(alert_ts))

    # Strip the internal _epoch helper from anything we'd surface (not
    # currently surfaced, but defensive — log_window can leak into
    # future debug additions).
    for e in log_window:
        e.pop("_epoch", None)

    # 4) Walk rows and build explanations.
    rows_out: list[dict[str, Any]] = []
    on_trigger_block = shaped_actions.get("on_trigger") or {}
    for row in on_trigger_block.get("actions", []):
        rows_out.append(
            _build_row_explanation(
                row, alert_facts, zone_events, alert_ended, retrigger_in_window
            )
        )

    return {
        "alert": alert_facts,
        "actionset_meta": {
            "reg_mtime_age_days": age_days,
            "stale": age_days > 7.0,
            "row_count": len(rows_out),
            "hook": "on_trigger",
        },
        "log_cross_reference": {
            "window_sec": _LOG_WINDOW_SEC,
            "alert_epoch": alert_ts,
            "alert_appears_ended": alert_ended,
            "retrigger_in_window": retrigger_in_window,
            "observed_actions": observed_actions,
            "zone_events": zone_events,
        },
        "rows": rows_out,
        "guidance": (
            "Each row's `filters` block lists the simple criteria (objects, "
            "profiles, zones, trigger sources). Compare them against `alert` "
            "to decide FIRED vs SUPPRESSED for the simple cases. Where this "
            "tool emits a verdict (UNKNOWN, SUPPRESSED-on-disabled, "
            "WAIT_GATE) or evaluations (complex_predicate_evaluations, "
            "cross_zone_evaluation), those cover cases where caller "
            "reasoning is error-prone. Cross-check against "
            "`log_cross_reference.observed_actions` to confirm what BI "
            "actually did."
        ),
    }


def register() -> None:
    register_tool(
        "bi_explain_alert_chain",
        _tool_explain_alert_chain,
        description=(
            "**Use after `bi_list_alerts`** to decode what actions fired on "
            "a specific alert. Pass the alert's `path` from that response. "
            "Explain a specific alert's action chain. Given (camera, "
            "alert_path), returns the alert's facts (memo, profile/preset "
            "at trigger, zones), each action row with its decoded filters, "
            "comparator verdicts for the cases that need them (compound "
            "predicates like 'car+licenseplate', confidence thresholds "
            "like 'person:80', cross-zone sequencing, wait-row gating), "
            "and a ±2-minute log cross-reference of what BI actually did "
            "(MQTT publishes, email/SMS/FTP results, AI cancellations). "
            "Simple filter matches (object-in-list, profile, source bit) "
            "are surfaced as raw facts; the caller decides FIRED vs "
            "SUPPRESSED for those. Admin-gated (uses the log cmd)."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Alert path from bi_list_alerts (e.g. "
                        "'@4473131744.bvr'). Required."
                    ),
                },
                "max_alert_age_h": {
                    "type": "number",
                    "description": (
                        "Refuse to query the log for alerts older than "
                        "this many hours. Default 24. BI's log cmd has "
                        "no upper-time bound, so older alerts trigger a "
                        "global log slice; raise this only when forensic "
                        "review is worth the cost."
                    ),
                },
            },
            "required": ["camera", "path"],
            "additionalProperties": True,
        },
        annotations={
            "readOnlyHint": True,
            "title": "Explain BI alert action chain",
        },
    )
