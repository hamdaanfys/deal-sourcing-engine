from conftest import SITES

from dealsource.enrich.extract import (
    PageText,
    build_document,
    extract_text,
    page_text,
    select_links,
)

INDEX = (SITES / "acme" / "index.html").read_text()
ABOUT = (SITES / "acme" / "about.html").read_text()
BASE = "https://acme-precision.test/"


def test_extract_text_drops_boilerplate_and_duplicates():
    title, description, text = extract_text(INDEX)
    assert title == "Acme Precision | CNC Machining in Macon, Georgia"
    assert description.startswith("Precision CNC machining")
    assert "tight-tolerance components" in text
    assert "should never appear" not in text and ".hero" not in text  # script, style
    assert "Our Team" not in text and "100 Industrial Way" not in text  # nav, footer
    assert text.count("Precision machining since 1962") == 1


def test_select_links_picks_business_pages_and_skips_people_and_contact_pages():
    links = select_links(INDEX, BASE, "acme-precision.test", limit=10)
    assert links == [
        "https://acme-precision.test/about-us",
        "https://acme-precision.test/products",
        "https://acme-precision.test/products/steam-boilers",  # "steam" is not "team"
        "https://acme-precision.test/capabilities",  # query and fragment stripped
    ]


def test_select_links_respects_limit():
    assert select_links(INDEX, BASE, "acme-precision.test", limit=1) == [
        "https://acme-precision.test/about-us"
    ]


def test_page_text_scrubs_emails_and_phone_numbers():
    page = page_text("https://acme-precision.test/about-us", ABOUT)
    assert "jsmith@" not in page.text and "478.555.0142" not in page.text
    assert "[removed]" in page.text
    index = page_text(BASE, INDEX)
    assert "(478) 555-0101" not in index.text and "sales@acme-precision.test" not in index.text


def test_build_document_respects_budgets():
    pages = [PageText("/", "Home", "", "a" * 500), PageText("/about", "About", "", "b" * 500)]
    doc = build_document(pages, budget_chars=300, per_page_chars=200)
    assert doc.startswith("### /\nHome\n")
    assert len(doc) <= 300 + 2
    assert "b" * 50 in doc  # the second page still gets some room


def test_governance_pages_are_skipped():
    html = '<a href="/companygovernance">Governance</a><a href="/about">About</a>'
    assert select_links(html, BASE, "acme-precision.test", 5) == [
        "https://acme-precision.test/about"
    ]
