"""Regression tests for stale-counter fencing in shape_camlist.

When `isEnabled` is false, BI freezes `nAlerts`, `nTriggers`, `nClips`,
`nNoSignal`, and `ManRecElapsed` at whatever value they held when the
camera was disabled. Surfacing those at top level alongside enabled
cameras' live counters is misleading — a disabled camera with
`ManRecElapsed: 6591` is not currently recording.

The shaper now wraps those five fields under a `stale_when_disabled`
sub-dict for disabled cameras only. Enabled cameras keep the fields at
top level (their counters are live).
"""

from __future__ import annotations

from bi_mcp.shapers import shape_camlist


_STALE_KEYS = ("nAlerts", "nTriggers", "nClips", "nNoSignal", "ManRecElapsed")


def _enabled_cam() -> dict:
    return {
        "optionValue": "SecCam_3",
        "isEnabled": True,
        "isOnline": True,
        "nAlerts": 1943,
        "nTriggers": 2555,
        "nClips": 132,
        "nNoSignal": 1,
        "ManRecElapsed": 0,
        "FPS": 15.03,
    }


def _disabled_cam() -> dict:
    return {
        "optionValue": "SecCam_10",
        "isEnabled": False,
        "isOnline": False,
        "nAlerts": 30,
        "nTriggers": 6999,
        "nClips": 62,
        "nNoSignal": 2,
        "ManRecElapsed": 6591,
        "error": "Disabled",
    }


def test_enabled_camera_keeps_counters_at_top_level() -> None:
    out = shape_camlist([_enabled_cam()])
    assert len(out) == 1
    cam = out[0]
    assert "stale_when_disabled" not in cam
    assert cam["nAlerts"] == 1943
    assert cam["nTriggers"] == 2555


def test_disabled_camera_wraps_counters_under_stale_block() -> None:
    out = shape_camlist([_disabled_cam()])
    assert len(out) == 1
    cam = out[0]
    assert "stale_when_disabled" in cam
    stale = cam["stale_when_disabled"]
    assert stale["nAlerts"] == 30
    assert stale["nTriggers"] == 6999
    assert stale["nClips"] == 62
    assert stale["nNoSignal"] == 2
    assert stale["ManRecElapsed"] == 6591
    # And those keys must no longer appear at top level.
    for k in _STALE_KEYS:
        assert k not in cam, f"{k} should be moved under stale_when_disabled"


def test_disabled_camera_keeps_non_counter_fields_at_top_level() -> None:
    out = shape_camlist([_disabled_cam()])
    cam = out[0]
    assert cam["optionValue"] == "SecCam_10"
    assert cam["isEnabled"] is False
    assert cam["error"] == "Disabled"


def test_mixed_list_handles_both_kinds() -> None:
    out = shape_camlist([_enabled_cam(), _disabled_cam()])
    assert len(out) == 2
    assert "stale_when_disabled" not in out[0]
    assert "stale_when_disabled" in out[1]


def test_disabled_with_all_zero_counters_still_wraps() -> None:
    cam = _disabled_cam()
    for k in _STALE_KEYS:
        cam[k] = 0
    out = shape_camlist([cam])
    assert "stale_when_disabled" in out[0]
    # zeros are preserved (not dropped by _drop_empty since 0 is kept)
    assert out[0]["stale_when_disabled"]["ManRecElapsed"] == 0


def test_disabled_with_missing_counters_omits_them_in_stale_block() -> None:
    cam = {"optionValue": "SecCam_X", "isEnabled": False}
    out = shape_camlist([cam])
    # If none of the stale keys exist, no stale_when_disabled block at all.
    assert "stale_when_disabled" not in out[0]


def test_limit_still_applies() -> None:
    out = shape_camlist([_enabled_cam(), _disabled_cam()], limit=1)
    assert len(out) == 1
