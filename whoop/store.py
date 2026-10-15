"""Unified local store.

Both data sources land in one SQLite database:

* Official API tables: ``cycles``, ``recoveries``, ``sleeps``, ``workouts``
  (typed columns for the fields you actually query, plus ``raw_json`` for
  everything else so nothing is ever lost).
* BLE tables: ``hr_samples`` (live heart rate + R-R intervals) and
  ``raw_packets`` (raw proprietary-channel frames).
* Bookkeeping: ``oauth_tokens`` and ``source_sync`` (cursors).

Timestamps are stored as ISO-8601 UTC strings so SQLite's date functions work.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider      TEXT PRIMARY KEY,
    access_token  TEXT,
    refresh_token TEXT,
    token_type    TEXT,
    scope         TEXT,
    expires_at    TEXT,
    user_id       INTEGER,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS source_sync (
    source         TEXT NOT NULL,
    resource       TEXT NOT NULL,
    cursor         TEXT,
    last_synced_at TEXT,
    PRIMARY KEY (source, resource)
);

-- ------------------------------ Official API ------------------------------
CREATE TABLE IF NOT EXISTS cycles (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER,
    start             TEXT,
    end               TEXT,
    timezone_offset   TEXT,
    score_state       TEXT,
    strain            REAL,
    kilojoule         REAL,
    average_heart_rate INTEGER,
    max_heart_rate    INTEGER,
    step_count        INTEGER,
    raw_json          TEXT,
    fetched_at        TEXT
);

CREATE TABLE IF NOT EXISTS recoveries (
    cycle_id            INTEGER PRIMARY KEY,
    sleep_id            TEXT,
    user_id             INTEGER,
    score_state         TEXT,
    recovery_score      REAL,
    resting_heart_rate  REAL,
    hrv_rmssd_milli     REAL,
    spo2_percentage     REAL,
    skin_temp_celsius   REAL,
    raw_json            TEXT,
    fetched_at          TEXT
);

CREATE TABLE IF NOT EXISTS sleeps (
    id                             TEXT PRIMARY KEY,
    cycle_id                       INTEGER,
    user_id                        INTEGER,
    start                          TEXT,
    end                            TEXT,
    nap                            INTEGER,
    score_state                    TEXT,
    respiratory_rate               REAL,
    sleep_performance_percentage   REAL,
    sleep_efficiency_percentage    REAL,
    sleep_consistency_percentage   REAL,
    total_in_bed_time_milli        INTEGER,
    total_rem_sleep_time_milli     INTEGER,
    total_slow_wave_sleep_time_milli INTEGER,
    raw_json                       TEXT,
    fetched_at                     TEXT
);

CREATE TABLE IF NOT EXISTS workouts (
    id                TEXT PRIMARY KEY,
    user_id           INTEGER,
    start             TEXT,
    end               TEXT,
    sport_name        TEXT,
    score_state       TEXT,
    strain            REAL,
    average_heart_rate INTEGER,
    max_heart_rate    INTEGER,
    kilojoule         REAL,
    -- WHOOP 5/MG has NO GPS chip. The only location-like data is what the
    -- official API derives (usually from the paired phone), and it is coarse.
    distance_meter    REAL,
    altitude_gain_meter   REAL,
    altitude_change_meter REAL,
    raw_json          TEXT,
    fetched_at        TEXT
);

-- ---------------------------------- BLE -----------------------------------
CREATE TABLE IF NOT EXISTS hr_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT,
    ts         TEXT NOT NULL,
    bpm        INTEGER,
    rr_ms      TEXT,
    source     TEXT DEFAULT 'ble'
);
CREATE INDEX IF NOT EXISTS idx_hr_samples_ts ON hr_samples (device_id, ts);

CREATE TABLE IF NOT EXISTS raw_packets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT,
    ts           TEXT,
    channel      TEXT,
    char_uuid    TEXT,
    family       TEXT,
    packet_type  INTEGER,
    command      INTEGER,
    hex          TEXT,
    decoded_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_packets_ts ON raw_packets (device_id, ts);

-- One row per capture session, so a raw dump is reproducible/attributable.
CREATE TABLE IF NOT EXISTS capture_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT,
    family       TEXT,
    address      TEXT,
    started_at   TEXT,
    ended_at     TEXT,
    note         TEXT,
    packet_count INTEGER DEFAULT 0
);
"""

# Columns added after the initial schema; applied to pre-existing databases.
_MIGRATIONS = {
    "raw_packets": {
        "char_uuid": "TEXT",
        "family": "TEXT",
    },
    "workouts": {
        "altitude_gain_meter": "REAL",
        "altitude_change_meter": "REAL",
    },
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_ts(value: str | None) -> str | None:
    """Canonicalize a timestamp so API ('…Z') and BLE ('…+00:00') compare equal."""
    if value is None:
        return None
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def _get(d: dict[str, Any], *path: str, default: Any = None) -> Any:
    """Safe nested lookup: `_get(rec, "score", "strain")`."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return default if cur is None else cur


@dataclass
class Store:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock lets the threaded web server share one
        # connection safely while the CLI stays effectively single-threaded.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (idempotent)."""
        for table, columns in _MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

        # Idempotent BLE HR ingestion: collapse any pre-existing duplicates, then
        # enforce uniqueness so re-running a sync/backfill never double-counts.
        self._conn.execute(
            "DELETE FROM hr_samples WHERE id NOT IN ("
            "  SELECT MIN(id) FROM hr_samples"
            "  GROUP BY COALESCE(device_id, ''), ts, bpm)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_samples_dedupe "
            "ON hr_samples (COALESCE(device_id, ''), ts, bpm)"
        )

    # ------------------------------- reads -------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ----------------------------- lifecycle -----------------------------
    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------- tokens ------------------------------
    def save_tokens(self, provider: str, payload: dict[str, Any]) -> None:
        expires_at = None
        if payload.get("expires_in"):
            expires_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + int(payload["expires_in"]),
                tz=timezone.utc,
            ).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
            """
            INSERT INTO oauth_tokens
                (provider, access_token, refresh_token, token_type, scope, expires_at, user_id, updated_at)
            VALUES (:provider, :access_token, :refresh_token, :token_type, :scope, :expires_at, :user_id, :updated_at)
            ON CONFLICT(provider) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
                token_type    = excluded.token_type,
                scope         = excluded.scope,
                expires_at    = excluded.expires_at,
                updated_at    = excluded.updated_at
            """,
            {
                "provider": provider,
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token"),
                "token_type": payload.get("token_type", "Bearer"),
                "scope": payload.get("scope"),
                "expires_at": expires_at,
                "user_id": payload.get("user_id"),
                "updated_at": _utcnow(),
            },
            )
            self._conn.commit()

    def load_tokens(self, provider: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_tokens WHERE provider = ?", (provider,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["_expired"] = (
            data["expires_at"] is not None and data["expires_at"] <= _utcnow()
        )
        return data

    # ------------------------------- cursors -----------------------------
    def set_cursor(self, source: str, resource: str, cursor: str | None) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source_sync (source, resource, cursor, last_synced_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, resource) DO UPDATE SET
                    cursor = excluded.cursor,
                    last_synced_at = excluded.last_synced_at
                """,
                (source, resource, cursor, _utcnow()),
            )
            self._conn.commit()

    def get_cursor(self, source: str, resource: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM source_sync WHERE source = ? AND resource = ?",
                (source, resource),
            ).fetchone()
        return row["cursor"] if row else None

    # --------------------------- API upserts -----------------------------
    def upsert_cycles(self, records: Sequence[dict[str, Any]]) -> int:
        rows = [
            {
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "start": r.get("start"),
                "end": r.get("end"),
                "timezone_offset": r.get("timezone_offset"),
                "score_state": r.get("score_state"),
                "strain": _get(r, "score", "strain"),
                "kilojoule": _get(r, "score", "kilojoule"),
                "average_heart_rate": _get(r, "score", "average_heart_rate"),
                "max_heart_rate": _get(r, "score", "max_heart_rate"),
                "step_count": r.get("step_count"),
                "raw_json": json.dumps(r, separators=(",", ":")),
                "fetched_at": _utcnow(),
            }
            for r in records
        ]
        return self._upsert(
            "cycles",
            rows,
            cols=[
                "id", "user_id", "start", "end", "timezone_offset", "score_state",
                "strain", "kilojoule", "average_heart_rate", "max_heart_rate",
                "step_count", "raw_json", "fetched_at",
            ],
            conflict="id",
        )

    def upsert_recoveries(self, records: Sequence[dict[str, Any]]) -> int:
        rows = [
            {
                "cycle_id": r.get("cycle_id"),
                "sleep_id": r.get("sleep_id"),
                "user_id": r.get("user_id"),
                "score_state": r.get("score_state"),
                "recovery_score": _get(r, "score", "recovery_score"),
                "resting_heart_rate": _get(r, "score", "resting_heart_rate"),
                "hrv_rmssd_milli": _get(r, "score", "hrv_rmssd_milli"),
                "spo2_percentage": _get(r, "score", "spo2_percentage"),
                "skin_temp_celsius": _get(r, "score", "skin_temp_celsius"),
                "raw_json": json.dumps(r, separators=(",", ":")),
                "fetched_at": _utcnow(),
            }
            for r in records
        ]
        return self._upsert(
            "recoveries",
            rows,
            cols=[
                "cycle_id", "sleep_id", "user_id", "score_state", "recovery_score",
                "resting_heart_rate", "hrv_rmssd_milli", "spo2_percentage",
                "skin_temp_celsius", "raw_json", "fetched_at",
            ],
            conflict="cycle_id",
        )

    def upsert_sleeps(self, records: Sequence[dict[str, Any]]) -> int:
        rows = [
            {
                "id": r.get("id"),
                "cycle_id": r.get("cycle_id"),
                "user_id": r.get("user_id"),
                "start": r.get("start"),
                "end": r.get("end"),
                "nap": int(bool(r.get("nap"))),
                "score_state": r.get("score_state"),
                "respiratory_rate": _get(r, "score", "respiratory_rate"),
                "sleep_performance_percentage": _get(r, "score", "sleep_performance_percentage"),
                "sleep_efficiency_percentage": _get(r, "score", "sleep_efficiency_percentage"),
                "sleep_consistency_percentage": _get(r, "score", "sleep_consistency_percentage"),
                "total_in_bed_time_milli": _get(r, "score", "stage_summary", "total_in_bed_time_milli"),
                "total_rem_sleep_time_milli": _get(r, "score", "stage_summary", "total_rem_sleep_time_milli"),
                "total_slow_wave_sleep_time_milli": _get(r, "score", "stage_summary", "total_slow_wave_sleep_time_milli"),
                "raw_json": json.dumps(r, separators=(",", ":")),
                "fetched_at": _utcnow(),
            }
            for r in records
        ]
        return self._upsert(
            "sleeps",
            rows,
            cols=[
                "id", "cycle_id", "user_id", "start", "end", "nap", "score_state",
                "respiratory_rate", "sleep_performance_percentage",
                "sleep_efficiency_percentage", "sleep_consistency_percentage",
                "total_in_bed_time_milli", "total_rem_sleep_time_milli",
                "total_slow_wave_sleep_time_milli", "raw_json", "fetched_at",
            ],
            conflict="id",
        )

    def upsert_workouts(self, records: Sequence[dict[str, Any]]) -> int:
        rows = [
            {
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "start": r.get("start"),
                "end": r.get("end"),
                "sport_name": r.get("sport_name"),
                "score_state": r.get("score_state"),
                "strain": _get(r, "score", "strain"),
                "average_heart_rate": _get(r, "score", "average_heart_rate"),
                "max_heart_rate": _get(r, "score", "max_heart_rate"),
                "kilojoule": _get(r, "score", "kilojoule"),
                "distance_meter": _get(r, "score", "distance_meter"),
                "altitude_gain_meter": _get(r, "score", "altitude_gain_meter"),
                "altitude_change_meter": _get(r, "score", "altitude_change_meter"),
                "raw_json": json.dumps(r, separators=(",", ":")),
                "fetched_at": _utcnow(),
            }
            for r in records
        ]
        return self._upsert(
            "workouts",
            rows,
            cols=[
                "id", "user_id", "start", "end", "sport_name", "score_state",
                "strain", "average_heart_rate", "max_heart_rate", "kilojoule",
                "distance_meter", "altitude_gain_meter", "altitude_change_meter",
                "raw_json", "fetched_at",
            ],
            conflict="id",
        )

    def _upsert(
        self, table: str, rows: Sequence[dict[str, Any]], cols: Sequence[str], conflict: str
    ) -> int:
        if not rows:
            return 0
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != conflict)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        return len(rows)

    # ------------------------------ BLE writes ---------------------------
    def add_hr_samples(self, samples: Iterable[dict[str, Any]]) -> int:
        rows = [
            (
                s.get("device_id") or "",
                _norm_ts(s.get("ts")),
                s.get("bpm"),
                json.dumps(s["rr_ms"]) if s.get("rr_ms") is not None else None,
                s.get("source", "ble"),
            )
            for s in samples
        ]
        with self._lock:
            # INSERT OR IGNORE + the unique index make re-syncs idempotent.
            cur = self._conn.executemany(
                "INSERT OR IGNORE INTO hr_samples (device_id, ts, bpm, rr_ms, source) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else len(rows)

    def add_raw_packet(self, packet: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO raw_packets
                    (device_id, ts, channel, char_uuid, family,
                     packet_type, command, hex, decoded_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.get("device_id"),
                    _norm_ts(packet.get("ts")),
                    packet.get("channel"),
                    packet.get("char_uuid"),
                    packet.get("family"),
                    packet.get("packet_type"),
                    packet.get("command"),
                    packet.get("hex"),
                    json.dumps(packet["decoded"]) if packet.get("decoded") is not None else None,
                ),
            )
            self._conn.commit()

    # ---------------------------- reconciliation -------------------------
    def hr_summary(self, start: str, end: str) -> dict[str, Any]:
        """BLE HR stats over an inclusive [start, end] window."""
        row = self.query_one(
            "SELECT COUNT(*) AS n, AVG(bpm) AS avg_bpm, MIN(bpm) AS min_bpm, "
            "MAX(bpm) AS max_bpm FROM hr_samples WHERE ts >= ? AND ts <= ?",
            (_norm_ts(start), _norm_ts(end)),
        )
        return row or {"n": 0, "avg_bpm": None, "min_bpm": None, "max_bpm": None}

    def coverage(self) -> dict[str, dict[str, Any]]:
        """What window each source actually covers, computed from stored data."""
        api = self.query_one(
            "SELECT MIN(lo) AS lo, MAX(hi) AS hi FROM ("
            "  SELECT MIN(start) AS lo, MAX(end) AS hi FROM cycles"
            "  UNION ALL SELECT MIN(start), MAX(end) FROM sleeps"
            "  UNION ALL SELECT MIN(start), MAX(end) FROM workouts)"
        ) or {}
        api_rows = sum(
            self.query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
            for t in ("cycles", "recoveries", "sleeps", "workouts")
        )
        hr = self.query_one(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM hr_samples"
        ) or {}
        raw = self.query_one(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM raw_packets"
        ) or {}
        return {
            "api": {"start": _norm_ts(api.get("lo")), "end": _norm_ts(api.get("hi")), "rows": api_rows},
            "ble_hr": {"start": hr.get("lo"), "end": hr.get("hi"), "rows": hr.get("n") or 0},
            "ble_raw": {"start": raw.get("lo"), "end": raw.get("hi"), "rows": raw.get("n") or 0},
        }

    def reconcile(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        """Compare each API cycle/workout HR against BLE samples in the same window.

        The two sources are complementary, not duplicates -- but where they overlap
        the high-rate BLE stream should corroborate the API's averaged HR. A large
        delta is the signal that something is off (wrong device/day, or a gap).
        """
        out: list[dict[str, Any]] = []
        for kind, table in (("cycle", "cycles"), ("workout", "workouts")):
            for r in self.query(
                f"SELECT * FROM {table} WHERE start IS NOT NULL AND end IS NOT NULL"
            ):
                if start and _norm_ts(r["end"]) < _norm_ts(start):
                    continue
                if end and _norm_ts(r["start"]) > _norm_ts(end):
                    continue
                s = self.hr_summary(r["start"], r["end"])
                api_avg = r.get("average_heart_rate")
                ble_avg = round(s["avg_bpm"], 1) if s.get("avg_bpm") is not None else None
                out.append(
                    {
                        "kind": kind,
                        "id": r["id"],
                        "start": r["start"],
                        "end": r["end"],
                        "api_avg": api_avg,
                        "api_max": r.get("max_heart_rate"),
                        "ble_n": s.get("n") or 0,
                        "ble_avg": ble_avg,
                        "ble_max": s.get("max_bpm"),
                        "delta": (
                            round(ble_avg - api_avg, 1)
                            if ble_avg is not None and api_avg is not None
                            else None
                        ),
                    }
                )
        return out

    # ------------------------------ capture ------------------------------
    def start_capture(
        self, *, device_id: str, family: str, address: str, note: str | None = None
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO capture_sessions
                    (device_id, family, address, started_at, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, family, address, _utcnow(), note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_capture(self, session_id: int, packet_count: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE capture_sessions SET ended_at = ?, packet_count = ? WHERE id = ?",
                (_utcnow(), packet_count, session_id),
            )
            self._conn.commit()

    # -------------------------------- stats ------------------------------
    TABLES = (
        "cycles",
        "recoveries",
        "sleeps",
        "workouts",
        "hr_samples",
        "raw_packets",
        "capture_sessions",
    )

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                t: self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in self.TABLES
            }

    # ------------------------------- export ------------------------------
    def export_rows(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Return every column of every row in ``table`` (for NDJSON/CSV dumps)."""
        if table not in self.TABLES:
            raise ValueError(f"unknown table: {table}")
        sql = f"SELECT * FROM {table}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql)
