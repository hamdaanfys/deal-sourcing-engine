"""Guess-and-verify website finder: strict verification, ambiguity, resume, sample. No network."""

import csv
import json

import pytest
from conftest import EXAMPLE_THESIS
from typer.testing import CliRunner

from dealsource import cli, db
from dealsource.enrich.fetcher import PoliteFetcher, is_cacheable_page
from dealsource.httpcache import CachedHttp
from dealsource.models import RawCompanyRecord
from dealsource.resolve.pipeline import resolve
from dealsource.sources.csv_source import store_records
from dealsource.websites import finder as wf
from dealsource.websites.review import SampleRefused, write_sample

GOOD = """<html><head><title>Acme Precision Machining | CNC Machining in Macon</title></head>
<body><main><p>Tight-tolerance parts for aerospace.</p></main>
<footer>Acme Precision Machining · 100 Industrial Way · Macon, GA 31201 · 478-555-0101</footer></body></html>"""
NAME_ONLY = (
    "<html><head><title>Acme Precision Machining</title></head><body><p>Parts.</p></body></html>"
)
OTHER_STATE = """<html><head><title>Acme Precision Machining</title></head>
<body><footer>Fresno, CA 93701</footer></body></html>"""
LOCATION_ONLY = (
    "<html><head><title>Welcome</title></head><body><footer>Macon, GA 31201</footer></body></html>"
)
PARKED = "<html><head><title>acmeprecision.com</title></head><body>This domain is for sale! Macon, GA</body></html>"
BODY_CITY = """<html><head><title>Home</title></head><body><p>Acme Precision Machining has served
Macon since 1962.</p></body></html>"""


def target(
    name="ACME PRECISION MACHINING LLC", city="Macon", state="GA", uei="SAMGA0000001", cid=1
):
    return wf.Target(cid, wf.search_key(uei, name, state), name, city, state, uei)


# --- candidates ---------------------------------------------------------------------------


def test_candidate_domains():
    assert wf.candidate_domains("ACME PRECISION MACHINING LLC") == [
        "acmeprecisionmachining.com",
        "acme-precision-machining.com",
        "acmeprecision.com",
    ]
    assert wf.candidate_domains("Greene Machine & Manufacturing Inc") == [
        "greenemachinemanufacturing.com",
        "greene-machine-manufacturing.com",
        "greenemachinemfg.com",
        "greenemachine.com",
    ]


@pytest.mark.parametrize(
    "name", ["ACE INDUSTRIES, INC.", "UNITED SERVICES LLC", "AB CO", "GLOBAL SOLUTIONS GROUP"]
)
def test_generic_names_are_not_guessed(name):
    assert wf.candidate_domains(name) == []


# --- verification --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "confidence", "name_match", "location_match"),
    [
        (GOOD, 0.95, "title", "city"),
        (BODY_CITY, 0.85, "body", "city"),
    ],
)
def test_accepted_pages(html, confidence, name_match, location_match):
    conf, ev = wf.verify_page(html, target())
    assert conf == confidence
    assert (ev["name_match"], ev["location_match"]) == (name_match, location_match)
    assert ev["location_snippet"] and "478-555-0101" not in ev["location_snippet"]


def test_state_evidence_when_city_unknown():
    conf, ev = wf.verify_page(GOOD, target(city=None))
    assert conf == 0.90 and ev["location_match"] == "state"


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        (NAME_ONLY, "no city/state evidence"),
        (OTHER_STATE, "no city/state evidence"),  # same name, different state
        (LOCATION_ONLY, "no name evidence"),
        (PARKED, "parked domain"),
    ],
)
def test_uncertain_pages_are_rejected(html, reason):
    conf, ev = wf.verify_page(html, target())
    assert conf == 0.0 and ev["rejected"] == reason


def test_lowercase_state_code_is_not_evidence():
    html = "<html><head><title>Acme Precision Machining</title></head><body>go ahead, ga ga</body></html>"
    assert wf.verify_page(html, target(city=None))[0] == 0.0


# --- per-company checks ----------------------------------------------------------------------


def fetcher_for(conn, server, clock):
    http = CachedHttp(
        conn, server.client(), "dealsource/test (+https://example.org)", cacheable=is_cacheable_page
    )
    return PoliteFetcher(http, clock=clock, min_delay=2.0)


def resolver_for(*live):
    return lambda host: host in live


def test_found_with_confidence_and_evidence(conn, site_server, clock):
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    status, dom, conf, ev, log = wf.check_company(
        target(), fetcher_for(conn, site_server, clock), resolver_for("acmeprecisionmachining.com")
    )
    assert (status, dom, conf) == ("found", "acmeprecisionmachining.com", 0.95)
    assert [e["domain"] for e in log] == [
        "acmeprecisionmachining.com",
        "acme-precision-machining.com",
        "acmeprecision.com",
    ]
    assert log[1]["dns"] is False


def test_two_different_verified_domains_are_ambiguous(conn, site_server, clock):
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    site_server.add("acmeprecision.com", "/", body=GOOD)
    status, dom, *_ = wf.check_company(
        target(),
        fetcher_for(conn, site_server, clock),
        resolver_for("acmeprecisionmachining.com", "acmeprecision.com"),
    )
    assert (status, dom) == ("ambiguous", None)


def test_redirect_to_another_domain_is_verified_on_its_own(conn, site_server, clock):
    site_server.add(
        "acmeprecision.com", "/", status=301, headers={"location": "https://acme-mfg-group.com/"}
    )
    site_server.add("acme-mfg-group.com", "/", body=GOOD)
    status, dom, conf, *_ = wf.check_company(
        target(),
        fetcher_for(conn, site_server, clock),
        resolver_for("acmeprecision.com", "acme-mfg-group.com"),
    )
    assert (status, dom, conf) == ("found", "acme-mfg-group.com", 0.95)


def test_redirect_to_a_platform_is_not_a_website(conn, site_server, clock):
    site_server.add(
        "acmeprecision.com", "/", status=301, headers={"location": "https://www.facebook.com/acme"}
    )
    status, dom, *_ = wf.check_company(
        target(), fetcher_for(conn, site_server, clock), resolver_for("acmeprecision.com")
    )
    assert (status, dom) == ("not_found", None)
    assert not any(r.url.host.endswith("facebook.com") for r in site_server.requests)


def test_name_only_match_gives_no_website(conn, site_server, clock):
    site_server.add("acmeprecisionmachining.com", "/", body=NAME_ONLY)
    status, dom, *_ = wf.check_company(
        target(), fetcher_for(conn, site_server, clock), resolver_for("acmeprecisionmachining.com")
    )
    assert (status, dom) == ("not_found", None)


# --- runs: resume, errors, joining back to companies ------------------------------------------


def seed_companies(conn):
    store_records(
        conn,
        iter(
            [
                RawCompanyRecord(
                    source="usaspending",
                    source_record_id="SAMGA0000001",
                    name="ACME PRECISION MACHINING LLC",
                    state="GA",
                    naics="3327",
                    extra={"uei": "SAMGA0000001"},
                ),
                RawCompanyRecord(
                    source="usaspending",
                    source_record_id="USAGA0000011",
                    name="GREENE MACHINE & MANUFACTURING INC",
                    state="GA",
                    naics="3339",
                    extra={"uei": "USAGA0000011"},
                ),
                RawCompanyRecord(
                    source="usaspending",
                    source_record_id="USAGA0000013",
                    name="ACE INDUSTRIES INC",
                    state="GA",
                    naics="3327",
                    extra={"uei": "USAGA0000013"},
                ),
                RawCompanyRecord(
                    source="usaspending",
                    source_record_id="USAGA0000014",
                    name="SOFTWARE SHOP LLC",
                    state="GA",
                    naics="5415",
                    extra={"uei": "USAGA0000014"},
                ),
            ]
        ),
    )
    resolve(conn, source_priority=("usaspending",))


def test_targets_follow_thesis_and_skip_companies_with_websites(conn):
    from dealsource.score.thesis import load_thesis

    seed_companies(conn)
    thesis, _ = load_thesis(EXAMPLE_THESIS)
    targets = wf.targets(conn, thesis)
    assert {t.name for t in targets} == {
        "ACME PRECISION MACHINING LLC",
        "GREENE MACHINE & MANUFACTURING INC",
        "ACE INDUSTRIES INC",
    }
    assert "uei:SAMGA0000001" in {t.search_key for t in targets}
    # Stable pseudo-random order, so a partial run is spread across states and resumes the same way
    assert [t.search_key for t in targets] == [t.search_key for t in wf.targets(conn, thesis)]


def test_run_is_resumable_and_found_websites_join_on_resolve(conn, site_server, clock):
    from dealsource.score.thesis import load_thesis

    seed_companies(conn)
    thesis, _ = load_thesis(EXAMPLE_THESIS)
    site_server.add(
        "acmeprecisionmachining.com", "/", body=GOOD.replace("Macon, GA", "Atlanta, GA")
    )
    resolver = resolver_for("acmeprecisionmachining.com")
    targets = wf.targets(conn, thesis)
    acme = [t for t in targets if t.name.startswith("ACME")]

    first = wf.run_finder(
        conn, acme, fetcher=fetcher_for(conn, site_server, clock), resolver=resolver
    )
    assert first.statuses == {"found": 1}
    seen = []
    second = wf.run_finder(
        conn,
        targets,
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver,
        progress=lambda st: seen.append((st.already_done, st.checked)),
        progress_every=1,
    )
    assert second.already_done == 1 and second.checked == 2
    assert second.statuses == {"not_found": 1, "too_generic": 1}
    assert seen == [(1, 1), (1, 2)]
    third = wf.run_finder(
        conn, targets, fetcher=fetcher_for(conn, site_server, clock), resolver=resolver
    )
    assert third.checked == 0 and third.already_done == 3

    resolve(conn, source_priority=("usaspending", "websites"))
    domain = conn.execute(
        "SELECT domain FROM companies WHERE canonical_name = 'ACME PRECISION MACHINING LLC'"
    ).fetchone()[0]
    assert domain == "acmeprecisionmachining.com"
    ev = json.loads(
        conn.execute("SELECT evidence_json FROM website_search WHERE status = 'found'").fetchone()[
            0
        ]
    )
    assert ev["name_match"] == "title" and ev["location_match"] == "state"


def test_errors_are_recorded_and_retryable(conn, site_server, clock, monkeypatch):
    seed_companies(conn)
    t = target()

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(wf, "check_company", boom)
    stats = wf.run_finder(conn, [t], fetcher=None, resolver=resolver_for())
    assert stats.statuses == {"error": 1}
    monkeypatch.undo()
    again = wf.run_finder(
        conn, [t], fetcher=fetcher_for(conn, site_server, clock), resolver=resolver_for()
    )
    assert again.checked == 0  # not retried by default
    retried = wf.run_finder(
        conn,
        [t],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for(),
        retry_errors=True,
    )
    assert retried.statuses == {"not_found": 1}


# --- hand-check sample ------------------------------------------------------------------------


def test_sample_file_and_refusals(conn, tmp_path, site_server, clock):
    seed_companies(conn)
    with pytest.raises(SampleRefused, match="No websites"):
        write_sample(conn, tmp_path / "s.csv")
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    wf.run_finder(
        conn,
        [target()],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for("acmeprecisionmachining.com"),
    )
    result = write_sample(conn, tmp_path / "s.csv", n=30)
    assert (result.found, result.sampled) == (1, 1)
    rows = list(csv.DictReader((tmp_path / "s.csv").open()))
    assert rows[0]["website"] == "https://acmeprecisionmachining.com" and rows[0]["correct"] == ""
    assert rows[0]["confidence"] == "0.95"
    with pytest.raises(SampleRefused, match="never overwritten"):
        write_sample(conn, tmp_path / "s.csv")


# --- CLI ----------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_find_and_sample_print_counts_only(settings, site_server, clock, monkeypatch):
    monkeypatch.setenv("DEALSOURCE_USER_AGENT_CONTACT", "https://example.org/contact")
    monkeypatch.setattr(cli, "make_http_client", site_server.client)
    monkeypatch.setattr(cli, "make_clock", lambda: clock)
    monkeypatch.setattr(cli, "make_resolver", lambda: resolver_for("acmeprecisionmachining.com"))
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    conn = db.connect(settings.db_path)
    seed_companies(conn)
    conn.close()

    result = runner.invoke(
        cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS), "--limit", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "[2/3] checked this run 2; found 1," in result.output  # order is a stable hash
    again = runner.invoke(cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS)])
    assert "Resumed: 2 were already checked" in again.output
    assert "All runs so far: found 1, not_found 1, too_generic 1" in again.output
    for out in (result.output, again.output):
        assert "ACME" not in out and "acmeprecision" not in out

    runner.invoke(cli.app, ["resolve"])
    sample = runner.invoke(cli.app, ["websites", "sample"])
    assert sample.exit_code == 0, sample.output
    # USAspending companies have no city, so the match rests on state evidence (0.90)
    assert "Wrote 1 of 1 found websites" in sample.output and "0.90: 1" in sample.output
    assert "ACME" not in sample.output and (settings.review_dir / "website_sample.csv").exists()


def test_cli_find_requires_contact_and_not_the_split(settings, monkeypatch):
    monkeypatch.delenv("DEALSOURCE_USER_AGENT_CONTACT", raising=False)
    result = runner.invoke(cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 1 and "DEALSOURCE_USER_AGENT_CONTACT" in result.output
