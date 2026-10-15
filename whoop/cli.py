"""Command-line interface: ``python -m whoop.cli <command>`` or ``whoop <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

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


def cmd_sync_all(args: argparse.Namespace) -> int:
    """Pull the API and mine the strap at the same time, then reconcile."""
    from . import sync as sync_mod

    config, store = _config_and_store()
    address = args.address or config.ble_address
    resources = tuple(args.resources.split(",")) if args.resources else None
    with store:
        report = asyncio.run(
            sync_mod.sync_all(
                config,
                store,
                start=args.start,
                end=args.end,
                resources=resources,
                api=not args.no_api,
                ble=not args.no_ble,
                address=address,
                family=args.family,
                seconds=args.seconds,
                imu=args.imu,
            )
        )
        print("== dual-source sync (concurrent) ==")
        if report.api is not None:
            for name, n in report.api.items():
                print(f"  api.{name:14s} {n}")
        if report.ble is not None:
            print(
                f"  ble.packets      {report.ble.get('packets')} "
                f"(family={report.ble.get('family')}, "
                f"{report.ble.get('subscribed')} notify chars)"
            )
        for name, err in report.errors.items():
            print(f"  !! {name}: {err}")
        print("coverage:")
        _print_coverage(report.coverage)
        _print_reconciliation(report.reconciliation)
    return 0 if report.ok else 1


def _print_coverage(coverage: dict[str, dict]) -> None:
    for source, c in coverage.items():
        window = f"{c.get('start') or '-':32s} .. {c.get('end') or '-'}"
        print(f"  {source:8s} rows={c.get('rows', 0):<6d} {window}")


def _print_reconciliation(rows: list[dict]) -> None:
    if not rows:
        print("reconciliation: no overlapping API windows yet (sync both sides first)")
        return
    print("reconciliation (API average HR vs BLE samples in the same window):")
    for r in rows:
        delta = r["delta"]
        flag = "  <-- check" if delta is not None and abs(delta) >= 10 else ""
        print(
            f"  {r['kind']:8s} {str(r['id'])[:14]:14s} "
            f"api_avg={r['api_avg']} ble_avg={r['ble_avg']} "
            f"ble_n={r['ble_n']} delta={delta}{flag}"
        )


def cmd_reconcile(args: argparse.Namespace) -> int:
    config, store = _config_and_store()
    with store:
        print("coverage:")
        _print_coverage(store.coverage())
        _print_reconciliation(store.reconcile(start=args.start, end=args.end))
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
                family=args.family,
                seconds=args.seconds,
                store=store,
                device_id=config.device_id or address,
                handshake=not args.no_handshake,
                allow_side_effect=args.allow_side_effect,
            )
        )
    print(f"\nCaptured {count} proprietary packets.")
    if count == 0:
        print(
            "No packets. On macOS the vendor channel needs a bonded/encrypted link;\n"
            "try Linux (BlueZ) or the iOS path in ryanbr/noop for raw capture."
        )
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Enumerate full GATT, subscribe every notify char, log raw frames."""
    from .ble import stream

    config, store = _config_and_store()
    address = args.address or config.ble_address
    if not address:
        raise SystemExit("No address. Pass --address or set WHOOP_BLE_ADDRESS.")
    with store:
        summary = asyncio.run(
            stream.capture(
                address,
                family=args.family,
                seconds=args.seconds,
                store=store,
                device_id=config.device_id or address,
                imu=args.imu,
                note=args.note,
                quiet=args.quiet,
            )
        )
    print(
        f"\nCaptured {summary['packets']} raw frames "
        f"(family={summary['family']}, {summary['subscribed']} notifiable characteristics)"
    )
    print(f"  session_id={summary['session_id']}  db={store.path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Dump every stored row as NDJSON or CSV, so nothing stays locked in."""
    import csv
    import io

    config, store = _config_and_store()
    tables = [args.table] if args.table else list(Store.TABLES)
    fmt = args.format
    with store:
        for table in tables:
            rows = store.export_rows(table)
            if args.dest:
                path = Path(args.dest) / f"{table}.{ 'csv' if fmt == 'csv' else 'ndjson'}"
                out = path.open("w", encoding="utf-8")
                close = True
            else:
                out = io.StringIO() if fmt == "csv" else sys.stdout
                close = fmt == "csv"
            try:
                if fmt == "csv":
                    fieldnames = list(rows[0].keys()) if rows else []
                    writer = csv.DictWriter(out, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                else:
                    for row in rows:
                        out.write(json.dumps(row, separators=(",", ":")) + "\n")
            finally:
                if close:
                    out.close()
            if args.dest:
                print(f"{table:18s} {len(rows):>6d} rows -> {path}")
            elif fmt == "csv":
                sys.stdout.write(out.getvalue())
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    print(
        "WHOOP on-device retention\n"
        "  - The strap keeps up to ~14 days of data locally (WHOOP support docs).\n"
        "    One community post claims 30 days; treat 14 as the safe figure.\n"
        "  - It resumes syncing automatically when your phone is reachable; a full\n"
        "    14-day catch-up can take up to ~1 hour.\n"
        "  - The durable store is the type-47 historical record, served from a\n"
        "    page-slot ring buffer (the bounds come back from GET_DATA_RANGE).\n"
        "  - There is NO GPS on the strap: WHOOP 5/MG has no GNSS chip. Any\n"
        "    distance/altitude in the official API is phone- or health-derived.\n"
    )
    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Decode space/hex-packed frames (auto-detecting family), or rebuild one."""
    if args.build:
        pt, seq, cmd = args.build
        family = args.family or "whoop5"
        body = bytes.fromhex(args.body.replace(" ", "")) if args.body else b""
        frame = P.build_frame(family, int(pt, 0), int(seq, 0), int(cmd, 0), body)
        print(f"built ({family}): {frame.hex()}")
        return 0
    for token in args.hex:
        buf = bytes.fromhex(token.replace(" ", ""))
        frame = P.parse_frame(buf, family=args.family or None)
        if frame is None:
            print(f"{token}: not a valid frame")
            continue
        print(
            json.dumps(
                {
                    "family": frame.family.value,
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

    p = sub.add_parser(
        "sync-all", help="pull the API and mine the strap concurrently, then reconcile"
    )
    p.add_argument("--start", help="ISO datetime lower bound")
    p.add_argument("--end", help="ISO datetime upper bound")
    p.add_argument("--resources", help="comma list: cycles,recoveries,sleeps,workouts")
    p.add_argument("--address", help="BLE address (default: WHOOP_BLE_ADDRESS)")
    p.add_argument("--family", choices=["whoop4", "whoop5"])
    p.add_argument("--seconds", type=float, default=60.0, help="BLE capture duration")
    p.add_argument("--imu", action="store_true", help="also arm the raw IMU producer")
    p.add_argument("--no-api", action="store_true", help="skip the API half")
    p.add_argument("--no-ble", action="store_true", help="skip the BLE half")
    p.set_defaults(func=cmd_sync_all)

    p = sub.add_parser("reconcile", help="compare API metrics against BLE data")
    p.add_argument("--start", help="ISO datetime lower bound")
    p.add_argument("--end", help="ISO datetime upper bound")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("ble-scan", help="scan for WHOOP straps over BLE")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--broad", action="store_true", help="show all BLE devices")
    p.set_defaults(func=cmd_ble_scan)

    p = sub.add_parser("ble-hr", help="stream live heart rate + R-R")
    p.add_argument("--address")
    p.add_argument("--seconds", type=float, default=60.0)
    p.set_defaults(func=cmd_ble_hr)

    p = sub.add_parser("ble-probe", help="capture proprietary vendor-channel packets")
    p.add_argument("--address")
    p.add_argument("--family", choices=["whoop4", "whoop5"], default="whoop5")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--no-handshake", action="store_true")
    p.add_argument("--allow-side-effect", action="store_true")
    p.set_defaults(func=cmd_ble_probe)

    p = sub.add_parser(
        "capture", help="enumerate full GATT and log every raw frame (with char_uuid)"
    )
    p.add_argument("--address")
    p.add_argument("--family", choices=["whoop4", "whoop5"])
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--imu", action="store_true", help="arm the 100 Hz raw IMU producer")
    p.add_argument("--note", help="free-form label stored with the capture session")
    p.add_argument("--quiet", action="store_true", help="do not print each frame")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("export", help="dump every stored row as NDJSON or CSV")
    p.add_argument("--table", help="single table (default: all)")
    p.add_argument("--format", choices=["ndjson", "csv"], default="ndjson")
    p.add_argument("--dest", help="output directory (default: stdout)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("retention", help="explain WHOOP on-device data retention")
    p.set_defaults(func=cmd_retention)

    p = sub.add_parser("frames", help="decode or build protocol frames")
    p.add_argument("hex", nargs="*", help="hex frame(s) to decode")
    p.add_argument(
        "--family",
        choices=["whoop4", "whoop5"],
        help="force a family (decode auto-detects; build defaults to whoop5)",
    )
    p.add_argument(
        "--build",
        nargs=3,
        metavar=("PACKET_TYPE", "SEQ", "CMD"),
        help="build a frame from components (decimal or 0x-hex)",
    )
    p.add_argument("--body", help="hex body appended when building")
    p.set_defaults(func=cmd_frames, family=None)

    p = sub.add_parser("stats", help="show row counts in the local store")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
