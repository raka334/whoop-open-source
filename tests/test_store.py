"""Store round-trip tests using a temp database."""

from __future__ import annotations

import tempfile
from pathlib import Path

from whoop.store import Store


def _store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "test.sqlite"
    return Store(tmp)


def test_schema_and_stats():
    with _store() as store:
        stats = store.stats()
        assert set(stats) >= {"cycles", "recoveries", "sleeps", "workouts", "hr_samples"}
        assert all(v == 0 for v in stats.values())


def test_upsert_cycle_and_recovery():
    with _store() as store:
        store.upsert_cycles(
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
        store.upsert_recoveries(
            [{"cycle_id": 1, "score": {"recovery_score": 44, "hrv_rmssd_milli": 31.8}}]
        )
        stats = store.stats()
        assert stats["cycles"] == 1
        assert stats["recoveries"] == 1
        # Idempotent re-upsert.
        store.upsert_cycles([{"id": 1, "user_id": 42, "score": {"strain": 6.0}}])
        assert store.stats()["cycles"] == 1


def test_hr_samples_and_rmssd_source():
    with _store() as store:
        store.add_hr_samples(
            [
                {"device_id": "AA", "ts": "2026-01-01T00:00:01Z", "bpm": 64, "rr_ms": [871.1]},
                {"device_id": "AA", "ts": "2026-01-01T00:00:02Z", "bpm": 65, "rr_ms": None},
            ]
        )
        assert store.stats()["hr_samples"] == 2


def test_token_roundtrip_and_expiry():
    with _store() as store:
        store.save_tokens(
            "whoop",
            {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "read:sleep"},
        )
        tokens = store.load_tokens("whoop")
        assert tokens["access_token"] == "a"
        assert tokens["refresh_token"] == "r"
        assert tokens["_expired"] is False


def test_cursor_roundtrip():
    with _store() as store:
        assert store.get_cursor("api", "cycles") is None
        store.set_cursor("api", "cycles", "MTIz")
        assert store.get_cursor("api", "cycles") == "MTIz"
