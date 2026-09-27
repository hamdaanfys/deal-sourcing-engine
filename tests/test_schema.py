import json

from conftest import extraction_json

from dealsource.enrich.schema import Extraction, normalize_extraction


def parse(**overrides) -> Extraction:
    return Extraction.model_validate_json(extraction_json(**overrides))


def test_employee_quote_is_dropped_without_a_count():
    data = parse(
        size_signals={"employee_count": None, "employee_count_quote": "70+ Years Of Experience"}
    )
    assert normalize_extraction(data).size_signals.employee_count_quote is None


def test_filler_strings_become_null():
    data = parse(size_signals={"employee_count": 40, "employee_count_quote": "unknown"})
    assert normalize_extraction(data).size_signals.employee_count_quote is None


def test_lists_are_deduplicated_and_capped():
    data = parse(
        end_markets=["Medical gas", "medical gas", "", "unknown"] + [f"m{i}" for i in range(20)]
    )
    markets = normalize_extraction(data).end_markets
    assert markets[0] == "Medical gas" and markets.count("medical gas") == 0
    assert "unknown" not in markets and len(markets) == 10


def test_summary_is_trimmed_to_two_sentences():
    data = parse(summary="One thing. Two things. Three things. Four.")
    assert normalize_extraction(data).summary == "One thing. Two things."


def test_normalize_keeps_valid_data_unchanged():
    data = parse()
    assert json.loads(normalize_extraction(data).model_dump_json()) == json.loads(
        data.model_dump_json()
    )


# --- Grounding against the page text ------------------------------------------------------

from dealsource.enrich.schema import ground_extraction  # noqa: E402

DOC = """### /about
Founded in 1962, Acme Precision is family-owned.
Our team of 85 employees works from two facilities totaling 60,000 square feet.
We have completed 10,000+ projects around the world."""


def ground(**overrides):
    return ground_extraction(parse(**overrides), DOC)


def test_supported_claims_are_kept():
    data, dropped = ground(
        evidence=[
            {"claim": "family_owned", "page": "/about", "quote": "Acme Precision is family-owned."}
        ]
    )
    assert dropped == []
    assert data.size_signals.employee_count == 85 and data.size_signals.founded_year == 1962
    assert data.size_signals.facility_sqft_total == 60000 and len(data.evidence) == 1


def test_employee_count_from_a_non_workforce_number_is_dropped():
    data, dropped = ground(
        size_signals={
            "employee_count": 1000,
            "employee_count_quote": "10,000+ projects around the world",
        }
    )
    assert (
        data.size_signals.employee_count is None and data.size_signals.employee_count_quote is None
    )
    assert "employee_count" in dropped


def test_employee_count_without_the_number_in_the_quote_is_dropped():
    data, _ = ground(
        size_signals={"employee_count": 100, "employee_count_quote": "Our team of 85 employees"}
    )
    assert data.size_signals.employee_count is None


def test_employee_quote_not_in_the_text_is_dropped():
    data, _ = ground(
        size_signals={
            "employee_count": 85,
            "employee_count_quote": "roughly 85 employees nationwide",
        }
    )
    assert data.size_signals.employee_count is None


def test_invented_quotes_and_numbers_are_dropped():
    data, dropped = ground(
        evidence=[
            {
                "claim": "family_owned",
                "page": "/about",
                "quote": "ACME PRECISION IS   FAMILY-OWNED.",
            },
            {"claim": "publicly_traded", "page": "/about", "quote": "Listed on NASDAQ as ACME"},
        ],
        size_signals={"employee_count": None, "founded_year": 1971, "facility_sqft_total": 75000},
    )
    assert [e.claim for e in data.evidence] == ["family_owned"]  # case/whitespace-insensitive match
    assert data.size_signals.founded_year is None and data.size_signals.facility_sqft_total is None
    assert set(dropped) == {"evidence:1", "founded_year", "facility_sqft_total"}


def test_evidence_for_a_dropped_field_is_removed_too():
    data, _ = ground(
        size_signals={
            "employee_count": 1000,
            "employee_count_quote": "10,000+ projects around the world",
        },
        evidence=[
            {
                "claim": "employee_count",
                "page": "/about",
                "quote": "10,000+ projects around the world",
            },
            {"claim": "family_owned", "page": "/about", "quote": "Acme Precision is family-owned."},
        ],
    )
    assert [e.claim for e in data.evidence] == ["family_owned"]
