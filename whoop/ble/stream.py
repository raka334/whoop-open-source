"""BLE transport for WHOOP straps (requires the ``ble`` extra: ``pip install .[ble]``).

Three modes:

* ``stream_heart_rate`` -- subscribe to the standard Heart Rate characteristic
  (0x2A37) and log BPM + R-R intervals. This is the *proven* path: on macOS it
  works once the strap has been enrolled in the official app, with no bond.
* ``probe_proprietary`` -- run the family handshake and dump notifications from
  the vendor channels.
* ``capture`` -- enumerate the whole GATT tree, subscribe to **every** notify
  characteristic, and log each raw frame with its ``char_uuid`` and family;
  optionally arm the 100 Hz IMU producer. This is the "keep everything raw"
  path: nothing is decoded or dropped.

The vendor channels require a *bonded/encrypted* link (Linux/BlueZ or iOS). On
macOS the proprietary channel typically rejects reads/writes with
"Encryption is insufficient" -- use ``stream_heart_rate`` there.
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
    services: list[str]


def _write_batch(family: str, *, imu: bool) -> list[tuple[int, bytes]]:
    """The ordered (opcode, frame) handshake/arming batch for a family.

    Order matters: ``client_hello`` must go before anything else, and on 5/MG the
    raw-data producer is armed by ``START_RAW_DATA`` -- ``TOGGLE_IMU_MODE`` alone
    only acknowledges.
    """
    batch = [
        (P.CMD_HELLO if family == "whoop5" else P.CMD_HELLO_HARVARD,
         P.client_hello() if family == "whoop5" else P.hello_harvard(1)),
        (P.CMD_GET_BATTERY, P.get_battery(family, 2)),
        (P.CMD_GET_DATA_RANGE, P.get_data_range(family, 3)),
        (P.CMD_STD_HR_BROADCAST, P.enable_standard_hr(family, 4)),
        (P.CMD_RT_HR_ON, P.rt_hr_on(family, 5)),
    ]
    if imu:
        batch += [
            (P.CMD_START_RAW_DATA, P.start_raw_data(family, 6)),
            (P.CMD_TOGGLE_IMU_MODE, P.toggle_imu_mode(family, 7, enabled=True)),
        ]
    return batch


def _stop_batch(family: str) -> list[tuple[int, bytes]]:
    return [
        (P.CMD_STOP_RAW_DATA, P.stop_raw_data(family, 8)),
        (P.CMD_TOGGLE_IMU_MODE, P.toggle_imu_mode(family, 9, enabled=False)),
    ]


async def scan(timeout: float = 8.0, broad: bool = False) -> list[ScannedDevice]:
    """Discover nearby BLE devices, filtering for WHOOP straps unless broad."""
    _require_bleak()
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out: list[ScannedDevice] = []
    for device, adv in found.values():
        name = (device.name or adv.local_name or "") or ""
        service_uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_whoop = "whoop" in name.lower() or P.family_for_service(
            service_uuids[0] if service_uuids else ""
        ) is not None or any(
            u.startswith("fd4b0001") or u.startswith("61080001") for u in service_uuids
        )
        if broad or is_whoop:
            out.append(ScannedDevice(device.address, name or "<no name>", adv.rssi, service_uuids))
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


def _make_frame_handler(
    channel: str,
    char_uuid: str,
    *,
    store: Store | None,
    device_id: str,
    counter: list[int],
    echo: bool,
    quiet: bool,
):
    def handler(_sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        frame = P.parse_frame(raw)
        packet = {
            "device_id": device_id,
            "ts": _utcnow(),
            "channel": channel,
            "char_uuid": char_uuid.lower(),
            "family": frame.family.value if frame else None,
            "hex": raw.hex(),
            "packet_type": frame.packet_type if frame else None,
            "command": frame.command if frame else None,
            "decoded": (
                {"name": frame.name, "valid_crc": frame.valid_crc, "data": frame.data.hex()}
                if frame
                else {"unparsed": True}
            ),
        }
        counter[0] += 1
        if store is not None:
            store.add_raw_packet(packet)
        if not quiet:
            label = frame.name if frame else "unparsed"
            print(f"[{channel}] {label}  {raw.hex()}")

    return handler


async def probe_proprietary(
    address: str,
    *,
    family: str = "whoop5",
    seconds: float = 30.0,
    store: Store | None = None,
    device_id: str = "",
    handshake: bool = True,
    allow_side_effect: bool = False,
) -> int:
    """Send the family handshake and log vendor-channel notifications.

    Returns the number of packets captured. On macOS without a bond this often
    yields zero packets after the handshake writes are rejected.
    """
    _require_bleak()
    from bleak import BleakClient

    spec = P.family_spec(family)
    counter = [0]

    async with BleakClient(address, timeout=30.0) as client:
        for channel in ("response", "event", "data"):
            uuid = spec.channels.get(channel)
            if not uuid:
                continue
            try:
                await client.start_notify(
                    uuid,
                    _make_frame_handler(
                        channel,
                        uuid,
                        store=store,
                        device_id=device_id or address,
                        counter=counter,
                        echo=True,
                        quiet=False,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - surface and continue
                print(f"subscribe {channel} failed: {exc}")

        if handshake:
            for cmd, frame in _write_batch(family, imu=False):
                try:
                    P.assert_writable(cmd, allow_side_effect=allow_side_effect)
                except PermissionError as exc:
                    print(f"skipped: {exc}")
                    continue
                try:
                    await client.write_gatt_char(spec.channels["command"], frame, response=True)
                    print(f"-> sent {frame.hex()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"write failed: {exc}")
                await asyncio.sleep(0.5)

        await asyncio.sleep(seconds)

    return counter[0]


async def capture(
    address: str,
    *,
    family: str | None = None,
    seconds: float = 60.0,
    store: Store | None = None,
    device_id: str = "",
    imu: bool = False,
    note: str | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Enumerate every GATT characteristic, subscribe to all notify ones, and
    log raw frames (with ``char_uuid`` + family) until the timer expires.

    If ``imu`` is set, arm the 100 Hz raw-motion producer after the handshake and
    stop it afterwards. Returns a small summary dict.
    """
    _require_bleak()
    from bleak import BleakClient

    counter = [0]
    gatt: list[dict[str, Any]] = []
    resolved_family = family

    async with BleakClient(address, timeout=30.0) as client:
        # 1. Enumerate the GATT tree so nothing is invisible.
        for service in client.services:
            svc_row: dict[str, Any] = {"service": service.uuid.lower(), "chars": []}
            for char in service.characteristics:
                svc_row["chars"].append(
                    {"uuid": char.uuid.lower(), "properties": list(char.properties)}
                )
                if resolved_family is None:
                    resolved_family = (
                        P.family_for_service(service.uuid) or resolved_family
                    )
            gatt.append(svc_row)

        resolved_family = resolved_family or "whoop5"
        spec = P.family_spec(resolved_family)
        print(f"family: {spec.label}  ({spec.service})")
        for s in gatt:
            props = " ".join(sorted({p for c in s["chars"] for p in c["properties"]}))
            print(f"  {s['service']}  [{props}]")

        session_id = None
        if store is not None:
            session_id = store.start_capture(
                device_id=device_id or address,
                family=resolved_family,
                address=address,
                note=note,
            )

        # 2. Subscribe to every notifiable characteristic, tagged by UUID.
        subscribed = 0
        for service in client.services:
            for char in service.characteristics:
                if not ({"notify", "indicate"} & set(char.properties)):
                    continue
                channel = next(
                    (k for k, v in spec.channels.items() if v == char.uuid.lower()),
                    "other",
                )
                try:
                    await client.start_notify(
                        char.uuid,
                        _make_frame_handler(
                            channel,
                            char.uuid,
                            store=store,
                            device_id=device_id or address,
                            counter=counter,
                            echo=True,
                            quiet=quiet,
                        ),
                    )
                    subscribed += 1
                except Exception as exc:  # noqa: BLE001
                    if not quiet:
                        print(f"subscribe {char.uuid} failed: {exc}")

        # 3. Handshake (+ optional IMU arming) on the vendor command channel.
        if spec.channels.get("command"):
            for cmd, frame in _write_batch(resolved_family, imu=imu):
                try:
                    P.assert_writable(cmd)
                except PermissionError as exc:
                    print(f"skipped: {exc}")
                    continue
                try:
                    await client.write_gatt_char(
                        spec.channels["command"], frame, response=True
                    )
                    if not quiet:
                        print(f"-> sent {frame.hex()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"write failed: {exc}")
                await asyncio.sleep(0.4)

        await asyncio.sleep(seconds)

        if imu and spec.channels.get("command"):
            for _cmd, frame in _stop_batch(resolved_family):
                try:
                    await client.write_gatt_char(
                        spec.channels["command"], frame, response=True
                    )
                except Exception:  # noqa: BLE001
                    pass

    if store is not None and session_id is not None:
        store.finish_capture(session_id, counter[0])

    return {
        "family": resolved_family,
        "packets": counter[0],
        "subscribed": subscribed,
        "gatt": gatt,
        "session_id": session_id,
        "imu": imu,
    }
