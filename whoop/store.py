"""Separated local stores -- **one SQLite file per source**.

The official API and the reverse-engineered BLE path are kept physically
separate. Each gets its own file and its own tables, and nothing ever writes
across:

* :class:`ApiStore` -> ``data/whoop_api.sqlite``
  ``cycles`` / ``recoveries`` / ``sleeps`` / ``workouts`` + OAuth tokens.
* :class:`BleStore` -> ``data/whoop_ble.sqlite``
  ``hr_samples`` / ``raw_packets`` / ``capture_sessions``.

Cross-source work (coverage, reconciliation) lives in :mod:`whoop.reconcile`,
which opens both files **read-only**. At rest the two sources never touch, so you
can back up, delete, rebuild, or re-sync either one without the other.

Timestamps are stored as ISO-8601 UTC strings, canonicalized to a trailing ``Z``
so API values and BLE values compare equal when they are joined.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# ===========================================================================
# Schema
# ===========================================================================
SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_sync (
    source         TEXT NOT NULL,
    resource       TEXT NOT NULL,
    cursor         TEXT,
    last_synced_at TEXT,
    PRIMARY KEY (source, resource)
);
"""

API_SCHEMA = """
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
    distance_meter         REAL,
    altitude_gain_meter    REAL,
    altitude_change_meter  REAL,
    raw_json          TEXT,
    fetched_at        TEXT
);
"""

BLE_SCHEMA = """
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_ts(value: str | None) -> str | None:
    """Canonicalize a timestamp so API ('...Z') and BLE ('...+00:00') compare equal."""
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


# ===========================================================================
# Base store
# ===========================================================================
class Store:
    """One SQLite file, one lock, shared read/export helpers.

    Instantiate a subclass (:class:`ApiStore`, :class:`BleStore`), or open an
    existing file read-only with ``readonly=True`` (used by reconciliation).
    """

    KIND = "base"
    SCHEMA = ""
    TABLES: tuple[str, ...] = ()
    MIGRATIONS: dict[str, dict[str, str]] = {}

    def __init__(self, path: Path | str, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.RLock()

        if readonly:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SHARED_SCHEMA + self.SCHEMA)
            self._migrate()
            self._conn.commit()

    # ----------------------------- lifecycle -----------------------------
    def _ensure_writable(self) -> None:
        if self.readonly:
            raise RuntimeError(f"{self.KIND} store is read-only: {self.path}")

    def _migrate(self) -> None:
        for table, columns in self.MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

        # Idempotent BLE HR ingestion: collapse any pre-existing duplicates, then
        # enforce uniqueness so re-running a sync/backfill never double-counts.
        if "hr_samples" in self.TABLES:
            self._conn.execute(
                "DELETE FROM hr_samples WHERE id NOT IN ("
                "  SELECT MIN(id) FROM hr_samples"
                "  GROUP BY COALESCE(device_id, ''), ts, bpm)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_samples_dedupe "
                "ON hr_samples (COALESCE(device_id, ''), ts, bpm)"
            )

    def close(self) -> None:
        if not self.readonly:
            self._conn.commit()
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------- reads -------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def count(self, table: str) -> int:
        return self.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Execute one write statement and commit immediately.

        Unlike :meth:`query`, this is safe for tools (e.g. the ESP32 bridge)
        that insert rows while other processes read the database.
        """
        self._ensure_writable()
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        return {t: self.count(t) for t in self.TABLES}

    def export_rows(self, table: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Return every column of every row in ``table`` (for NDJSON/CSV dumps)."""
        if table not in self.TABLES:
            raise ValueError(f"unknown table for {self.KIND}: {table}")
        sql = f"SELECT * FROM {table}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql)

    # ------------------------------- cursors -----------------------------
    def set_cursor(self, source: str, resource: str, cursor: str | None) -> None:
        self._ensure_writable()
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
        row = self.query_one(
            "SELECT cursor FROM source_sync WHERE source = ? AND resource = ?",
            (source, resource),
        )
        return row["cursor"] if row else None

    def _upsert(
        self, table: str, rows: Sequence[dict[str, Any]], cols: Sequence[str], conflict: str
    ) -> int:
        self._ensure_writable()
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


# ===========================================================================
# API store
# ===========================================================================
class ApiStore(Store):
    """The official-API database. Never holds BLE data."""

    KIND = "api"
    SCHEMA = API_SCHEMA
    TABLES = ("cycles", "recoveries", "sleeps", "workouts")
    MIGRATIONS = {
        "workouts": {
            "altitude_gain_meter": "REAL",
            "altitude_change_meter": "REAL",
        }
    }

    # ------------------------------- tokens ------------------------------
    def save_tokens(self, provider: str, payload: dict[str, Any]) -> None:
        self._ensure_writable()
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
        row = self.query_one("SELECT * FROM oauth_tokens WHERE provider = ?", (provider,))
        if row is None:
            return None
        row["_expired"] = row["expires_at"] is not None and row["expires_at"] <= _utcnow()
        return row

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

    def coverage(self) -> dict[str, dict[str, Any]]:
        window = self.query_one(
            "SELECT MIN(lo) AS lo, MAX(hi) AS hi FROM ("
            "  SELECT MIN(start) AS lo, MAX(end) AS hi FROM cycles"
            "  UNION ALL SELECT MIN(start), MAX(end) FROM sleeps"
            "  UNION ALL SELECT MIN(start), MAX(end) FROM workouts)"
        ) or {}
        return {
            "api": {
                "start": _norm_ts(window.get("lo")),
                "end": _norm_ts(window.get("hi")),
                "rows": sum(self.count(t) for t in self.TABLES),
            }
        }

    def cycles_with_hr(self) -> list[dict[str, Any]]:
        """Windows + averaged HR that reconciliation compares against BLE."""
        out: list[dict[str, Any]] = []
        for kind, table in (("cycle", "cycles"), ("workout", "workouts")):
            for r in self.query(
                f"SELECT id, start, end, average_heart_rate, max_heart_rate FROM {table} "
                "WHERE start IS NOT NULL AND end IS NOT NULL"
            ):
                r["kind"] = kind
                out.append(r)
        return out


# ===========================================================================
# BLE store
# ===========================================================================
class BleStore(Store):
    """The strap database. Never holds API data."""

    KIND = "ble"
    SCHEMA = BLE_SCHEMA
    TABLES = ("hr_samples", "raw_packets", "capture_sessions")
    MIGRATIONS = {
        "raw_packets": {"char_uuid": "TEXT", "family": "TEXT"},
    }

    # ------------------------------ HR samples ---------------------------
    def add_hr_samples(self, samples: Iterable[dict[str, Any]]) -> int:
        self._ensure_writable()
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
            return max(cur.rowcount or 0, 0)

    def hr_summary(self, start: str, end: str) -> dict[str, Any]:
        row = self.query_one(
            "SELECT COUNT(*) AS n, AVG(bpm) AS avg_bpm, MIN(bpm) AS min_bpm, "
            "MAX(bpm) AS max_bpm FROM hr_samples WHERE ts >= ? AND ts <= ?",
            (_norm_ts(start), _norm_ts(end)),
        )
        return row or {"n": 0, "avg_bpm": None, "min_bpm": None, "max_bpm": None}

    def hr_series(
        self, *, start: str | None = None, end: str | None = None, max_points: int = 900
    ) -> list[dict[str, Any]]:
        """HR over a window, bucket-averaged down to at most ``max_points``."""
        where, params = [], []
        if start:
            where.append("ts >= ?")
            params.append(_norm_ts(start))
        if end:
            where.append("ts <= ?")
            params.append(_norm_ts(end))
        sql = "SELECT ts, bpm FROM hr_samples"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts"
        rows = self.query(sql, params)
        if len(rows) <= max_points:
            return rows
        step = len(rows) / max_points
        out: list[dict[str, Any]] = []
        i = 0.0
        while int(i) < len(rows):
            chunk = rows[int(i) : int(i + step)] or [rows[int(i)]]
            bpms = [c["bpm"] for c in chunk if c["bpm"] is not None]
            if bpms:
                out.append(
                    {"ts": chunk[0]["ts"], "bpm": round(sum(bpms) / len(bpms), 1), "n": len(chunk)}
                )
            i += step
        return out

    # ------------------------------ raw packets --------------------------
    def add_raw_packet(self, packet: dict[str, Any]) -> None:
        self._ensure_writable()
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

    def recent_raw(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id, ts, family, channel, char_uuid, packet_type, command, hex "
            "FROM raw_packets ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )

    # ------------------------------ captures -----------------------------
    def start_capture(
        self, *, device_id: str, family: str, address: str, note: str | None = None
    ) -> int:
        self._ensure_writable()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO capture_sessions (device_id, family, address, started_at, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (device_id, family, address, _utcnow(), note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_capture(self, session_id: int, packet_count: int) -> None:
        self._ensure_writable()
        with self._lock:
            self._conn.execute(
                "UPDATE capture_sessions SET ended_at = ?, packet_count = ? WHERE id = ?",
                (_utcnow(), packet_count, session_id),
            )
            self._conn.commit()

    def coverage(self) -> dict[str, dict[str, Any]]:
        hr = self.query_one(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM hr_samples"
        ) or {}
        raw = self.query_one(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM raw_packets"
        ) or {}
        return {
            "ble_hr": {"start": hr.get("lo"), "end": hr.get("hi"), "rows": hr.get("n") or 0},
            "ble_raw": {"start": raw.get("lo"), "end": raw.get("hi"), "rows": raw.get("n") or 0},
        }
