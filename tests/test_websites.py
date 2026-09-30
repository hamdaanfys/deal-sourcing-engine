"""Guess-and-verify website finder: strict verification, ambiguity, resume, sample. No network."""

import csv
import json
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable

import httpx
import pytest
from conftest import EXAMPLE_THESIS, SiteServer
from typer.testing import CliRunner

from dealsource import cli, db
from dealsource.clock import SystemClock
from dealsource.enrich.fetcher import PoliteFetcher, SiteGate, is_cacheable_page
from dealsource.httpcache import CachedHttp
from dealsource.models import RawCompanyRecord
from dealsource.resolve.normalize import domain_key, name_key
from dealsource.resolve.pipeline import resolve
from dealsource.score.thesis import load_thesis
from dealsource.sources.csv_source import store_records
from dealsource.websites import finder as wf
from dealsource.websites.review import SampleRefused, refresh_sample, write_sample

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


def test_snippet_never_holds_part_of_a_phone_number_or_email_cut_at_its_edges():
    # The phone number ends just inside the 40-character window before ", GA" and the email
    # starts just inside the window after it; scrubbing after the cut would leave
    # "55-0101" and "sales@acmepr" behind.
    html = f"""<html><head><title>Acme Precision Machining</title></head><body><p>Parts.</p>
<footer>Call 478-555-0101 {"x" * 26} Macon, GA 31201 {"y" * 20} sales@acmeprecisionmachining.com
</footer></body></html>"""
    conf, ev = wf.verify_page(html, target(city=None))
    snippet = ev["location_snippet"]
    assert conf == 0.90 and ", GA 31201" in snippet
    assert "0101" not in snippet and "555" not in snippet
    assert "@" not in snippet and "sales" not in snippet


def test_page_title_is_scrubbed_before_it_is_shortened():
    title = "Acme Precision Machining " + "z" * 90 + " 478-555-0101 today"  # cut at 120: " 478-"
    html = GOOD.replace(
        "<title>Acme Precision Machining | CNC Machining in Macon</title>",
        f"<title>{title}</title>",
    )
    ev = wf.verify_page(html, target())[1]
    assert len(ev["page_title"]) <= 120 and "478" not in ev["page_title"]


def test_lowercase_state_code_is_not_evidence():
    html = "<html><head><title>Acme Precision Machining</title></head><body>go ahead, ga ga</body></html>"
    assert wf.verify_page(html, target(city=None))[0] == 0.0


# A different company whose name is close to Acme Precision Machining's.
FUZZY_TITLE = """<html><head><title>Acma Precision Machining | Home</title></head>
<body><p>Acma Precision Machining: parts since 1971.</p>
<footer>Warner Robins, GA 31088</footer></body></html>"""


def test_close_but_different_title_with_only_the_state_is_rejected():
    conf, ev = wf.verify_page(FUZZY_TITLE, target())
    assert wf.TITLE_MATCH <= ev["name_score"] < wf.EXACT_TITLE
    assert ev["name_match"] == "title_fuzzy" and ev["location_match"] == "state"
    assert conf == 0.0 and ev["rejected"] == "fuzzy title needs city"
    # No city on record: a close title can never be enough.
    assert wf.verify_page(FUZZY_TITLE, target(city=None))[0] == 0.0


def test_close_title_with_the_city_is_accepted_at_lower_confidence():
    conf, ev = wf.verify_page(FUZZY_TITLE, target(city="Warner Robins"))
    assert (conf, ev["name_match"], ev["location_match"]) == (0.85, "title_fuzzy", "city")


def test_body_words_do_not_rescue_a_close_but_different_title():
    html = FUZZY_TITLE.replace("parts since", "Acme precision machining since")
    conf, ev = wf.verify_page(html, target())
    assert ev["name_match"] == "title_fuzzy" and conf == 0.0


def test_exact_normalized_title_accepts_suffix_plural_and_abbreviation_changes():
    html = GOOD.replace("Acme Precision Machining |", "ACME Precision Machinings, Inc. |")
    conf, ev = wf.verify_page(html, target())
    assert (conf, ev["name_match"], ev["name_score"]) == (0.95, "title", 100.0)


def titled_page(title, footer="Warner Robins, GA 31088"):
    return f"<html><head><title>{title}</title></head><body><footer>{footer}</footer></body></html>"


def test_title_matching_only_without_group_is_close_not_exact():
    # A parent group and a similarly named subsidiary: name_key makes them equal, but the
    # finder doesn't treat a dropped "Group" as the same name, so the state isn't enough.
    assert name_key("KESTRELINE GROUP, INC.") == name_key("Kestreline Corp")
    group = target(name="KESTRELINE GROUP, INC.", city=None)
    conf, ev = wf.verify_page(titled_page("Kestreline Corporation"), group)
    assert (ev["name_match"], ev["name_score"], ev["location_match"]) == (
        "title_fuzzy",
        100.0,
        "state",
    )
    assert conf == 0.0 and ev["rejected"] == "fuzzy title needs city"
    # With the city on the page it passes as a close title; the reverse direction is close too.
    with_city = target(name="KESTRELINE GROUP, INC.", city="Warner Robins")
    assert wf.verify_page(titled_page("Kestreline Corporation"), with_city)[0] == 0.85
    plain = target(name="KESTRELINE LLC", city=None)
    assert (
        wf.verify_page(titled_page("Kestreline Holdings"), plain)[1]["name_match"] == "title_fuzzy"
    )


@pytest.mark.parametrize(
    ("company", "title"),
    [
        ("KESTRELINE GROUP, INC.", "Kestreline Group | Home"),
        ("KESTRELINE HOLDING CO", "Kestreline Holdings, LLC"),
        ("KESTRELINE CORP", "Kestreline, Inc."),  # legal forms still match each other
    ],
)
def test_same_group_or_holding_words_still_match_exactly(company, title):
    conf, ev = wf.verify_page(titled_page(title), target(name=company, city=None))
    assert (conf, ev["name_match"]) == (0.90, "title")


def body_page(words):
    return f"<html><head><title>Home</title></head><body><p>{words}</p><footer>Macon, GA</footer></body></html>"


@pytest.mark.parametrize(
    ("words", "accepted"),
    [
        ("Kraft Mold since 1980", True),
        ("Kraft Molds since 1980", True),  # plural of the same word
        ("Kraftwerk Molding since 1980", False),  # the words only as prefixes
        ("Hovercraft Moldy since 1980", False),
    ],
)
def test_body_name_words_must_be_whole_words_but_plurals_count(words, accepted):
    conf, ev = wf.verify_page(body_page(words), target(name="KRAFT MOLD LLC"))
    assert (conf > 0) is accepted
    assert ev["name_match"] == ("body" if accepted else None)


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


def test_sample_leaves_out_companies_from_earlier_samples(conn, tmp_path, site_server, clock):
    seed_companies(conn)
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    site_server.add(
        "greenemachinemanufacturing.com",
        "/",
        body=GOOD.replace("Acme Precision Machining", "Greene Machine & Manufacturing"),
    )
    greene = target(name="GREENE MACHINE & MANUFACTURING INC", uei="USAGA0000011", cid=2)
    wf.run_finder(
        conn,
        [target(), greene],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for("acmeprecisionmachining.com", "greenemachinemanufacturing.com"),
    )
    first = write_sample(conn, tmp_path / "first.csv", n=1, seed=1)
    second = write_sample(conn, tmp_path / "second.csv", n=30, exclude=[tmp_path / "first.csv"])
    assert (second.found, second.excluded, second.sampled) == (2, 1, 1)
    old = {r["website"] for r in csv.DictReader((tmp_path / "first.csv").open())}
    new = {r["website"] for r in csv.DictReader((tmp_path / "second.csv").open())}
    assert first.sampled == 1 and not old & new


def test_refresh_sample_rewrites_evidence_and_keeps_rows_and_verdicts(
    conn, tmp_path, site_server, clock
):
    seed_companies(conn)
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    wf.run_finder(
        conn,
        [target()],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for("acmeprecisionmachining.com"),
    )
    path = tmp_path / "s.csv"
    write_sample(conn, path)
    rows = list(csv.DictReader(path.open()))
    rows[0]["location_snippet"] = "55-0101 Macon, GA 31201 sales@acmepr"  # stored before the fix
    rows[0]["correct"] = "y"
    rows.append(dict(rows[0], company_name="GONE LLC", website="https://gone-example.com"))
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    result = refresh_sample(conn, path)
    assert (result.rows, result.changed, result.no_longer_found) == (2, 2, 1)
    out = list(csv.DictReader(path.open()))
    assert [r["company_name"] for r in out] == [r["company_name"] for r in rows]
    assert [r["correct"] for r in out] == ["y", "y"]
    assert "0101" not in out[0]["location_snippet"] and "@" not in out[0]["location_snippet"]
    assert out[1]["location_snippet"] == "(no longer found)" and out[1]["confidence"] == ""
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


# --- recheck with current rules ----------------------------------------------------------


def test_recheck_downgrades_old_accepts_offline_and_resolve_drops_them(
    conn, site_server, clock, monkeypatch
):
    seed_companies(conn)
    site_server.add("acmeprecisionmachining.com", "/", body=FUZZY_TITLE)
    site_server.add(
        "greenemachinemanufacturing.com",
        "/",
        status=301,
        headers={"location": "https://greene-mfg.com/"},
    )
    site_server.add(
        "greene-mfg.com",
        "/",
        body=GOOD.replace("Acme Precision Machining", "Greene Machine & Manufacturing"),
    )
    greene = target(name="GREENE MACHINE & MANUFACTURING INC", uei="USAGA0000011", cid=2)
    resolver = resolver_for(
        "acmeprecisionmachining.com", "greenemachinemanufacturing.com", "greene-mfg.com"
    )
    # Find both under the old rule, where a close title plus the state was enough.
    with monkeypatch.context() as m:
        m.setitem(wf.CONFIDENCE, ("title_fuzzy", "state"), 0.90)
        stats = wf.run_finder(
            conn,
            [target(), greene],
            fetcher=fetcher_for(conn, site_server, clock),
            resolver=resolver,
        )
    assert stats.statuses == {"found": 2}
    resolve(conn, source_priority=("usaspending", "websites"))
    assert (
        conn.execute("SELECT COUNT(*) FROM companies WHERE domain IS NOT NULL").fetchone()[0] == 2
    )

    requests_before = len(site_server.requests)
    result = wf.recheck(conn)
    assert len(site_server.requests) == requests_before  # cache only, redirects included
    assert result.rechecked == 2 and result.not_replayable == 0
    assert result.downgraded == {"not_found": 1} and result.unchanged == 1
    row = conn.execute(
        "SELECT status, domain, evidence_json FROM website_search WHERE search_key = 'uei:SAMGA0000001'"
    ).fetchone()
    assert (row["status"], row["domain"], row["evidence_json"]) == ("not_found", None, None)

    resolve(conn, source_priority=("usaspending", "websites"))
    domains = dict(conn.execute("SELECT canonical_name, domain FROM companies").fetchall())
    assert domains["ACME PRECISION MACHINING LLC"] is None
    assert domains["GREENE MACHINE & MANUFACTURING INC"] == "greene-mfg.com"


def test_recheck_turns_ambiguous_into_found_when_one_site_no_longer_passes(
    conn, site_server, clock, monkeypatch
):
    seed_companies(conn)
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    site_server.add("acmeprecision.com", "/", body=FUZZY_TITLE)
    resolver = resolver_for("acmeprecisionmachining.com", "acmeprecision.com")
    with monkeypatch.context() as m:
        m.setitem(wf.CONFIDENCE, ("title_fuzzy", "state"), 0.90)
        wf.run_finder(
            conn, [target()], fetcher=fetcher_for(conn, site_server, clock), resolver=resolver
        )
    assert conn.execute("SELECT status FROM website_search").fetchone()[0] == "ambiguous"
    result = wf.recheck(conn)
    assert result.upgraded == 1
    assert tuple(conn.execute("SELECT status, domain FROM website_search").fetchone()) == (
        "found",
        "acmeprecisionmachining.com",
    )


def test_recheck_leaves_rows_it_cannot_replay_from_the_cache(conn, site_server, clock):
    seed_companies(conn)
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    wf.run_finder(
        conn,
        [target()],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for("acmeprecisionmachining.com"),
    )
    with conn:
        conn.execute("DELETE FROM http_cache WHERE url LIKE '%acmeprecisionmachining.com/'")
    result = wf.recheck(conn)
    assert (result.not_replayable, result.rechecked) == (1, 0)
    assert conn.execute("SELECT status FROM website_search").fetchone()[0] == "found"


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
    assert "[2/3] checked this run 2;" in result.output and "8 worker(s)" in result.output
    assert "1 companies left; rerun the same command to continue." in result.output
    conn = db.connect(settings.db_path)
    run = json.loads(conn.execute("SELECT params_json FROM runs").fetchone()[0])
    assert (run["limit"], run["workers"], run["seed"]) == (2, 8, wf.DEFAULT_SEED)
    first_two = {r[0] for r in conn.execute("SELECT search_key FROM website_search")}
    thesis, _ = load_thesis(EXAMPLE_THESIS)
    assert first_two == {t.search_key for t in wf.targets(conn, thesis)[:2]}  # seeded order
    conn.close()
    again = runner.invoke(
        cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS), "--workers", "2"]
    )
    assert again.exit_code == 0, again.output
    assert "Resumed: 2 were already checked" in again.output
    assert "left; rerun" not in again.output
    assert "All runs so far: found 1, not_found 1, too_generic 1" in again.output
    for out in (result.output, again.output):
        assert "ACME" not in out and "acmeprecision" not in out

    runner.invoke(cli.app, ["resolve"])
    sample = runner.invoke(cli.app, ["websites", "sample"])
    assert sample.exit_code == 0, sample.output
    # USAspending companies have no city, so the match rests on state evidence (0.90)
    assert "Wrote 1 of 1 found websites" in sample.output and "0.90: 1" in sample.output
    assert "ACME" not in sample.output and (settings.review_dir / "website_sample.csv").exists()


def test_cli_find_rejects_bad_workers(settings, monkeypatch):
    monkeypatch.setenv("DEALSOURCE_USER_AGENT_CONTACT", "https://example.org/contact")
    for bad in (["--workers", "0"], ["--limit", "0"]):
        result = runner.invoke(cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS), *bad])
        assert result.exit_code == 2


def test_cli_find_requires_contact_and_not_the_split(settings, monkeypatch):
    monkeypatch.delenv("DEALSOURCE_USER_AGENT_CONTACT", raising=False)
    result = runner.invoke(cli.app, ["websites", "find", "--thesis", str(EXAMPLE_THESIS)])
    assert result.exit_code == 1 and "DEALSOURCE_USER_AGENT_CONTACT" in result.output


# --- sampling order and --limit ----------------------------------------------------------------


def synthetic_targets(sizes: dict[str, int]) -> list[wf.Target]:
    out, cid = [], 0
    for state, n in sizes.items():
        for i in range(n):
            cid += 1
            name = f"FICTIONAL WIDGET WORKS {state} {i:03d} LLC"
            out.append(wf.Target(cid, wf.search_key(None, name, state), name, None, state, None))
    return out


def test_sample_order_is_seeded_and_spread_across_states_in_proportion():
    sizes = {"GA": 60, "NC": 30, "SC": 10}
    targets = synthetic_targets(sizes)
    order = wf.sample_order(targets, seed=7)
    assert sorted(t.search_key for t in order) == sorted(t.search_key for t in targets)
    assert [t.search_key for t in wf.sample_order(reversed(targets), seed=7)] == [
        t.search_key for t in order
    ]
    assert [t.search_key for t in wf.sample_order(targets, seed=8)] != [t.search_key for t in order]
    # Every prefix holds each state's proportional share, give or take about one company
    for k in range(1, len(order) + 1):
        for state, n in sizes.items():
            got = sum(t.state == state for t in order[:k])
            assert abs(got - k * n / 100) < 2, (k, state, got)
    assert {t.state for t in order[:10]} == {"GA", "NC", "SC"}
    # Within a state the order is shuffled, not the input order
    ga = [t.name for t in order if t.state == "GA"]
    assert ga != sorted(ga)
    # Adding another state leaves the relative order within existing states alone
    more = wf.sample_order(targets + synthetic_targets({"TN": 20}), seed=7)
    assert [t.name for t in more if t.state == "GA"] == ga


def test_limit_then_full_run_checks_everyone_exactly_once(conn):
    targets = wf.sample_order(synthetic_targets({"GA": 12, "NC": 6, "SC": 2}), seed=3)
    no_dns = resolver_for()  # nothing resolves: no fetching needed

    first = wf.run_finder(conn, targets, fetcher=None, resolver=no_dns, limit=5)
    assert first.checked == 5
    checked = {r[0] for r in conn.execute("SELECT search_key FROM website_search")}
    assert checked == {t.search_key for t in targets[:5]}  # the start of the seeded order

    second = wf.run_finder(conn, targets, fetcher=None, resolver=no_dns, limit=5)
    assert (second.already_done, second.checked) == (5, 5)
    checked = {r[0] for r in conn.execute("SELECT search_key FROM website_search")}
    assert checked == {t.search_key for t in targets[:10]}

    rest = wf.run_finder(conn, targets, fetcher=None, resolver=no_dns)
    assert (rest.already_done, rest.checked) == (10, 10)
    assert conn.execute("SELECT COUNT(*) FROM website_search").fetchone()[0] == 20


# --- parallel workers ---------------------------------------------------------------------------

WORDS = ["ACME", "BRAVO", "CEDAR", "DELTA", "EAGLE", "FALCON"]


class ConcurrencyServer(SiteServer):
    """A SiteServer that holds each request briefly and records overlap per site."""

    def __init__(self, hold: float = 0.01):
        super().__init__()
        self.hold = hold
        self._lock = threading.Lock()
        self.in_flight: Counter = Counter()
        self.max_per_site: Counter = Counter()
        self.max_total = 0
        self.started: dict[str, list[float]] = defaultdict(list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        site = domain_key(request.url.host)
        with self._lock:
            self.started[request.url.host].append(time.monotonic())
            self.in_flight[site] += 1
            self.max_per_site[site] = max(self.max_per_site[site], self.in_flight[site])
            self.max_total = max(self.max_total, sum(self.in_flight.values()))
        try:
            time.sleep(self.hold)
            return super().handler(request)
        finally:
            with self._lock:
                self.in_flight[site] -= 1


def shared_site_setup(server: SiteServer) -> tuple[list[wf.Target], Callable[[str], bool]]:
    """Same-name companies in GA and NC guess the same domains, so workers compete for sites.

    Half the sites answer only on www., so a site is reached under two host names."""
    live, targets, cid = set(), [], 0
    for i, word in enumerate(WORDS):
        name = f"{word} PRECISION MACHINING LLC"
        dom = wf.candidate_domains(name)[0]
        live.add(dom)
        html = GOOD.replace("Acme", word.title())
        server.add(f"www.{dom}" if i % 2 else dom, "/", body=html)
        for state in ("GA", "NC"):
            cid += 1
            targets.append(
                wf.Target(cid, wf.search_key(None, name, state), name, None, state, None)
            )
    return wf.sample_order(targets), lambda host: host in live


def test_two_workers_never_hit_the_same_site_at_once(settings, tmp_path):
    min_delay = 0.05
    runs = {}
    for workers in (1, 4):
        server = ConcurrencyServer()
        targets, resolver = shared_site_setup(server)
        path = tmp_path / f"w{workers}.db"
        conn = db.connect(path)
        gate, conns = SiteGate(), []
        fetchers = []
        for _ in range(workers):
            wconn = db.connect(path, check_same_thread=False)
            conns.append(wconn)
            http = CachedHttp(
                wconn,
                server.client(),
                "dealsource/test (+https://example.org)",
                cacheable=is_cacheable_page,
            )
            fetchers.append(
                PoliteFetcher(http, gate=gate, clock=SystemClock(), min_delay=min_delay)
            )
        stats = wf.run_finder(conn, targets, fetchers=fetchers, resolver=resolver)
        rows = dict(
            conn.execute(
                "SELECT search_key, status || ':' || IFNULL(domain, '') FROM website_search"
            )
        )
        runs[workers] = (server, stats, rows, sum(fp.requests_made for fp in fetchers))
        for c in [conn, *conns]:
            c.close()

    seq_server, seq_stats, seq_rows, seq_requests = runs[1]
    par_server, par_stats, par_rows, par_requests = runs[4]
    assert par_server.max_total >= 2  # the workers really did run at the same time...
    assert max(par_server.max_per_site.values()) == 1  # ...but never on the same site
    # Same results, and no site got more requests than in a sequential run
    assert par_rows == seq_rows and par_stats.statuses == seq_stats.statuses == {
        "found": 6,
        "not_found": 6,
    }
    per_host = lambda s: Counter((r.url.host, r.url.path) for r in s.requests)  # noqa: E731
    assert per_host(par_server) == per_host(seq_server) and par_requests == seq_requests
    # Requests to one host stay min_delay apart (small allowance for handler entry)
    for starts in par_server.started.values():
        assert all(b - a >= min_delay * 0.9 for a, b in zip(starts, starts[1:], strict=False))


def test_site_lock_covers_www_and_bare_host():
    gate = SiteGate()
    assert gate.site_lock("https://acme.com/") is gate.site_lock("https://www.acme.com/robots.txt")
    assert gate.site_lock("https://acme.com/") is not gate.site_lock("https://bravo.com/")


def test_worker_errors_are_recorded_like_sequential_ones(conn, settings, monkeypatch):
    targets = synthetic_targets({"GA": 3, "NC": 3})

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(wf, "check_company", boom)
    stats = wf.run_finder(conn, targets, fetchers=[None, None, None], resolver=resolver_for())
    assert stats.statuses == {"error": 6} and stats.checked == 6


def test_cli_recheck_prints_counts_only(settings, site_server, clock, monkeypatch):
    site_server.add("acmeprecisionmachining.com", "/", body=GOOD)
    conn = db.connect(settings.db_path)
    seed_companies(conn)
    wf.run_finder(
        conn,
        [target()],
        fetcher=fetcher_for(conn, site_server, clock),
        resolver=resolver_for("acmeprecisionmachining.com"),
    )
    conn.close()
    monkeypatch.setattr(cli, "make_http_client", lambda: pytest.fail("recheck must not fetch"))
    result = runner.invoke(cli.app, ["websites", "recheck"])
    assert result.exit_code == 0, result.output
    assert "Rechecked 1 results offline" in result.output
    assert "found, no longer accepted: 0" in result.output
    assert "acme" not in result.output.lower()
