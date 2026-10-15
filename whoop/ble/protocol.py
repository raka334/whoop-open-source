"""WHOOP proprietary BLE protocol -- family-aware (WHOOP 4 and WHOOP 5/MG).

WHOOP ships **two incompatible on-the-wire envelopes**. They share a command
number vocabulary and a CRC32 payload trailer, but the headers differ. A WHOOP 5
frame is *not* a WHOOP 4 frame with shifted offsets -- always select the family
before parsing.

WHOOP 4 ("Harvard")::

    +----+--------+-------+--------------------------+-----------+
    | AA | LEN LE | CRC8  | type|seq|cmd|data         | CRC32 LE  |
    +----+--------+-------+--------------------------+-----------+
      1B    2B      1B      LEN - 4 bytes                4B

    * inner  = type|seq|cmd|data
    * LEN    = len(inner) + 4
    * CRC8   = poly 0x07, init 0x00, over the two LEN bytes ONLY
    * CRC32  = zlib.crc32(inner), little-endian
    * GATT   = service 61080001-...

WHOOP 5 / MG ("puffin")::

    +----+----+--------+-------+--------+------------------+-----------+
    | AA | 01 | LEN LE | FIELD | CRC16  | inner (4-padded)  | CRC32 LE  |
    +----+----+--------+-------+--------+------------------+-----------+
      1B   1B    2B       2B      2B       LEN - 4 bytes        4B

    * FIELD = 00 01 requests / 01 00 responses
    * LEN   = len(padded inner) + 4  ;  total frame = LEN + 8
    * CRC16 = Modbus: poly 0xA001 (reflected 0x8005), init 0xFFFF,
              over bytes [0, 6)
    * inner = type|seq|cmd|body, zero-padded to a multiple of 4
    * CRC32 = zlib.crc32(inner), little-endian
    * GATT  = service fd4b0001-...

Verified anchors (see ``tests/test_protocol.py``):

* WHOOP 4 HELLO, ``aa 10 00 57 23 04 23 ...`` (CRC8 ``0x57`` over the LEN bytes).
* WHOOP 5 CLIENT_HELLO, ``aa 01 08 00 00 01 e6 71 23 01 91 01 36 3e 5c 8d``
  (CRC16-Modbus ``0x71e6`` header, CRC32 ``0x8d5c3e36`` body).

Credits: the 4.0 envelope from ``johnmiddleton12/my-whoop``; the 5/MG envelope
from ``b-nnett/goose`` and the ``ryanbr/noop`` protocol reference.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SOF = 0xAA


# ===========================================================================
# Device families
# ===========================================================================
class DeviceFamily(str, Enum):
    """The two wire generations. The GATT service selects one, unambiguously."""

    WHOOP4 = "whoop4"
    WHOOP5 = "whoop5"


SERVICE_WHOOP4 = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
SERVICE_WHOOP5 = "fd4b0001-cce1-4033-93ce-002d5875f58a"

WHOOP4_CHANNELS = {
    "command": "61080002-8d6d-82b8-614a-1c8cb0f8dcc6",  # write
    "response": "61080003-8d6d-82b8-614a-1c8cb0f8dcc6",  # notify
    "event": "61080004-8d6d-82b8-614a-1c8cb0f8dcc6",  # notify
    "data": "61080005-8d6d-82b8-614a-1c8cb0f8dcc6",  # notify
}

WHOOP5_CHANNELS = {
    "command": "fd4b0002-cce1-4033-93ce-002d5875f58a",  # write
    "response": "fd4b0003-cce1-4033-93ce-002d5875f58a",  # notify
    "event": "fd4b0004-cce1-4033-93ce-002d5875f58a",  # notify
    "data": "fd4b0005-cce1-4033-93ce-002d5875f58a",  # notify
    "memfault": "fd4b0007-cce1-4033-93ce-002d5875f58a",  # notify
}

# Back-compat alias (older code/tests referenced FD4B_CHANNELS).
FD4B_CHANNELS = WHOOP5_CHANNELS


@dataclass(frozen=True)
class FamilySpec:
    family: DeviceFamily
    label: str
    service: str
    channels: dict[str, str]
    inner_offset: int  # first byte of the inner record
    min_frame: int
    padded: bool  # inner is zero-padded to a 4-byte multiple

    def total_size(self, declared_len: int) -> int:
        """Total frame length implied by the header's declared length."""
        return declared_len + self.inner_offset


FAMILIES: dict[DeviceFamily, FamilySpec] = {
    DeviceFamily.WHOOP4: FamilySpec(
        family=DeviceFamily.WHOOP4,
        label='WHOOP 4 ("Harvard")',
        service=SERVICE_WHOOP4,
        channels=WHOOP4_CHANNELS,
        inner_offset=4,
        min_frame=11,
        padded=False,
    ),
    DeviceFamily.WHOOP5: FamilySpec(
        family=DeviceFamily.WHOOP5,
        label='WHOOP 5/MG ("puffin")',
        service=SERVICE_WHOOP5,
        channels=WHOOP5_CHANNELS,
        inner_offset=8,
        min_frame=16,
        padded=True,
    ),
}

# The 2-byte WHOOP 5 header field that distinguishes direction.
FIELD_REQUEST = b"\x00\x01"
FIELD_RESPONSE = b"\x01\x00"


def as_family(value: "DeviceFamily | str") -> DeviceFamily:
    """Coerce a string or enum into a :class:`DeviceFamily`."""
    return value if isinstance(value, DeviceFamily) else DeviceFamily(value)


def family_spec(value: "DeviceFamily | str") -> FamilySpec:
    return FAMILIES[as_family(value)]


def channels(value: "DeviceFamily | str") -> dict[str, str]:
    """Return a copy of the channel map for a family."""
    return dict(family_spec(value).channels)


def family_for_service(uuid: str) -> DeviceFamily | None:
    """Map a GATT service/characteristic UUID to its family, if it is a WHOOP one."""
    u = uuid.lower()
    for spec in FAMILIES.values():
        if spec.service == u or u in spec.channels.values():
            return spec.family
    # Tolerate 16-bit / shortened forms.
    if u.startswith("6108000"):
        return DeviceFamily.WHOOP4
    if u.startswith("fd4b000"):
        return DeviceFamily.WHOOP5
    return None


# ===========================================================================
# Packet types (shared vocabulary; some names are family-specific)
# ===========================================================================
PT_COMMAND = 0x23  # 35: app -> strap request
PT_RESPONSE = 0x24  # 36: strap -> app command reply
PT_DATA = 0x28  # 40: 4.0 generic data / 5.0 live HR + R-R
PT_REALTIME = 0x2B  # 43: realtime stream
PT_HISTORICAL = 0x2F  # 47: historical / offload records
PT_EVENT = 0x30  # 48: events

PACKET_TYPE_NAMES = {
    PT_COMMAND: "command",
    PT_RESPONSE: "response",
    PT_DATA: "data",
    PT_REALTIME: "realtime",
    PT_HISTORICAL: "historical",
    PT_EVENT: "event",
}


# ===========================================================================
# Commands (numbers share one vocabulary across generations where they match)
# ===========================================================================
CMD_RT_HR_ON = 0x03
CMD_CLOCK_SYNC = 0x0A
CMD_STD_HR_BROADCAST = 0x0E  # enables the standard 0x2A37 characteristic
CMD_SEND_HISTORICAL = 0x16
CMD_HISTORICAL_RESULT = 0x17
CMD_GET_BATTERY = 0x1A
CMD_GET_DATA_RANGE = 0x22
CMD_HELLO_HARVARD = 0x23  # WHOOP 4 GET_HELLO
CMD_SEND_R10_R11_REALTIME = 0x3F  # 63: the real "stop the realtime flood" op
CMD_SET_ALARM_TIME = 0x42  # 5.0: NOT SET_CLOCK -- it sets an alarm (buzzes)
CMD_START_RAW_DATA = 0x51  # 81
CMD_STOP_RAW_DATA = 0x52  # 82
CMD_TOGGLE_IMU_MODE = 0x6A  # 106
CMD_SET_CONFIG = 0x78  # 120
CMD_GET_ADVERTISING_NAME = 0x8D  # 141
CMD_HELLO = 0x91  # 145: WHOOP 5 GET_HELLO
CMD_GET_BATTERY_PACK = 0x97  # 151

CMD_NAMES = {
    CMD_RT_HR_ON: "RT_HR_ON",
    CMD_CLOCK_SYNC: "SET_CLOCK",
    CMD_STD_HR_BROADCAST: "STD_HR_BROADCAST",
    CMD_SEND_HISTORICAL: "SEND_HISTORICAL",
    CMD_HISTORICAL_RESULT: "HISTORICAL_RESULT",
    CMD_GET_BATTERY: "GET_BATTERY",
    CMD_GET_DATA_RANGE: "GET_DATA_RANGE",
    CMD_HELLO_HARVARD: "HELLO_HARVARD",
    CMD_SEND_R10_R11_REALTIME: "SEND_R10_R11_REALTIME",
    CMD_SET_ALARM_TIME: "SET_ALARM_TIME",
    CMD_START_RAW_DATA: "START_RAW_DATA",
    CMD_STOP_RAW_DATA: "STOP_RAW_DATA",
    CMD_TOGGLE_IMU_MODE: "TOGGLE_IMU_MODE",
    CMD_SET_CONFIG: "SET_CONFIG",
    CMD_GET_ADVERTISING_NAME: "GET_ADVERTISING_NAME",
    CMD_HELLO: "HELLO",
    CMD_GET_BATTERY_PACK: "GET_BATTERY_PACK",
}


class CommandSafety(str, Enum):
    """How safe a command is to *write* to a strap you own.

    We do not invent opcodes for destructive operations. Instead, anything not
    explicitly listed as benign or a known side effect is ``UNKNOWN`` and is
    refused unless the caller opts in -- the safe default for a device that can
    brick itself (firmware load, force-trim, ship mode, fuel-gauge reset).
    """

    BENIGN = "benign"
    SIDE_EFFECT = "side_effect"
    UNKNOWN = "unknown"


_BENIGN = {
    CMD_RT_HR_ON,
    CMD_STD_HR_BROADCAST,
    CMD_SEND_HISTORICAL,
    CMD_HISTORICAL_RESULT,
    CMD_GET_BATTERY,
    CMD_GET_BATTERY_PACK,
    CMD_GET_DATA_RANGE,
    CMD_HELLO_HARVARD,
    CMD_HELLO,
    CMD_GET_ADVERTISING_NAME,
    CMD_START_RAW_DATA,
    CMD_STOP_RAW_DATA,
    CMD_SET_CONFIG,
    CMD_SEND_R10_R11_REALTIME,
}
# Reversible, but with a user-visible effect (e.g. 0x42 sets an alarm: it buzzes).
_SIDE_EFFECT = {CMD_CLOCK_SYNC, CMD_SET_ALARM_TIME, CMD_TOGGLE_IMU_MODE}


def classify_command(cmd: int) -> CommandSafety:
    if cmd in _BENIGN:
        return CommandSafety.BENIGN
    if cmd in _SIDE_EFFECT:
        return CommandSafety.SIDE_EFFECT
    return CommandSafety.UNKNOWN


def assert_writable(
    cmd: int, *, allow_side_effect: bool = False, allow_unknown: bool = False
) -> None:
    """Raise unless ``cmd`` is safe to send under the caller's policy."""
    safety = classify_command(cmd)
    if safety is CommandSafety.BENIGN:
        return
    name = CMD_NAMES.get(cmd, f"0x{cmd:02x}")
    if safety is CommandSafety.SIDE_EFFECT and allow_side_effect:
        return
    if safety is CommandSafety.UNKNOWN and allow_unknown:
        return
    raise PermissionError(
        f"refusing to write {name} ({safety.value}): "
        "pass allow_side_effect/allow_unknown to override"
    )


# ===========================================================================
# Standard Bluetooth SIG services (unbonded, both generations)
# ===========================================================================
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# WHOOP 5 connect hello -- a real, captured frame (see module docstring).
CLIENT_HELLO = bytes.fromhex("aa 01 08 00 00 01 e6 71 23 01 91 01 36 3e 5c 8d")


# ===========================================================================
# Checksums
# ===========================================================================
def crc8(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    """CRC-8, poly 0x07 (WHOOP 4 header check)."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16_modbus(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/MODBUS: reflected poly 0xA001 (0x8005), init 0xFFFF (WHOOP 5 header)."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc32_le(data: bytes) -> bytes:
    """CRC-32 (zlib) little-endian trailer, shared by both families."""
    return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)


# ===========================================================================
# Framing
# ===========================================================================
@dataclass
class Frame:
    family: DeviceFamily
    packet_type: int
    sequence: int
    command: int
    data: bytes
    valid_crc: bool
    raw: bytes
    header_field: bytes = b""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def type_name(self) -> str:
        return PACKET_TYPE_NAMES.get(self.packet_type, f"0x{self.packet_type:02x}")

    @property
    def command_name(self) -> str:
        return CMD_NAMES.get(self.command, f"0x{self.command:02x}")

    @property
    def name(self) -> str:
        return f"{self.type_name}:{self.command_name}"

    @property
    def is_response(self) -> bool:
        if self.family is DeviceFamily.WHOOP5 and self.header_field:
            return self.header_field == FIELD_RESPONSE
        return self.packet_type == PT_RESPONSE


def build_frame(
    family: "DeviceFamily | str",
    packet_type: int,
    seq: int,
    cmd: int,
    body: bytes = b"",
) -> bytes:
    """Assemble a framed packet for the given family."""
    spec = family_spec(family)
    inner = bytes([packet_type & 0xFF, seq & 0xFF, cmd & 0xFF]) + bytes(body)
    if spec.padded and len(inner) % 4:
        inner += bytes(4 - len(inner) % 4)
    declared = len(inner) + 4

    if spec.family is DeviceFamily.WHOOP4:
        len_bytes = struct.pack("<H", declared)
        return bytes([SOF]) + len_bytes + bytes([crc8(len_bytes)]) + inner + crc32_le(inner)

    head = bytes([SOF, 0x01]) + struct.pack("<H", declared) + FIELD_REQUEST
    return head + struct.pack("<H", crc16_modbus(head)) + inner + crc32_le(inner)


def detect_family(buf: bytes) -> DeviceFamily | None:
    """Identify the family of a complete frame by validating its header check."""
    if len(buf) < 8 or buf[0] != SOF:
        return None

    # WHOOP 5: format byte 0x01 + CRC16-Modbus header.
    if len(buf) >= 12 and buf[1] == 0x01:
        declared = struct.unpack_from("<H", buf, 2)[0]
        if declared >= 8 and len(buf) >= declared + 8:
            if struct.unpack_from("<H", buf, 6)[0] == crc16_modbus(buf[0:6]):
                return DeviceFamily.WHOOP5

    # WHOOP 4: CRC8 over the two declared-length bytes.
    declared = struct.unpack_from("<H", buf, 1)[0]
    if declared >= 7 and len(buf) >= declared + 4 and buf[3] == crc8(buf[1:3]):
        return DeviceFamily.WHOOP4
    return None


def parse_frame(buf: bytes, family: "DeviceFamily | str | None" = None) -> Frame | None:
    """Parse a complete frame. Family is auto-detected unless supplied."""
    fam = as_family(family) if family is not None else detect_family(buf)
    if fam is None:
        return None
    spec = FAMILIES[fam]
    if len(buf) < spec.min_frame or buf[0] != SOF:
        return None

    len_at = 2 if fam is DeviceFamily.WHOOP5 else 1
    declared = struct.unpack_from("<H", buf, len_at)[0]
    total = spec.total_size(declared)
    if total < spec.min_frame or len(buf) < total:
        return None

    inner = buf[spec.inner_offset : total - 4]
    crc_field = buf[total - 4 : total]
    if len(inner) < 3:
        return None

    if fam is DeviceFamily.WHOOP5:
        header_field = bytes(buf[4:6])
        valid = (
            header_field in (FIELD_REQUEST, FIELD_RESPONSE)
            and struct.unpack_from("<H", buf, 6)[0] == crc16_modbus(buf[0:6])
            and crc32_le(inner) == crc_field
        )
    else:
        header_field = b""
        valid = buf[3] == crc8(buf[1:3]) and crc32_le(inner) == crc_field

    return Frame(
        family=fam,
        packet_type=inner[0],
        sequence=inner[1],
        command=inner[2],
        data=inner[3:],
        valid_crc=valid,
        raw=buf[:total],
        header_field=header_field,
    )


# ===========================================================================
# Standard BLE Heart Rate Measurement (0x2A37) parsing
# ===========================================================================
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


# ===========================================================================
# Command builders
# ===========================================================================
def client_hello() -> bytes:
    """WHOOP 5 connect hello (sequence 1, command 0x91, selector byte 0x01)."""
    return build_frame(DeviceFamily.WHOOP5, PT_COMMAND, 0x01, CMD_HELLO, b"\x01")


def hello_harvard(seq: int, timestamp: int = 0) -> bytes:
    """WHOOP 4 GET_HELLO (0x23) with an optional 8-byte clock."""
    return build_frame(
        DeviceFamily.WHOOP4, PT_COMMAND, seq, CMD_HELLO_HARVARD, struct.pack("<Q", timestamp)
    )


def rt_hr_on(family: "DeviceFamily | str", seq: int) -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_RT_HR_ON)


def enable_standard_hr(family: "DeviceFamily | str", seq: int) -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_STD_HR_BROADCAST)


def get_battery(family: "DeviceFamily | str", seq: int) -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_GET_BATTERY)


def get_data_range(family: "DeviceFamily | str", seq: int) -> bytes:
    """Read the strap's ring-buffer bounds / capacity (selector byte 0x00)."""
    return build_frame(family, PT_COMMAND, seq, CMD_GET_DATA_RANGE, b"\x00")


def get_advertising_name(family: "DeviceFamily | str", seq: int) -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_GET_ADVERTISING_NAME, b"\x01")


def send_historical(family: "DeviceFamily | str", seq: int, cursor: bytes = b"") -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_SEND_HISTORICAL, cursor)


def historical_ack(family: "DeviceFamily | str", seq: int, cursor: bytes) -> bytes:
    """Ack a history chunk. The 8-byte cursor must be echoed verbatim, or the
    strap keeps re-serving the same chunk forever."""
    return build_frame(family, PT_COMMAND, seq, CMD_HISTORICAL_RESULT, cursor)


def start_raw_data(family: "DeviceFamily | str", seq: int) -> bytes:
    """Toggle raw/motion producer on (enable byte 0x01)."""
    return build_frame(family, PT_COMMAND, seq, CMD_START_RAW_DATA, b"\x01")


def stop_raw_data(family: "DeviceFamily | str", seq: int) -> bytes:
    return build_frame(family, PT_COMMAND, seq, CMD_STOP_RAW_DATA, b"\x01")


def toggle_imu_mode(
    family: "DeviceFamily | str", seq: int, *, enabled: bool
) -> bytes:
    """Arm/disarm IMU mode. Two bytes: [mode, enable]. Command 106 only ACKs --
    it does not itself start the producer; pair it with start/stop_raw_data."""
    return build_frame(
        family, PT_COMMAND, seq, CMD_TOGGLE_IMU_MODE, bytes([0x01, 0x01 if enabled else 0x00])
    )


def parse_hello_response(data: bytes) -> dict[str, Any]:
    """Extract what we can from a HELLO response body."""
    out: dict[str, Any] = {"len": len(data)}
    if len(data) >= 8:
        serial = data[:8]
        out["serial_raw"] = serial.hex()
        out["serial_ascii"] = "".join(chr(b) if 32 <= b < 127 else "." for b in serial)
    return out
