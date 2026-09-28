"""Export for labeling: eligibility, state spread, per-state cap, and what the file contains."""

import csv
from collections import Counter

import pytest
from conftest import EXAMPLE_THESIS
from typer.testing import CliRunner

from dealsource import cli, db
from dealsource.eval.label_export import (
    COLUMNS,
    ExportRefused,
    default_cap,
    export_for_labeling,
    spread_sample,
)
from dealsource.score.thesis import load_thesis

THESIS, _ = load_thesis(EXAMPLE_THESIS)
runner = CliRunner()


def add(conn, name, *, domain="site.test", state="GA", naics="332710", country="US"):
    cur = conn.execute(
        """INSERT INTO companies (canonical_name, domain, state, naics_codes, country, city, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'Somewhere', 'x', 'x')""",
        (name, domain, state, naics, country),
    )
    conn.commit()
    return cur.lastrowid


def populate(conn, per_state):
    for state, count in per_state.items():
        for i in range(count):
            add(
                conn,
                f"{state} Company {i:03d}",
                domain=f"{state.lower()}-co-{i:03d}.test",
                state=state,
            )


def read(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def test_file_has_only_the_four_columns_and_empty_decisions(conn, tmp_path):
    populate(conn, {"GA": 5})
    out = tmp_path / "to_label.csv"
    export_for_labeling(conn, THESIS, out_path=out, n=3)
    with out.open(newline="") as f:
        assert next(csv.reader(f)) == COLUMNS == ["company_name", "website", "state", "decision"]
    rows = read(out)
    assert len(rows) == 3 and {r["decision"] for r in rows} == {""}
    assert [r["company_name"] for r in rows] == sorted(r["company_name"] for r in rows)


def test_only_thesis_matching_companies_with_websites_are_eligible(conn, tmp_path):
    keep = add(conn, "Keep Machining", domain="keep.test")
    add(conn, "No Website Machining", domain=None)
    add(conn, "Wrong State Machining", state="CA")
    add(conn, "Software Co", naics="541511")
    add(conn, "Foreign Machining", country="CA")
    result = export_for_labeling(conn, THESIS, out_path=tmp_path / "out.csv", n=10)
    assert (result.eligible, result.without_website, result.sampled) == (1, 1, 1)
    assert read(tmp_path / "out.csv")[0]["company_name"] == "Keep Machining"
    assert keep


def test_sample_is_spread_across_states_with_a_cap(conn, tmp_path):
    populate(conn, {"GA": 300, "NC": 40, "TN": 10, "AL": 5})
    result = export_for_labeling(conn, THESIS, out_path=tmp_path / "out.csv", n=200)
    counts = Counter(r["state"] for r in read(tmp_path / "out.csv"))
    # Even share is 50 per state; small states give all they have, and GA may take up to the cap.
    assert result.per_state_cap == default_cap(200, 4) == 75
    assert counts == {"GA": 75, "NC": 40, "TN": 10, "AL": 5}
    assert result.sampled == 130  # the cap binds before 200 is reached


def test_explicit_cap_and_even_spread(conn, tmp_path):
    populate(conn, {"GA": 100, "NC": 100, "SC": 100, "FL": 100})
    result = export_for_labeling(
        conn, THESIS, out_path=tmp_path / "out.csv", n=200, per_state_cap=60
    )
    assert result.per_state == {"FL": 50, "GA": 50, "NC": 50, "SC": 50}


def test_sample_is_deterministic_for_a_seed():
    by_state = {"GA": list(range(100)), "NC": list(range(100, 150))}
    a = spread_sample(by_state, n=40, cap=30, seed=1)
    assert a == spread_sample(by_state, n=40, cap=30, seed=1)
    assert a != spread_sample(by_state, n=40, cap=30, seed=2)
    assert len(a) == 40


def test_existing_output_is_never_overwritten(conn, tmp_path):
    populate(conn, {"GA": 5})
    out = tmp_path / "to_label.csv"
    out.write_text("my half-finished labels\n")
    with pytest.raises(ExportRefused, match="never overwritten"):
        export_for_labeling(conn, THESIS, out_path=out, n=3)
    assert out.read_text() == "my half-finished labels\n"


def test_already_labeled_companies_are_skipped(conn, tmp_path):
    populate(conn, {"GA": 3})
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "company_name,website,state,decision\nGA Company 000,ga-co-000.test,GA,pass\n"
    )
    result = export_for_labeling(
        conn, THESIS, out_path=tmp_path / "out.csv", labels_path=labels, n=10
    )
    assert result.already_labeled == 1 and result.sampled == 2
    assert "GA Company 000" not in {r["company_name"] for r in read(tmp_path / "out.csv")}


def test_output_round_trips_into_the_labels_loader(conn, tmp_path, settings):
    from dealsource.eval.labels import load_labels

    populate(conn, {"GA": 4})
    out = tmp_path / "to_label.csv"
    export_for_labeling(conn, THESIS, out_path=out, n=4)
    filled = out.read_text().replace(",\r\n", ",pursue\r\n").replace(",\n", ",pursue\n")
    labels = tmp_path / "labels.csv"
    labels.write_text(filled)
    rows = load_labels(labels)
    assert len(rows) == 4 and all(r.key.startswith("d:") for r in rows)


# --- CLI --------------------------------------------------------------------------------


def test_cli_export_before_split_prints_counts_only(settings, tmp_path):
    conn = db.connect(settings.db_path)
    populate(conn, {"GA": 6, "NC": 4})
    with db.record_run(conn, "resolve", {}):
        pass
    conn.close()
    result = runner.invoke(
        cli.app, ["labels", "export", "--thesis", str(EXAMPLE_THESIS), "--n", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote 5 companies" in result.output and "per state: GA 3, NC 2" in result.output
    assert "Company" not in result.output and ".test" not in result.output
    assert settings.to_label_path.exists()
    assert not settings.split_manifest_path.exists()


def test_cli_export_requires_resolve_after_new_records(settings, tmp_path):
    conn = db.connect(settings.db_path)
    conn.execute(
        "INSERT INTO raw_records (source, source_record_id, payload_json, payload_hash, ingested_at) "
        "VALUES ('csv', '1', '{}', 'h', '2999-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    result = runner.invoke(cli.app, ["labels", "export", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 1 and "run it first" in result.output


def test_cli_discover_sam_with_local_file_then_resolve_and_export(settings, sam_zip):
    result = runner.invoke(
        cli.app, ["discover", "sam", "--thesis", str(EXAMPLE_THESIS), "--file", str(sam_zip)]
    )
    assert result.exit_code == 0, result.output
    assert (
        "Scanned 10 registrations: 4 active in thesis NAICS/states (3 with a website)"
        in result.output
    )
    assert "ACME" not in result.output
    assert runner.invoke(cli.app, ["resolve"]).exit_code == 0
    result = runner.invoke(cli.app, ["labels", "export", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 0, result.output
    assert "Wrote 3 companies" in result.output
    names = {r["company_name"] for r in read(settings.to_label_path)}
    assert names == {
        "ACME PRECISION MACHINING LLC",
        "SPLIT LINE METAL WORKS LLC",
        "VOLUNTEER GEAR WORKS INC",
    }


def test_cli_discover_sam_without_key_or_file_explains(settings):
    result = runner.invoke(cli.app, ["discover", "sam", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 1 and "SAM_API_KEY" in result.output


def test_cli_discover_sam_downloads_with_key(settings, sam_zip, monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=sam_zip.read_bytes())

    monkeypatch.setenv("SAM_API_KEY", "SECRET-KEY")
    monkeypatch.setattr(
        cli, "make_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    from datetime import date

    monkeypatch.setattr(cli, "make_today", lambda: date(2026, 9, 27))
    result = runner.invoke(cli.app, ["discover", "sam", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 0, result.output
    assert "downloaded now" in result.output and "SECRET-KEY" not in result.output
    assert seen[0].url.params["date"] == "09/2026"
    again = runner.invoke(cli.app, ["discover", "sam", "--thesis", str(EXAMPLE_THESIS)])
    assert "already on disk" in again.output and len(seen) == 1


def test_cli_discover_usaspending(settings, usa_server, monkeypatch, clock):
    monkeypatch.setattr(cli, "make_http_client", usa_server.client)
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    result = runner.invoke(
        cli.app, ["discover", "usaspending", "--thesis", str(EXAMPLE_THESIS), "--state", "GA,NC"]
    )
    assert result.exit_code == 0, result.output
    assert "4 recipients (3 requests, 0 from cache)" in result.output
    assert "GREENE" not in result.output


def test_cli_bad_thesis_is_a_clean_error(settings, tmp_path):
    bad = tmp_path / "t.yaml"
    bad.write_text("name: x\n")
    result = runner.invoke(cli.app, ["labels", "export", "--thesis", str(bad)])
    assert result.exit_code == 1 and "Invalid thesis" in result.output


def test_no_eligible_companies_writes_nothing_and_explains(conn, tmp_path):
    add(conn, "USAspending Only Co", domain=None, naics=None)
    add(conn, "Unknown NAICS Co", domain="unknown-naics.test", naics=None)
    out = tmp_path / "to_label.csv"
    with pytest.raises(ExportRefused) as exc:
        export_for_labeling(conn, THESIS, out_path=out, n=10)
    msg = str(exc.value)
    assert (
        "2 in thesis states" in msg and "1 have no website" in msg and "1 have no NAICS code" in msg
    )
    assert "discover sam" in msg
    assert not out.exists()  # a later export isn't blocked by an empty file


def test_breakdown_counts(conn, tmp_path):
    add(conn, "Keep Machining", domain="keep.test")
    add(conn, "No Site", domain=None)
    add(conn, "No NAICS", domain="nonaics.test", naics=None)
    add(conn, "Software Co", domain="soft.test", naics="541511")
    r = export_for_labeling(conn, THESIS, out_path=tmp_path / "o.csv", n=5)
    assert (
        r.in_thesis_states,
        r.eligible,
        r.without_website,
        r.naics_unknown,
        r.naics_outside_thesis,
    ) == (4, 1, 1, 1, 1)
