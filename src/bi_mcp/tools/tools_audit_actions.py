"""Cross-camera action-set cohort-divergence audit.

bi_audit_actions walks every camera's .reg export, runs each through
shape_actionset, then groups action rows into cohorts by a structural
signature (action type + description + a type-specific discriminator like
the PTZ ``command`` int or the ``<CAM>``-templated MQTT path). Within each
cohort it computes the modal value of every leaf field and reports rows
whose value differs as ``outliers``.

This is an **informational** tool — outliers are NOT necessarily bugs.
They are simply values that deviate from how most cameras in the cohort
are configured. Outliers fall into two categories:

  1. **Intentional per-camera customizations** — e.g. one camera's MQTT
     action filters out motion-source triggers because that camera fires
     too often on motion, or one camera fires on a narrower profile set
     because it covers a different schedule.
  2. **Genuine misconfigurations** — typos like ``"Person"`` vs
     ``"person"``, a row left ``enabled: 0`` by accident, a stale
     ``trig_source`` bit from an old config.

The tool can't tell which is which from .reg data alone — the user
knows their intent. Consumers should present outliers as "values worth
reviewing" and ask the user to confirm intent, NOT as bugs to fix. (See
``feedback_known_intentional_outliers`` in project memory if it exists
for items already reviewed.)

Pure-read: no JSON API calls, no admin gating. Source data is the same
.reg exports ``bi_get_actionset`` consumes; results are only as fresh as
the most recent re-export. Stale cameras (mtime > 7 days) are listed in
``meta.cameras_stale`` so the LLM can prompt for a re-export.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from .. import reg as reg_mod
from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest, BiError
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA

_VALID_HOOKS = ("on_trigger", "on_reset", "both")
_DEFAULT_MIN_COHORT = 3

# Fields that are part of the row identity, not the value space being
# audited. Excluded from outlier comparison.
_IDENTITY_FIELDS = frozenset({"index", "type", "description", "raw"})

# Raw-row fields the shaper normalizes (e.g. case-folds) before exposing
# them. The audit re-surfaces them under ``raw.<name>`` paths so
# normalization-masked divergences (typos like "Person" vs "person")
# still get flagged.
_RAW_FIELDS_TO_AUDIT = ("trig_object", "trig_skip", "descript")

# Per-action-kind "identity hint" — the field most likely to identify
# which logical action a row represents when the user hasn't given it a
# distinguishing description. Used ONLY to compute a soft signature for
# the unbucketed cross-reference hint; never for primary cohort
# bucketing (a wrong guess here just produces a less-useful hint, not
# corrupted outlier output). Action kinds absent from this map get no
# soft-signature hint.
_SOFT_KIND_KEY: dict[str, tuple[str, ...]] = {
    "email": ("to",),
    "sms": ("to",),
    "push": ("device_tag",),
    "phone": ("number",),
    "sound": ("path",),
    "dio": ("dio_output", "dio_outputs"),
    "ftp": ("source", "target_camera"),
    "schedule": ("set_schedule", "set_profile"),
    "run": ("run_action",),
}


def _camera_short_token(value: Any, short: str) -> Any:
    """Substitute a camera's short name with the ``<CAM>`` token inside
    string values so per-camera paths like ``ai/SecCam_3/motion`` don't
    false-positive as outliers.

    Uses word-boundary matching to avoid mangling prefix-overlapping
    names: substituting ``SecCam_1`` inside ``ai/SecCam_10/motion`` must
    NOT yield ``ai/<CAM>0/motion``. The boundary requires that neither
    neighbor character is alphanumeric or underscore — matching the
    camera-short regex used by ``reg.resolve_reg_file``.

    Non-string values pass through unchanged; lists recurse.
    """
    if isinstance(value, str):
        if short not in value:
            return value
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(short)}(?![A-Za-z0-9_])"
        return re.sub(pattern, "<CAM>", value)
    if isinstance(value, list):
        return [_camera_short_token(v, short) for v in value]
    return value


def _soft_signature(
    row: dict[str, Any], short: str, hook_name: str
) -> tuple | None:
    """Description-free identity key for cross-cohort hinting.

    Returns ``None`` when the row's kind isn't in ``_SOFT_KIND_KEY`` or
    none of its identity-hint fields are populated — those rows simply
    don't get a hint. The value is templated through
    ``_camera_short_token`` so per-camera substrings don't fragment the
    soft-signature space the same way they would the primary one.
    """
    kind = row.get("type", "unknown")
    if kind == "do_command":
        return (hook_name, kind, ("command", row.get("command")))
    if kind == "web_or_mqtt":
        templated = _camera_short_token(row.get("path", ""), short)
        return (hook_name, kind, ("path_template", templated))
    fields = _SOFT_KIND_KEY.get(kind)
    if not fields:
        return None
    key_parts: list[tuple[str, Any]] = []
    for f in fields:
        if f in row and row[f] not in (None, "", []):
            key_parts.append((f, _freeze(_camera_short_token(row[f], short))))
    if not key_parts:
        return None
    return (hook_name, kind, tuple(key_parts))


def _row_signature(row: dict[str, Any], short: str, hook_name: str) -> tuple:
    """Bucket key for cohort grouping. Same signature → same logical row
    across cameras. ``description`` is lowercased so a typo'd description
    still buckets together (and surfaces as an outlier on its own).

    ``hook_name`` (``"on_trigger"`` / ``"on_reset"``) is part of the
    signature: OnTrigger and OnReset rows are different logical rule sets
    and must not bucket together, even when their other fields match.

    Per-kind discriminators keep PTZ-preset rows for different presets
    from collapsing into one bucket (each preset is its own cohort) and
    keep MQTT rows pointing at different topic templates separate.
    """
    kind = row.get("type", "unknown")
    desc = (row.get("description") or "").strip().lower()
    if kind == "do_command":
        return (hook_name, kind, desc, ("command", row.get("command")))
    if kind == "web_or_mqtt":
        templated = _camera_short_token(row.get("path", ""), short)
        return (hook_name, kind, desc, ("path_template", templated))
    return (hook_name, kind, desc, None)


def _walk_leaves(obj: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (dotted_path, leaf_value) for every leaf in a nested dict.
    Lists are treated as leaves (compared as a whole) to keep
    multi-value fields like ``filters.objects`` semantically intact."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _IDENTITY_FIELDS:
                continue
            sub = f"{prefix}.{k}" if prefix else k
            yield from _walk_leaves(v, sub)
    else:
        yield prefix, obj


def _freeze(value: Any) -> Any:
    """Make a value hashable so it can key a Counter. Lists become tuples;
    dicts become sorted item tuples."""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


def _unfreeze_for_json(value: Any) -> Any:
    """Inverse of ``_freeze`` for hint payloads only.

    Soft-signature components run through ``_freeze`` so they can hash
    into a dict key. When those frozen forms are later embedded in
    output (``matched_on`` on cohort-match hints), they need to be
    rehydrated to JSON-native shapes (list for sequences, dict for the
    sorted-item-tuple form of dicts). The heuristic for telling a
    frozen-dict from a frozen-list: a tuple of 2-tuples whose first
    elements are all strings is treated as a dict; everything else as a
    list.
    """
    if isinstance(value, tuple):
        if value and all(
            isinstance(p, tuple) and len(p) == 2 and isinstance(p[0], str)
            for p in value
        ):
            return {k: _unfreeze_for_json(v) for k, v in value}
        return [_unfreeze_for_json(v) for v in value]
    return value


def _modal(values: list[Any]) -> tuple[Any, int]:
    """Return (modal_value, count). Ties broken by first-seen order."""
    counter = Counter(_freeze(v) for v in values)
    # Counter.most_common preserves insertion order on ties (Py 3.7+).
    frozen_top, count = counter.most_common(1)[0]
    # Map back to the un-frozen value for output readability.
    for v in values:
        if _freeze(v) == frozen_top:
            return v, count
    return frozen_top, count


def _shape_one_camera(
    short: str, hook: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(shaped, skip_reason)`` for one camera.

    On success: ``(shaped_dict, None)``. On a known BI-side failure
    (missing .reg, parser subprocess error): ``(None, "<error class>: <msg>")``.
    Unexpected exceptions (bugs in the shaper or registry) are NOT
    caught here — they propagate so a real defect surfaces loudly
    instead of silently shrinking the audit's camera set.
    """
    try:
        parsed, age_days = reg_mod.parse_reg(short, key_path="Alerts")
    except BiError as e:
        return None, f"{type(e).__name__}: {e}"
    shaped = shapers.shape_actionset(
        parsed, camera_short=short, mtime_age_days=age_days, hook=hook
    )
    shaped["_mtime_age_days"] = age_days
    return shaped, None


def _collect_rows(
    shaped: dict[str, Any], hook: str
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield (hook_name, row_dict) for the action rows in a shaped
    camera dict, respecting the requested hook scope."""
    for hook_key in ("on_trigger", "on_reset"):
        if hook not in (hook_key, "both"):
            continue
        block = shaped.get(hook_key)
        if not block:
            continue
        for row in block.get("actions", []):
            yield hook_key, row


def _audit(
    shaped_by_camera: dict[str, dict[str, Any]],
    hook: str,
    min_cohort: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bucket rows by signature, return (cohorts, unbucketed)."""
    # signature -> list of (camera, hook, row)
    buckets: dict[tuple, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    for short, shaped in shaped_by_camera.items():
        for hook_name, row in _collect_rows(shaped, hook):
            sig = _row_signature(row, short, hook_name)
            buckets[sig].append((short, hook_name, row))

    cohorts: list[dict[str, Any]] = []
    unbucketed: list[dict[str, Any]] = []

    for sig, members in buckets.items():
        hook_name, kind, desc_lower, type_key = sig
        member_ids = [f"{c}#{r['index']}@{h}" for c, h, r in members]

        if len(members) < min_cohort:
            unbucketed.append(
                {
                    "signature": {
                        "hook": hook_name,
                        "type": kind,
                        "description_lower": desc_lower,
                        "type_specific": type_key,
                    },
                    "size": len(members),
                    "members": member_ids,
                    # Stashed for post-pass cohort-match hinting; stripped
                    # before return so it doesn't leak into output.
                    "_raw_members": members,
                }
            )
            continue

        # Collect templated leaves per member.
        member_leaves: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        for short, hook_name, row in members:
            templated = {
                path: _camera_short_token(val, short)
                for path, val in _walk_leaves(row)
            }
            # Re-surface raw fields the shaper normalizes (case-folding,
            # comma-splitting). These ride alongside the decoded leaves so
            # typos like "Person" vs "person" surface even after the
            # shaper lowercases them.
            raw_row = row.get("raw") or {}
            for f in _RAW_FIELDS_TO_AUDIT:
                if f in raw_row:
                    templated[f"raw.{f}"] = _camera_short_token(raw_row[f], short)
            member_leaves.append((short, hook_name, row, templated))

        # One vote per camera: collapse duplicate-signature rows on the
        # same camera so a camera that happens to have N redundant
        # copies doesn't overweight modal voting against cameras with
        # one. If a camera's duplicates disagree on a field, that
        # camera's vote for that field becomes "inconsistent" and is
        # surfaced as its own finding rather than polluting outliers.
        per_camera: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = (
            defaultdict(list)
        )
        for short, hook_name, row, templated in member_leaves:
            per_camera[short].append((hook_name, row, templated))

        # Every field path that appears in at least one member.
        all_paths = sorted({p for _, _, _, lv in member_leaves for p in lv})

        outlier_findings: list[dict[str, Any]] = []
        disabled_outliers: list[dict[str, Any]] = []
        intra_camera_inconsistency: list[dict[str, Any]] = []

        # Per-field, per-camera vote table. Sentinel ``_INCONSISTENT`` for
        # cameras whose duplicate rows disagree on the field.
        _INCONSISTENT = object()
        for path in all_paths:
            votes: list[tuple[str, Any]] = []  # (camera_short, value-or-sentinel)
            inconsistent_cams: list[dict[str, Any]] = []
            for short, rows in per_camera.items():
                vals = [tpl.get(path) for _, _, tpl in rows]
                # Collapse identical values; sentinel-on-disagreement.
                unique = {_freeze(v) for v in vals}
                if len(unique) == 1:
                    votes.append((short, vals[0]))
                else:
                    votes.append((short, _INCONSISTENT))
                    inconsistent_cams.append(
                        {
                            "camera": short,
                            "values": [
                                {
                                    "row": f"{short}#{row['index']}@{h}",
                                    "value": tpl.get(path),
                                }
                                for h, row, tpl in rows
                            ],
                        }
                    )

            # Outlier voting ignores INCONSISTENT votes — those are
            # their own finding, not data for modal computation.
            consistent_values = [v for _, v in votes if v is not _INCONSISTENT]
            if not consistent_values:
                if inconsistent_cams:
                    intra_camera_inconsistency.append(
                        {"field": path, "cameras": inconsistent_cams}
                    )
                continue

            modal_val, modal_count = _modal(consistent_values)
            if modal_count == len(consistent_values) and not inconsistent_cams:
                continue  # unanimous and no inconsistent cameras; nothing to report
            if modal_count <= len(consistent_values) // 2:
                # No >50% majority among consistent voters; can't call
                # any side an outlier — but inconsistent cameras still
                # warrant a finding if any.
                if inconsistent_cams:
                    intra_camera_inconsistency.append(
                        {"field": path, "cameras": inconsistent_cams}
                    )
                continue

            outliers: list[dict[str, Any]] = []
            for short, val in votes:
                if val is _INCONSISTENT:
                    continue
                if _freeze(val) != _freeze(modal_val):
                    # Report every row from that camera as an outlier
                    # so the operator sees exactly which rows to fix.
                    for h, row, _ in per_camera[short]:
                        outliers.append(
                            {
                                "member": f"{short}#{row['index']}@{h}",
                                "value": val,
                            }
                        )
            entry = {
                "field": path,
                "majority_value": modal_val,
                "majority_count": modal_count,
                "majority_total": len(consistent_values),
                "outliers": outliers,
            }
            if outliers:
                if path == "enabled":
                    disabled_outliers.append(entry)
                else:
                    outlier_findings.append(entry)
            if inconsistent_cams:
                intra_camera_inconsistency.append(
                    {"field": path, "cameras": inconsistent_cams}
                )

        cohort_entry: dict[str, Any] = {
            "signature": {
                "hook": hook_name,
                "type": kind,
                "description_lower": desc_lower,
                "type_specific": type_key,
            },
            "size": len(members),
            "members": member_ids,
            "outliers": outlier_findings,
            "disabled_outliers": disabled_outliers,
            "_raw_members": members,
        }
        if intra_camera_inconsistency:
            cohort_entry["intra_camera_inconsistency"] = intra_camera_inconsistency
        cohorts.append(cohort_entry)

    _attach_cohort_match_hints(cohorts, unbucketed)

    # Strip the private members stash before returning.
    for c in cohorts:
        c.pop("_raw_members", None)
    for u in unbucketed:
        u.pop("_raw_members", None)

    # Stable ordering: largest cohorts first, then by signature for ties.
    cohorts.sort(key=lambda c: (-c["size"], str(c["signature"])))
    unbucketed.sort(key=lambda c: (-c["size"], str(c["signature"])))
    return cohorts, unbucketed


def _attach_cohort_match_hints(
    cohorts: list[dict[str, Any]],
    unbucketed: list[dict[str, Any]],
) -> None:
    """For each unbucketed entry, compute soft signatures of its member
    rows and attach hints pointing at any cohort that shares one.

    The hint surfaces the case where a user named a row on one camera
    but left the same logical row blank on others — those rows end up
    in different primary buckets (because description is in the
    signature) but share a description-free identity, so they're
    likely the same action.

    Soft signatures are computed per row, not per cohort, because two
    rows in the same primary cohort can have different soft signatures
    if their identity fields differ. We index cohorts by every soft
    signature any of their member rows produces.
    """
    if not unbucketed:
        return

    # soft_sig -> list of cohort indices that contain a member with that soft_sig
    cohort_index: dict[tuple, list[int]] = defaultdict(list)
    for i, c in enumerate(cohorts):
        seen: set[tuple] = set()
        for short, hook_name, row in c["_raw_members"]:
            soft = _soft_signature(row, short, hook_name)
            if soft is not None and soft not in seen:
                seen.add(soft)
                cohort_index[soft].append(i)

    if not cohort_index:
        return

    for u in unbucketed:
        matches: list[dict[str, Any]] = []
        seen_cohort_ids: set[int] = set()
        for short, hook_name, row in u["_raw_members"]:
            soft = _soft_signature(row, short, hook_name)
            if soft is None:
                continue
            for cid in cohort_index.get(soft, ()):
                if cid in seen_cohort_ids:
                    continue
                seen_cohort_ids.add(cid)
                cohort = cohorts[cid]
                # Describe what matched (the soft-key field/value pairs)
                # so the reader can see why the hint fired.
                matched_on: dict[str, Any] = {}
                if isinstance(soft[-1], tuple) and soft[-1]:
                    last = soft[-1]
                    # Two possible shapes:
                    #   bare pair:      ("field", value)           — used by
                    #                    structured kinds (do_command,
                    #                    web_or_mqtt).
                    #   tuple of pairs: (("field", value), ...)    — used by
                    #                    _SOFT_KIND_KEY kinds.
                    # Discriminate purely by the first element's type: a
                    # bare pair starts with a string, a tuple-of-pairs
                    # starts with a tuple. This is independent of the
                    # value's type, so a frozen-list value (`("x","y","z")`)
                    # in either shape stays correct. Values may have been
                    # ``_freeze``d (lists → tuples, dicts → sorted-item
                    # tuples) so rehydrate to JSON-native shapes before
                    # they leave the audit boundary.
                    if len(last) == 2 and isinstance(last[0], str):
                        matched_on[last[0]] = _unfreeze_for_json(last[1])
                    else:
                        for pair in last:
                            if isinstance(pair, tuple) and len(pair) == 2:
                                matched_on[pair[0]] = _unfreeze_for_json(pair[1])
                matches.append(
                    {
                        "cohort_signature": cohort["signature"],
                        "cohort_size": cohort["size"],
                        "matched_on": matched_on,
                    }
                )
        if matches:
            u["possible_cohort_matches"] = matches
            u["hint"] = (
                "This row's description differs from a cohort that "
                "shares its identity fields. Consider naming the "
                "description consistently across cameras so the audit "
                "can compare them directly."
            )


@log_tool_usage("bi_audit_actions")
def _tool_audit_actions(client: BiClients, args: dict) -> Any:
    cameras = args.get("cameras")
    if cameras is None:
        cameras = reg_mod.list_reg_cameras()
    elif not isinstance(cameras, list) or not all(isinstance(c, str) for c in cameras):
        raise BiBadRequest("bi_audit_actions 'cameras' must be a list of strings")

    hook = args.get("hook", "both")
    if hook not in _VALID_HOOKS:
        raise BiBadRequest(
            f"bi_audit_actions 'hook' must be one of {_VALID_HOOKS}, got {hook!r}"
        )

    min_cohort = args.get("min_cohort", _DEFAULT_MIN_COHORT)
    if not isinstance(min_cohort, int) or min_cohort < 2:
        raise BiBadRequest("bi_audit_actions 'min_cohort' must be an int >= 2")

    shaped_by_camera: dict[str, dict[str, Any]] = {}
    skip_reasons: dict[str, str] = {}
    stale: list[str] = []
    for short in cameras:
        shaped, reason = _shape_one_camera(short, hook)
        if shaped is None:
            skip_reasons[short] = reason or "unknown"
            continue
        if shaped.get("_mtime_age_days", 0) > 7.0:
            stale.append(short)
        shaped_by_camera[short] = shaped

    if args.get("raw"):
        # Strip the private mtime helper before returning.
        for s in shaped_by_camera.values():
            s.pop("_mtime_age_days", None)
        return {
            "cameras_scanned": sorted(shaped_by_camera),
            "cameras_skipped": sorted(skip_reasons),
            "skip_reasons": skip_reasons,
            "cameras_stale": stale,
            "shaped": shaped_by_camera,
        }

    cohorts, unbucketed = _audit(shaped_by_camera, hook, min_cohort)

    return {
        "meta": {
            "cameras_scanned": sorted(shaped_by_camera),
            "cameras_skipped": sorted(skip_reasons),
            "skip_reasons": skip_reasons,
            "cameras_stale": stale,
            "hook": hook,
            "min_cohort": min_cohort,
            "cohort_count": len(cohorts),
            "unbucketed_count": len(unbucketed),
        },
        "cohorts": cohorts,
        "unbucketed": unbucketed,
    }


def register() -> None:
    register_tool(
        "bi_audit_actions",
        _tool_audit_actions,
        description=(
            "**Informational tool** — surfaces cross-camera action-row "
            "outliers for user review. Walks every camera's .reg export, "
            "buckets action rows into cohorts by (type, description, "
            "type-specific key), and reports fields where one camera's "
            "value deviates from the cohort's modal value under "
            "'outliers'. Per-camera path tokens (e.g. 'ai/SecCam_3/"
            "motion') are templated to '<CAM>' before comparison so "
            "legitimate per-camera substitution doesn't false-positive. "
            "The 'enabled' field is reported separately under "
            "'disabled_outliers' so a row left disabled by accident is "
            "easy to spot. **Outliers are NOT necessarily bugs** — they "
            "may be intentional per-camera customizations (e.g. one "
            "camera filtering different trigger sources, or running a "
            "narrower profile set). Present findings to the user as "
            "'values worth confirming' and ask whether each is "
            "intentional. Pure read; no live BI connection."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "cameras": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of camera short names to audit. "
                        "Defaults to every camera with a .reg export."
                    ),
                },
                "hook": {
                    "type": "string",
                    "enum": list(_VALID_HOOKS),
                    "description": (
                        "Which hook(s) to audit: 'on_trigger', 'on_reset', "
                        "or 'both' (default)."
                    ),
                },
                "min_cohort": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        "Minimum cohort size before outliers are computed. "
                        "Cohorts smaller than this are listed under "
                        "'unbucketed' for visibility but not analyzed. "
                        "Default 3."
                    ),
                },
            },
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Audit BI action-set cohort divergences"},
    )
