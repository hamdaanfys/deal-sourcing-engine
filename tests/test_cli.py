import csv
import socket

import httpx
import pytest
from conftest import FIXTURES
from typer.testing import CliRunner

from dealsource import cli, db

runner = CliRunner()


def invoke(*args):
    return runner.invoke(cli.app, list(args))


@pytest.mark.parametrize(
    "command",
    [
        ["ingest", "csv", str(FIXTURES / "companies_messy.csv")],
        ["resolve"],
        ["ingest", "cbp", "--naics", "332700"],
    ],
)
def test_pipeline_commands_refuse_without_labels_split(settings, command):
    result = invoke(*command)
    assert result.exit_code == 2
    assert "create" in result.output and "labels split" in result.output
    assert not settings.db_path.exists()


def test_refusal_message_when_labels_exist_but_are_not_split(settings):
    settings.ensure_data_dir()
    settings.labels_path.write_text("company_name,decision\nSynthetic Co,1\n")
    result = invoke("resolve")
    assert result.exit_code == 2
    assert "run `dealsource labels split` first" in result.output


def test_ingest_and_resolve_end_to_end(split_ready):
    result = invoke("ingest", "csv", str(FIXTURES / "companies_messy.csv"))
    assert result.exit_code == 0, result.output
    assert "Dropped 4 contact-looking column(s)" in result.output
    assert "Records: 3 new, 0 updated, 0 unchanged, 1 skipped (no name)" in result.output

    result = invoke("resolve", "--review")
    assert result.exit_code == 0, result.output
    assert "3 records -> 3 companies" in result.output
    assert (split_ready.review_dir / "possible_matches.csv").exists()

    result = invoke("stats")
    assert "companies    3" in result.output

    conn = db.connect(split_ready.db_path)
    stages = [r[0] for r in conn.execute("SELECT stage FROM runs ORDER BY started_at")]
    assert stages == ["ingest_csv", "resolve"]


def test_resolve_output_is_aggregate_only(split_ready):
    invoke("ingest", "csv", str(FIXTURES / "companies_messy.csv"))
    result = invoke("resolve", "--review")
    for name in ("Acme", "Bluegrass", "Harbor"):
        assert name not in result.output


def test_ingest_cbp_uses_cache_on_rerun(split_ready, cbp_server, monkeypatch):
    monkeypatch.setattr(cli, "make_http_client", cbp_server.client)
    args = ("ingest", "cbp", "--naics", "332300", "--geo", "state:13,37", "--year", "2022")
    first = invoke(*args)
    assert first.exit_code == 0, first.output
    assert "Stored 2 market rows (1 requests, 0 from cache)" in first.output
    assert "Georgia: 412 establishments, 9,870 employees" in first.output
    second = invoke(*args)
    assert "(0 requests, 1 from cache)" in second.output
    assert len(cbp_server.calls) == 1


def test_ingest_cbp_reports_key_error(split_ready, cbp_server, monkeypatch):
    monkeypatch.setattr(cli, "make_http_client", cbp_server.client)
    monkeypatch.setenv("CENSUS_API_KEY", "BAD")
    result = invoke("ingest", "cbp", "--naics", "332700")
    assert result.exit_code == 1
    assert "CENSUS_API_KEY" in result.output


def test_csv_with_contact_column_mapped_fails_cleanly(split_ready):
    result = invoke(
        "ingest", "csv", str(FIXTURES / "companies_messy.csv"), "--map", "description=Contact Email"
    )
    assert result.exit_code == 1
    assert "contact details" in result.output


def test_review_file_has_no_contact_columns(split_ready, tmp_path):
    invoke("ingest", "csv", str(FIXTURES / "companies_messy.csv"))
    invoke("resolve", "--review")
    header = next(csv.reader((split_ready.review_dir / "possible_matches.csv").open()))
    assert not any(w in h for h in header for w in ("email", "phone", "contact"))


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_network_is_blocked_in_tests():
    with pytest.raises(Exception, match="(?i)socket"):
        socket.create_connection(("api.census.gov", 443), timeout=1)
    with pytest.raises(Exception, match="(?i)socket"):
        httpx.get("https://api.census.gov/data")
