"""SQLite connection and numbered migrations (no ORM)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: ingest, resolution, market data, HTTP cache, run log
    """
    CREATE TABLE raw_records (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        UNIQUE (source, source_record_id)
    );

    CREATE TABLE companies (
        id INTEGER PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        domain TEXT,
        city TEXT,
        state TEXT,
        country TEXT,
        naics_codes TEXT,
        employee_count INTEGER,
        employee_count_source TEXT,
        revenue_usd_m REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE company_records (
        raw_record_id INTEGER PRIMARY KEY REFERENCES raw_records(id) ON DELETE CASCADE,
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        match_method TEXT NOT NULL,
        match_score REAL,
        evidence_json TEXT NOT NULL
    );
    CREATE INDEX idx_company_records_company ON company_records(company_id);

    CREATE TABLE market_stats (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        year INTEGER NOT NULL,
        naics TEXT NOT NULL,
        naics_label TEXT,
        geo_level TEXT NOT NULL,
        geo_code TEXT NOT NULL,
        geo_name TEXT,
        establishments INTEGER,
        employees INTEGER,
        employees_noise_flag TEXT,
        annual_payroll_usd_k INTEGER,
        size_classes_json TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        UNIQUE (source, year, naics, geo_level, geo_code)
    );

    CREATE TABLE http_cache (
        url TEXT PRIMARY KEY,
        final_url TEXT,
        status INTEGER NOT NULL,
        headers_json TEXT NOT NULL,
        body BLOB,
        content_hash TEXT,
        fetched_at TEXT NOT NULL
    );

    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        params_json TEXT NOT NULL,
        stats_json TEXT
    );
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply pending migrations."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version, sql in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        with conn:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


@contextmanager
def record_run(conn: sqlite3.Connection, stage: str, params: dict) -> Iterator[dict]:
    """Log a stage run in ``runs``. The caller fills the yielded dict with aggregate stats."""
    run_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, stage, started_at, params_json) VALUES (?, ?, ?, ?)",
            (run_id, stage, utcnow(), json.dumps(params, sort_keys=True, default=str)),
        )
    stats: dict = {}
    try:
        yield stats
    finally:
        with conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
                (utcnow(), json.dumps(stats, sort_keys=True, default=str), run_id),
            )
