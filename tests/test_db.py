import json

from dealsource import db


def test_migrations_are_idempotent(settings):
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    assert versions == list(range(1, len(db.MIGRATIONS) + 1))


def test_record_run_logs_stats(conn):
    with db.record_run(conn, "demo", {"x": 1}) as stats:
        stats["rows"] = 3
    row = conn.execute("SELECT stage, params_json, stats_json, finished_at FROM runs").fetchone()
    assert row["stage"] == "demo"
    assert json.loads(row["stats_json"]) == {"rows": 3}
    assert row["finished_at"] is not None
