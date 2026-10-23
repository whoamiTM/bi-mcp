"""shape_export_result: a success reply with no usable export item is not ok.

An empty / data-less reply used to shape to ``{'ok': True, 'item': {}}``, so a
caller polling an export saw success with nothing behind it. Replies that do
carry an item (incl. the unset-export-folder relative-uri case) and the
list-mode array (possibly empty: an empty queue) are unchanged.
"""

from __future__ import annotations

import pytest

from bi_mcp import shapers

_NO_ITEM = {"ok": False, "reason": "Blue Iris returned no export item"}


@pytest.mark.parametrize(
    "raw",
    [{}, {"result": "success"}, {"result": "success", "session": "s"}, {"result": "success", "data": None},
     {"result": "success", "data": {}}],
    ids=["empty", "result-only", "result+session", "data-null", "data-empty"],
)
def test_reply_without_item_is_not_ok(raw: dict) -> None:
    assert shapers.shape_export_result(raw) == _NO_ITEM


def test_real_item_is_unchanged() -> None:
    raw = {"result": "success", "data": {"path": "@1", "status": "queued", "uri": "Clipboard\\x.mp4"}}
    assert shapers.shape_export_result(raw) == {
        "ok": True, "item": {"path": "@1", "status": "queued", "uri": "Clipboard\\x.mp4"},
    }


def test_empty_queue_list_is_ok() -> None:
    assert shapers.shape_export_result({"result": "success", "data": []}) == {"ok": True, "items": []}
