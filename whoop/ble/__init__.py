"""Reverse-engineered WHOOP BLE protocol (4.0 + 5.0/MG) and streaming helpers."""

from . import protocol, sensors
from .protocol import (
    DeviceFamily,
    build_frame,
    crc16_modbus,
    crc8,
    detect_family,
    parse_frame,
    parse_heart_rate,
    rmssd,
)
from .stream import capture, probe_proprietary, scan, stream_heart_rate

__all__ = [
    "protocol",
    "sensors",
    "DeviceFamily",
    "build_frame",
    "parse_frame",
    "detect_family",
    "crc8",
    "crc16_modbus",
    "parse_heart_rate",
    "rmssd",
    "scan",
    "stream_heart_rate",
    "probe_proprietary",
    "capture",
]
