"""Protocol tests, anchored on real captured frames.

Two real anchors, one per family:

* WHOOP 4 -> ``REAL_FRAME_4``  (a HELLO, CRC8 over the LEN bytes).
* WHOOP 5 -> ``P.CLIENT_HELLO`` (the fixed connect hello, CRC16-Modbus header).
"""

from __future__ import annotations

import struct

import pytest

from whoop.ble import protocol as P

# Captured WHOOP 4 app -> strap frame: HELLO_HARVARD, sequence 4, cmd 0x23.
REAL_FRAME_4 = bytes.fromhex("aa 10 00 57 23 04 23 aa 8e d4 69 a9 6d 00 00 00 51 30 fe f3")
REAL_FRAME_5 = P.CLIENT_HELLO


# --------------------------------- WHOOP 4 ---------------------------------
def test_whoop4_crc8_covers_length_bytes_only():
    assert P.crc8(REAL_FRAME_4[1:3]) == REAL_FRAME_4[3] == 0x57


def test_whoop4_crc32_is_zlib_over_inner_little_endian():
    inner = REAL_FRAME_4[4:-4]
    assert P.crc32_le(inner) == REAL_FRAME_4[-4:]


def test_whoop4_parse_real_frame():
    frame = P.parse_frame(REAL_FRAME_4)
    assert frame is not None
    assert frame.family is P.DeviceFamily.WHOOP4
    assert frame.packet_type == P.PT_COMMAND
    assert frame.sequence == 4
    assert frame.command == P.CMD_HELLO_HARVARD
    assert frame.valid_crc
    assert frame.name == "command:HELLO_HARVARD"


def test_whoop4_build_round_trips_the_capture():
    # Same shape as the capture, timestamp zeroed -> framing must stay valid.
    frame = P.build_frame("whoop4", P.PT_COMMAND, 4, P.CMD_HELLO_HARVARD, bytes(9))
    parsed = P.parse_frame(frame)
    assert parsed is not None and parsed.valid_crc
    assert parsed.family is P.DeviceFamily.WHOOP4
    assert parsed.command == P.CMD_HELLO_HARVARD
    assert frame[0] == P.SOF
    assert struct.unpack("<H", frame[1:3])[0] == len(frame) - 4


def test_whoop4_does_not_pad():
    frame = P.build_frame("whoop4", P.PT_COMMAND, 1, P.CMD_GET_BATTERY, b"\x01\x02")
    assert len(frame) == 13  # inner 5 + crc32 4 + header 4


# --------------------------------- WHOOP 5 ---------------------------------
def test_crc16_modbus_known_vector():
    # The canonical CRC-16/MODBUS check value.
    assert P.crc16_modbus(b"123456789") == 0x4B37


def test_whoop5_header_and_body_checksums():
    assert len(REAL_FRAME_5) == 16
    assert struct.unpack("<H", REAL_FRAME_5[2:4])[0] == len(REAL_FRAME_5) - 8
    assert REAL_FRAME_5[4:6] == P.FIELD_REQUEST
    assert struct.unpack_from("<H", REAL_FRAME_5, 6)[0] == P.crc16_modbus(REAL_FRAME_5[0:6])
    inner = REAL_FRAME_5[8:-4]
    assert P.crc32_le(inner) == REAL_FRAME_5[-4:]


def test_whoop5_parse_client_hello():
    frame = P.parse_frame(REAL_FRAME_5)
    assert frame is not None
    assert frame.family is P.DeviceFamily.WHOOP5
    assert frame.packet_type == P.PT_COMMAND
    assert frame.sequence == 1
    assert frame.command == P.CMD_HELLO
    assert frame.data == b"\x01"
    assert frame.valid_crc
    assert not frame.is_response
    assert frame.name == "command:HELLO"


def test_whoop5_build_reproduces_client_hello_exactly():
    assert P.build_frame("whoop5", P.PT_COMMAND, 1, P.CMD_HELLO, b"\x01") == REAL_FRAME_5
    assert P.client_hello() == REAL_FRAME_5


def test_whoop5_pads_inner_to_four_bytes():
    frame = P.build_frame("whoop5", P.PT_COMMAND, 9, P.CMD_GET_BATTERY, b"\x01\x02")
    declared = struct.unpack("<H", frame[2:4])[0]
    assert declared % 4 == 0
    assert len(frame) == declared + 8
    assert P.parse_frame(frame).valid_crc


def test_whoop5_flags_response_direction():
    # Rebuild the hello as a response (header field 01 00).
    raw = bytearray(P.client_hello())
    raw[4:6] = P.FIELD_RESPONSE
    raw[6:8] = struct.pack("<H", P.crc16_modbus(bytes(raw[0:6])))
    frame = P.parse_frame(bytes(raw))
    assert frame is not None and frame.is_response and frame.valid_crc


def test_whoop5_rejects_corrupt_header():
    raw = bytearray(REAL_FRAME_5)
    raw[6] ^= 0xFF
    frame = P.parse_frame(bytes(raw))
    assert frame is None or not frame.valid_crc


# ------------------------------- detection ---------------------------------
def test_detect_family_by_header_check():
    assert P.detect_family(REAL_FRAME_4) is P.DeviceFamily.WHOOP4
    assert P.detect_family(REAL_FRAME_5) is P.DeviceFamily.WHOOP5
    assert P.detect_family(b"") is None
    assert P.detect_family(b"\x00\x01\x02") is None


def test_family_for_service():
    assert P.family_for_service(P.SERVICE_WHOOP5) is P.DeviceFamily.WHOOP5
    assert P.family_for_service(P.SERVICE_WHOOP4) is P.DeviceFamily.WHOOP4
    assert P.family_for_service("fd4b0003-cce1-4033-93ce-002d5875f58a") is P.DeviceFamily.WHOOP5
    assert P.family_for_service("0000180d-0000-1000-8000-00805f9b34fb") is None


def test_parse_rejects_garbage():
    assert P.parse_frame(b"") is None
    assert P.parse_frame(b"\x00\x01\x02") is None


# --------------------------------- safety ----------------------------------
def test_command_safety_classification():
    assert P.classify_command(P.CMD_GET_BATTERY) is P.CommandSafety.BENIGN
    assert P.classify_command(P.CMD_HELLO) is P.CommandSafety.BENIGN
    assert P.classify_command(P.CMD_SET_ALARM_TIME) is P.CommandSafety.SIDE_EFFECT
    assert P.classify_command(0x99) is P.CommandSafety.UNKNOWN


def test_assert_writable_blocks_unknown_by_default():
    P.assert_writable(P.CMD_GET_BATTERY)  # benign -> no raise
    with pytest.raises(PermissionError):
        P.assert_writable(P.CMD_SET_ALARM_TIME)
    with pytest.raises(PermissionError):
        P.assert_writable(0x99)
    P.assert_writable(P.CMD_SET_ALARM_TIME, allow_side_effect=True)
    P.assert_writable(0x99, allow_unknown=True)


# ------------------------------ standard HR --------------------------------
def test_parse_heart_rate_8bit_with_rr():
    buf = bytes([0x10, 64]) + struct.pack("<H", 0x0378)
    parsed = P.parse_heart_rate(buf)
    assert parsed["bpm"] == 64
    assert parsed["rr_ms"] == [round(0x0378 * 1000 / 1024, 1)]


def test_parse_heart_rate_16bit():
    buf = bytes([0x01]) + struct.pack("<H", 130)
    assert P.parse_heart_rate(buf)["bpm"] == 130


def test_rmssd():
    assert P.rmssd([800.0, 810.0, 790.0]) == 15.8
    assert P.rmssd([800.0]) is None
