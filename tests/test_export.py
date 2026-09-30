"""Ranked CSV export (DESIGN.md §10), on synthetic companies."""

import csv

import pytest
from synthetic import add_company, extraction, thesis
from typer.testing import CliRunner

from dealsource import cli
from dealsource.db import utcnow
from dealsource.export.csv_export import COLUMNS, ExportRefused, export_ranked
from dealsource.score.scorer import score_all

runner = CliRunner()


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def scored(conn):
    t = thesis()
    add_company(
        conn,
        "Kestrel Ridge Machining",
        domain="kestrelridge.test",
        city="Macon",
        extraction=extraction(),
    )
    add_company(conn, "Copperfield Tooling", domain="copperfield.test", naics="541511")
    add_company(
        conn,
        "Juniper Precision",
        domain="juniperprecision.test",
        extraction=extraction(ownership={"publicly_traded": "yes"}),
    )
    add_company(conn, "Tamarack Fabrication", state="NC", extraction=extraction(), source="osm")
    score_all(conn, t, "hash-a")
    return t


def test_columns_order_and_bom(conn, scored, tmp_path):
    out = tmp_path / "exports" / "ranked.csv"
    res = export_ranked(conn, scored, "hash-a", out_path=out)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    with out.open(encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f)) == COLUMNS
    assert (res.rows, res.excluded) == (4, 1)


def test_ranked_rows_then_excluded_at_the_bottom(conn, scored, tmp_path):
    rows = read(export_ranked(conn, scored, "hash-a", out_path=tmp_path / "r.csv").path)
    assert [r["rank"] for r in rows] == ["1", "2", "3", ""]
    assert rows[-1]["excluded"] == "yes" and rows[-1]["score"] == "0.0"
    assert rows[-1]["reason"].startswith("Excluded: ownership signal publicly_traded")
    scores = [float(r["score"]) for r in rows[:-1]]
    assert scores == sorted(scores, reverse=True)


def test_row_contents(conn, scored, tmp_path):
    rows = read(export_ranked(conn, scored, "hash-a", out_path=tmp_path / "r.csv").path)
    top = next(r for r in rows if r["company"] == "Kestrel Ridge Machining")
    assert top["employees"] == "85" and top["employees_source"] == "website"
    assert top["facilities"] == "2" and top["revenue_usd_m"] == ""
    assert top["product_lines"] == "CNC turned parts; custom fabrication"
    assert top["family_owned"] == "yes" and top["pe_backed"] == "unknown"
    assert top["sources"] == "csv" and top["thesis_name"] == scored.name
    assert top["reason"]
    unenriched = next(r for r in rows if r["company"] == "Copperfield Tooling")
    assert unenriched["business_model"] == "" and unenriched["founder_led"] == "unknown"


def test_no_contact_or_label_columns():
    banned = {"email", "phone", "contact", "linkedin", "address", "decision", "label", "split"}
    assert not {c for c in COLUMNS if any(b in c for b in banned)}


def test_refuses_without_scores_and_never_overwrites(conn, scored, tmp_path):
    with pytest.raises(ExportRefused, match="No scores for this thesis"):
        export_ranked(conn, scored, "other-hash", out_path=tmp_path / "x.csv")
    out = tmp_path / "r.csv"
    export_ranked(conn, scored, "hash-a", out_path=out)
    with pytest.raises(ExportRefused, match="never overwritten"):
        export_ranked(conn, scored, "hash-a", out_path=out)


def test_market_sidecar_has_thesis_sectors_and_states_only(conn, scored, tmp_path):
    def stat(naics, level, code, name):
        conn.execute(
            """INSERT INTO market_stats (source, year, naics, naics_label, geo_level, geo_code,
                 geo_name, establishments, employees, size_classes_json, fetched_at)
               VALUES ('census_cbp', 2022, ?, 'label', ?, ?, ?, 10, 100, '{}', ?)""",
            (naics, level, code, name, utcnow()),
        )

    with conn:
        stat("332700", "state", "13", "Georgia")
        stat("332300", "us", "1", "United States")
        stat("332700", "county", "13021", "Bibb County, Georgia")
        stat("332700", "state", "48", "Texas")  # state outside the thesis
        stat("541511", "state", "13", "Georgia")  # sector outside the thesis
    res = export_ranked(conn, scored, "hash-a", out_path=tmp_path / "r.csv")
    assert res.market_path == tmp_path / "r.market.csv" and res.market_rows == 3
    assert {r["geo_name"] for r in read(res.market_path)} == {
        "Georgia",
        "United States",
        "Bibb County, Georgia",
    }


def test_cli_score_then_export_prints_counts_only(split_ready, conn):
    add_company(
        conn, "Kestrel Ridge Machining", domain="kestrelridge.test", extraction=extraction()
    )
    add_company(conn, "Copperfield Tooling", domain="copperfield.test")
    thesis_path = split_ready.data_dir / "thesis.yaml"
    import yaml
    from synthetic import THESIS

    thesis_path.write_text(yaml.safe_dump(THESIS))
    r = runner.invoke(cli.app, ["score", "--thesis", str(thesis_path)])
    assert r.exit_code == 0, r.output
    assert "Scored 2 companies" in r.output
    r = runner.invoke(cli.app, ["export", "--thesis", str(thesis_path)])
    assert r.exit_code == 0, r.output
    for text in ("Kestrel", "Copperfield", "kestrelridge.test"):
        assert text not in r.output
    exported = list((split_ready.data_dir / "exports").glob("thesis_*.csv"))
    assert len(exported) == 1 and len(read(exported[0])) == 2


@pytest.mark.parametrize("command", ["score", "export", "eval"])
def test_pipeline_commands_refuse_without_the_split(settings, command, tmp_path):
    r = runner.invoke(cli.app, [command, "--thesis", str(tmp_path / "missing.yaml")])
    assert r.exit_code == 2 and "labels split" in r.output
