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
    # 2: enrichment (LLM cache, per-call metrics, per-company results)
    """
    CREATE TABLE llm_cache (
        cache_key TEXT PRIMARY KEY,
        backend TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY,
        company_id INTEGER,
        stage TEXT NOT NULL,
        backend TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        latency_ms REAL,
        cache_hit INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_llm_calls_company ON llm_calls(company_id);

    CREATE TABLE enrichments (
        company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        detail TEXT,
        extraction_json TEXT,
        pages_used_json TEXT NOT NULL,
        text_sha256 TEXT,
        llm_cache_key TEXT,
        mask_version INTEGER,
        fetch_ms REAL,
        llm_ms REAL,
        total_ms REAL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        updated_at TEXT NOT NULL
    );
    """,
    # 3: daily request counts for rate-limited APIs (SAM.gov allows 10 requests/day without a role)
    """
    CREATE TABLE api_usage (
        api TEXT NOT NULL,
        day TEXT NOT NULL,
        requests INTEGER NOT NULL,
        PRIMARY KEY (api, day)
    );
    """,
    # 4: website finder (one row per company, written as soon as that company is done)
    """
    CREATE TABLE website_search (
        search_key TEXT PRIMARY KEY,
        company_id INTEGER,
        status TEXT NOT NULL,
        domain TEXT,
        confidence REAL,
        evidence_json TEXT,
        candidates_json TEXT NOT NULL,
        finished_at TEXT NOT NULL
    );
    """,
    # 5: scoring (one row per company per score run) and evaluation runs (aggregate metrics only)
    """
    CREATE TABLE scores (
        run_id TEXT NOT NULL,
        company_id INTEGER NOT NULL,
        thesis_hash TEXT NOT NULL,
        total REAL NOT NULL,
        components_json TEXT NOT NULL,
        excluded INTEGER NOT NULL,
        exclusion_rule TEXT,
        reason TEXT NOT NULL,
        confidence TEXT NOT NULL,
        scored_at TEXT NOT NULL,
        PRIMARY KEY (run_id, company_id)
    );
    CREATE INDEX idx_scores_thesis ON scores(thesis_hash, scored_at);

    CREATE TABLE eval_runs (
        eval_id TEXT PRIMARY KEY,
        split TEXT NOT NULL,
        thesis_hash TEXT NOT NULL,
        score_run_id TEXT NOT NULL,
        code_version TEXT,
        bootstrap_seed INTEGER NOT NULL,
        metrics_json TEXT NOT NULL,
        report_path TEXT,
        created_at TEXT NOT NULL
    );
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply pending migrations.

    ``check_same_thread=False`` is for a connection handed between worker threads (one at a
    time). Writers from several connections wait up to 30 s for the lock (WAL mode)."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=check_same_thread)
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
