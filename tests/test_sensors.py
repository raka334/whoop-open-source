"""Sensor decoder tests (WHOOP 5/MG packet 40 and R21 IMU).

These use **synthetic** buffers built to the documented layout, so they only
prove the decoder is self-consistent -- not that the offsets match a real strap.
Provenance is labelled in ``whoop/ble/sensors.py``.
"""

from __future__ import annotations

import struct
import zlib

from whoop.ble import sensors as S


def _i16(value: int) -> bytes:
    return struct.pack("<h", value)


# ------------------------------- packet 40 ---------------------------------
def test_decode_live_hr():
    buf = bytearray(32)
    buf[16] = 58
    buf[17] = 2
    buf[18:20] = struct.pack("<H", 1024)  # 1000 ms
    buf[20:22] = struct.pack("<H", 512)  # 500 ms
    out = S.decode_live_hr(bytes(buf))
    assert out is not None
    assert out["bpm"] == 58
    assert out["rr_ms"] == [1000, 500]


def test_decode_live_hr_too_short():
    assert S.decode_live_hr(b"\x00" * 8) is None


# --------------------------------- R21 -------------------------------------
def _make_r21(accel_count: int, gyro_count: int) -> bytes:
    buf = bytearray(S.R21_FRAME_SIZE)
    buf[0] = 0xAA
    buf[1] = 0x01
    buf[24] = accel_count
    buf[630] = gyro_count
    for i in range(accel_count):
        buf[28 + 2 * i : 30 + 2 * i] = _i16(4096)  # 1.0 g
        buf[228 + 2 * i : 230 + 2 * i] = _i16(0)
        buf[428 + 2 * i : 430 + 2 * i] = _i16(-4096)  # -1.0 g
    for i in range(gyro_count):
        buf[640 + 2 * i : 642 + 2 * i] = _i16(16384)  # 1000 dps
        buf[840 + 2 * i : 842 + 2 * i] = _i16(0)
        buf[1040 + 2 * i : 1042 + 2 * i] = _i16(-32768)  # -2000 dps
    # CRC32 trailer over the inner record [8, 1240).
    crc = zlib.crc32(bytes(buf[8 : S.R21_FRAME_SIZE - 4])) & 0xFFFFFFFF
    struct.pack_into("<I", buf, S.R21_FRAME_SIZE - 4, crc)
    return bytes(buf)


def test_is_r21_frame_checks_crc():
    good = _make_r21(3, 3)
    assert S.is_r21_frame(good)
    bad = bytearray(good)
    bad[40] ^= 0x01
    assert not S.is_r21_frame(bytes(bad))


def test_decode_imu_r21_scales_and_counts():
    burst = S.decode_imu_r21(_make_r21(4, 4))
    assert burst is not None
    assert burst.crc_ok
    assert burst.accel_count == 4 and burst.gyro_count == 4
    assert len(burst.samples) == 4
    ax, ay, az = burst.accel_g[0]
    assert round(ax, 3) == 1.0
    assert ay == 0.0
    assert round(az, 3) == -1.0
    gx, gy, gz = burst.gyro_dps[0]
    assert round(gx, 1) == 1000.0
    assert round(gz, 1) == -2000.0


def test_decode_imu_r21_too_short():
    assert S.decode_imu_r21(b"\x00" * 10) is None
