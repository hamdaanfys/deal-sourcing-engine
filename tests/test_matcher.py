"""Entity-resolution rules on tricky synthetic cases."""

import pytest

from dealsource.resolve.matcher import (
    AUTO_MERGE,
    DISTINCT,
    MERGE,
    REVIEW,
    REVIEW_ONLY,
    MatchRecord,
    cluster,
    compare,
    name_similarity,
)
from dealsource.resolve.normalize import domain_key, name_key, normalize_city, normalize_state


def rec(id_, name, website=None, state=None, city=None, country=None):
    return MatchRecord(
        id=id_,
        ref=f"t:{id_}",
        name=name,
        key=name_key(name),
        domain=domain_key(website),
        state=normalize_state(state),
        city=normalize_city(city),
        country=country,
    )


def same_company(clustering, a, b):
    return any(a in c and b in c for c in clustering.clusters)


# --- The four cases asked for -------------------------------------------------------------


def test_abbreviation_and_suffix_variants_are_the_same_company():
    a = rec(1, "Acme Mfg. LLC", state="GA", city="Macon")
    b = rec(2, "ACME Manufacturing, Inc.", state="Georgia", city="Macon")
    d = compare(a, b)
    assert d.decision == MERGE and d.score == 100.0
    assert same_company(cluster([a, b]), 1, 2)


def test_similar_names_in_different_states_are_not_merged():
    a = rec(1, "Southern Precision Machining", state="GA")
    b = rec(2, "Southern Precision Machining Co.", state="AL")
    d = compare(a, b)
    assert d.score == 100.0  # identical after normalization...
    assert d.decision == REVIEW_ONLY  # ...but a different state blocks an automatic merge
    result = cluster([a, b])
    assert not same_company(result, 1, 2)
    assert len(result.review) == 1


def test_same_domain_with_different_names_is_the_same_company():
    a = rec(1, "Blue Ridge Fabrication", website="https://brf-industrial.test", state="NC")
    b = rec(2, "BRF Industrial Services", website="www.brf-industrial.test/contact", state="SC")
    d = compare(a, b)
    assert d.decision == MERGE and d.method == "domain"
    result = cluster([a, b])
    assert same_company(result, 1, 2)
    # Names this different are merged but also surfaced for a human to glance at.
    assert [(r.a, r.b) for r in result.review] == [(1, 2)]


def test_company_without_website_matches_on_name_and_location():
    with_site = rec(
        1, "Harbor Line Fabricators", website="harborline.test", state="GA", city="Savannah"
    )
    no_site = rec(2, "Harbor Line Fabricators LLC", state="GA", city="Savannah")
    assert compare(with_site, no_site).decision == MERGE
    assert same_company(cluster([with_site, no_site]), 1, 2)


def test_companies_without_websites_are_not_grouped_by_their_missing_domain():
    a = rec(1, "Harbor Line Fabricators", state="GA")
    b = rec(2, "Oakmont Valve Service", state="GA")
    result = cluster([a, b])
    assert result.clusters == [[1], [2]]


def test_company_without_website_or_location_is_only_flagged():
    a = rec(1, "Harbor Line Fabricators", website="harborline.test", state="GA")
    b = rec(2, "Harbor Line Fabricators")  # no website, no state
    d = compare(a, b)
    assert d.decision == REVIEW_ONLY and "location unknown" in d.reason
    assert not same_company(cluster([a, b]), 1, 2)


# --- Other edge cases -------------------------------------------------------------------


def test_same_name_same_location_different_domains_is_flagged_not_merged():
    # Could be one company with two domains, or two companies: a person decides.
    a = rec(1, "Summit Controls", website="summitcontrols.test", state="TN", city="Knoxville")
    b = rec(
        2, "Summit Controls LLC", website="summit-controls-tn.test", state="TN", city="Knoxville"
    )
    d = compare(a, b)
    assert d.decision == REVIEW_ONLY
    assert d.reason == "same name and location, different domains"
    result = cluster([a, b])
    assert not same_company(result, 1, 2)
    assert [(r.a, r.b, r.method) for r in result.review] == [(1, 2, "domain_conflict")]


def test_different_domains_in_different_states_are_distinct_and_not_flagged():
    a = rec(1, "Summit Controls", website="summitcontrols.test", state="TN")
    b = rec(2, "Summit Controls", website="summit-controls-oh.test", state="OH")
    assert compare(a, b).decision == DISTINCT
    result = cluster([a, b])
    assert not same_company(result, 1, 2) and result.review == []


def test_different_domains_with_different_names_are_not_flagged():
    a = rec(1, "Summit Controls", website="summitcontrols.test", state="TN", city="Knoxville")
    b = rec(2, "Summit Automation", website="summitautomation.test", state="TN", city="Knoxville")
    assert compare(a, b).decision == DISTINCT
    assert cluster([a, b]).review == []


def test_different_domains_with_unknown_location_are_not_flagged():
    a = rec(1, "Summit Controls", website="summitcontrols.test")
    b = rec(2, "Summit Controls", website="summit-controls-tn.test")
    assert compare(a, b).decision == DISTINCT


# --- Platform and shared-hosting URLs ------------------------------------------------------


def test_unrelated_companies_on_facebook_are_not_merged():
    # If facebook.com counted as a domain, these would merge on "same domain".
    a = rec(1, "Oakmont Valve Service", website="https://www.facebook.com/oakmontvalve", state="GA")
    b = rec(2, "Harbor Line Fabricators", website="facebook.com/harborlinefab", state="GA")
    assert a.domain is None and b.domain is None
    assert compare(a, b).decision == DISTINCT
    result = cluster([a, b])
    assert result.clusters == [[1], [2]] and result.review == []


@pytest.mark.parametrize(
    "platform_url",
    [
        "https://www.linkedin.com/company/{slug}",
        "https://www.yelp.com/biz/{slug}",
        "{slug}.wixsite.com/home",
        "{slug}.godaddysites.com",
        "{slug}.business.site",
    ],
)
def test_unrelated_companies_on_the_same_platform_are_not_merged(platform_url):
    a = rec(1, "Oakmont Valve Service", website=platform_url.format(slug="oakmont"), state="GA")
    b = rec(
        2, "Harbor Line Fabricators", website=platform_url.format(slug="harborline"), state="GA"
    )
    assert (a.domain, b.domain) == (None, None)
    assert cluster([a, b]).clusters == [[1], [2]]


def test_platform_page_is_treated_as_no_website_for_matching():
    # The Facebook page must not block a match with the company's real site (no domain conflict)...
    real = rec(1, "Harbor Line Fabricators", website="harborline.test", state="GA", city="Savannah")
    fb = rec(
        2,
        "Harbor Line Fabricators LLC",
        website="facebook.com/harborlinefab",
        state="GA",
        city="Savannah",
    )
    d = compare(real, fb)
    assert d.decision == MERGE and d.method == "name_location"
    # ...and it must not merge on its own without a location match either.
    elsewhere = rec(3, "Harbor Line Fabricators", website="facebook.com/harborline-fl", state="FL")
    assert compare(fb, elsewhere).decision == REVIEW_ONLY


def test_www_and_subdomains_count_as_the_same_domain():
    a = rec(1, "Acme Manufacturing", website="https://www.acme-mfg.test/about", state="GA")
    b = rec(2, "Acme Parts Store", website="http://shop.acme-mfg.test", state="SC")
    c = rec(3, "Acme Mfg", website="ACME-MFG.TEST", state="GA")
    assert a.domain == b.domain == c.domain == "acme-mfg.test"
    assert cluster([a, b, c]).clusters == [[1, 2, 3]]


def test_record_without_domain_cannot_bridge_two_domains():
    # 2 matches both 1 and 3 by name and location, but 1 and 3 have different domains.
    a = rec(1, "Summit Controls", website="summitcontrols.test", state="TN", city="Knoxville")
    b = rec(2, "Summit Controls Inc", state="TN", city="Knoxville")
    c = rec(3, "Summit Controls", website="summit-controls-tn.test", state="TN", city="Knoxville")
    result = cluster([a, b, c])
    assert not same_company(result, 1, 3)
    assert len(result.rejected) == 1
    assert result.rejected[0][1] == "would join different domains"


def test_typos_and_plurals_merge_in_same_location():
    assert (
        compare(
            rec(1, "Carolina Valve", state="NC"), rec(2, "Carolina Valves", state="NC")
        ).decision
        == MERGE
    )
    assert (
        compare(
            rec(1, "Acme Manufacturing", state="GA"), rec(2, "Acme Manufacturng", state="GA")
        ).decision
        == MERGE
    )
    assert (
        compare(
            rec(1, "United Metal Works", state="OH"), rec(2, "United Metalworks", state="OH")
        ).decision
        == MERGE
    )


def test_one_word_difference_is_review_not_merge():
    # 88 on the similarity scale: inside the review band, below the auto-merge threshold.
    d = compare(rec(1, "Delta Machine", state="FL"), rec(2, "Delta Marine", state="FL"))
    assert REVIEW <= d.score < AUTO_MERGE
    assert d.decision == REVIEW_ONLY


def test_clearly_different_names_are_distinct():
    assert (
        compare(rec(1, "Apex Tool", state="GA"), rec(2, "Ajax Tool", state="GA")).decision
        == DISTINCT
    )
    assert (
        compare(
            rec(1, "Southern Precision Machining", state="GA"),
            rec(2, "Northern Precision Machining", state="GA"),
        ).decision
        == DISTINCT
    )


def test_same_state_different_city_needs_review():
    d = compare(
        rec(1, "Acme Manufacturing", state="GA", city="Macon"),
        rec(2, "Acme Mfg", state="GA", city="Athens"),
    )
    assert d.decision == REVIEW_ONLY and "different city" in d.reason


def test_different_countries_conflict():
    d = compare(
        rec(1, "Acme Manufacturing", state="ON", country="CA"),
        rec(2, "Acme Manufacturing", state="ON", country="US"),
    )
    assert d.decision == REVIEW_ONLY


def test_split_override_prevents_merge():
    a = rec(1, "Acme Mfg. LLC", state="GA")
    b = rec(2, "ACME Manufacturing, Inc.", state="GA")
    assert not same_company(cluster([a, b], force_split=[(1, 2)]), 1, 2)


def test_merge_override_forces_merge_even_across_domains():
    a = rec(1, "Summit Controls", website="summitcontrols.test", state="TN")
    b = rec(2, "Summit Automation", website="summitautomation.test", state="TN")
    assert same_company(cluster([a, b], force_merge=[(1, 2)]), 1, 2)


def test_contradictory_overrides_are_rejected():
    a, b = rec(1, "A Co", state="GA"), rec(2, "B Co", state="GA")
    with pytest.raises(ValueError):
        cluster([a, b], force_merge=[(1, 2)], force_split=[(2, 1)])


def test_transitive_merge_via_shared_domain_and_name():
    a = rec(1, "Acme Mfg. LLC", website="acme-mfg.test", state="GA", city="Macon")
    b = rec(2, "Acme Manufacturing Company", website="https://acme-mfg.test/about", state="GA")
    c = rec(3, "ACME Manufacturing, Inc.", state="GA", city="Macon")
    result = cluster([a, b, c])
    assert result.clusters == [[1, 2, 3]]


def test_name_similarity_handles_spacing_and_empty():
    assert name_similarity("metal works", "metalworks") == 100.0
    assert name_similarity("", "acme") == 0.0
