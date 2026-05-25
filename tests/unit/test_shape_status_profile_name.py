"""Regression tests for profile_name resolution in shape_status.

BI's `status` payload returns `profile` as a bare int (e.g. 1). The
`profiles` array from `bi_get_session` is 0-indexed where element 0 is
"Inactive" and element 1 is the first armed profile. Without resolving
this, a caller reading `profile: 1` may mistake it for "Inactive" (a
real session miss that motivated this fix).

shape_status now accepts an optional `profiles` argument; when supplied
and the int falls inside the array, it adds `profile_name` alongside
the raw `profile` int. Out-of-range or missing inputs leave the key
absent — never injects "Unknown".
"""

from __future__ import annotations

from bi_mcp.shapers import shape_status


_PROFILES = [
    "Inactive",
    "Active/Day",
    "Armed/Away",
    "Active/Night",
    "Profile 4",
    "Profile 5",
    "Profile 6",
    "Profile 7",
]


def test_profile_name_resolved_for_first_armed_profile() -> None:
    out = shape_status({"profile": 1, "cpu": 26}, profiles=_PROFILES)
    assert out["profile"] == 1
    assert out["profile_name"] == "Active/Day"


def test_profile_name_resolved_for_inactive() -> None:
    out = shape_status({"profile": 0}, profiles=_PROFILES)
    assert out["profile"] == 0
    assert out["profile_name"] == "Inactive"


def test_profile_name_resolved_for_last_armed_profile() -> None:
    out = shape_status({"profile": 7}, profiles=_PROFILES)
    assert out["profile_name"] == "Profile 7"


def test_profile_name_omitted_when_out_of_range() -> None:
    out = shape_status({"profile": 99}, profiles=_PROFILES)
    assert out["profile"] == 99
    assert "profile_name" not in out


def test_profile_name_omitted_when_negative() -> None:
    out = shape_status({"profile": -1}, profiles=_PROFILES)
    assert "profile_name" not in out


def test_profile_name_omitted_when_profiles_missing() -> None:
    out = shape_status({"profile": 1})
    assert out["profile"] == 1
    assert "profile_name" not in out


def test_profile_name_omitted_when_profile_missing() -> None:
    out = shape_status({"cpu": 26}, profiles=_PROFILES)
    assert "profile_name" not in out


def test_profile_name_omitted_when_profile_not_int() -> None:
    out = shape_status({"profile": "1"}, profiles=_PROFILES)
    assert "profile_name" not in out


def test_non_dict_passes_through() -> None:
    assert shape_status("oops", profiles=_PROFILES) == {"raw": "oops"}
