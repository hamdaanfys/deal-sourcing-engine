"""SAM.gov extract and USAspending discovery with saved synthetic fixtures. No network."""

import json
from datetime import date

import httpx
import pytest
from conftest import SAM_DAT

from dealsource.httpcache import CachedHttp
from dealsource.sources.csv_source import store_records
from dealsource.sources.sam_extract import (
    SamError,
    SamExtractSource,
    download_extract,
    first_sunday,
    iter_rows,
    latest_month,
    parse_naics_string,
)
from dealsource.sources.usaspending import UsaSpendingError, UsaSpendingSource, fiscal_year_window

THESIS_NAICS = ["3323", "3327", "3329", "3339"]
THESIS_STATES = ["AL", "FL", "GA", "NC", "SC", "TN"]


# --- SAM.gov extract parsing -------------------------------------------------------------


def test_sam_extract_filters_and_parses(sam_zip):
    source = SamExtractSource(sam_zip, naics_prefixes=THESIS_NAICS, states=THESIS_STATES)
    records = {r.source_record_id: r for r in source.iter_records()}
    assert set(records) == {"SAMGA0000001", "SAMNC0000002", "SAMTN0000009", "SAMGA0000010"}
    acme = records["SAMGA0000001"]
    assert (acme.name, acme.website, acme.city, acme.state) == (
        "ACME PRECISION MACHINING LLC",
        "https://www.acme-precision.test",
        "Macon",
        "GA",
    )
    assert acme.naics == "332710,332999,541330"
    assert acme.extra["uei"] == "SAMGA0000001" and acme.extra["dba_name"] == "ACME PRECISION"
    assert acme.extra["sba_small_for_primary_naics"] == "Y"
    assert records["SAMNC0000002"].website is None
    assert records["SAMGA0000010"].name == "SPLIT LINE METAL WORKS LLC"  # record spanning two lines

    st = source.stats
    assert (st.scanned, st.kept, st.with_website) == (10, 4, 3)
    assert (st.inactive, st.not_public, st.excluded_entities, st.dnb_era) == (1, 1, 1, 1)
    assert st.wrong_country_or_state == 1 and st.wrong_naics == 1
    assert st.per_state == {"GA": 2, "NC": 1, "TN": 1}


def test_point_of_contact_columns_never_reach_the_database(conn, sam_zip):
    store_records(
        conn,
        SamExtractSource(sam_zip, naics_prefixes=THESIS_NAICS, states=THESIS_STATES).iter_records(),
    )
    blob = " ".join(r[0] for r in conn.execute("SELECT payload_json FROM raw_records"))
    assert "Pat" not in blob and "Example" not in blob and "Contact Street" not in blob


def test_iter_rows_skips_header_footer_and_incomplete_records():
    lines = ["BOF PUBLIC V2 x", "A|B", "EOF PUBLIC V2 x"]
    assert list(iter_rows(iter(lines))) == []
    full = SAM_DAT.read_text().splitlines()
    assert len(list(iter_rows(iter(full)))) == 10


def test_parse_naics_string():
    assert parse_naics_string("333611Y~333612N~541120 ~bad") == [
        ("333611", "Y"),
        ("333612", "N"),
        ("541120", ""),
    ]


def test_bad_zip_is_a_clear_error(tmp_path):
    path = tmp_path / "x.ZIP"
    path.write_bytes(b"not a zip")
    with pytest.raises(SamError, match="not a valid ZIP"):
        list(SamExtractSource(path, naics_prefixes=["33"], states=["GA"]).iter_records())


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 27), (2026, 9)),
        (date(2026, 9, 6), (2026, 8)),
        (date(2026, 9, 7), (2026, 9)),
        (date(2026, 1, 3), (2025, 12)),
    ],
)
def test_latest_month(today, expected):
    assert first_sunday(2026, 9) == date(2026, 9, 6)
    assert latest_month(today) == expected


# --- SAM.gov download ---------------------------------------------------------------------


def sam_client(status=200, body=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        headers = {
            "content-disposition": 'attachment; filename="SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP"'
        }
        return httpx.Response(status, headers=headers, content=body or b"")

    return httpx.Client(transport=httpx.MockTransport(handler))


def download(conn, tmp_path, client, **kw):
    args = dict(
        api_key="SECRET-KEY",
        dest_dir=tmp_path / "sam",
        year=2026,
        month=9,
        user_agent="dealsource/test",
        today=date(2026, 9, 27),
    )
    args.update(kw)
    return download_extract(conn, client, **args)


def test_download_once_then_reuse_file(conn, tmp_path, sam_zip):
    seen = []
    path, fresh = download(conn, tmp_path, sam_client(body=sam_zip.read_bytes(), seen=seen))
    assert fresh and path.name == "SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP"
    params = seen[0].url.params
    assert (
        params["fileType"],
        params["sensitivity"],
        params["frequency"],
        params["charset"],
        params["date"],
    ) == (
        "ENTITY",
        "PUBLIC",
        "MONTHLY",
        "UTF8",
        "09/2026",
    )
    assert params["api_key"] == "SECRET-KEY"
    path2, fresh2 = download(conn, tmp_path, sam_client(body=b"never used", seen=seen))
    assert (path2, fresh2) == (path, False) and len(seen) == 1
    assert conn.execute("SELECT requests FROM api_usage").fetchone()[0] == 1
    # The key is never stored anywhere in the database.
    dump = "\n".join(conn.iterdump())
    assert "SECRET-KEY" not in dump


def test_daily_budget_is_enforced(conn, tmp_path, sam_zip):
    conn.execute("INSERT INTO api_usage (api, day, requests) VALUES ('sam.gov', '2026-09-27', 8)")
    conn.commit()
    seen = []
    with pytest.raises(SamError, match="budget"):
        download(conn, tmp_path, sam_client(body=sam_zip.read_bytes(), seen=seen))
    assert seen == []


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the API key"),
        (403, "rejected the API key"),
        (404, "No public monthly extract"),
        (429, "daily request limit"),
        (500, "HTTP 500"),
    ],
)
def test_download_errors_are_clear_and_leave_no_partial_file(conn, tmp_path, status, message):
    with pytest.raises(SamError, match=message):
        download(conn, tmp_path, sam_client(status=status))
    assert list((tmp_path / "sam").glob("*")) == []


def test_non_zip_body_is_rejected(conn, tmp_path):
    with pytest.raises(SamError, match="did not return a ZIP"):
        download(conn, tmp_path, sam_client(body=b"<html>error</html>"))
    assert list((tmp_path / "sam").glob("*")) == []


# --- USAspending ------------------------------------------------------------------------


def usa_source(conn, server, clock, **kw):
    http = CachedHttp(conn, server.client(), "dealsource/test")
    return UsaSpendingSource(
        http,
        naics_prefixes=THESIS_NAICS,
        states=["GA", "NC"],
        today=date(2026, 9, 27),
        clock=clock,
        **kw,
    )


def test_fiscal_year_window():
    assert fiscal_year_window(date(2026, 9, 27), 5) == ("2021-10-01", "2026-09-27")
    assert fiscal_year_window(date(2026, 10, 2), 5) == ("2022-10-01", "2026-10-02")


def test_usaspending_queries_each_prefix_and_tags_award_naics(conn, usa_server, clock):
    source = usa_source(conn, usa_server, clock)
    records = {r.source_record_id: r for r in source.iter_records()}
    assert list(records) == ["SAMGA0000001", "USAGA0000011", "USAGA0000012", "SAMNC0000002"]
    acme = records["SAMGA0000001"]
    assert acme.naics == "3327,3339"  # contracts under two thesis prefixes
    assert acme.extra["naics_source"] == "usaspending_award_prefix"
    assert acme.extra["federal_contract_obligations_usd"] == 5264685.71
    assert records["SAMNC0000002"].naics == "3323"
    assert all(r.website is None for r in records.values())
    # One query (plus pages) per state x prefix, each with a single prefix
    assert len(usa_server.bodies) == 2 * 4 + 1  # GA 3327 has 2 pages
    assert {tuple(b["filters"]["naics_codes"]["require"]) for b in usa_server.bodies} == {
        (p,) for p in THESIS_NAICS
    }
    body = usa_server.bodies[0]
    assert body["filters"]["award_type_codes"] == ["A", "B", "C", "D"]
    assert body["filters"]["time_period"] == [
        {"start_date": "2021-10-01", "end_date": "2026-09-27"}
    ]
    assert source.stats.skipped_no_uei == 1
    assert source.stats.requests == 9 and clock.sleeps == [1.0] * 8


def test_usaspending_rerun_is_cached(conn, usa_server, clock):
    list(usa_source(conn, usa_server, clock).iter_records())
    again = usa_source(conn, usa_server, clock)
    assert len(list(again.iter_records())) == 4
    assert again.stats.requests == 0 and again.stats.cache_hits == 9
    assert len(usa_server.bodies) == 9


def test_usaspending_joins_sam_on_uei_during_resolution(conn, usa_server, clock, sam_zip):
    from dealsource.resolve.pipeline import resolve

    store_records(
        conn,
        SamExtractSource(sam_zip, naics_prefixes=THESIS_NAICS, states=THESIS_STATES).iter_records(),
    )
    store_records(conn, usa_source(conn, usa_server, clock).iter_records())
    resolve(conn, source_priority=("sam", "usaspending"))
    rows = conn.execute(
        """SELECT c.canonical_name, c.domain, COUNT(*) n FROM companies c
           JOIN company_records cr ON cr.company_id = c.id GROUP BY c.id HAVING n > 1"""
    ).fetchall()
    merged = {r["canonical_name"]: r["domain"] for r in rows}
    assert merged == {
        "ACME PRECISION MACHINING LLC": "acme-precision.test",
        "PIEDMONT STEEL FABRICATORS INC": None,
    }
    methods = {
        r[0]
        for r in conn.execute(
            "SELECT match_method FROM company_records WHERE match_method != 'singleton'"
        )
    }
    assert methods == {"uei"}
    payloads = [
        json.loads(r[0])
        for r in conn.execute("SELECT payload_json FROM raw_records WHERE source = 'usaspending'")
    ]
    assert all(p["website"] is None for p in payloads)


def test_five_digit_prefixes_are_expanded_to_their_children(conn, usa_server, clock):
    http = CachedHttp(conn, usa_server.client(), "dealsource/test")
    source = UsaSpendingSource(
        http, naics_prefixes=["3327", "33992"], states=["GA"], today=date(2026, 9, 27), clock=clock
    )
    assert source.expand_prefix("3327") == ["3327"]
    records = {r.source_record_id: r for r in source.iter_records()}
    sporting = [b for b in usa_server.bodies if b["filters"]["naics_codes"]["require"] != ["3327"]]
    assert sporting[0]["filters"]["naics_codes"]["require"] == [
        "339920"
    ]  # only children under 33992
    assert usa_server.references == ["3399"]  # looked up once, then cached
    assert records["SAMGA0000001"].naics == "3327,33992"  # tagged with the thesis prefix


def test_prefix_without_children_is_reported_and_skipped(conn, usa_server, clock):
    http = CachedHttp(conn, usa_server.client(), "dealsource/test")
    source = UsaSpendingSource(
        http, naics_prefixes=["99999"], states=["GA"], today=date(2026, 9, 27), clock=clock
    )
    assert list(source.iter_records()) == []
    assert source.stats.unknown_prefixes == ["99999"]


def test_timeouts_are_retried_then_reported(conn, usa_server, clock):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("slow")
        return usa_server.handler(request)

    http = CachedHttp(conn, httpx.Client(transport=httpx.MockTransport(flaky)), "dealsource/test")
    source = UsaSpendingSource(
        http, naics_prefixes=["3323"], states=["NC"], today=date(2026, 9, 27), clock=clock
    )
    assert [r.source_record_id for r in source.iter_records()] == ["SAMNC0000002", "SAMGA0000001"]
    assert source.stats.retries == 2 and 10.0 in clock.sleeps and 20.0 in clock.sleeps

    def always_slow(request):
        raise httpx.ReadTimeout("slow")

    dead = UsaSpendingSource(
        CachedHttp(
            conn, httpx.Client(transport=httpx.MockTransport(always_slow)), "dealsource/test"
        ),
        naics_prefixes=["3327"],
        states=["GA"],
        today=date(2026, 9, 27),
        clock=clock,
    )
    with pytest.raises(UsaSpendingError, match="after 3 attempts"):
        list(dead.iter_records())
