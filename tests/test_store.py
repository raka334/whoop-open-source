"""Separated-store round-trips.

The two sources must never share a file or a table set. These tests pin that
down: an ``ApiStore`` has no BLE tables, a ``BleStore`` has no API tables, and a
read-only store refuses every write.
"""

from __future__ import annotations

import pytest

from whoop.store import ApiStore, BleStore


def test_the_two_sources_use_separate_files_and_tables(tmp_path):
    api = ApiStore(tmp_path / "api.sqlite")
    ble = BleStore(tmp_path / "ble.sqlite")
    try:
        assert api.path != ble.path
        assert api.path.name == "api.sqlite" and ble.path.name == "ble.sqlite"

        api_tables = {
            r["name"] for r in api.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        ble_tables = {
            r["name"] for r in ble.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"cycles", "recoveries", "sleeps", "workouts"} <= api_tables
        assert not ({"hr_samples", "raw_packets", "capture_sessions"} & api_tables)
        assert {"hr_samples", "raw_packets", "capture_sessions"} <= ble_tables
        assert not ({"cycles", "recoveries", "sleeps", "workouts"} & ble_tables)
    finally:
        api.close()
        ble.close()


def test_api_store_stats_and_upserts(tmp_path):
    with ApiStore(tmp_path / "api.sqlite") as api:
        assert set(api.stats()) == {"cycles", "recoveries", "sleeps", "workouts"}
        assert all(v == 0 for v in api.stats().values())

        api.upsert_cycles(
            [
                {
                    "id": 1,
                    "user_id": 42,
                    "start": "2026-01-01T00:00:00Z",
                    "score_state": "SCORED",
                    "score": {"strain": 5.2, "average_heart_rate": 68},
                }
            ]
        )
        api.upsert_recoveries(
            [{"cycle_id": 1, "score": {"recovery_score": 44, "hrv_rmssd_milli": 31.8}}]
        )
        assert api.stats()["cycles"] == 1
        assert api.stats()["recoveries"] == 1

        # Idempotent re-upsert.
        api.upsert_cycles([{"id": 1, "user_id": 42, "score": {"strain": 6.0}}])
        assert api.stats()["cycles"] == 1


def test_ble_store_hr_samples_and_idempotency(tmp_path):
    with BleStore(tmp_path / "ble.sqlite") as ble:
        assert set(ble.stats()) == {"hr_samples", "raw_packets", "capture_sessions"}
        sample = {"device_id": "AA", "ts": "2026-01-01T00:00:01Z", "bpm": 64, "rr_ms": [871.1]}
        ble.add_hr_samples([sample, {**sample, "ts": "2026-01-01T00:00:02Z", "bpm": 65, "rr_ms": None}])
        assert ble.stats()["hr_samples"] == 2
        # A re-sync must not double-count (unique index + INSERT OR IGNORE).
        ble.add_hr_samples([sample])
        assert ble.stats()["hr_samples"] == 2


def test_ble_store_timestamps_normalize_to_z(tmp_path):
    with BleStore(tmp_path / "ble.sqlite") as ble:
        # BLE writes near-UTC ("+00:00"); the API window uses "Z".
        ble.add_hr_samples(
            [{"device_id": "d", "ts": "2026-01-01T00:00:00.000+00:00", "bpm": 60}]
        )
        assert ble.query_one("SELECT ts FROM hr_samples")["ts"] == "2026-01-01T00:00:00.000Z"


def test_api_store_token_roundtrip_and_expiry(tmp_path):
    with ApiStore(tmp_path / "api.sqlite") as api:
        api.save_tokens(
            "whoop",
            {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "read:sleep"},
        )
        tokens = api.load_tokens("whoop")
        assert tokens["access_token"] == "a"
        assert tokens["refresh_token"] == "r"
        assert tokens["_expired"] is False


def test_cursor_roundtrip_on_either_store(tmp_path):
    with ApiStore(tmp_path / "api.sqlite") as api:
        assert api.get_cursor("api", "cycles") is None
        api.set_cursor("api", "cycles", "MTIz")
        assert api.get_cursor("api", "cycles") == "MTIz"
    with BleStore(tmp_path / "ble.sqlite") as ble:
        ble.set_cursor("ble", "hr", "abc")
        assert ble.get_cursor("ble", "hr") == "abc"


def test_readonly_store_refuses_writes(tmp_path):
    path = tmp_path / "api.sqlite"
    ApiStore(path).close()  # create the file + schema
    with ApiStore(path, readonly=True) as ro:
        assert ro.readonly
        with pytest.raises(RuntimeError):
            ro.set_cursor("api", "cycles", "x")
        with pytest.raises(RuntimeError):
            ro.upsert_cycles([{"id": 1}])


def test_export_rows_rejects_a_foreign_table(tmp_path):
    with BleStore(tmp_path / "ble.sqlite") as ble:
        with pytest.raises(ValueError):
            ble.export_rows("cycles")  # API table -> belongs to the other store
