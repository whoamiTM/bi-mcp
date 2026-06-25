"""Tests for the server dispatcher's result -> MCP content conversion.

`_result_to_content` decides whether a tool's return value becomes a plain
JSON text block or an image block + metadata text block (when the result
carries the `_mcp_image` marker that image tools set).
"""

from __future__ import annotations

import json

from mcp.types import ImageContent, TextContent

from bi_mcp.server import _result_to_content


def test_plain_dict_becomes_single_text_block() -> None:
    blocks = _result_to_content({"camera": "SecCam_3", "n": 1})
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert json.loads(blocks[0].text) == {"camera": "SecCam_3", "n": 1}


def test_image_marker_splits_into_image_plus_metadata_text() -> None:
    result = {
        "camera": "SecCam_4",
        "alert_record": "@123",
        "memo": "person",
        "image_base64": "QUJD",
        "_mcp_image": {"data": "QUJD", "mimeType": "image/jpeg"},
    }
    blocks = _result_to_content(result)
    assert len(blocks) == 2

    img, text = blocks
    assert isinstance(img, ImageContent)
    assert img.type == "image"
    assert img.data == "QUJD"
    assert img.mimeType == "image/jpeg"

    assert isinstance(text, TextContent)
    meta = json.loads(text.text)
    # Metadata block carries everything EXCEPT the marker key.
    assert "_mcp_image" not in meta
    assert meta["camera"] == "SecCam_4"
    assert meta["alert_record"] == "@123"
    assert meta["memo"] == "person"


def test_non_dict_result_becomes_text_block() -> None:
    blocks = _result_to_content([1, 2, 3])
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert json.loads(blocks[0].text) == [1, 2, 3]
