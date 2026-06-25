"""Camera snapshot tool — pull a single JPEG frame from a camera.

Wraps the documented `GET /image/{cam-short-name}` endpoint
(BlueIris_Manual.md § HTTP Interface, line 8072). Non-mutating; uses the
read client's session token via the URL query param.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from ..client import BiClients
from ..errors import BiBadRequest
from ..utils.logging import log_tool_usage
from .registry import register_tool
from .tools_status import COMMON_SCHEMA


_SHORT_NAME_RE = re.compile(r"\A[A-Za-z0-9_\-]+\Z")


@log_tool_usage("bi_get_camera_snapshot")
def _tool_get_camera_snapshot(client: BiClients, args: dict) -> Any:
    short = args.get("camera") or args.get("short") or args.get("short_name")
    if not short:
        raise BiBadRequest(
            "bi_get_camera_snapshot requires a 'camera' (short name) argument"
        )
    if not _SHORT_NAME_RE.fullmatch(short):
        # Reject anything that isn't a BI short-name shape. Without this,
        # httpx normalizes `/image/../mjpg/<cam>/video.mjpg` (or similar) to
        # a different BI endpoint before sending, and the read-everything
        # helper would buffer an MJPEG stream.
        raise BiBadRequest(
            f"Invalid camera short name {short!r}: must match [A-Za-z0-9_-]+"
        )
    body, content_type = client.get_bytes(f"/image/{short}")
    image_base64 = base64.b64encode(body).decode("ascii")
    return {
        "camera": short,
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
        "bi_get_camera_snapshot",
        _tool_get_camera_snapshot,
        description=(
            "Fetch a single current JPEG frame from a camera via "
            "`GET /image/<short>`. Returns the image as base64 — the calling "
            "agent decides where (if anywhere) to write it to disk. Useful "
            "for cross-referencing live camera coverage against spatial maps, "
            "verifying PTZ preset framing, or capturing a still without "
            "going through the alert/clip pipeline."
        ),
        schema={
            "type": "object",
            "properties": {
                **COMMON_SCHEMA,
                "camera": {
                    "type": "string",
                    "description": "Camera short name (e.g. 'SecCam_3'). Required.",
                },
            },
            "required": ["camera"],
            "additionalProperties": True,
        },
        annotations={"readOnlyHint": True, "title": "Get BI camera snapshot"},
    )
