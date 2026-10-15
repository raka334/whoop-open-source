"""Command-line interface: ``python -m whoop.cli <command>`` or ``whoop <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Config
from .store import Store
from .ble import protocol as P


def _config_and_store() -> tuple[Config, Store]:
    config = Config.load()
    return config, Store(config.db_path)


# ------------------------------- commands ----------------------------------
def cmd_auth_url(args: argparse.Namespace) -> int:
    from .api import oauth

    config, _ = _config_and_store()
    config.require_api()
    print(oauth.authorization_url(config, state=args.state))
    print("\nOpen the URL above, approve access, then copy the `code` query")
    print("parameter from the redirect URL and run:")
    print("    whoop auth-exchange '<code>'")
    return 0


def cmd_auth_exchange(args: argparse.Namespace) -> int:
    from .api import oauth

    config, store = _config_and_store()
    config.require_api()
    payload = oauth.exchange_code(config, args.code)
    store.save_tokens("whoop", payload)
    print("Tokens stored.")
    with store:
        print(f"  scope: {payload.get('scope')}")
        print(f"  db:    {store.path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .api import sync as api_sync

    config, store = _config_and_store()
    resources = tuple(args.resources.split(",")) if args.resources else None
    with store:
        counts = api_sync.sync(
            config, store, start=args.start, end=args.end, resources=resources
        )
        print("Synced from official API:")
        for name, n in counts.items():
            print(f"  {name:12s} {n}")
    return 0


def cmd_ble_scan(args: argparse.Namespace) -> int:
    from .ble import stream

    devices = asyncio.run(stream.scan(timeout=args.timeout, broad=args.broad))
    if not devices:
        print("No devices found. Try --broad, or move the strap closer.")
        return 1
    for d in devices:
        print(f"  {d.address}  rssi={d.rssi}  name={d.name}")
    print("\nSet WHOOP_BLE_ADDRESS in .env to the address above.")
    return 0


def cmd_ble_hr(args: argparse.Namespace) -> int:
    from .ble import stream

    config, store = _config_and_store()
    address = args.address or config.ble_address
    if not address:
        raise SystemExit("No address. Pass --address or set WHOOP_BLE_ADDRESS.")
    with store:
        samples = asyncio.run(
            stream.stream_heart_rate(
                address,
                seconds=args.seconds,
                store=store,
                device_id=config.device_id or address,
            )
        )
        rrs = [rr for s in samples if s.get("rr_ms") for rr in s["rr_ms"]]
        print(f"\nCaptured {len(samples)} HR samples into {store.path}")
        if rrs:
            print(f"  RMSSD (approx HRV): {P.rmssd(rrs)} ms")
    return 0


def cmd_ble_probe(args: argparse.Namespace) -> int:
    from .ble import stream

    config, store = _config_and_store()
    address = args.address or config.ble_address
    if not address:
        raise SystemExit("No address. Pass --address or set WHOOP_BLE_ADDRESS.")
    with store:
        count = asyncio.run(
            stream.probe_proprietary(
                address,
                seconds=args.seconds,
                store=store,
                device_id=config.device_id or address,
                handshake=not args.no_handshake,
            )
        )
    print(f"\nCaptured {count} proprietary packets.")
    if count == 0:
        print(
            "No packets. On macOS the fd4b channel needs a bonded/encrypted link;\n"
            "try Linux (BlueZ) or the iOS path in ryanbr/noop for raw capture."
        )
    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Decode space/hex-packed frames, or rebuild one to sanity-check."""
    if args.build:
        pt, seq, cmd = args.build
        frame = P.build_frame(int(pt, 0), int(seq, 0), int(cmd, 0))
        print(f"built: {frame.hex()}")
        return 0
    for token in args.hex:
        buf = bytes.fromhex(token.replace(" ", ""))
        frame = P.parse_frame(buf)
        if frame is None:
            print(f"{token}: not a valid frame")
            continue
        print(
            json.dumps(
                {
                    "name": frame.name,
                    "valid_crc": frame.valid_crc,
                    "packet_type": hex(frame.packet_type),
                    "sequence": frame.sequence,
                    "command": hex(frame.command),
                    "data": frame.data.hex(),
                }
            )
        )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config, store = _config_and_store()
    with store:
        print(f"db: {store.path}")
        for table, n in store.stats().items():
            print(f"  {table:14s} {n}")
    return 0


# -------------------------------- parser -----------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="whoop", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth-url", help="print the OAuth authorization URL")
    p.add_argument("--state", default="whoop-open-source")
    p.set_defaults(func=cmd_auth_url)

    p = sub.add_parser("auth-exchange", help="exchange an OAuth code for tokens")
    p.add_argument("code")
    p.set_defaults(func=cmd_auth_exchange)

    p = sub.add_parser("sync", help="pull official API data into the store")
    p.add_argument("--start", help="ISO datetime lower bound")
    p.add_argument("--end", help="ISO datetime upper bound")
    p.add_argument("--resources", help="comma list: cycles,recoveries,sleeps,workouts")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ble-scan", help="scan for WHOOP straps over BLE")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--broad", action="store_true", help="show all BLE devices")
    p.set_defaults(func=cmd_ble_scan)

    p = sub.add_parser("ble-hr", help="stream live heart rate + R-R")
    p.add_argument("--address")
    p.add_argument("--seconds", type=float, default=60.0)
    p.set_defaults(func=cmd_ble_hr)

    p = sub.add_parser("ble-probe", help="capture proprietary fd4b packets")
    p.add_argument("--address")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--no-handshake", action="store_true")
    p.set_defaults(func=cmd_ble_probe)

    p = sub.add_parser("frames", help="decode or build protocol frames")
    p.add_argument("hex", nargs="*", help="hex frame(s) to decode")
    p.add_argument(
        "--build",
        nargs=3,
        metavar=("PACKET_TYPE", "SEQ", "CMD"),
        help="build a frame from components (decimal or 0x-hex)",
    )
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("stats", help="show row counts in the local store")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
