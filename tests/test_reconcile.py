"""Cross-source reconciliation -- the only place both databases are joined.

Nothing here writes: :class:`Combined` opens both files read-only.
"""

from __future__ import annotations

import stat

import pytest

from whoop.reconcile import Combined
from whoop.store import ApiStore, BleStore


def _seed(api_path, ble_path):
    with ApiStore(api_path) as api:
        api.upsert_cycles(
            [
                {
                    "id": 1,
                    "start": "2026-01-01T00:00:00.000Z",
                    "end": "2026-01-01T01:00:00.000Z",
                    "score": {"average_heart_rate": 60, "max_heart_rate": 120},
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


def test_coverage_merges_both_sources(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    _seed(api_path, ble_path)
    with Combined.open(api_path, ble_path) as both:
        cov = both.coverage()
    assert cov["api"]["rows"] == 1
    assert cov["api"]["start"] == "2026-01-01T00:00:00.000Z"
    assert cov["api"]["end"] == "2026-01-01T01:00:00.000Z"
    assert cov["ble_hr"]["rows"] == 2
    assert cov["ble_hr"]["start"] == "2026-01-01T00:10:00.000Z"


def test_reconcile_matches_ble_against_api(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    _seed(api_path, ble_path)
    with Combined.open(api_path, ble_path) as both:
        rows = both.reconcile()
    assert len(rows) == 1
    r = rows[0]
    assert r["api_avg"] == 60
    assert r["ble_avg"] == 60.0
    assert r["ble_n"] == 2
    assert r["ble_max"] == 70
    assert r["delta"] == 0.0


def test_reconcile_ignores_windows_outside_the_range(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    _seed(api_path, ble_path)
    with Combined.open(api_path, ble_path) as both:
        assert both.reconcile(start="2026-02-01T00:00:00.000Z") == []
        assert both.reconcile(end="2025-12-01T00:00:00.000Z") == []


def test_reconcile_uses_a_readonly_connection(tmp_path):
    api_path, ble_path = tmp_path / "api.sqlite", tmp_path / "ble.sqlite"
    _seed(api_path, ble_path)
    before = stat.S_IMODE((ble_path).stat().st_mode)
    with Combined.open(api_path, ble_path) as both:
        assert both.api.readonly and both.ble.readonly
        both.reconcile()
        with pytest.raises(RuntimeError):
            both.ble.add_hr_samples([{"device_id": "d", "ts": "2026-03-01T00:00:00.000Z", "bpm": 99}])
    assert stat.S_IMODE(ble_path.stat().st_mode) == before


def test_open_creates_missing_files_so_the_ui_shows_zeros(tmp_path):
    api_path, ble_path = tmp_path / "nope_api.sqlite", tmp_path / "nope_ble.sqlite"
    assert not api_path.exists() and not ble_path.exists()
    with Combined.open(api_path, ble_path) as both:
        cov = both.coverage()
    assert cov["api"]["rows"] == 0 and cov["ble_hr"]["rows"] == 0
