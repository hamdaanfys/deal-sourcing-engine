"""Deterministic scoring (DESIGN.md §9): components, size precedence, exclusions, reasons."""

import ast
import json
from pathlib import Path

import pytest
from synthetic import add_company, extraction, thesis

from dealsource.score import scorer
from dealsource.score.scorer import CompanyFacts, phrase_in, range_fit, score_company
from dealsource.score.thesis import Range, Thesis, ThesisError, load_thesis


def facts(**overrides) -> CompanyFacts:
    data = {
        "company_id": 1,
        "name": "Kestrel Ridge Machining LLC",
        "domain": "kestrelridge.test",
        "state": "GA",
        "country": "US",
        "naics_codes": ["332710"],
        "employee_count": None,
        "employee_count_source": None,
        "revenue_usd_m": None,
        "enrichment_status": "ok",
        "extraction": extraction(),
    }
    data.update(overrides)
    return CompanyFacts(**data)


# --- building blocks ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "text", "hit"),
    [
        ("aerospace", "parts for aerospace and defense", True),
        (
            "medical devices",
            "we serve medical device makers",
            False,
        ),  # singular text, plural phrase
        ("medical device", "we serve medical devices makers", True),  # plural of the last word
        ("precision machining", "precision  machining", True),
        ("franchise", "franchisee support", False),  # a longer word is not a match
        ("fab", "fabrication", False),
    ],
)
def test_phrase_in_matches_whole_words(phrase, text, hit):
    assert phrase_in(phrase, text) is hit


@pytest.mark.parametrize(
    ("x", "expected"),
    [(20, 1.0), (250, 1.0), (100, 1.0), (15, 0.5), (10, 0.0), (5, 0.0), (312.5, 0.5), (375, 0.0)],
)
def test_range_fit_is_one_inside_and_decays_to_zero_at_half_beyond(x, expected):
    assert range_fit(x, Range(min=20, max=250)) == pytest.approx(expected)


def test_open_ranges():
    assert range_fit(10_000, Range(min=20)) == 1.0
    assert range_fit(1, Range(max=250)) == 1.0


# --- components -----------------------------------------------------------------------------------


def test_sector_naics_and_two_text_hits_is_full():
    res = score_company(facts(), thesis())
    assert res.components["sector"] == 1.0
    assert "NAICS 332710" in res.reason and "precision machining" in res.reason


def test_sector_grades():
    t = thesis()
    naics_only = facts(extraction=extraction(summary="Parts.", product_lines=[], end_markets=[]))
    assert score_company(naics_only, t).components["sector"] == pytest.approx(0.6)
    one_hit = facts(
        naics_codes=["541511"],
        extraction=extraction(summary="Parts.", product_lines=[], end_markets=["aerospace"]),
    )
    assert score_company(one_hit, t).components["sector"] == pytest.approx(0.2)
    nothing = facts(naics_codes=[], extraction=None, enrichment_status=None)
    res = score_company(nothing, t)
    assert res.components["sector"] == 0.0 and "No sector evidence" in res.reason


def test_size_prefers_record_employees_over_the_website():
    res = score_company(facts(employee_count=400, employee_count_source="csv"), thesis())
    assert res.details["employees"] == 400 and res.details["employees_source"] == "csv"
    assert res.components["size"] == pytest.approx(range_fit(400, Range(min=20, max=250)))
    assert "above the 20-250 range" in res.reason


def test_size_uses_the_website_count_with_its_quote():
    res = score_company(facts(), thesis())
    assert res.components["size"] == 1.0 and res.details["employees_source"] == "website"
    assert "'a team of 85 employees'" in res.reason


def test_facility_signal_is_capped_and_only_used_without_employees():
    ex = extraction(size_signals={"employee_count": None, "employee_count_quote": None})
    res = score_company(facts(extraction=ex), thesis())
    assert res.components["size"] == scorer.FACILITY_CAP
    assert "2 facilities (no employee count)" in res.reason


def test_revenue_only_counts_when_csv_supplied_and_is_averaged():
    t = thesis(size={"employees": {"min": 20, "max": 250}, "revenue_usd_m": {"min": 5, "max": 75}})
    both = score_company(facts(revenue_usd_m=150.0), t)  # employees fit 1.0, revenue fit 0.0
    assert both.components["size"] == pytest.approx(0.5)
    alone = facts(revenue_usd_m=10.0, extraction=None, enrichment_status=None)
    assert score_company(alone, t).components["size"] == 1.0


def test_missing_revenue_is_not_penalized_or_called_unknown():
    t = thesis(size={"employees": {"min": 20, "max": 250}, "revenue_usd_m": {"min": 5, "max": 75}})
    res = score_company(facts(), t)
    assert res.components["size"] == 1.0 and "revenue" not in res.reason.lower()


def test_no_size_signal_is_neutral_and_unknown():
    res = score_company(facts(extraction=None, enrichment_status=None), thesis())
    assert res.components["size"] == 0.5 and "Size unknown." in res.reason
    assert "size" not in res.evidenced


@pytest.mark.parametrize(
    ("state", "country", "expected"),
    [
        ("GA", "US", 1.0),
        ("VA", "US", 0.25),
        (None, "US", 0.5),
        ("GA", None, 1.0),
        (None, "CA", 0.0),
    ],
)
def test_geography(state, country, expected):
    assert (
        score_company(facts(state=state, country=country), thesis()).components["geography"]
        == expected
    )


@pytest.mark.parametrize(
    ("founder", "family", "expected"),
    [("unknown", "yes", 1.0), ("yes", "no", 1.0), ("no", "no", 0.25), ("no", "unknown", 0.5)],
)
def test_ownership(founder, family, expected):
    ex = extraction(ownership={"founder_led": founder, "family_owned": family})
    assert score_company(facts(extraction=ex), thesis()).components["ownership"] == expected


def test_ownership_reason_cites_the_evidence():
    res = score_company(facts(), thesis())
    assert "Family-owned (2nd generation) — 'a family-owned shop since 1971' (/about)" in res.reason


def test_total_is_the_weighted_sum():
    ex = extraction(ownership={"family_owned": "unknown"})
    res = score_company(facts(state="VA", extraction=ex), thesis())
    # sector 1, size 1, geography 0.25, ownership 0.5
    assert res.total == pytest.approx(100 * (0.4 + 0.2 + 0.2 * 0.25 + 0.2 * 0.5))


# --- exclusions -----------------------------------------------------------------------------------


def test_ownership_exclusion_scores_zero_and_cites_the_quote():
    ex = extraction(
        ownership={"pe_or_strategic_backed": "yes"},
        evidence=[
            {
                "claim": "pe_or_strategic_backed",
                "page": "/about",
                "quote": "a portfolio company of Harbor Crest Capital",
            }
        ],
    )
    res = score_company(facts(extraction=ex), thesis())
    assert res.excluded and res.total == 0.0
    assert res.exclusion_rule == "ownership:pe_or_strategic_backed"
    assert res.reason == (
        "Excluded: ownership signal pe_or_strategic_backed — "
        "'a portfolio company of Harbor Crest Capital' (/about)."
    )


def test_keyword_exclusion_in_extracted_text_or_name():
    ex = extraction(summary="A franchise network of repair shops.")
    res = score_company(facts(extraction=ex), thesis())
    assert res.exclusion_rule == "keyword:franchise" and "(in summary)" in res.reason
    by_name = facts(name="Bluewater Staffing Agency Inc", extraction=None, enrichment_status=None)
    assert score_company(by_name, thesis()).exclusion_rule == "keyword:staffing agency"


def test_domain_exclusion():
    t = thesis(
        exclusions={
            **thesis().exclusions.model_dump(),
            "domains": ["https://www.kestrelridge.test/"],
        }
    )
    res = score_company(facts(), t)
    assert res.exclusion_rule == "domain" and res.total == 0.0


# --- confidence and reasons -------------------------------------------------------------------


def test_confidence_counts_evidenced_components():
    assert score_company(facts(), thesis()).confidence == "high"
    ex = extraction(ownership={"family_owned": "unknown"})
    assert score_company(facts(extraction=ex), thesis()).confidence == "medium"
    bare = score_company(facts(extraction=None, enrichment_status=None), thesis())
    assert bare.confidence == "low" and bare.evidenced == ["sector", "geography"]


def test_every_company_gets_a_reason_including_enrichment_status():
    for f in (
        facts(extraction=None, enrichment_status=None),
        facts(extraction=None, enrichment_status="blocked_by_robots"),
        facts(state=None, country=None, naics_codes=[], extraction=None, enrichment_status=None),
    ):
        assert score_company(f, thesis()).reason
    blocked = score_company(facts(extraction=None, enrichment_status="blocked_by_robots"), thesis())
    assert "Website not analyzed (blocked_by_robots)." in blocked.reason


def test_scoring_is_deterministic():
    assert score_company(facts(), thesis()) == score_company(facts(), thesis())


def test_scorer_never_imports_label_or_eval_code():
    tree = ast.parse(Path(scorer.__file__).read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any(m.startswith("dealsource.eval") for m in modules)


# --- thesis validation --------------------------------------------------------------------------


def test_weights_are_normalized_and_default():
    t = thesis(weights={"sector": 2, "size": 1, "geography": 1})
    assert t.weights == {"sector": 0.5, "size": 0.25, "geography": 0.25, "ownership": 0.0}
    assert thesis(weights={}).weights == {
        "sector": 0.4,
        "size": 0.2,
        "geography": 0.2,
        "ownership": 0.2,
    }


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"weights": {"sector": 1, "revenue": 1}}, "weights: unknown_weight"),
        ({"weights": {"sector": -1, "size": 2}}, "weights: negative_weight"),
        ({"weights": {"sector": 0}}, "weights: weights_sum_zero"),
        ({"ownership": {"prefer": ["family_run"]}}, "ownership.prefer: unknown_ownership_signal"),
        ({"shortlist_threshold": 150}, "shortlist_threshold: less_than_equal"),
    ],
)
def test_invalid_thesis_is_rejected_with_the_field_named(tmp_path, override, message):
    import yaml
    from synthetic import THESIS

    path = tmp_path / "thesis.yaml"
    path.write_text(yaml.safe_dump({**THESIS, **override}))
    with pytest.raises(ThesisError, match=message):
        load_thesis(path)


def test_example_thesis_still_loads():
    t, _ = load_thesis(Path(__file__).parent.parent / "examples" / "thesis.example.yaml")
    assert isinstance(t, Thesis) and sum(t.weights.values()) == pytest.approx(1.0)


# --- scoring the database -------------------------------------------------------------------------


def test_score_all_stores_one_run_and_latest_run_picks_it(conn):
    t = thesis()
    add_company(
        conn, "Kestrel Ridge Machining", domain="kestrelridge.test", extraction=extraction()
    )
    add_company(conn, "Copperfield Tooling", naics="541511")  # not enriched
    add_company(
        conn,
        "Juniper Staffing Agency",
        extraction=extraction(summary="A staffing agency."),
    )
    first = scorer.score_all(conn, t, "hash-a")
    second = scorer.score_all(conn, t, "hash-a")
    assert (second.scored, second.excluded) == (3, 1)
    assert second.exclusions == {"keyword:staffing agency": 1}
    assert scorer.latest_score_run(conn, "hash-a") == second.run_id != first.run_id
    assert scorer.latest_score_run(conn, "other-hash") is None
    rows = conn.execute("SELECT * FROM scores WHERE run_id = ?", (second.run_id,)).fetchall()
    assert len(rows) == 3 and all(r["reason"] for r in rows)
    comp = json.loads(rows[0]["components_json"])
    assert comp["employees"] == 85 and comp["evidenced"]
