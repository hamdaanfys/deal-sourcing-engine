import csv
import json

from dealsource.models import RawCompanyRecord
from dealsource.resolve.pipeline import resolve
from dealsource.sources.csv_source import store_records


def raw(rid, name, source="csv", **kw):
    return RawCompanyRecord(source=source, source_record_id=str(rid), name=name, **kw)


def companies(conn):
    return conn.execute("SELECT * FROM companies ORDER BY id").fetchall()


def test_resolve_merges_and_builds_canonical_record(conn):
    store_records(
        conn,
        iter(
            [
                raw(
                    1,
                    "Acme Mfg. LLC",
                    website="acme-mfg.test",
                    city="Macon",
                    state="GA",
                    employees=85,
                    naics="332710",
                ),
                raw(
                    2,
                    "ACME Manufacturing, Inc.",
                    source="crm",
                    state="Georgia",
                    revenue_usd_m=12.5,
                    naics="332999",
                ),
                raw(3, "Oakmont Valve Service", state="TN"),
            ]
        ),
    )
    stats = resolve(conn, source_priority=("csv", "crm"))
    assert (
        stats["records"] == 3 and stats["companies"] == 2 and stats["multi_record_companies"] == 1
    )
    acme = companies(conn)[0]
    assert acme["canonical_name"] == "Acme Mfg. LLC"  # from the higher-priority source
    assert acme["domain"] == "acme-mfg.test"
    assert (acme["city"], acme["state"]) == ("Macon", "GA")
    assert acme["naics_codes"] == "332710,332999"
    assert (acme["employee_count"], acme["employee_count_source"]) == (85, "csv")
    assert acme["revenue_usd_m"] == 12.5

    evidence = conn.execute(
        "SELECT match_method, match_score, evidence_json FROM company_records WHERE raw_record_id = 2"
    ).fetchone()
    assert evidence["match_method"] == "name_location" and evidence["match_score"] == 100.0
    assert json.loads(evidence["evidence_json"])[0]["other"] == "csv:1"


def test_company_ids_are_stable_across_reruns_and_new_records(conn):
    store_records(
        conn,
        iter([raw(1, "Acme Mfg. LLC", state="GA"), raw(2, "Oakmont Valve Service", state="TN")]),
    )
    resolve(conn, source_priority=("csv",))
    before = {r["canonical_name"]: r["id"] for r in companies(conn)}
    store_records(
        conn,
        iter(
            [
                raw(3, "ACME Manufacturing, Inc.", state="GA"),
                raw(4, "Harbor Line Fabricators", state="GA"),
            ]
        ),
    )
    stats = resolve(conn, source_priority=("csv",))
    after = {r["canonical_name"]: r["id"] for r in companies(conn)}
    assert after["Acme Mfg. LLC"] == before["Acme Mfg. LLC"]
    assert after["Oakmont Valve Service"] == before["Oakmont Valve Service"]
    assert stats["new_companies"] == 1


def test_split_override_creates_company_and_removes_nothing_else(conn, tmp_path):
    store_records(
        conn,
        iter([raw(1, "Acme Mfg. LLC", state="GA"), raw(2, "ACME Manufacturing, Inc.", state="GA")]),
    )
    resolve(conn, source_priority=("csv",))
    assert len(companies(conn)) == 1
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("split:\n  - ['csv:1', 'csv:2']\nmerge:\n  - ['csv:1', 'csv:999']\n")
    stats = resolve(conn, source_priority=("csv",), overrides_path=overrides)
    assert len(companies(conn)) == 2
    assert stats["unknown_override_refs"] == 1


def test_merged_away_company_is_deleted(conn, tmp_path):
    store_records(
        conn,
        iter(
            [
                raw(1, "Summit Controls", website="summitcontrols.test"),
                raw(2, "Summit Automation", website="summitautomation.test"),
            ]
        ),
    )
    resolve(conn, source_priority=("csv",))
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text("merge:\n  - ['csv:1', 'csv:2']\n")
    stats = resolve(conn, source_priority=("csv",), overrides_path=overrides)
    assert len(companies(conn)) == 1 and stats["removed_companies"] == 1


def test_review_file_lists_near_misses(conn, tmp_path):
    store_records(
        conn,
        iter(
            [
                raw(1, "Southern Precision Machining", state="GA"),
                raw(2, "Southern Precision Machining Co.", state="AL"),
                raw(3, "Blue Ridge Fabrication", website="brf-industrial.test"),
                raw(4, "BRF Industrial Services", website="brf-industrial.test"),
                raw(
                    5,
                    "Summit Controls",
                    website="summitcontrols.test",
                    state="TN",
                    city="Knoxville",
                ),
                raw(
                    6,
                    "Summit Controls LLC",
                    website="summit-controls-tn.test",
                    state="TN",
                    city="Knoxville",
                ),
            ]
        ),
    )
    path = tmp_path / "review" / "possible_matches.csv"
    stats = resolve(conn, source_priority=("csv",), review_path=path)
    rows = list(csv.DictReader(path.open()))
    assert stats["review_items"] == 3
    assert {(r["kind"], r["reason"]) for r in rows} == {
        ("possible_match", "similar name, different state"),
        ("merged_on_domain", "same domain, names differ"),
        ("different_domains", "same name and location, different domains"),
    }
    assert stats["companies"] == 5  # the Summit pair is flagged, not merged


def test_facebook_pages_do_not_merge_companies_end_to_end(conn):
    store_records(
        conn,
        iter(
            [
                raw(
                    1,
                    "Oakmont Valve Service",
                    website="https://www.facebook.com/oakmontvalve",
                    state="GA",
                ),
                raw(2, "Harbor Line Fabricators", website="facebook.com/harborlinefab", state="GA"),
            ]
        ),
    )
    stats = resolve(conn, source_priority=("csv",))
    assert stats["companies"] == 2
    assert [r["domain"] for r in companies(conn)] == [None, None]
