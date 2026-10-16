#!/usr/bin/env python3
"""Live dashboard for the ESP32 WHOOP bridge — realtime view of hr_samples
and R21 IMU frames as the bridge writes them into whoop_ble.sqlite.

Run alongside tools/esp32_bridge.py:
    python3 tools/live_dashboard.py [--port 8790] [--db data/whoop_ble.sqlite]
Then open http://127.0.0.1:8790
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>WHOOP live</title>
<meta http-equiv="refresh" content="5">
<style>
body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:20px;font-size:14px}
h1{font-size:15px;color:#8b949e;font-weight:normal}
table{border-collapse:collapse;margin-top:10px}
td{border:1px solid #30363d;padding:8px 16px}
.k{color:#8b949e}
.big{font-size:42px;color:#3fb950}
pre{background:#161b22;border:1px solid #30363d;padding:10px;margin-top:14px;white-space:pre-wrap}
</style></head><body>
<h1><b>WHOOP 5.0</b> — ESP32 bridge · safe-mode view (auto-refresh 5s) · <a href='/dump' style='color:#58a6ff'>full data dump →</a></h1>
<table>
<tr><td class="k">HEART RATE</td><td><span class="big" id="bpm">--</span> bpm <span id="hage" class="k"></span></td></tr>
<tr><td class="k">ACCEL X</td><td id="ax">--</td></tr>
<tr><td class="k">ACCEL Y</td><td id="ay">--</td></tr>
<tr><td class="k">ACCEL Z</td><td id="az">--</td></tr>
<tr><td class="k">IMU age</td><td id="iage">--</td></tr>
<tr><td class="k">packets / imu frames</td><td id="counts">--</td></tr>
<tr><td class="k">bridge</td><td id="alive">--</td></tr>
<tr><td class="k">HISTORICAL R18</td><td id="hist">--</td></tr>
<tr><td class="k">hist HR range</td><td id="histr">--</td></tr>
</table>
<pre id="raw">waiting for first poll…</pre>
<script>
function upd(){
  var x = new XMLHttpRequest();
  x.open('GET', '/api/live');
  x.onload = function(){
    var d = JSON.parse(x.responseText);
    document.getElementById('raw').textContent = x.responseText;
    if(d.hr){ document.getElementById('bpm').textContent = d.hr.bpm;
              document.getElementById('hage').textContent = '(' + d.hr.age_s + 's ago)'; }
    if(d.imu){ document.getElementById('ax').textContent = d.imu.ax;
               document.getElementById('ay').textContent = d.imu.ay;
               document.getElementById('az').textContent = d.imu.az;
               document.getElementById('iage').textContent = d.imu.age_s + 's ago'; }
    if(d.stats){ document.getElementById('counts').textContent =
                 d.stats.packets + ' / ' + d.stats.imu_frames;
                 document.getElementById('alive').textContent =
                 d.stats.bridge_alive ? 'STREAMING' : 'idle'; }
    if(d.hist){ document.getElementById('hist').textContent =
                 d.hist.records + ' records ' + d.hist.from + '→' + d.hist.to +
                 '  last-HR? ' + d.hist.hr_last + '  ' + d.hist.spark;
                 document.getElementById('histr').textContent =
                 (d.hist.hr_min || '-') + ' .. ' + (d.hist.hr_max || '-'); }
  };
  x.onerror = function(){ document.getElementById('raw').textContent =
    'XHR ERROR — cannot reach 127.0.0.1:8790 — is the dashboard running?'; };
  x.send();
}
upd();
</script>
</body></html>"""




def dump_page(db_path: str) -> bytes:
    import datetime as _dt
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        stats = {r["t"]: r["c"] for r in conn.execute(
            "SELECT packet_type t, COUNT(*) c FROM raw_packets GROUP BY packet_type")}
        hr_n = conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"]
        sess = conn.execute("SELECT COUNT(*) c FROM capture_sessions").fetchone()["c"]
        rows_hist = conn.execute(
            "SELECT ts, decoded_json FROM raw_packets WHERE decoded_json LIKE '%r18_hist%' "
            "ORDER BY id DESC LIMIT 500").fetchall()
        seen, hist = set(), []
        for r in rows_hist:
            try:
                d = json.loads(r["decoded_json"])
            except Exception:
                continue
            e = d.get("epoch")
            if e in seen:
                continue
            seen.add(e)
            hist.append((e, d))
        hist.sort()
        packets = conn.execute(
            "SELECT ts, channel, packet_type, command, length(hex)/2 n, substr(hex,1,80) hx "
            "FROM raw_packets ORDER BY id DESC LIMIT 120").fetchall()
        hrs = conn.execute(
            "SELECT ts, bpm, length(rr_ms) rrn FROM hr_samples ORDER BY id DESC LIMIT 40").fetchall()
        type_names = {0x23: "CMD", 0x24: "RESP", 0x28: "DATA/HR", 0x2B: "REALTIME",
                      0x2F: "HISTORICAL", 0x38: "METADATA"}
        out = ["<!doctype html><html><head><meta charset='utf-8'>",
               "<meta http-equiv='refresh' content='10'>",
               "<title>WHOOP — full dump</title>",
               "<style>body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:16px;font-size:12px}",
               "h1{font-size:15px;color:#8b949e}h2{font-size:13px;color:#58a6ff;margin:18px 0 6px}",
               "table{border-collapse:collapse;margin-bottom:8px}td,th{border:1px solid #30363d;padding:3px 8px;text-align:left}",
               "th{color:#8b949e}.dim{color:#8b949e}a{color:#58a6ff}",
               ".hex{color:#8b949e;word-break:break-all;max-width:520px}</style></head><body>",
               "<h1><b>WHOOP 5.0 — whole data dump</b> · <a href='/'>← live view</a></h1>",
               f"<p class='dim'>packets by type: " +
               " ".join(f"{type_names.get(t, hex(t))}={c}" for t, c in sorted(stats.items())) +
               f" · HR rows={hr_n} · sessions={sess}</p>"]

        out.append(f"<h2>R18 historical records ({len(hist)} unique epochs)</h2>")
        out.append("<table><tr><th>time (UTC)</th><th>epoch</th><th>hr?</th><th>f22</th><th>f24</th><th>f26</th><th>f35</th></tr>")
        for e, d in hist:
            t = _dt.datetime.fromtimestamp(e, _dt.timezone.utc).strftime("%H:%M:%S")
            out.append(f"<tr><td>{t}</td><td class='dim'>{e}</td><td>{d.get('hr_cand')}</td>"
                       f"<td>{d.get('f22')}</td><td>{d.get('f24')}</td><td>{d.get('f26')}</td><td>{d.get('f35')}</td></tr>")
        out.append("</table>")

        out.append(f"<h2>HR samples ({len(hrs)} latest)</h2><table><tr><th>ts</th><th>bpm</th><th>rr bytes</th></tr>")
        for r in hrs:
            out.append(f"<tr><td>{r['ts'][11:19]}</td><td>{r['bpm']}</td><td class='dim'>{r['rrn']}</td></tr>")
        out.append("</table>")

        out.append(f"<h2>raw packets ({len(packets)} latest)</h2>")
        out.append("<table><tr><th>ts</th><th>ch</th><th>type</th><th>cmd</th><th>len</th><th>hex (first 40B)</th></tr>")
        for r in packets:
            tn = type_names.get(r["packet_type"], hex(r["packet_type"]) if r["packet_type"] else "std")
            cmd = f"0x{r['command']:02x}" if r["command"] is not None else "—"
            out.append(f"<tr><td>{r['ts'][11:19]}</td><td>{r['channel']}</td><td>{tn}</td>"
                       f"<td>{cmd}</td><td>{r['n']}</td><td class='hex'>{r['hx']}</td></tr>")
        out.append("</table></body></html>")
        return "".join(out).encode()
    finally:
        conn.close()


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                return
            if self.path.startswith("/dump"):
                try:
                    self._send(200, dump_page(db_path), "text/html; charset=utf-8")
                except Exception as e:
                    self._send(500, str(e).encode(), "text/plain")
                return
            if self.path.startswith("/api/live"):
                try:
                    payload = live_snapshot(db_path)
                except Exception as e:
                    payload = {"error": str(e)}
                self._send(200, json.dumps(payload).encode(), "application/json")
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def live_snapshot(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    out: dict = {"ts": datetime.now(timezone.utc).isoformat()}
    try:
        hr = conn.execute(
            "SELECT ts, bpm, rr_ms FROM hr_samples ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if hr:
            rr = json.loads(hr["rr_ms"]) if hr["rr_ms"] else []
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(hr["ts"].replace("Z", "+00:00"))).total_seconds()
            out["hr"] = {"bpm": hr["bpm"], "rr_n": len(rr), "age_s": round(age)}
        row = conn.execute(
            "SELECT ts, decoded_json FROM raw_packets "
            "WHERE decoded_json LIKE '%r21_imu%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            d = json.loads(row["decoded_json"])
            a = (d.get("accel_g") or [[0, 0, 0]])[0]
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))).total_seconds()
            out["imu"] = {"ax": a[0], "ay": a[1], "az": a[2],
                          "n": d.get("n_accel"), "age_s": round(age)}
        stats = {}
        stats["packets"] = conn.execute("SELECT COUNT(*) c FROM raw_packets").fetchone()["c"]
        stats["hr_rows"] = conn.execute("SELECT COUNT(*) c FROM hr_samples").fetchone()["c"]
        stats["imu_frames"] = conn.execute(
            "SELECT COUNT(*) c FROM raw_packets WHERE decoded_json LIKE '%r21_imu%'"
        ).fetchone()["c"]
        sess = conn.execute(
            "SELECT started_at, ended_at FROM capture_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if sess and sess["ended_at"]:
            span = (datetime.fromisoformat(sess["ended_at"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(sess["started_at"].replace("Z", "+00:00"))).total_seconds()
            stats["span_s"] = round(span)
        hist = conn.execute(
            "SELECT COUNT(*) c, MIN(decoded_json) mn, MAX(decoded_json) mx FROM raw_packets "
            "WHERE decoded_json LIKE '%r18_hist%'").fetchone()
        if hist and hist["c"]:
            import datetime as _dt
            def _ep(j):
                try: return json.loads(j)["epoch"]
                except Exception: return 0
            series = [json.loads(r["decoded_json"]) for r in conn.execute(
                "SELECT decoded_json FROM raw_packets WHERE decoded_json LIKE '%r18_hist%' ORDER BY id").fetchall()]
            series = sorted({s["epoch"]: s for s in series if s.get("epoch")}.values(), key=lambda s: s["epoch"])
            hrs = [s.get("hr_cand") for s in series if s.get("hr_cand")]
            blocks = " ▁▂▃▄▅▆▇█"
            spark_txt = "".join(blocks[min(8, int((v - min(hrs)) / max(1, max(hrs) - min(hrs)) * 8) + 1)] if hrs else " " for v in hrs[-60:])
            out["hist"] = {
                "records": len(series),
                "from": _dt.datetime.fromtimestamp(series[0]["epoch"], _dt.timezone.utc).isoformat()[11:19] if series else "",
                "to": _dt.datetime.fromtimestamp(series[-1]["epoch"], _dt.timezone.utc).isoformat()[11:19] if series else "",
                "hr_min": min(hrs) if hrs else None,
                "hr_max": max(hrs) if hrs else None,
                "hr_last": hrs[-1] if hrs else None,
                "spark": spark_txt,
            }
        last = conn.execute("SELECT ts FROM raw_packets ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            stats["last_packet"] = last["ts"][11:19]
            stats["bridge_alive"] = (datetime.now(timezone.utc)
                - datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))).total_seconds() < 10
        out["stats"] = stats
    finally:
        conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--db", default=str(ROOT / "data" / "whoop_ble.sqlite"))
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.db))
    print(f"[dashboard] http://127.0.0.1:{args.port}  (db: {args.db})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
