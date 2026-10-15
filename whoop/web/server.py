"""Local dashboard: a stdlib HTTP server over the two separated databases.

No CDN, no build step, no dependencies -- `python -m whoop.web` or `whoop web`.

The API surface mirrors the storage split: every endpoint is namespaced by source
(``/api/api/...`` or ``/api/ble/...``), so the UI can render the two datasets in
fully separate panels. The only cross-source endpoint is ``/api/reconcile`` (and
``/api/coverage``), which are read-only joins.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..ble import protocol as P
from ..reconcile import Combined
from ..store import ApiStore, BleStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
JSON_CT = "application/json; charset=utf-8"


class Dashboard:
    """Owns the connections and builds every JSON payload."""

    def __init__(self, *, api_path, ble_path, sources) -> None:
        wanted = set(sources) or {"api", "ble"}
        self.sources = wanted
        self.api = ApiStore(api_path) if "api" in wanted else None
        self.ble = BleStore(ble_path) if "ble" in wanted else None

    def close(self) -> None:
        for store in (self.api, self.ble):
            if store is not None:
                store.close()

    # ------------------------------- payloads ----------------------------
    def health(self) -> dict:
        return {
            "ok": True,
            "sources": sorted(self.sources),
            "databases": {
                "api": str(self.api.path) if self.api else None,
                "ble": str(self.ble.path) if self.ble else None,
            },
        }

    def _store(self, source: str):
        if source == "api":
            return self.api
        if source == "ble":
            return self.ble
        raise KeyError(source)

    def summary(self, source: str) -> dict:
        store = self._store(source)
        if store is None:
            raise KeyError(source)
        payload: dict = {
            "source": source,
            "kind": store.KIND,
            "db": str(store.path),
            "tables": list(store.TABLES),
            "stats": store.stats(),
        }
        if source == "api":
            tokens = store.load_tokens("whoop")
            payload["auth"] = {
                "authenticated": bool(tokens and tokens.get("access_token")),
                "scope": (tokens or {}).get("scope"),
                "expires_at": (tokens or {}).get("expires_at"),
                "expired": bool((tokens or {}).get("_expired")),
            }
        else:
            payload["captures"] = store.query(
                "SELECT id, family, address, started_at, ended_at, note, packet_count "
                "FROM capture_sessions ORDER BY id DESC LIMIT 20"
            )
        return payload

    def rows(self, source: str, table: str, limit: int, offset: int) -> dict:
        store = self._store(source)
        if store is None:
            raise KeyError(source)
        if table not in store.TABLES:
            raise ValueError(f"unknown table: {table}")
        order = "ORDER BY id DESC" if table != "capture_sessions" else "ORDER BY id DESC"
        total = store.count(table)
        data = store.query(
            f"SELECT * FROM {table} {order} LIMIT ? OFFSET ?", (int(limit), int(offset))
        )
        return {"source": source, "table": table, "total": total, "rows": data}

    def hr(self, start: str | None, end: str | None, max_points: int) -> dict:
        if self.ble is None:
            raise KeyError("ble")
        return {
            "source": "ble",
            "series": self.ble.hr_series(start=start, end=end, max_points=max_points),
            "summary": self.ble.hr_summary(start or "0000", end or "9999"),
        }

    def raw(self, limit: int) -> dict:
        if self.ble is None:
            raise KeyError("ble")
        return {"source": "ble", "rows": self.ble.recent_raw(limit)}

    def coverage(self) -> dict:
        return self._combined().coverage()

    def reconcile(self, start: str | None, end: str | None) -> dict:
        rows = self._combined().reconcile(start=start, end=end)
        return {"rows": rows, "flagged": sum(1 for r in rows if (r["delta"] or 0) and abs(r["delta"]) >= 10)}

    def _combined(self) -> Combined:
        if self.api is None or self.ble is None:
            raise KeyError("reconcile needs both sources")
        return Combined(self.api, self.ble)

    def frames(self, tokens: list[str]) -> dict:
        out = []
        for token in tokens:
            try:
                buf = bytes.fromhex(token.replace(" ", ""))
            except ValueError:
                out.append({"hex": token, "error": "not hex"})
                continue
            frame = P.parse_frame(buf, family=None)
            if frame is None:
                out.append({"hex": token, "error": "not a valid frame"})
            else:
                out.append(
                    {
                        "hex": token,
                        "family": frame.family.value,
                        "name": frame.name,
                        "valid_crc": frame.valid_crc,
                        "packet_type": hex(frame.packet_type),
                        "sequence": frame.sequence,
                        "command": hex(frame.command),
                        "data": frame.data.hex(),
                    }
                )
        return {"frames": out}


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "whoop-local/0.1"

        def log_message(self, *args) -> None:  # quiet by default
            pass

        # ------------------------------ helpers ------------------------------
        def _json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", JSON_CT)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status=status)

        def _file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._error(404, "not found")
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _export(self, source: str, params: dict) -> None:
            store = dash._store(source)
            if store is None:
                self._error(404, f"source not enabled: {source}")
                return
            table = (params.get("table") or [""])[0]
            fmt = (params.get("format") or ["ndjson"])[0]
            if table not in store.TABLES:
                self._error(400, f"unknown table: {table}")
                return
            rows = store.export_rows(table)
            if fmt == "csv":
                buffer = io.StringIO()
                writer = csv.DictWriter(
                    buffer, fieldnames=list(rows[0].keys()) if rows else []
                )
                writer.writeheader()
                writer.writerows(rows)
                body = buffer.getvalue().encode()
                ctype = "text/csv; charset=utf-8"
                name = f"{source}_{table}.csv"
            else:
                body = ("\n".join(json.dumps(r) for r in rows)).encode()
                ctype = "application/x-ndjson; charset=utf-8"
                name = f"{source}_{table}.ndjson"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -------------------------------- GET --------------------------------
        def do_GET(self) -> None:  # noqa: N802
            split = urlsplit(self.path)
            path = split.path
            params = parse_qs(split.query)
            one = lambda k, d=None: (params.get(k) or [d])[0]  # noqa: E731

            try:
                if path in ("/", "/index.html"):
                    return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    name = path[len("/static/") :]
                    if "/" in name or ".." in name:
                        return self._error(400, "bad path")
                    ctype = "text/css" if name.endswith(".css") else "application/javascript"
                    return self._file(STATIC_DIR / name, ctype)
                if not path.startswith("/api/"):
                    return self._error(404, "not found")

                parts = path[len("/api/") :].strip("/").split("/")

                if parts == ["health"]:
                    return self._json(dash.health())
                if parts == ["coverage"]:
                    return self._json(dash.coverage())
                if parts == ["reconcile"]:
                    return self._json(dash.reconcile(one("start"), one("end")))
                if parts == ["frames"]:
                    tokens = [t for t in (one("hex") or "").split(",") if t.strip()]
                    return self._json(dash.frames(tokens))

                if len(parts) >= 2 and parts[0] in ("api", "ble"):
                    source, rest = parts[0], parts[1:]
                    if rest == ["summary"]:
                        return self._json(dash.summary(source))
                    if rest == ["rows"]:
                        return self._json(
                            dash.rows(
                                source,
                                one("table") or "",
                                int(one("limit", 100)),
                                int(one("offset", 0)),
                            )
                        )
                    if rest == ["export"]:
                        return self._export(source, params)
                    if source == "ble" and rest == ["hr"]:
                        return self._json(
                            dash.hr(one("start"), one("end"), int(one("max_points", 900)))
                        )
                    if source == "ble" and rest == ["raw"]:
                        return self._json(dash.raw(int(one("limit", 100))))

                return self._error(404, "not found")
            except KeyError as exc:
                return self._error(404, f"not available: {exc}")
            except ValueError as exc:
                return self._error(400, str(exc))
            except Exception as exc:  # noqa: BLE001 - surface, never crash the server
                return self._error(500, f"{type(exc).__name__}: {exc}")

    return Handler


def build_server(*, host, port, sources, api_path, ble_path) -> tuple[ThreadingHTTPServer, Dashboard]:
    dash = Dashboard(api_path=api_path, ble_path=ble_path, sources=sources)
    httpd = ThreadingHTTPServer((host, port), _make_handler(dash))
    httpd.daemon_threads = True
    return httpd, dash


def serve(*, host="127.0.0.1", port=8765, sources=(), api_path, ble_path, open_browser=False) -> None:
    httpd, dash = build_server(
        host=host, port=port, sources=sources, api_path=api_path, ble_path=ble_path
    )
    url = f"http://{host}:{httpd.server_address[1]}/"
    sources_label = ", ".join(sorted(dash.sources))
    print(f"WHOOP local dashboard -> {url}")
    print(f"  sources: {sources_label}")
    if dash.api:
        print(f"  api db:  {dash.api.path}")
    if dash.ble:
        print(f"  ble db:  {dash.ble.path}")
    print("  Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        dash.close()
