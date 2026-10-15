"""Dual-source tests: BLE idempotency, coverage, reconciliation, concurrency."""

from __future__ import annotations

import asyncio
import time
import types

from whoop import sync as sync_mod
from whoop.store import Store


def test_api_sync_module_is_reachable_not_shadowed():
    # Regression: whoop/api/__init__.py once bound the *function* `sync`, making
    # `whoop.api.sync` un-importable as a module and silently breaking `whoop sync`.
    import whoop.api as api

    assert isinstance(api.sync, types.ModuleType)
    assert callable(api.sync.sync)
    assert callable(api.sync_records)


def _store(tmp_path) -> Store:
    return Store(tmp_path / "sync.sqlite")


# ------------------------------- idempotency --------------------------------
def test_hr_samples_are_idempotent_across_resyncs(tmp_path):
    s = _store(tmp_path)
    sample = {
        "device_id": "dev",
        "ts": "2026-01-01T00:00:00.000+00:00",
        "bpm": 60,
        "rr_ms": [1000.0],
    }
    s.add_hr_samples([sample])
    s.add_hr_samples([sample])  # a re-sync must not double-count
    assert s.stats()["hr_samples"] == 1
    s.close()


def test_timestamps_normalize_across_sources(tmp_path):
    s = _store(tmp_path)
    # BLE writes near-UTC ("+00:00"); the API window uses "Z".
    s.add_hr_samples([{"device_id": "d", "ts": "2026-01-01T00:00:00.000+00:00", "bpm": 60}])
    row = s.query_one("SELECT ts FROM hr_samples")
    assert row["ts"] == "2026-01-01T00:00:00.000Z"
    s.close()


# --------------------------------- coverage ---------------------------------
def test_coverage_reports_both_sources(tmp_path):
    s = _store(tmp_path)
    s.upsert_cycles(
        [
            {
                "id": 1,
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2026-01-02T00:00:00.000Z",
                "score": {"average_heart_rate": 55},
            }
        ]
    )
    s.add_hr_samples([{"device_id": "d", "ts": "2026-01-01T12:00:00.000Z", "bpm": 60}])
    cov = s.coverage()
    assert cov["api"]["rows"] == 1
    assert cov["api"]["start"] == "2026-01-01T00:00:00.000Z"
    assert cov["api"]["end"] == "2026-01-02T00:00:00.000Z"
    assert cov["ble_hr"]["rows"] == 1
    assert cov["ble_hr"]["start"] == "2026-01-01T12:00:00.000Z"
    s.close()


# ------------------------------ reconciliation ------------------------------
def test_reconcile_matches_ble_against_api(tmp_path):
    s = _store(tmp_path)
    s.upsert_cycles(
        [
            {
                "id": 1,
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2026-01-01T01:00:00.000Z",
                "score": {"average_heart_rate": 60, "max_heart_rate": 120},
            }
        ]
    )
    s.add_hr_samples(
        [
            {"device_id": "d", "ts": "2026-01-01T00:10:00.000Z", "bpm": 50},
            {"device_id": "d", "ts": "2026-01-01T00:20:00.000Z", "bpm": 70},
        ]
    )
    rows = s.reconcile()
    assert len(rows) == 1
    r = rows[0]
    assert r["api_avg"] == 60
    assert r["ble_avg"] == 60.0
    assert r["ble_n"] == 2
    assert r["delta"] == 0.0
    s.close()


def test_reconcile_ignores_windows_outside_the_range(tmp_path):
    s = _store(tmp_path)
    s.upsert_cycles(
        [
            {
                "id": 1,
                "start": "2026-01-01T00:00:00.000Z",
                "end": "2026-01-01T01:00:00.000Z",
                "score": {"average_heart_rate": 60},
            }
        ]
    )
    assert s.reconcile(start="2026-02-01T00:00:00.000Z") == []
    s.close()


# ------------------------------- concurrency --------------------------------
def test_sync_all_runs_both_sources_concurrently(tmp_path):
    s = _store(tmp_path)
    order: list[str] = []

    async def fake_ble():
        order.append("ble-start")
        await asyncio.sleep(0.05)
        order.append("ble-end")
        s.add_hr_samples([{"device_id": "d", "ts": "2026-01-01T00:00:00.000Z", "bpm": 61}])
        return {"packets": 3, "family": "whoop5", "subscribed": 2}

    def fake_api():
        order.append("api-start")
        time.sleep(0.05)
        order.append("api-end")
        s.upsert_cycles(
            [
                {
                    "id": 1,
                    "start": "2026-01-01T00:00:00.000Z",
                    "end": "2026-01-01T01:00:00.000Z",
                    "score": {"average_heart_rate": 61},
                }
            ]
        )
        return {"cycles": 1}

    report = asyncio.run(sync_mod.sync_all(None, s, api_fn=fake_api, ble_fn=fake_ble))

    assert report.ok
    assert report.api == {"cycles": 1}
    assert report.ble["packets"] == 3
    # Both started before either finished -> they overlapped in time.
    first_end = min(order.index("api-end"), order.index("ble-end"))
    assert order.index("api-start") < first_end
    assert order.index("ble-start") < first_end
    # Both landed in the same store.
    assert s.stats()["cycles"] == 1
    assert s.stats()["hr_samples"] == 1
    assert report.coverage["api"]["rows"] == 1
    assert report.coverage["ble_hr"]["rows"] == 1
    s.close()


def test_sync_all_survives_one_source_failing(tmp_path):
    s = _store(tmp_path)

    async def bad_ble():
        raise RuntimeError("Encryption is insufficient")

    def good_api():
        return {"cycles": 2}

    report = asyncio.run(sync_mod.sync_all(None, s, api_fn=good_api, ble_fn=bad_ble))
    assert report.api == {"cycles": 2}
    assert report.ble is None
    assert "ble" in report.errors and "Encryption" in report.errors["ble"]
    assert not report.ok
    s.close()


def test_sync_all_reports_missing_ble_address(tmp_path):
    s = _store(tmp_path)
    report = asyncio.run(
        sync_mod.sync_all(None, s, api_fn=lambda: {"cycles": 0}, ble=True, address=None)
    )
    assert "ble" in report.errors
    assert report.api == {"cycles": 0}
    s.close()
