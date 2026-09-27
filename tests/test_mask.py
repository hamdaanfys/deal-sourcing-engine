import pytest

from dealsource.enrich.mask import PERSON, mask_extraction, mask_names


@pytest.mark.parametrize(
    ("text", "company", "expected"),
    [
        (
            "Founded in 1962 by John Smith, Acme is family-owned.",
            "Acme",
            "Founded in 1962 by [PERSON], Acme is family-owned.",
        ),
        (
            "Our President, Mary Jones, leads the team.",
            "Acme",
            "Our President, [PERSON], leads the team.",
        ),
        ("Mary Jones, President of Acme", "Acme", "[PERSON], President of Acme"),
        ("Dr. Alan Turing designed the line.", "Acme", "Dr. [PERSON] designed the line."),
        ("Founded by Robert and Linda Walsh", "Acme", "Founded by [PERSON]"),
        ("CEO Tom Harris said", "Acme", "CEO [PERSON] said"),
        ("William Carter joined in 1990", "Acme", "[PERSON] joined in 1990"),
        # never masked: company names, places, ordinary capitalized phrases
        (
            "Smith & Sons has served Georgia since 1950.",
            "Smith & Sons",
            "Smith & Sons has served Georgia since 1950.",
        ),
        (
            "Founded by Taylor Devices engineers",
            "Taylor Devices",
            "Founded by Taylor Devices engineers",
        ),
        ("Precision Machining for Aerospace", "Acme", "Precision Machining for Aerospace"),
        ("Serving New York and North Carolina", "Acme", "Serving New York and North Carolina"),
        ("A third-generation family business", "Acme", "A third-generation family business"),
    ],
)
def test_mask_names(text, company, expected):
    assert mask_names(text, company_name=company) == expected


def test_city_is_protected():
    assert mask_names(
        "Owned by Grand Rapids investors", company_name="Acme", city="Grand Rapids"
    ) == ("Owned by Grand Rapids investors")


def test_mask_extraction_covers_summary_quotes_and_employee_quote():
    data = {
        "summary": "Run by Mary Jones.",
        "size_signals": {
            "employee_count": 5,
            "employee_count_quote": "Mary Jones and her 5 employees",
        },
        "evidence": [{"claim": "founder_led", "page": "/about", "quote": "founded by John Smith"}],
        "product_lines": ["valves"],
    }
    out = mask_extraction(data, company_name="Acme", city=None)
    assert out["summary"] == f"Run by {PERSON}."
    assert out["size_signals"]["employee_count_quote"] == f"{PERSON} and her 5 employees"
    assert out["evidence"][0]["quote"] == f"founded by {PERSON}"
    assert out["product_lines"] == ["valves"]
    assert data["summary"] == "Run by Mary Jones."  # input not mutated
