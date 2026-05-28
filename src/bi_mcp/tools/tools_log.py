"""BI system log tool — admin-gated, with client-side filtering."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiAdminRequired
from ..utils.logging import log_tool_usage
from ..utils.time import parse_since
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


def _apply_filters(
    entries: list[dict[str, Any]],
    *,
    obj_match: str | None,
    levels: list[int] | None,
    match: str | None,
    regex_pat: "re.Pattern[str] | None",
) -> list[dict[str, Any]]:
    out = entries
    if obj_match is not None:
        out = [e for e in out if e.get("obj") == obj_match]
    if levels is not None:
        levelset = set(levels)
        out = [e for e in out if e.get("level") in levelset]
    if match is not None:
        needle = match.lower()
        out = [e for e in out if needle in str(e.get("msg", "")).lower()]
    if regex_pat is not None:
        out = [e for e in out if regex_pat.search(str(e.get("msg", "")))]
    return out


@log_tool_usage("bi_list_log")
def _tool_list_log(client: BiClients, args: dict) -> Any:
    if "level" in args:
        raise ValueError(
            "the 'level' arg was renamed to 'levels' (list). "
            "Use levels=[2] for errors, levels=[1,2] for warn+error, etc."
        )
    if "camera" in args and "obj" in args:
        raise ValueError("camera and obj are mutually exclusive — pick one")
    if "match" in args and "regex" in args:
        raise ValueError("match and regex are mutually exclusive — pick one")

    payload: dict[str, Any] = {}
    if "since" in args and args["since"] is not None:
        payload["aftertime"] = parse_since(args["since"])

    obj_match: str | None = args.get("camera") or args.get("obj")

    levels: list[int] | None = None
    if "levels" in args and args["levels"] is not None:
        raw_levels = args["levels"]
        if isinstance(raw_levels, str):
            try:
                raw_levels = json.loads(raw_levels)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"levels={args['levels']!r} not parseable as JSON list"
                ) from e
        if not isinstance(raw_levels, list) or not all(isinstance(x, int) for x in raw_levels):
            raise ValueError("levels must be a list of ints, e.g. [0,1,2] or [3]")
        levels = raw_levels

    regex_pat: "re.Pattern[str] | None" = None
    if "regex" in args and args["regex"] is not None:
        try:
            regex_pat = re.compile(args["regex"], re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"regex did not compile: {e}") from e

    match: str | None = args.get("match")
    limit = int(args.get("limit", 100))

    if client.resolve_admin() is None:
        raise BiAdminRequired(
            "bi_list_log requires admin Blue Iris credentials; the `log` JSON cmd "
            "is gated behind admin. Set BI_ADMIN_USER/BI_ADMIN_PASS in bi-mcp/.env, "
            "or grant admin to the existing BI_USER."
        )

    raw = client.admin_call("log", **payload)
    if args.get("raw"):
        return raw

    shaped = shapers.shape_log(raw, limit=10_000_000)
    scanned = len(shaped)

    filters_active = any(x is not None for x in (obj_match, levels, match, regex_pat))
    filtered = _apply_filters(
        shaped,
        obj_match=obj_match,
        levels=levels,
        match=match,
        regex_pat=regex_pat,
    )
    matched = len(filtered)
    entries = filtered[: max(0, limit)]

    envelope: dict[str, Any] = {
        "entries": entries,
        "scanned": scanned,
        "matched": matched,
    }
    if filters_active and "aftertime" not in payload:
        envelope["warning"] = (
            "unbounded log scan — pass since=… (e.g. '-15m' or an alert timestamp) "
            "to bound the query and avoid scanning the full BI log buffer"
        )
    return envelope


def register() -> None:
    register_tool(
        "bi_list_log",
        _tool_list_log,
        description=(
            "Recent Blue Iris system log entries with optional filters.\n\n"
            "**Pick the right tool:** for reconstructing 'what fired when' on a "
            "camera, start with `bi_list_alerts` — per-alert timestamps with no "
            "dedup. This log is best for system events (profile changes, disk "
            "ops, logins, errors) and *aggregate* activity counts.\n\n"
            "Filters:\n"
            "  since   — UTC epoch sec, ISO-8601, or '-15m'/'-2h'/'-1d' "
            "(server-side via aftertime)\n"
            "  camera  — exact match on entry.obj (clone cameras log under "
            "their own short names)\n"
            "  obj     — exact match on entry.obj (escape hatch: 'App', "
            "'MQTT', 'DB', 'AI_Input', drive letters, usernames)\n"
            "  levels  — list of accepted level ints; empirical: 0=info, "
            "1=warn, 2=error, 3=trigger/alert aggregate (deduped — use "
            "`bi_list_alerts` for per-event), 4=status change, 10=user\n"
            "  match   — case-insensitive substring on entry.msg\n"
            "  regex   — Python regex on entry.msg (IGNORECASE); xor with match\n"
            "  limit   — applied AFTER filtering (default 100)\n\n"
            "Returns {entries, scanned, matched, warning?}. `raw=true` bypasses "
            "the envelope and shaper. Admin required.\n\n"
            "BI aggregates repeated messages: `count` is cumulative since BI "
            "startup (or last log clear), and `date` is when BI **last summed** "
            "the entry, not necessarily the most recent occurrence. To tell "
            "whether a message is actively firing now, re-query with a tight "
            "since=-5m window."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "since": {
                    "description": (
                        "Earliest entry to return. Int (UTC sec), ISO-8601, or "
                        "relative shorthand like '-15m', '-2h', '-1d'."
                    ),
                },
                "camera": {
                    "type": "string",
                    "description": (
                        "Exact match on entry.obj. Clone cameras (e.g. SecCam_11AI) "
                        "have their own short names and log separately."
                    ),
                },
                "obj": {
                    "type": "string",
                    "description": (
                        "Exact match on entry.obj. Use for non-camera subsystems: "
                        "'App', 'MQTT', 'DB', 'AI_Input', 'Alerts', 'Log', drive "
                        "letters ('A:', 'D:'), or usernames."
                    ),
                },
                "levels": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Keep entries whose level is in this list. Empirical: "
                        "0=info, 1=warn, 2=error, 3=trigger/alert aggregate "
                        "(deduped — use bi_list_alerts for per-event), "
                        "4=status, 10=user."
                    ),
                },
                "match": {
                    "type": "string",
                    "description": "Case-insensitive substring match on entry.msg.",
                },
                "regex": {
                    "type": "string",
                    "description": "Python regex on entry.msg (IGNORECASE). XOR with match.",
                },
                "limit": {"type": "integer", "description": "Max entries (default 100)."},
            },
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "List BI log entries"},
    )
