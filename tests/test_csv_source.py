import json
import re

import pytest
from conftest import FIXTURES

from dealsource.sources.csv_source import (
    EMAIL_RE,
    PHONE_RE,
    ContactColumnError,
    CSVSource,
    parse_int,
    store_records,
)

MESSY = FIXTURES / "companies_messy.csv"


def test_auto_detects_columns_and_drops_contact_columns():
    src = CSVSource(MESSY)
    records = list(src.iter_records())
    assert src.plan.mapping == {
        "name": "Company Name",
        "website": "Website",
        "city": "City",
        "state": "ST",
        "employees": "Employees",
        "revenue_usd_m": "Revenue ($M)",
    }
    # Dropped by header ("Contact", "Email", "Phone") and by content (Misc is mostly emails).
    assert set(src.plan.dropped) == {"Primary Contact", "Contact Email", "Phone", "Misc"}
    assert src.plan.extra == ["Notes", "Ownership"]
    assert [r.name for r in records] == [
        "Acme Mfg. LLC",
        "Bluegrass Precision Tooling Inc.",
        "Harbor Line Fabricators",
    ]
    assert src.skipped_rows == 1


def test_contact_details_in_free_text_are_scrubbed():
    acme = next(CSVSource(MESSY).iter_records())
    assert acme.extra["Notes"] == "Call [removed] or write [removed]"


def test_no_contact_details_reach_the_database(conn):
    store_records(conn, CSVSource(MESSY).iter_records())
    blob = " ".join(row[0] for row in conn.execute("SELECT payload_json FROM raw_records"))
    assert not EMAIL_RE.search(blob)
    assert not PHONE_RE.search(blob)
    for person in ("Pat Example", "Sam Sample", "Lee Placeholder"):
        assert person not in blob


def test_parses_numbers_and_revenue_units():
    records = {r.name: r for r in CSVSource(MESSY).iter_records()}
    assert records["Acme Mfg. LLC"].employees == 85
    assert records["Bluegrass Precision Tooling Inc."].employees == 50  # "40-60" -> midpoint
    assert records["Harbor Line Fabricators"].employees == 1200
    assert records["Acme Mfg. LLC"].revenue_usd_m == 12.5
    assert records["Bluegrass Precision Tooling Inc."].revenue_usd_m is None

    in_thousands = {r.name: r for r in CSVSource(MESSY, revenue_unit="usd_k").iter_records()}
    assert in_thousands["Acme Mfg. LLC"].revenue_usd_m == 0.0125


@pytest.mark.parametrize(
    ("raw", "value"),
    [("1,200", 1200), ("~50", 50), ("50 to 100", 75), ("200+", 200), ("n/a", None), (None, None)],
)
def test_parse_int(raw, value):
    assert parse_int(raw) == value


def test_mapping_a_contact_column_is_refused():
    with pytest.raises(ContactColumnError):
        list(CSVSource(MESSY, column_map={"description": "Contact Email"}).iter_records())


def test_explicit_map_and_bad_map(tmp_path):
    path = tmp_path / "odd.csv"
    path.write_text("Firm;Web\nOakmont Valve Service;oakmont.test\n")
    rec = next(CSVSource(path, column_map={"name": "Firm", "website": "Web"}).iter_records())
    assert (rec.name, rec.website) == ("Oakmont Valve Service", "oakmont.test")
    with pytest.raises(ValueError, match="not in the file"):
        list(CSVSource(path, column_map={"name": "Nope"}).iter_records())
    with pytest.raises(ValueError, match="company-name column"):
        list(CSVSource(path).iter_records())


def test_reimport_is_idempotent_and_ids_are_stable(conn):
    first = store_records(conn, CSVSource(MESSY).iter_records())
    second = store_records(conn, CSVSource(MESSY).iter_records())
    assert first == {"inserted": 3, "updated": 0, "unchanged": 0}
    assert second == {"inserted": 0, "updated": 0, "unchanged": 3}
    ids = [r[0] for r in conn.execute("SELECT source_record_id FROM raw_records")]
    assert all(re.fullmatch(r"[0-9a-f]{16}", i) for i in ids)


def test_changed_row_with_id_column_is_updated(conn, tmp_path):
    path = tmp_path / "ids.csv"
    path.write_text("id,company,employees\n7,Oakmont Valve Service,20\n")
    store_records(conn, CSVSource(path).iter_records())
    path.write_text("id,company,employees\n7,Oakmont Valve Service,25\n")
    assert store_records(conn, CSVSource(path).iter_records())["updated"] == 1
    payload = json.loads(conn.execute("SELECT payload_json FROM raw_records").fetchone()[0])
    assert payload["employees"] == 25
