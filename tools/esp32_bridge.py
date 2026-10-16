#!/usr/bin/env python3
"""ESP32 WHOOP bridge — serial stream -> whoop_ble.sqlite.

Reads the NOTIFY lines printed by the whoop_central ESP32-S3 firmware,
reassembles fragmented WHOOP-5 frames, decodes them with the repo's own
protocol/sensor modules, and persists:

* R21 realtime IMU bursts -> ``raw_packets`` (+ decoded_json)
* PT_DATA (0x28) live HR  -> ``hr_samples``
* everything else         -> ``raw_packets`` (raw hex)

Usage:
    python3 tools/esp32_bridge.py [--port /dev/ttyACM0] [--seconds 60]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whoop.ble import protocol as P
from whoop.ble import sensors as S
from whoop.store import BleStore

NOTIFY_RE = re.compile(r"NOTIFY attr=([0-9a-f]{4}) \[(\d+)\]: ([0-9a-f ]*)")
DEVICE = "FD:17:43:A4:4D:82"


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


class FrameAssembler:
    """Accumulate notification fragments into complete WHOOP-5 frames."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames = []
        while True:
            # find a frame start
            i = self.buf.find(b"\xaa\x01")
            if i < 0:
                self.buf.clear()
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < 4:
                break
            declared = int.from_bytes(self.buf[2:4], "little")
            total = declared + 8
            if total < 12 or total > 4096:
                self.buf = self.buf[1:]  # false start
                continue
            if len(self.buf) < total:
                break
            frames.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return frames


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--drain", action="store_true",
                    help="run the full history drain loop (0x16/0x17 commit protocol)")
    ap.add_argument("--db", default=None, help="path to whoop_ble.sqlite")
    args = ap.parse_args()

    import serial  # pyserial

    db_path = args.db or str(Path(__file__).resolve().parent.parent / "data" / "whoop_ble.sqlite")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = BleStore(db_path)

    store.execute(
        "INSERT INTO capture_sessions (device_id, family, address, started_at, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (DEVICE, "whoop5", DEVICE, utcnow_iso(), "esp32-s3 bridge"),
    )
    session_id = store.query_one("SELECT last_insert_rowid() AS id")["id"]

    ser = serial.Serial(args.port, 115200, timeout=1)
    import time as _t
    ser.setDTR(False); ser.setRTS(True); _t.sleep(0.1); ser.setRTS(False)
    print("[bridge] ESP32 reset — full command batch will fire under capture", flush=True)
    asm = FrameAssembler()
    counts = {"frames": 0, "imu": 0, "hr": 0, "other": 0, "bytes": 0}
    t0 = time.time()
    last_stat = t0

    gw_seq = [57]
    def gw_write(cmd: int, body: bytes = b"") -> None:
        f = P.build_frame(family="whoop5", packet_type=0x23, seq=gw_seq[0], cmd=cmd, body=body)
        gw_seq[0] = (gw_seq[0] + 1) % 256
        ser.write(b"W " + bytes(f).hex().encode() + b"\n")
        ser.flush()

    drain_state = {"active": args.drain, "phase": "wait_link", "cycle": 0,
                   "deadline": 0.0, "kinds": set(), "records": 0, "acked": False,
                   "empty_cycles": 0}
    if args.drain:
        print("[drain] will start once the link is up", flush=True)

    print(f"[bridge] port={args.port} db={db_path} session=#{session_id}", flush=True)
    try:
        while time.time() - t0 < args.seconds:
            line = ser.readline().decode(errors="replace").strip()
            if any(k in line for k in ("CCCD", "FOUND", "subscribing", "sending",
                                       "CMD write", "LINK ENCRYPTED", "PARING")):
                print("[fw] " + line[:120], flush=True)
            m = NOTIFY_RE.search(line)
            if not m:
                continue
            try:
                chunk = bytes.fromhex(m.group(3).replace(" ", ""))
            except ValueError:
                continue  # torn/garbled serial line — skip it
            counts["bytes"] += len(chunk)
            # standard HR characteristic (0x2a37): raw flags+bpm+rr format,
            # not aa-framed — parse directly
            if m.group(1) not in ("09a3", "099d", "09a0", "09a6") and len(chunk) >= 2:
                try:
                    flags = chunk[0]
                    bpm = int.from_bytes(chunk[1:3], "little") if flags & 1 else chunk[1]
                    i = 3 if flags & 1 else 2
                    rr = []
                    if flags & 0x10:
                        while i + 1 < len(chunk):
                            rr.append(round(int.from_bytes(chunk[i:i+2], "little") / 1024.0 * 1000))
                            i += 2
                    if 30 <= bpm <= 250:
                        ts_h = utcnow_iso()
                        counts["hr"] += 1
                        store.add_hr_samples([{"device_id": DEVICE, "ts": ts_h,
                                               "bpm": bpm, "rr_ms": rr, "source": "esp32"}])
                        store.execute(
                            "INSERT INTO raw_packets (device_id, ts, channel, char_uuid, family, "
                            "packet_type, command, hex, decoded_json) VALUES (?,?,?,?,?,?,?,?,?)",
                            (DEVICE, ts_h, "data", "00002a37-0000-1000-8000-00805f9b34fb",
                             "whoop5", 0x28, None, chunk.hex(),
                             '{"kind":"std_hr","bpm":%d,"rr_n":%d}' % (bpm, len(rr))))
                        print(f"[{ts_h}] HR {bpm} bpm rr_n={len(rr)} (standard 2a37)", flush=True)
                except Exception:
                    pass
                continue
            for frame in asm.feed(chunk):
                counts["frames"] += 1
                ts = utcnow_iso()
                try:
                    parsed = P.parse_frame(frame, family="whoop5")
                    ptype, cmd = parsed.packet_type, parsed.command
                except Exception:
                    ptype, cmd = None, None

                decoded = None
                if ptype == 0x28:   # PT_DATA: the real live-HR frames
                    try:
                        hr = S.decode_live_hr(frame)
                    except Exception:
                        hr = None
                    if hr:
                        counts["hr"] += 1
                        store.add_hr_samples(
                            [{"device_id": DEVICE, "ts": ts, "bpm": hr.get("bpm"),
                              "rr_ms": hr.get("rr_ms"), "source": "esp32"}]
                        )
                        rr = hr.get("rr_ms") or []
                        print(f"[{ts}] HR {hr.get('bpm')} bpm rr_n={len(rr)}", flush=True)
                        decoded = {"kind": "live_hr", **hr}
                elif ptype == 0x2B or len(frame) == 1244 or S.is_r21_frame(frame):
                    burst = S.decode_imu_r21(frame)
                    if burst:
                        counts["imu"] += 1
                        decoded = {
                            "kind": "r21_imu",
                            "accel_g": burst.accel_g[:3],
                            "gyro_dps": burst.gyro_dps[:3],
                            "n_accel": burst.accel_count,
                            "n_gyro": burst.gyro_count,
                            "crc_ok": burst.crc_ok,
                        }
                        a = decoded["accel_g"][0] if decoded["accel_g"] else (0, 0, 0)
                        print(
                            f"[{ts}] R21 IMU n={decoded['n_accel']}/{decoded['n_gyro']} crc={burst.crc_ok} "
                            f"accel[0]=({a[0]:.3f},{a[1]:.3f},{a[2]:.3f})g",
                            flush=True,
                        )
                elif ptype in (0x31, 0x32, 0x30) and decoded is None:
                    import struct as _s2
                    d2 = {"kind": {0x31: "metadata", 0x32: "hist_record", 0x30: "event"}.get(ptype),
                          "len": len(frame)}
                    if len(frame) >= 14:
                        d2["epoch"] = _s2.unpack_from("<I", frame, 12)[0]
                        if 0x6ad00000 < d2["epoch"] < 0x6ae00000:
                            import datetime as _dt2
                            d2["time"] = _dt2.datetime.fromtimestamp(d2["epoch"], _dt2.timezone.utc).isoformat()
                    if ptype == 0x31:
                        d2["meta_kind"] = frame[10] if len(frame) > 10 else None
                    decoded = d2
                elif ptype == 0x2F and len(frame) == 124:
                    import struct as _s
                    counts["other"] += 1
                    try:
                        dec = {
                            "kind": "r18_hist",
                            "epoch": _s.unpack_from("<I", frame, 15)[0],
                            "f22": _s.unpack_from("<H", frame, 22)[0],
                            "f24": _s.unpack_from("<H", frame, 24)[0],
                            "f26": _s.unpack_from("<H", frame, 26)[0],
                            "f35": _s.unpack_from("<H", frame, 35)[0],
                            "hr_cand": _s.unpack_from("<H", frame, 37)[0],
                        }
                        decoded = dec
                        print(f"[{ts}] R18 hist epoch={dec['epoch']} hr?={dec['hr_cand']} "
                              f"f22={dec['f22']} f24={dec['f24']} f35={dec['f35']}", flush=True)
                    except Exception:
                        pass
                else:
                    counts["other"] += 1

                store.execute(
                    "INSERT INTO raw_packets (device_id, ts, channel, char_uuid, family, "
                    "packet_type, command, hex, decoded_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        DEVICE, ts, "data",
                        "fd4b0005-cce1-4033-93ce-002d5875f58a",
                        "whoop5", ptype, cmd, frame.hex(),
                        json.dumps(decoded) if decoded else None,
                    ),
                )
                if counts["frames"] % 10 == 0:
                    store.execute(
                        "UPDATE capture_sessions SET packet_count=?, ended_at=? WHERE id=?",
                        (counts["frames"], ts, session_id),
                    )

                # ---- history drain state machine ----
                if drain_state["active"]:
                    now = time.time()
                    if ptype in (0x2F, 0x32):
                        drain_state["records"] += 1
                    # metadata: 0x31 (kind = 1 byte @ frame[10]) or 0x38 (u16 @ [10:12])
                    kind = None
                    if ptype == 0x31 and len(frame) >= 11:
                        kind = frame[10]
                    elif ptype == 0x38 and len(frame) >= 12:
                        kind = int.from_bytes(frame[10:12], "little")
                    if kind is not None:
                        drain_state["kinds"].add(kind)
                        if kind == 2 and len(frame) >= 29 and not drain_state["acked"]:
                            ack = bytes([1]) + frame[21:29]
                            gw_write(0x17, ack)
                            drain_state["acked"] = True
                            print(f"[drain] cycle {drain_state['cycle']}: historyEnd -> ACK {ack.hex()}", flush=True)
                    if drain_state["phase"] == "wait_link" and (ptype in (0x2B, 0x2F, 0x28)):
                        drain_state["phase"] = "cycle"
                        drain_state["deadline"] = now + 3
                    elif drain_state["phase"] == "cycle" and now >= drain_state["deadline"]:
                        recs, kinds = drain_state["records"], drain_state["kinds"]
                        print(f"[drain] cycle {drain_state['cycle']}: records={recs} kinds={sorted(kinds)}", flush=True)
                        if 3 in kinds:
                            print("[drain] HISTORY COMPLETE — strap vault emptied", flush=True)
                            drain_state["active"] = False
                        elif recs == 0:
                            drain_state["empty_cycles"] += 1
                            if drain_state["empty_cycles"] >= 2:
                                print("[drain] no new records twice — stopping", flush=True)
                                drain_state["active"] = False
                        else:
                            drain_state["empty_cycles"] = 0
                        if drain_state["active"]:
                            drain_state["cycle"] += 1
                            drain_state["records"] = 0
                            drain_state["kinds"] = set()
                            drain_state["acked"] = False
                            drain_state["deadline"] = now + 12
                            # mirror goose: GET_DATA_RANGE immediately before SEND_HISTORICAL
                            gw_write(0x22)
                            time.sleep(1.0)
                            gw_write(0x16)
                            print(f"[drain] cycle {drain_state['cycle']}: 0x22 + 0x16 sent (seq {gw_seq[0]})", flush=True)
    finally:
        ts = utcnow_iso()
        store.execute(
            "UPDATE capture_sessions SET packet_count=?, ended_at=? WHERE id=?",
            (counts["frames"], ts, session_id),
        )
        store.close()
        ser.close()
        el = time.time() - t0
        print(
            f"\n[bridge] done in {el:.0f}s: frames={counts['frames']} imu={counts['imu']} "
            f"hr={counts['hr']} other={counts['other']} bytes={counts['bytes']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
