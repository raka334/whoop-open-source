"""Protocol tests, anchored on a real captured frame."""

from __future__ import annotations

import struct

from whoop.ble import protocol as P

# Captured app -> strap frame: HELLO, sequence 4, cmd 0x23.
REAL_FRAME = bytes.fromhex("aa 10 00 57 23 04 23 aa 8e d4 69 a9 6d 00 00 00 51 30 fe f3")


def test_crc8_covers_length_bytes_only():
    assert P.crc8(REAL_FRAME[1:3]) == REAL_FRAME[3] == 0x57


def test_crc32_is_zlib_over_payload_little_endian():
    payload = REAL_FRAME[4:-4]
    assert P.crc32_le(payload) == REAL_FRAME[-4:]


def test_parse_real_frame():
    frame = P.parse_frame(REAL_FRAME)
    assert frame is not None
    assert frame.packet_type == P.PT_COMMAND
    assert frame.sequence == 4
    assert frame.command == P.CMD_HELLO
    assert frame.valid_crc
    assert frame.name == "command:HELLO"


def test_build_hello_reproduces_capture_shape():
    # Same command/sequence but timestamp zeroed: framing must still be valid.
    frame = P.build_frame(P.PT_COMMAND, 4, P.CMD_HELLO, bytes(9))
    parsed = P.parse_frame(frame)
    assert parsed is not None and parsed.valid_crc
    assert parsed.command == P.CMD_HELLO
    assert frame[0] == P.SOF
    assert struct.unpack("<H", frame[1:3])[0] == len(frame) - 4


def test_parse_rejects_garbage():
    assert P.parse_frame(b"") is None
    assert P.parse_frame(b"\x00\x01\x02") is None


def test_parse_heart_rate_8bit_with_rr():
    # flags=0x10 (RR present), bpm=64, one RR of 0x0378 -> 888 ms
    buf = bytes([0x10, 64]) + struct.pack("<H", 0x0378)
    parsed = P.parse_heart_rate(buf)
    assert parsed["bpm"] == 64
    assert parsed["rr_ms"] == [round(0x0378 * 1000 / 1024, 1)]


def test_parse_heart_rate_16bit():
    buf = bytes([0x01]) + struct.pack("<H", 130)
    assert P.parse_heart_rate(buf)["bpm"] == 130


def test_rmssd():
    # Successive differences of 800, 810, 790 -> sqrt((100+400)/2) = 15.8
    assert P.rmssd([800.0, 810.0, 790.0]) == 15.8
    assert P.rmssd([800.0]) is None
