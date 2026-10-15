"""Dual-source sync: concurrency, error isolation, and the separate-database split."""

from __future__ import annotations

import asyncio
import time
import types

from whoop import sync as sync_mod
from whoop.store import ApiStore, BleStore


def test_api_sync_module_is_reachable_not_shadowed():
    # Regression: whoop/api/__init__.py once bound the *function* `sync`, making
    # `whoop.api.sync` un-importable as a module and silently breaking `whoop api sync`.
    import whoop.api as api

    assert isinstance(api.sync, types.ModuleType)
    assert callable(api.sync.sync)
    assert callable(api.sync_records)


def _stores(tmp_path) -> tuple[ApiStore, BleStore]:
    return ApiStore(tmp_path / "api.sqlite"), BleStore(tmp_path / "ble.sqlite")


def test_sync_all_writes_each_source_to_its_own_database(tmp_path):
    api, ble = _stores(tmp_path)
    order: list[str] = []

    async def fake_ble():
        order.append("ble-start")
        await asyncio.sleep(0.05)
        order.append("ble-end")
        ble.add_hr_samples([{"device_id": "d", "ts": "2026-01-01T00:00:00.000Z", "bpm": 61}])
        return {"packets": 3, "family": "whoop5", "subscribed": 2}

    def fake_api():
        order.append("api-start")
        time.sleep(0.05)
        order.append("api-end")
        api.upsert_cycles(
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

    report = asyncio.run(sync_mod.sync_all(None, api, ble, api_fn=fake_api, ble_fn=fake_ble))

    assert report.ok
    assert report.api == {"cycles": 1}
    assert report.ble["packets"] == 3
    # Both started before either finished -> they overlapped in time.
    first_end = min(order.index("api-end"), order.index("ble-end"))
    assert order.index("api-start") < first_end
    assert order.index("ble-start") < first_end
    # Each row landed in its own database, and only there.
    assert api.stats()["cycles"] == 1
    assert api.stats()["workouts"] == 0
    assert ble.stats()["hr_samples"] == 1
    assert ble.stats()["raw_packets"] == 0
    # The report joins them read-only, without merging the files.
    assert report.coverage["api"]["rows"] == 1
    assert report.coverage["ble_hr"]["rows"] == 1
    assert report.reconciliation[0]["ble_n"] == 1
    api.close()
    ble.close()


def test_sync_all_survives_one_source_failing(tmp_path):
    api, ble = _stores(tmp_path)

    async def bad_ble():
        raise RuntimeError("Encryption is insufficient")

    def good_api():
        return {"cycles": 2}

    report = asyncio.run(sync_mod.sync_all(None, api, ble, api_fn=good_api, ble_fn=bad_ble))
    assert report.api == {"cycles": 2}
    assert report.ble is None
    assert "ble" in report.errors and "Encryption" in report.errors["ble"]
    assert not report.ok
    # The API database is untouched by the BLE failure.
    assert api.stats()["cycles"] is not None
    assert ble.stats()["hr_samples"] == 0
    api.close()
    ble.close()


def test_sync_all_reports_missing_ble_address(tmp_path):
    api, ble = _stores(tmp_path)
    report = asyncio.run(
        sync_mod.sync_all(None, api, ble, api_fn=lambda: {"cycles": 0}, ble=True, address=None)
    )
    assert "ble" in report.errors
    assert report.api == {"cycles": 0}
    api.close()
    ble.close()
