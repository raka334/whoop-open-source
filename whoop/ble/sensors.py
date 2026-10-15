"""WHOOP 5/MG sensor record decoders.

Provenance: the record layouts come from the community protocol reference
(``ryanbr/noop`` -> ``PROTOCOL_SENSORS.md``), **not** from a capture verified in
this repository. Offsets are frame-relative (byte 0 = the ``AA`` start-of-frame
byte), which is what those documents describe. Treat every field as provisional
until corroborated against your own capture -- "plausible" is not "verified".

Implemented here:

* **Packet 40** -- live HR + R-R, pushed on the realtime/data channel.
* **R21** -- 100-sample six-axis IMU burst (the 100 Hz raw-motion payload).

The R21 record is itself a complete WHOOP 5 frame whose CRC32 trailer covers the
inner record, so it can be integrity-checked before decoding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from . import protocol as P

# --- scales ---------------------------------------------------------------
ACCEL_SCALE_G = 1.0 / 4096.0  # 16 g full-scale, counts -> g
GYRO_SCALE_DPS = 2000.0 / 32768.0  # counts -> deg/s

R21_FRAME_SIZE = 1244
R21_IMU_CAPACITY = 100


def _i16(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<h", buf, offset)[0]


# ===========================================================================
# Packet 40 -- live heart rate + R-R intervals
# ===========================================================================
def decode_live_hr(frame_bytes: bytes) -> dict[str, Any] | None:
    """Decode a WHOOP 5 packet-40 live HR/R-R frame.

    Layout (frame-relative): ``bpm`` at 16, sample ``count`` at 17, then
    ``count`` little-endian uint16 R-R ticks at 18 + 2*i, each in 1/1024 s.
    Returns ``None`` if the buffer is too short.
    """
    if len(frame_bytes) < 18:
        return None
    bpm = frame_bytes[16]
    count = frame_bytes[17]
    rr_ms: list[float] = []
    for i in range(count):
        at = 18 + 2 * i
        if at + 2 > len(frame_bytes):
            break
        ticks = struct.unpack_from("<H", frame_bytes, at)[0]
        rr_ms.append((ticks * 1000 + 512) // 1024)
    return {"bpm": bpm, "rr_ticks": count, "rr_ms": rr_ms}


# ===========================================================================
# R21 -- 100-sample six-axis IMU burst
# ===========================================================================
@dataclass
class ImuSample:
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass
class ImuBurst:
    accel_count: int
    gyro_count: int
    accel_g: list[tuple[float, float, float]]
    gyro_dps: list[tuple[float, float, float]]
    crc_ok: bool

    @property
    def samples(self) -> list[ImuSample]:
        n = min(
            self.accel_count,
            self.gyro_count,
            len(self.accel_g),
            len(self.gyro_dps),
            R21_IMU_CAPACITY,
        )
        return [
            ImuSample(
                ax=self.accel_g[i][0],
                ay=self.accel_g[i][1],
                az=self.accel_g[i][2],
                gx=self.gyro_dps[i][0],
                gy=self.gyro_dps[i][1],
                gz=self.gyro_dps[i][2],
            )
            for i in range(n)
        ]


def is_r21_frame(buf: bytes) -> bool:
    """True if ``buf`` looks like a 1244-byte WHOOP 5 frame with a valid CRC32."""
    if len(buf) != R21_FRAME_SIZE or buf[:1] != bytes([P.SOF]):
        return False
    import zlib

    inner = buf[8 : R21_FRAME_SIZE - 4]
    trailer = struct.unpack_from("<I", buf, R21_FRAME_SIZE - 4)[0]
    return (zlib.crc32(inner) & 0xFFFFFFFF) == trailer


def decode_imu_r21(frame_bytes: bytes) -> ImuBurst | None:
    """Decode one R21 six-axis burst.

    Frame-relative layout: accel count at 24, accel x/y/z at 28/228/428 + 2*i;
    gyro count at 630, gyro x/y/z at 640/840/1040 + 2*i. Accel is scaled to g
    (1/4096) and gyro to deg/s (2000/32768).
    """
    if len(frame_bytes) < R21_FRAME_SIZE:
        return None

    accel_count = frame_bytes[24]
    gyro_count = frame_bytes[630]

    accel: list[tuple[float, float, float]] = []
    for i in range(min(accel_count, R21_IMU_CAPACITY)):
        accel.append(
            (
                _i16(frame_bytes, 28 + 2 * i) * ACCEL_SCALE_G,
                _i16(frame_bytes, 228 + 2 * i) * ACCEL_SCALE_G,
                _i16(frame_bytes, 428 + 2 * i) * ACCEL_SCALE_G,
            )
        )

    gyro: list[tuple[float, float, float]] = []
    for i in range(min(gyro_count, R21_IMU_CAPACITY)):
        gyro.append(
            (
                _i16(frame_bytes, 640 + 2 * i) * GYRO_SCALE_DPS,
                _i16(frame_bytes, 840 + 2 * i) * GYRO_SCALE_DPS,
                _i16(frame_bytes, 1040 + 2 * i) * GYRO_SCALE_DPS,
            )
        )

    return ImuBurst(
        accel_count=accel_count,
        gyro_count=gyro_count,
        accel_g=accel,
        gyro_dps=gyro,
        crc_ok=is_r21_frame(frame_bytes),
    )
