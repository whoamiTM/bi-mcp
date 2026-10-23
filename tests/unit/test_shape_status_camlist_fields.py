"""Field-decoding regressions in shape_status / shape_camlist.

Covers:
  * status ``warnings`` is a COUNT (manual: "current number of warnings in
    Messages"), sent as a string — it must not be ISO-ified into a 1970 date.
  * camlist ``alertutc`` (UTC seconds; the field BI 5.9.9.71 actually sends —
    no ``lastalertutc``/``newalertsutc`` on the wire) is ISO-ified.
  * camlist ``error: ""`` is BI's "camera OK" value and survives empty-dropping.
"""

from __future__ import annotations

from bi_mcp import shapers


def test_status_warnings_count_is_not_a_timestamp() -> None:
    out = shapers.shape_status({"warnings": "7", "profile": 1})
    assert out["warnings"] == "7"


def test_status_zero_warnings_is_not_epoch_zero() -> None:
    assert shapers.shape_status({"warnings": "0"})["warnings"] == "0"


def test_camlist_alertutc_is_isoified() -> None:
    out = shapers.shape_camlist([{"optionValue": "SecCam_3", "alertutc": 1792467304}])
    assert out[0]["alertutc"] == "2026-10-20T03:35:04+00:00"


def test_camera_config_fallback_alertutc_is_isoified() -> None:
    out = shapers.shape_camera_config([{"optionValue": "SecCam_3", "alertutc": 1792467304}], "SecCam_3")
    assert out is not None and out["alertutc"] == "2026-10-20T03:35:04+00:00"


def test_camlist_empty_error_is_kept() -> None:
    out = shapers.shape_camlist([{"optionValue": "SecCam_3", "error": "", "notes": ""}])
    assert out[0]["error"] == ""
    assert "notes" not in out[0]  # other empties are still dropped
