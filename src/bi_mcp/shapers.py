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
    """Shape the `ptz` query response — position + preset list."""
    if isinstance(raw, dict):
        return _drop_empty(raw)
    return raw
