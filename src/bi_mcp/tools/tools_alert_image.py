"""Alert-image tool — fetch the stored JPEG for an alert by camera + time.

Unlike `bi_get_camera_snapshot` (a live `/image/<short>` frame), this returns
the *stored* alert image from the Alerts list — the frame BI saved when the
alert fired, optionally with AI markup (`v=2`, per BlueIris_Manual.md
§ HTTP Interface line 1295). The image endpoint is `/alerts/@<record>`
(manual line 8129: a database record number `@record` may stand in for the
filename); the filename form was found to 404 on BI 5.9.9.71, the `@record`
form works.

Resolution is camera + time, matching how the request is actually phrased
("pull the alert image from SecCam_4 around 7:15"). We call `alertlist` with
`enddate=<time>` and take the most-recent alert at-or-before it — BI returns
newest-first, but we sort by `date` descending defensively rather than trust
array position, since `shape_alerts` preserves BI's order without sorting.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from .. import shapers
from ..client import BiClients
from ..errors import BiBadRequest, BiError, BiNotFound
from ..utils.logging import log_tool_usage
from ..utils.time import parse_since
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


_SHORT_NAME_RE = re.compile(r"\A[A-Za-z0-9_\-]+\Z")
_RECORD_RE = re.compile(r"\A\d+\Z")


@log_tool_usage("bi_get_alert_image")
def _tool_get_alert_image(client: BiClients, args: dict) -> Any:
    short = args.get("camera") or args.get("short") or args.get("short_name")
    if not short:
        raise BiBadRequest(
            "bi_get_alert_image requires a 'camera' (short name) argument, e.g. "
            "'SecCam_4'. Use a specific camera, not 'Index' — that resolves a "
            "cross-camera alert, which is rarely what you want for an image."
        )
    if not _SHORT_NAME_RE.fullmatch(short):
        raise BiBadRequest(
            f"Invalid camera short name {short!r}: must match [A-Za-z0-9_-]+"
        )
    if short.casefold() == "index":
        # 'Index' is BI's reserved all-cameras selector; alertlist(camera=Index)
        # resolves a cross-camera latest alert, so the tool would return an
        # image from some other camera while reporting camera="Index". An image
        # must come from one named camera — reject rather than silently mislead.
        raise BiBadRequest(
            "bi_get_alert_image needs a specific camera, not 'Index' "
            "(the all-cameras selector) — it would return an image from an "
            "unpredictable camera. Pass a single camera short name."
        )

    # Resolve camera + time -> alert record. `enddate` omitted means "most
    # recent alert" (BI returns the whole list; we take the latest).
    payload: dict[str, Any] = {"camera": short}
    if args.get("at") is not None:
        try:
            payload["enddate"] = parse_since(args["at"], arg_name="at")
        except ValueError as e:
            raise BiBadRequest(str(e)) from e

    alerts = shapers.shape_alerts(client.call("alertlist", **payload), limit=200)
    # shape_alerts wraps a non-list response (BI error object, login challenge)
    # as a single [{"raw": <payload>}] sentinel — which the `not alerts` guard
    # below would NOT catch. Surface the upstream payload instead of letting it
    # fall through to a misleading "no path record" error.
    if len(alerts) == 1 and set(alerts[0]) == {"raw"}:
        raise BiError(
            f"alertlist for {short} returned an unexpected payload: {alerts[0]['raw']!r}"
        )
    if not alerts:
        when = f" at/before {args['at']!r}" if args.get("at") is not None else ""
        raise BiNotFound(f"No alert for {short}{when}")

    # Defensive: pick the most-recent alert by timestamp rather than trusting
    # BI's array position — shape_alerts preserves BI order without sorting.
    # `date` is the shaped ISO string; `or ""` makes a dateless alert sort
    # earliest, so it only wins when every alert lacks a date.
    alert = max(alerts, key=lambda a: a.get("date") or "")

    raw_path = alert.get("path")
    if not raw_path:
        raise BiNotFound(
            f"Alert for {short} has no 'path' record; cannot fetch its image"
        )
    record = str(raw_path).removeprefix("@").removesuffix(".bvr")
    if not _RECORD_RE.fullmatch(record):
        # Guard against a crafted path redirecting the /alerts/ endpoint, same
        # spirit as the snapshot tool's short-name guard.
        raise BiBadRequest(
            f"Unexpected alert record {raw_path!r}: expected '@<digits>[.bvr]'"
        )

    params: dict[str, Any] = {}
    if args.get("markup"):
        params["v"] = 2

    # Image resolution is whatever BI stored for this alert. If the camera has
    # hi-res alert JPEGs disabled (registry `hiresalerts=0`, BI Alerts tab), BI
    # only keeps a small thumbnail (~426x240) and `v=2` markup has no full frame
    # to draw the AI box on. The /alerts/ endpoint ignores sizing params
    # (w/h/scale/q — those are for /image/), so we cannot upscale here; the fix
    # is camera-side. The tool description tells the agent to warn the user.
    body, content_type = client.get_bytes(f"/alerts/@{record}", **params)
    image_base64 = base64.b64encode(body).decode("ascii")
    return {
        "camera": short,
        "alert_record": f"@{record}",
        "alert_time": alert.get("date"),
        "memo": alert.get("memo"),
        "markup": bool(args.get("markup")),
        "content_type": content_type,
        "size_bytes": len(body),
        "image_base64": image_base64,
        # The server dispatcher splits this marker into an MCP image block so
        # the frame renders inline in image-aware clients (e.g. Claude Desktop);
        # the remaining fields above become the accompanying text block.
        "_mcp_image": {"data": image_base64, "mimeType": content_type},
    }


def register() -> None:
    register_tool(
        "bi_get_alert_image",
        _tool_get_alert_image,
        description=(
            "Fetch the STORED alert image (the frame BI saved when an alert "
            "fired) for a camera, resolved by time — not a live frame (that's "
            "bi_get_camera_snapshot). Pass 'camera' (short name) and optional "
            "'at' (the alert time: ISO-8601, unix epoch int, or relative like "
            "'-2h'); omit 'at' for the most recent alert. Returns the most-recent "
            "alert at-or-before 'at' as base64, plus its record/time/memo so you "
            "can confirm which alert came back. Optional 'markup' (bool) requests "
            "the AI-overlay variant (manual's v=2). Internally resolves via "
            "alertlist + the /alerts/@record endpoint. Use a specific camera, "
            "not 'Index'. "
            "MARKUP — when 'markup'=true draws no box, it's almost always the "
            "ALERT'S SOURCE, not a tool bug: a box exists ONLY if CodeProject.AI "
            "classified the alert (memo has a score, e.g. `person:89%`). A bare "
            "`person` memo (no %) is an ONVIF/camera-IVS alert that BI's AI never "
            "scored, so there is no box to burn — v=2 just re-encodes the frame. "
            "(Separately, low-res thumbnails come from the camera's Hi-res-JPEG "
            "alert setting being off; that's a distinct issue from markup.) The "
            "/alerts/ endpoint only serves what BI stored and ignores w/h/scale "
            "params. State the source-vs-bug distinction proactively."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_4'). Required.",
                },
                "at": {
                    "description": (
                        "Alert time. ISO-8601, unix epoch int, or relative "
                        "shorthand ('-2h', '-1d'). Resolves to the most-recent "
                        "alert at-or-before this time. Omit for the latest alert."
                    ),
                },
                "markup": {
                    "type": "boolean",
                    "description": (
                        "If true, request the AI-markup overlay variant (v=2). "
                        "Markup only appears when BI stored a detection box."
                    ),
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI alert image"},
    )
