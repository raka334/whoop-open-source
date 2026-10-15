"""The local dashboard: stdlib HTTP server over the two separated databases."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from whoop.store import ApiStore, BleStore
from whoop.web.server import build_server


@pytest.fixture()
def server(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    with ApiStore(api_path) as api:
        api.upsert_cycles(
            [
                {
                    "id": 1,
                    "start": "2026-01-01T00:00:00.000Z",
                    "end": "2026-01-01T01:00:00.000Z",
                    "score": {"average_heart_rate": 60},
                }
            ]
        )
    with BleStore(ble_path) as ble:
        ble.add_hr_samples(
            [
                {"device_id": "d", "ts": "2026-01-01T00:10:00.000Z", "bpm": 50},
                {"device_id": "d", "ts": "2026-01-01T00:20:00.000Z", "bpm": 70},
            ]
        )
        ble.add_raw_packet(
            {"device_id": "d", "ts": "2026-01-01T00:10:00.000Z", "hex": "aabb", "channel": "data"}
        )

    httpd, dash = build_server(
        host="127.0.0.1", port=0, sources=(), api_path=api_path, ble_path=ble_path
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(base: str, path: str):
    status, body = _get(base, path)
    assert status == 200, body
    return json.loads(body)


def test_health_names_the_two_databases(server):
    health = _json(server, "/api/health")
    assert health["ok"] is True
    assert health["sources"] == ["api", "ble"]
    assert health["databases"]["api"].endswith("api.sqlite")
    assert health["databases"]["ble"].endswith("ble.sqlite")


def test_index_is_served_and_keeps_the_panels_apart(server):
    status, body = _get(server, "/")
    assert status == 200
    html = body.decode()
    assert "API source" in html and "BLE source" in html
    # Fully offline: no external asset, no CDN.
    assert "https://" not in html and "cdn" not in html.lower()


def test_api_summary_and_rows_stay_in_the_api_db(server):
    summary = _json(server, "/api/api/summary")
    assert summary["source"] == "api"
    assert summary["stats"]["cycles"] == 1
    assert "hr_samples" not in summary["stats"]
    assert summary["auth"]["authenticated"] is False

    rows = _json(server, "/api/api/rows?table=cycles&limit=10")
    assert rows["total"] == 1
    assert rows["rows"][0]["id"] == 1


def test_ble_summary_rows_hr_and_raw(server):
    summary = _json(server, "/api/ble/summary")
    assert summary["stats"]["hr_samples"] == 2
    assert "cycles" not in summary["stats"]

    rows = _json(server, "/api/ble/rows?table=hr_samples&limit=10")
    assert rows["total"] == 2

    hr = _json(server, "/api/ble/hr?max_points=100")
    assert len(hr["series"]) == 2
    assert hr["summary"]["n"] == 2

    raw = _json(server, "/api/ble/raw?limit=10")
    assert raw["rows"][0]["hex"] == "aabb"


def test_cross_source_endpoints_are_read_only_joins(server):
    cov = _json(server, "/api/coverage")
    assert cov["api"]["rows"] == 1 and cov["ble_hr"]["rows"] == 2

    rec = _json(server, "/api/reconcile")
    assert len(rec["rows"]) == 1
    assert rec["rows"][0]["api_avg"] == 60
    assert rec["rows"][0]["ble_avg"] == 60.0

    # And they never wrote anything.
    assert _json(server, "/api/ble/summary")["stats"]["hr_samples"] == 2


def test_unknown_table_is_a_400_not_a_crash(server):
    status, body = _get(server, "/api/api/rows?table=hr_samples&limit=10")
    assert status == 400
    assert "unknown table" in json.loads(body)["error"]


def test_frame_decoder_endpoint(server):
    # The real WHOOP 5 CLIENT_HELLO must decode with a valid CRC16-Modbus.
    out = _json(server, "/api/frames?hex=aa0108000001e67123019101363e5c8d")
    frame = out["frames"][0]
    assert frame["family"] == "whoop5"
    assert frame["valid_crc"] is True
    assert frame["command"] == "0x91"


def test_export_sets_a_download_filename(server):
    with urllib.request.urlopen(f"{server}/api/api/export?table=cycles&format=csv", timeout=5) as r:
        assert r.status == 200
        assert "attachment" in (r.headers.get("Content-Disposition") or "")
        assert b"id" in r.read()


def test_only_enabled_sources_are_exposed(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    ApiStore(api_path).close()
    BleStore(ble_path).close()
    httpd, dash = build_server(
        host="127.0.0.1", port=0, sources=("api",), api_path=api_path, ble_path=ble_path
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _json(base, "/api/health")["sources"] == ["api"]
        status, _ = _get(base, "/api/ble/summary")
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        dash.close()
        thread.join(timeout=5)
