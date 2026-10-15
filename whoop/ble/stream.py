"""BLE streaming for WHOOP 5.0 (requires the ``ble`` extra: ``pip install .[ble]``).

Two modes:

* ``stream_heart_rate`` - subscribe to the standard Heart Rate characteristic
  (0x2A37) and log BPM + R-R intervals. This is the *proven* path: on macOS it
  works once the strap has been enrolled in the official app, with no bond.
* ``probe_proprietary`` - attempt the fd4b handshake and dump every notification.
  Requires a *bonded/encrypted* link (Linux/BlueZ or iOS); on macOS the
  proprietary channel typically rejects reads/writes with
  "Encryption is insufficient".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..store import Store
from . import protocol as P


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _require_bleak():
    try:
        from bleak import BleakClient, BleakScanner  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise SystemExit(
            "BLE support needs the optional dependency:\n"
            "    pip install 'bleak>=0.22'\n"
            "(or `pip install -e '.[ble]'`)"
        ) from exc


@dataclass
class ScannedDevice:
    address: str
    name: str
    rssi: int | None


async def scan(timeout: float = 8.0, broad: bool = False) -> list[ScannedDevice]:
    """Discover nearby BLE devices, filtering for WHOOP straps unless broad."""
    _require_bleak()
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out: list[ScannedDevice] = []
    for device, adv in found.values():
        name = (device.name or adv.local_name or "") or ""
        service_uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_whoop = (
            "whoop" in name.lower()
            or any(u.startswith("fd4b0001") or u.startswith("61080001") for u in service_uuids)
        )
        if broad or is_whoop:
            out.append(ScannedDevice(device.address, name or "<no name>", adv.rssi))
    return sorted(out, key=lambda d: (d.rssi or -999), reverse=True)


async def stream_heart_rate(
    address: str,
    *,
    seconds: float = 60.0,
    store: Store | None = None,
    device_id: str = "",
    on_sample: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Subscribe to 0x2A37 and collect BPM + R-R samples for ``seconds``."""
    _require_bleak()
    from bleak import BleakClient

    samples: list[dict[str, Any]] = []

    def handler(_sender: Any, data: bytearray) -> None:
        parsed = P.parse_heart_rate(bytes(data))
        sample = {"device_id": device_id or address, "ts": _utcnow(), **parsed}
        samples.append(sample)
        if store is not None:
            store.add_hr_samples([sample])
        if on_sample is not None:
            on_sample(sample)
        else:
            rr = parsed["rr_ms"]
            print(f"HR {parsed['bpm']} bpm" + (f"  rr={rr}" if rr else ""))

    async with BleakClient(address, timeout=30.0) as client:
        await client.start_notify(P.HR_MEASUREMENT, handler)
        await asyncio.sleep(seconds)
        await client.stop_notify(P.HR_MEASUREMENT)

    return samples


async def probe_proprietary(
    address: str,
    *,
    seconds: float = 30.0,
    store: Store | None = None,
    device_id: str = "",
    handshake: bool = True,
) -> int:
    """Dump proprietary-channel notifications (and optionally run the handshake).

    Returns the number of packets captured. On macOS without a bond this often
    yields zero packets after the handshake writes are rejected.
    """
    _require_bleak()
    from bleak import BleakClient

    count = 0

    def make_handler(channel: str):
        def handler(_sender: Any, data: bytearray) -> None:
            nonlocal count
            frame = P.parse_frame(bytes(data))
            packet = {
                "device_id": device_id or address,
                "ts": _utcnow(),
                "channel": channel,
                "hex": bytes(data).hex(),
                "packet_type": frame.packet_type if frame else None,
                "command": frame.command if frame else None,
                "decoded": (
                    {"name": frame.name, "valid_crc": frame.valid_crc, "data": frame.data.hex()}
                    if frame
                    else None
                ),
            }
            count += 1
            if store is not None:
                store.add_raw_packet(packet)
            label = frame.name if frame else "unparsed"
            print(f"[{channel}] {label}  {bytes(data).hex()}")

        return handler

    async with BleakClient(address, timeout=30.0) as client:
        for channel in ("response", "event", "data"):
            uuid = P.FD4B_CHANNELS[channel]
            try:
                await client.start_notify(uuid, make_handler(channel))
            except Exception as exc:  # noqa: BLE001 - surface and continue
                print(f"subscribe {channel} failed: {exc}")

        if handshake:
            for seq, frame in enumerate(
                (P.hello(0), P.get_battery(1), P.enable_standard_hr(2), P.rt_hr_on(3))
            ):
                try:
                    await client.write_gatt_char(P.FD4B_CHANNELS["command"], frame, response=False)
                    print(f"-> sent {frame.hex()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"write seq={seq} failed: {exc}")
                await asyncio.sleep(0.5)

        await asyncio.sleep(seconds)

    return count
