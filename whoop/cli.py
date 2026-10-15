"""Command-line interface.

The two sources get their own command groups and their own databases:

    whoop api   auth-url | auth-exchange | sync | stats | export
    whoop ble   scan | hr | probe | capture | stats | export
    whoop sync-all          # both at once, into their separate files
    whoop reconcile         # read-only join across both
    whoop web               # local dashboard
    whoop frames            # protocol encode/decode
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
from pathlib import Path

from .config import Config
from .store import ApiStore, BleStore, Store
from .ble import protocol as P

RETENTION_TEXT = """WHOOP on-device retention
  - The strap keeps up to ~14 days of data locally (WHOOP support docs).
    One community post claims 30 days; treat 14 as the safe figure.
  - It resumes syncing automatically when your phone is reachable; a full
    14-day catch-up can take up to ~1 hour.
  - The durable store is the type-47 historical record, served from a
    page-slot ring buffer (the bounds come back from GET_DATA_RANGE).
  - There is NO GPS on the strap: WHOOP 5/MG has no GNSS chip. Any
    distance/altitude in the official API is phone- or health-derived.
"""


def _config() -> Config:
    return Config.load()


def _api_store(config: Config) -> ApiStore:
    return ApiStore(config.api_db_path)


def _ble_store(config: Config) -> BleStore:
    return BleStore(config.ble_db_path)


# ============================= shared helpers ==============================
def _export(store: Store, *, table: str | None, fmt: str, dest: str | None) -> None:
    tables = [table] if table else list(store.TABLES)
    for name in tables:
        rows = store.export_rows(name)
        if dest:
            outdir = Path(dest)
            outdir.mkdir(parents=True, exist_ok=True)
            path = outdir / f"{store.KIND}_{name}.{'csv' if fmt == 'csv' else 'ndjson'}"
            buffer = io.StringIO() if fmt == "csv" else None
            if fmt == "csv":
                writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()) if rows else [])
                writer.writeheader()
                writer.writerows(rows)
                path.write_text(buffer.getvalue(), encoding="utf-8")
            else:
                with path.open("w", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            print(f"{name:18s} {len(rows):>6d} rows -> {path}")
            continue
        if fmt == "csv":
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        else:
            for row in rows:
                sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")


def _print_stats(title: str, store: Store) -> None:
    print(f"{title}  db={store.path}")
    for table, n in store.stats().items():
        print(f"  {table:18s} {n}")


# ================================= API =====================================
def cmd_api_auth_url(args: argparse.Namespace) -> int:
    from .api import oauth

    config = _config()
    config.require_api()
    print(oauth.authorization_url(config, state=args.state))
    print("\nOpen the URL above, approve access, then copy the `code` query")
    print("parameter from the redirect URL and run:")
    print("    whoop api auth-exchange '<code>'")
    return 0


def cmd_api_auth_exchange(args: argparse.Namespace) -> int:
    from .api import oauth

    config = _config()
    config.require_api()
    payload = oauth.exchange_code(config, args.code)
    with _api_store(config) as store:
        store.save_tokens("whoop", payload)
        print("Tokens stored.")
        print(f"  scope: {payload.get('scope')}")
        print(f"  db:    {store.path}")
    return 0


def cmd_api_sync(args: argparse.Namespace) -> int:
    from .api import sync as api_sync

    config = _config()
    resources = tuple(args.resources.split(",")) if args.resources else None
    with _api_store(config) as store:
        counts = api_sync.sync(config, store, start=args.start, end=args.end, resources=resources)
        print("Synced from official API:")
        for name, n in counts.items():
            print(f"  {name:12s} {n}")
    return 0


def cmd_api_stats(args: argparse.Namespace) -> int:
    config = _config()
    with _api_store(config) as store:
        _print_stats("api", store)
    return 0


def cmd_api_export(args: argparse.Namespace) -> int:
    config = _config()
    with _api_store(config) as store:
        _export(store, table=args.table, fmt=args.format, dest=args.dest)
    return 0


# ================================= BLE =====================================
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


def _ble_address(config: Config, args: argparse.Namespace) -> str:
    address = args.address or config.ble_address
    if not address:
        raise SystemExit("No address. Pass --address or set WHOOP_BLE_ADDRESS.")
    return address


def cmd_ble_hr(args: argparse.Namespace) -> int:
    from .ble import stream

    config = _config()
    address = _ble_address(config, args)
    with _ble_store(config) as store:
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

    config = _config()
    address = _ble_address(config, args)
    with _ble_store(config) as store:
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
    print(f"\nCaptured {count} vendor-channel packets.")
    if count == 0:
        print(
            "No packets. On macOS the vendor channel needs a bonded/encrypted link;\n"
            "try Linux (BlueZ) or the iOS path in ryanbr/noop for raw capture."
        )
    return 0


def cmd_ble_capture(args: argparse.Namespace) -> int:
    from .ble import stream

    config = _config()
    address = _ble_address(config, args)
    with _ble_store(config) as store:
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


def cmd_ble_stats(args: argparse.Namespace) -> int:
    config = _config()
    with _ble_store(config) as store:
        _print_stats("ble", store)
    return 0


def cmd_ble_export(args: argparse.Namespace) -> int:
    config = _config()
    with _ble_store(config) as store:
        _export(store, table=args.table, fmt=args.format, dest=args.dest)
    return 0


# ============================= combined ====================================
def cmd_sync_all(args: argparse.Namespace) -> int:
    from . import sync as sync_mod

    config = _config()
    address = args.address or config.ble_address
    resources = tuple(args.resources.split(",")) if args.resources else None
    with _api_store(config) as api_store, _ble_store(config) as ble_store:
        report = asyncio.run(
            sync_mod.sync_all(
                config,
                api_store,
                ble_store,
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
        print("== dual-source sync (concurrent, separate files) ==")
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
    from .reconcile import Combined

    config = _config()
    with Combined.open(config.api_db_path, config.ble_db_path, readonly=True) as both:
        print("coverage (read-only join):")
        _print_coverage(both.coverage())
        _print_reconciliation(both.reconcile(start=args.start, end=args.end))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web.server import serve

    config = _config()
    serve(
        host=args.host,
        port=args.port,
        sources=tuple(args.source),
        api_path=config.api_db_path,
        ble_path=config.ble_db_path,
        open_browser=args.open,
    )
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    print(RETENTION_TEXT, end="")
    return 0


def cmd_frames(args: argparse.Namespace) -> int:
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


# ================================ parser ===================================
def _add_export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--table", help="single table (default: all)")
    p.add_argument("--format", choices=["ndjson", "csv"], default="ndjson")
    p.add_argument("--dest", help="output directory (default: stdout)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whoop",
        description=(
            "Two-source WHOOP pipeline. The official API and the BLE strap each "
            "get their own command group and their own database."
        ),
        epilog=(
            "examples:\n"
            "  whoop api sync                 pull API metrics -> whoop_api.sqlite\n"
            "  whoop ble capture --imu        mine raw frames  -> whoop_ble.sqlite\n"
            "  whoop sync-all                 both at once, concurrently\n"
            "  whoop reconcile                read-only join across both\n"
            "  whoop web                      local dashboard\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    groups = parser.add_subparsers(dest="group", required=True)

    # ------------------------------- api -------------------------------
    api = groups.add_parser("api", help="official API source (separate database)")
    api_cmds = api.add_subparsers(dest="command", required=True)

    p = api_cmds.add_parser("auth-url", help="print the OAuth authorization URL")
    p.add_argument("--state", default="whoop-open-source")
    p.set_defaults(func=cmd_api_auth_url)

    p = api_cmds.add_parser("auth-exchange", help="exchange an OAuth code for tokens")
    p.add_argument("code")
    p.set_defaults(func=cmd_api_auth_exchange)

    p = api_cmds.add_parser("sync", help="pull API collections into the API database")
    p.add_argument("--start", help="ISO datetime lower bound")
    p.add_argument("--end", help="ISO datetime upper bound")
    p.add_argument("--resources", help="comma list: cycles,recoveries,sleeps,workouts")
    p.set_defaults(func=cmd_api_sync)

    p = api_cmds.add_parser("stats", help="row counts in the API database")
    p.set_defaults(func=cmd_api_stats)

    p = api_cmds.add_parser("export", help="dump the API database")
    _add_export_args(p)
    p.set_defaults(func=cmd_api_export)

    # ------------------------------- ble -------------------------------
    ble = groups.add_parser("ble", help="strap source (separate database)")
    ble_cmds = ble.add_subparsers(dest="command", required=True)

    p = ble_cmds.add_parser("scan", help="scan for WHOOP straps over BLE")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--broad", action="store_true", help="show all BLE devices")
    p.set_defaults(func=cmd_ble_scan)

    p = ble_cmds.add_parser("hr", help="stream live heart rate + R-R")
    p.add_argument("--address")
    p.add_argument("--seconds", type=float, default=60.0)
    p.set_defaults(func=cmd_ble_hr)

    p = ble_cmds.add_parser("probe", help="capture vendor-channel packets")
    p.add_argument("--address")
    p.add_argument("--family", choices=["whoop4", "whoop5"], default="whoop5")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--no-handshake", action="store_true")
    p.add_argument("--allow-side-effect", action="store_true")
    p.set_defaults(func=cmd_ble_probe)

    p = ble_cmds.add_parser(
        "capture", help="enumerate full GATT and log every raw frame (with char_uuid)"
    )
    p.add_argument("--address")
    p.add_argument("--family", choices=["whoop4", "whoop5"])
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--imu", action="store_true", help="arm the 100 Hz raw IMU producer")
    p.add_argument("--note", help="free-form label stored with the capture session")
    p.add_argument("--quiet", action="store_true", help="do not print each frame")
    p.set_defaults(func=cmd_ble_capture)

    p = ble_cmds.add_parser("stats", help="row counts in the BLE database")
    p.set_defaults(func=cmd_ble_stats)

    p = ble_cmds.add_parser("export", help="dump the BLE database")
    _add_export_args(p)
    p.set_defaults(func=cmd_ble_export)

    # ----------------------------- combined ----------------------------
    p = groups.add_parser(
        "sync-all", help="pull the API and mine the strap concurrently, then reconcile"
    )
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--resources")
    p.add_argument("--address")
    p.add_argument("--family", choices=["whoop4", "whoop5"])
    p.add_argument("--seconds", type=float, default=60.0, help="BLE capture duration")
    p.add_argument("--imu", action="store_true")
    p.add_argument("--no-api", action="store_true", help="skip the API half")
    p.add_argument("--no-ble", action="store_true", help="skip the BLE half")
    p.set_defaults(func=cmd_sync_all)

    p = groups.add_parser("reconcile", help="read-only join across both databases")
    p.add_argument("--start")
    p.add_argument("--end")
    p.set_defaults(func=cmd_reconcile)

    p = groups.add_parser("web", help="serve the local dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--source",
        action="append",
        choices=["api", "ble"],
        default=[],
        help="which source(s) to expose (repeatable; default: both)",
    )
    p.add_argument("--open", action="store_true", help="open a browser")
    p.set_defaults(func=cmd_web)

    p = groups.add_parser("retention", help="explain WHOOP on-device data retention")
    p.set_defaults(func=cmd_retention)

    p = groups.add_parser("frames", help="decode or build protocol frames")
    p.add_argument("hex", nargs="*", help="hex frame(s) to decode")
    p.add_argument("--family", choices=["whoop4", "whoop5"])
    p.add_argument(
        "--build",
        nargs=3,
        metavar=("PACKET_TYPE", "SEQ", "CMD"),
        help="build a frame from components (decimal or 0x-hex)",
    )
    p.add_argument("--body", help="hex body appended when building")
    p.set_defaults(func=cmd_frames, family=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
