"""WHOOP 5.0 proprietary BLE protocol + standard BLE Heart Rate parsing.

Framing (verified against a real captured frame,
``aa 10 00 57 23 04 23 aa 8e d4 69 a9 6d 00 00 00 51 30 fe f3``)::

    +----+--------+-------+---------------------------+-----------+
    | AA | LEN LE | CRC8  | payload                   | CRC32 LE  |
    +----+--------+-------+---------------------------+-----------+
      1B    2B      1B      LEN - 4 bytes               4B

* ``LEN`` counts everything after the CRC8 byte: payload + CRC32, so
  ``LEN = len(payload) + 4`` where ``payload = packet_type | seq | cmd | data``.
* ``CRC8`` (poly 0x07, init 0x00) is computed over the two LEN bytes ONLY.
* ``CRC32`` is ``zlib.crc32(payload)``, little-endian.

Packet types on the wire are 0x23 (command / app->strap) and 0x24
(response / strap->app); the 0x72/0x73 in older write-ups are wrong.
Data/event channels use 0x28 and 0x30 respectively.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

SOF = 0xAA

# --- packet types -----------------------------------------------------------
PT_COMMAND = 0x23
PT_RESPONSE = 0x24
PT_DATA = 0x28
PT_EVENT = 0x30

PACKET_TYPE_NAMES = {
    PT_COMMAND: "command",
    PT_RESPONSE: "response",
    PT_DATA: "data",
    PT_EVENT: "event",
}

# --- commands (subset confirmed by packet capture) --------------------------
CMD_RT_HR_ON = 0x03
CMD_CLOCK_SYNC = 0x0A
CMD_STD_HR_BROADCAST = 0x0E  # enables the standard 0x2A37 characteristic
CMD_HISTORICAL_REQUEST = 0x16
CMD_HISTORICAL_ACK = 0x17
CMD_GET_BATTERY = 0x1A
CMD_CONFIG_QUERY = 0x22
CMD_HELLO = 0x23  # GET_HELLO_HARVARD

CMD_NAMES = {
    CMD_RT_HR_ON: "RT_HR_ON",
    CMD_CLOCK_SYNC: "CLOCK_SYNC",
    CMD_STD_HR_BROADCAST: "STD_HR_BROADCAST",
    CMD_HISTORICAL_REQUEST: "HISTORICAL_REQUEST",
    CMD_HISTORICAL_ACK: "HISTORICAL_ACK",
    CMD_GET_BATTERY: "GET_BATTERY",
    CMD_CONFIG_QUERY: "CONFIG_QUERY",
    CMD_HELLO: "HELLO",
}

# --- GATT identifiers -------------------------------------------------------
# WHOOP 5.0 / MG use the fd4b0001 base; WHOOP 4.0 uses 61080001.
SERVICE_FD4B = "fd4b0001-cce1-4033-93ce-002d5875f58a"
SERVICE_GEN4 = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"

FD4B_CHANNELS = {
    "command": "fd4b0002-cce1-4033-93ce-002d5875f58a",  # write
    "response": "fd4b0003-cce1-4033-93ce-002d5875f58a",  # notify
    "event": "fd4b0004-cce1-4033-93ce-002d5875f58a",  # notify
    "data": "fd4b0005-cce1-4033-93ce-002d5875f58a",  # notify
    "memfault": "fd4b0007-cce1-4033-93ce-002d5875f58a",  # notify
}

# Standard Bluetooth SIG services (dormant until the proprietary channel wakes them).
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------
def crc8(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc32_le(data: bytes) -> bytes:
    return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
def build_frame(packet_type: int, seq: int, cmd: int, data: bytes = b"") -> bytes:
    """Assemble a framed command packet."""
    payload = bytes([packet_type & 0xFF, seq & 0xFF, cmd & 0xFF]) + data
    length = len(payload) + 4
    len_bytes = struct.pack("<H", length)
    return (
        bytes([SOF])
        + len_bytes
        + bytes([crc8(len_bytes)])
        + payload
        + crc32_le(payload)
    )


@dataclass
class Frame:
    packet_type: int
    sequence: int
    command: int
    data: bytes
    valid_crc: bool
    raw: bytes
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        kind = PACKET_TYPE_NAMES.get(self.packet_type, f"0x{self.packet_type:02x}")
        cmd = CMD_NAMES.get(self.command, f"0x{self.command:02x}")
        return f"{kind}:{cmd}"


def parse_frame(buf: bytes) -> Frame | None:
    """Parse a complete framed packet. Returns None if the buffer is malformed."""
    if len(buf) < 8 or buf[0] != SOF:
        return None
    length = struct.unpack("<H", buf[1:3])[0]
    if len(buf) < 4 + length:
        return None
    crc8_byte = buf[3]
    body = buf[4 : 4 + length]
    payload, crc32_field = body[:-4], body[-4:]
    if len(payload) < 3:
        return None
    valid = (
        crc8(buf[1:3]) == crc8_byte
        and crc32_le(payload) == crc32_field
    )
    return Frame(
        packet_type=payload[0],
        sequence=payload[1],
        command=payload[2],
        data=payload[3:],
        valid_crc=valid,
        raw=buf[: 4 + length],
    )


# ---------------------------------------------------------------------------
# Standard BLE Heart Rate Measurement (0x2A37) parsing
# ---------------------------------------------------------------------------
def parse_heart_rate(buf: bytes) -> dict[str, Any]:
    """Parse a Heart Rate Measurement characteristic value.

    Bit 0 of the flags byte selects 8-bit vs 16-bit BPM; bit 4 indicates that
    R-R interval values follow, each a little-endian uint16 in 1/1024 s units.
    """
    if not buf:
        return {"bpm": None, "rr_ms": None}
    flags = buf[0]
    offset = 1
    if flags & 0x01:  # uint16 BPM
        bpm = struct.unpack_from("<H", buf, offset)[0]
        offset += 2
    else:
        bpm = buf[offset]
        offset += 1

    rr_ms: list[float] | None = None
    if flags & 0x10:  # R-R present
        rr_ms = []
        while offset + 1 < len(buf):
            raw = struct.unpack_from("<H", buf, offset)[0]
            rr_ms.append(round(raw * 1000 / 1024, 1))
            offset += 2
    return {"bpm": bpm, "rr_ms": rr_ms}


def rmssd(rr_ms: list[float]) -> float | None:
    """Root mean square of successive differences (a common HRV proxy)."""
    if len(rr_ms) < 2:
        return None
    diffs = [rr_ms[i + 1] - rr_ms[i] for i in range(len(rr_ms) - 1)]
    return round((sum(d * d for d in diffs) / len(diffs)) ** 0.5, 1)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------
def hello(seq: int, timestamp: int = 0) -> bytes:
    return build_frame(PT_COMMAND, seq, CMD_HELLO, struct.pack("<Q", timestamp)[:9])


def rt_hr_on(seq: int) -> bytes:
    return build_frame(PT_COMMAND, seq, CMD_RT_HR_ON)


def get_battery(seq: int) -> bytes:
    return build_frame(PT_COMMAND, seq, CMD_GET_BATTERY)


def enable_standard_hr(seq: int) -> bytes:
    return build_frame(PT_COMMAND, seq, CMD_STD_HR_BROADCAST)


def parse_hello_response(data: bytes) -> dict[str, Any]:
    """Extract what we can from the ~133-byte HELLO response."""
    out: dict[str, Any] = {"len": len(data)}
    if len(data) >= 8:
        serial = data[:8]
        out["serial_raw"] = serial.hex()
        out["serial_ascii"] = "".join(chr(b) if 32 <= b < 127 else "." for b in serial)
    return out
