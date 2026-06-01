"""Regression tests for the PTZ schedule-events annotation in shape_reg.

The BI Schedule → Events list ("Search-back at startup/reset") stores its
slots as a packed binary blob under ``PTZ\\events``. Each slot is 28 bytes.

Empirically observed 2026-05-31:
- bytes 0-1 (LE uint16) = ``docmd`` opcode. For preset moves,
  opcode = 2200 + preset_number (range 2201-2456 = preset 1-256).
  Confirmed by 3 anchor slots: preset 1 (0x0999), preset 10 (0x08a2),
  preset 256 (0x0998), all decode correctly via opcode - 2200.
- byte 21 = profile bitmask. Confirmed by single-byte .reg diff:
  editing slot 0 from "all profiles (1-7, 0xfe)" to "profile 1 only
  (0x02)" changed only byte 21.
- empty schedule = ``events`` blob with len=0 (every spotter cam).

shape_reg annotates the ``PTZ`` block with an ``events_slots`` list.
"""

from __future__ import annotations

import copy

from bi_mcp.shapers import _annotate_ptz_schedule_events


# SC11AI export 2026-05-31: 2 slots, P1 sunrise + P3 sunset.
_PROD_HEX_2_SLOTS_SC11AI = (
    "990800006b070c0006001e0006000f000000000001020000000000009b08"
    "00006b070c0006001e0014001a000000000002fe000000000000"
)


def test_two_slot_production_blob_decodes() -> None:
    """SC11AI's 2-slot schedule: opcodes 2201 (P1) and 2203 (P3)."""
    parsed = {
        "PTZ": {
            "events": {"_type": "binary", "hex": _PROD_HEX_2_SLOTS_SC11AI, "len": 56},
        }
    }
    out = _annotate_ptz_schedule_events(parsed)
    slots = out["PTZ"]["events_slots"]
    assert len(slots) == 2

    assert slots[0]["opcode"] == 2201
    assert slots[0]["preset"] == 1
    assert slots[0]["profile_mask"] == 0x02
    assert slots[0]["profiles"] == ["1"]

    assert slots[1]["opcode"] == 2203
    assert slots[1]["preset"] == 3
    assert slots[1]["profile_mask"] == 254
    assert slots[1]["profiles"] == ["1", "2", "3", "4", "5", "6", "7"]


# clone_sc10 export 2026-05-31: 2 slots, P256 sunset + P10 (profile 2 only).
_CLONE_SC10_2_SLOTS = (
    "98090000ea07050000001f00150024000000000002fe000068100000a208"
    "0000ea07050000001f0015002c000f0000000204000057120000"
)


def test_preset_256_decodes_via_opcode() -> None:
    """Preset 256 = opcode 2456 = LE bytes 0x98 0x09 — single-byte decode
    would have wrapped to preset 0. The opcode-based decoder gets it right."""
    parsed = {
        "PTZ": {
            "events": {"_type": "binary", "hex": _CLONE_SC10_2_SLOTS, "len": 56},
        }
    }
    out = _annotate_ptz_schedule_events(parsed)
    slot0 = out["PTZ"]["events_slots"][0]
    assert slot0["opcode"] == 2456
    assert slot0["preset"] == 256
    assert slot0["profiles"] == ["1", "2", "3", "4", "5", "6", "7"]


def test_preset_10_decodes_correctly() -> None:
    """Preset 10 = opcode 2210 = LE bytes 0xa2 0x08. Single-byte decode
    would have given preset (0xa2 - 0x98) = 10 by coincidence; the
    LE-uint16 path confirms it independently."""
    parsed = {
        "PTZ": {
            "events": {"_type": "binary", "hex": _CLONE_SC10_2_SLOTS, "len": 56},
        }
    }
    out = _annotate_ptz_schedule_events(parsed)
    slot1 = out["PTZ"]["events_slots"][1]
    assert slot1["opcode"] == 2210
    assert slot1["preset"] == 10
    # Slot is gated on profile 2 only (mask = 0b00000100 = 4).
    assert slot1["profile_mask"] == 0x04
    assert slot1["profiles"] == ["2"]


def test_opcode_full_preset_range() -> None:
    """Every opcode in 2201-2456 decodes to its preset 1-256."""
    for preset_num in range(1, 257):
        opcode = 2200 + preset_num
        chunk = opcode.to_bytes(2, "little") + b"\x00" * 19 + b"\xfe" + b"\x00" * 6
        assert len(chunk) == 28
        parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
        out = _annotate_ptz_schedule_events(parsed)
        slot = out["PTZ"]["events_slots"][0]
        assert slot["opcode"] == opcode
        assert slot["preset"] == preset_num


def test_profile_mask_0xfe_includes_profile_7() -> None:
    """``0xfe`` = bits 1-7 set = profiles 1-7 (all real profiles, excluding
    Inactive=0). Regression for a bug where the profile labels table only
    covered bits 0-6, silently dropping profile 7 from the decoded list.

    Per BI manual line 6031: "There are 8 profiles, 0-7." So a slot
    configured for "all profiles" (the BI UI's default for a new event)
    must report profiles 1-7, not 1-6.
    """
    chunk = bytearray(28)
    chunk[0:2] = (2201).to_bytes(2, "little")  # preset 1
    chunk[10] = 0x1E
    chunk[12] = 12
    chunk[14] = 0
    chunk[21] = 0xFE  # bits 1-7 set
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["profile_mask"] == 0xFE
    assert slot["profiles"] == ["1", "2", "3", "4", "5", "6", "7"]


def test_profile_mask_0xff_includes_inactive_and_profile_7() -> None:
    """``0xff`` = bits 0-7 all set = "Inactive + profiles 1-7" — distinct
    from 0xfe by inclusion of profile 0 (Inactive)."""
    chunk = bytearray(28)
    chunk[0:2] = (2201).to_bytes(2, "little")
    chunk[10] = 0x1E
    chunk[21] = 0xFF
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["profile_mask"] == 0xFF
    assert slot["profiles"] == ["0", "1", "2", "3", "4", "5", "6", "7"]


def test_profile_mask_only_profile_7() -> None:
    """Bit 7 alone (0x80) = profile 7 only — proves profile 7 is reachable
    via the decoder, not just as part of a wider mask."""
    chunk = bytearray(28)
    chunk[0:2] = (2201).to_bytes(2, "little")
    chunk[10] = 0x1E
    chunk[21] = 0x80
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["profile_mask"] == 0x80
    assert slot["profiles"] == ["7"]


def test_time_encoding_absolute_3pm() -> None:
    """Absolute 3:00 PM slot: byte 10 = 0x1e, byte 12 = 15, byte 14 = 0.

    Empirical anchor: edited the clone_sc10 slot from "sunset preset 256"
    to "absolute 3:00 PM preset 40". The shaper exposes the time as
    type='absolute', hour=15, minute=0, hhmm='15:00'.
    """
    # Real bytes from the clone_sc10 export post-edit.
    chunk_hex = "c00800006b070c0006001e000f00000000000000000000fe00000000000000"[:56]
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk_hex, "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["opcode"] == 2240
    assert slot["preset"] == 40
    assert slot["time_type_byte"] == 0x1E
    assert slot["time_type"] == "absolute"
    assert slot["time_hour"] == 15
    assert slot["time_minute"] == 0
    assert slot["time_hhmm"] == "15:00"


def test_time_encoding_sunset_relative() -> None:
    """Sunset slot stores type=0x1f + the resolved absolute trigger time.

    Empirical anchor: clone_sc10 slot 1 was configured for sunset at the
    time of capture; byte 10 = 0x1f, bytes 12-14 store 21:36 = the
    computed absolute sunset trigger for that date.
    """
    chunk_hex = "a2080000ea07050000001f0015002c000f0000000204000057120000"
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk_hex, "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["time_type_byte"] == 0x1F
    assert slot["time_type"] == "sunrise_sunset_relative"
    assert slot["time_hour"] == 21
    assert slot["time_minute"] == 44
    assert slot["time_hhmm"] == "21:44"


def test_time_bytes_out_of_range_return_none_hhmm() -> None:
    """If the bytes at offsets 12 or 14 aren't valid clock values (the
    offsets might be wrong for some slot variant), return None for the
    formatted time rather than emitting '99:88'."""
    chunk = bytearray(28)
    chunk[0:2] = (2210).to_bytes(2, "little")  # preset 10 opcode
    chunk[10] = 0x1E
    chunk[12] = 99  # invalid hour
    chunk[14] = 88  # invalid minute
    chunk[21] = 0xFE
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["time_hour"] == 99
    assert slot["time_minute"] == 88
    assert slot["time_hhmm"] is None


def test_unknown_time_type_byte_returns_none() -> None:
    """If byte 10 is something other than 0x1e/0x1f (e.g. a future BI
    version adds a new time-reference type), surface time_type=None."""
    chunk = bytearray(28)
    chunk[0:2] = (2210).to_bytes(2, "little")
    chunk[10] = 0x20  # unknown type
    chunk[12] = 12
    chunk[14] = 30
    chunk[21] = 0xFE
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["time_type_byte"] == 0x20
    assert slot["time_type"] is None


def test_non_preset_opcode_has_no_preset() -> None:
    """An opcode outside the preset range (e.g. 33203 = 'action set 1')
    returns preset=None, leaving consumers to look it up in the docmd
    table themselves."""
    opcode = 33203  # "Action set 1" per docmd table
    chunk = opcode.to_bytes(2, "little") + b"\x00" * 19 + b"\xfe" + b"\x00" * 6
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": chunk.hex(), "len": 28}}}
    out = _annotate_ptz_schedule_events(parsed)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["opcode"] == 33203
    assert slot["preset"] is None


def test_profile_mask_offset_pinned_by_single_byte_diff() -> None:
    """Pin byte 21 as the profile mask offset via empirical diff."""
    before_chunk = bytes.fromhex(
        "990800006b070c0006001e0006000f000000000001fe000000000000"
    )
    after_chunk = bytes.fromhex(
        "990800006b070c0006001e0006000f00000000000102000000000000"
    )
    assert len(before_chunk) == 28
    assert len(after_chunk) == 28
    diff_offsets = [i for i in range(28) if before_chunk[i] != after_chunk[i]]
    assert diff_offsets == [21], (
        f"Expected single-byte diff at offset 21, got diffs at {diff_offsets}"
    )

    parsed_after = {
        "PTZ": {"events": {"_type": "binary", "hex": after_chunk.hex(), "len": 28}}
    }
    out = _annotate_ptz_schedule_events(parsed_after)
    slot = out["PTZ"]["events_slots"][0]
    assert slot["profile_mask"] == 0x02
    assert slot["profiles"] == ["1"]


def test_no_events_blob_no_annotation() -> None:
    parsed = {"PTZ": {"events_enable": 0, "lastpreset": "SecCam_1"}}
    out = _annotate_ptz_schedule_events(parsed)
    assert "events_slots" not in out["PTZ"]


def test_non_binary_events_value_skipped() -> None:
    parsed = {"PTZ": {"events": 0}}
    out = _annotate_ptz_schedule_events(parsed)
    assert "events_slots" not in out["PTZ"]


def test_non_ptz_key_left_alone() -> None:
    parsed = {
        "AI\\1": {"smartzones": 255},
        "PTZ\\Presets\\1": {"desc": "SecCam_1"},
    }
    out = _annotate_ptz_schedule_events(parsed)
    assert set(out.keys()) == set(parsed.keys())
    assert "events_slots" not in out["AI\\1"]
    assert "events_slots" not in out["PTZ\\Presets\\1"]


def test_malformed_length_skipped() -> None:
    parsed = {
        "PTZ": {"events": {"_type": "binary", "hex": "00" * 30, "len": 30}}
    }
    out = _annotate_ptz_schedule_events(parsed)
    assert "events_slots" not in out["PTZ"]


def test_empty_blob_emits_empty_list() -> None:
    """Documented empty state across all spotter cams: len=0 binary blob."""
    parsed = {"PTZ": {"events": {"_type": "binary", "hex": "", "len": 0}}}
    out = _annotate_ptz_schedule_events(parsed)
    assert out["PTZ"]["events_slots"] == []


def test_corrupt_hex_skipped() -> None:
    parsed = {
        "PTZ": {"events": {"_type": "binary", "hex": "ZZZZ", "len": 2}}
    }
    out = _annotate_ptz_schedule_events(parsed)
    assert "events_slots" not in out["PTZ"]


def test_hex_length_mismatch_skipped() -> None:
    parsed = {
        "PTZ": {"events": {"_type": "binary", "hex": "00" * 20, "len": 56}}
    }
    out = _annotate_ptz_schedule_events(parsed)
    assert "events_slots" not in out["PTZ"]


def test_raw_slot_hex_round_trips() -> None:
    parsed = {
        "PTZ": {"events": {"_type": "binary", "hex": _CLONE_SC10_2_SLOTS, "len": 56}}
    }
    out = _annotate_ptz_schedule_events(parsed)
    slots = out["PTZ"]["events_slots"]
    rejoined = "".join(s["raw_slot_hex"] for s in slots)
    assert rejoined == _CLONE_SC10_2_SLOTS


def test_annotator_does_not_mutate_input() -> None:
    parsed = {
        "PTZ": {
            "events_enable": 1,
            "events": {"_type": "binary", "hex": _CLONE_SC10_2_SLOTS, "len": 56},
        },
        "AI\\1": {"smartzones": 255},
    }
    snapshot = copy.deepcopy(parsed)
    out = _annotate_ptz_schedule_events(parsed)
    assert parsed == snapshot, "annotator must not mutate input"
    assert out["PTZ"] is not parsed["PTZ"]
    assert out["AI\\1"] is parsed["AI\\1"]


def test_annotator_returns_same_keys() -> None:
    parsed = {
        "PTZ": {"events_enable": 0},
        "AI\\1": {"smartzones": 255},
        "Motion": {"sync": 0},
    }
    out = _annotate_ptz_schedule_events(parsed)
    assert set(out.keys()) == set(parsed.keys())
