import pytest

from dealsource.resolve.normalize import (
    domain_key,
    name_key,
    normalize_city,
    normalize_country,
    normalize_state,
)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Acme Mfg. LLC", "acme manufacturing"),
        ("ACME Manufacturing, Inc.", "acme manufacturing"),
        ("The Acme Tools Co.", "acme tool"),
        ("Smith & Sons, L.L.C.", "smith and son"),
        ("Smith and Sons", "smith and son"),
        ("Café Industries Group", "cafe industry"),
        ("Intl Svcs Corporation", "international service"),
        ("Holdings Inc", "holding inc"),  # nothing but suffixes: keep them rather than return ""
        ("  Peach  State Welding  ", "peach state welding"),
    ],
)
def test_name_key(raw, key):
    assert name_key(raw) == key


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.Acme-Mfg.test/about?x=1", "acme-mfg.test"),
        ("acme-mfg.test", "acme-mfg.test"),
        ("http://shop.acme.co.uk", "acme.co.uk"),
        ("acme.wixsite.com/home", "acme.wixsite.com"),  # public-suffix-list private domain
        ("acme.squarespace.com", "acme.squarespace.com"),  # our shared-hosting list
        ("https://www.facebook.com/acmemfg", None),  # generic platform
        ("jo@acme-mfg.test", None),  # an email address is not a website
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_domain_key(url, key):
    assert domain_key(url) == key


def test_location_normalization():
    assert normalize_state("Georgia") == "GA"
    assert normalize_state(" ga ") == "GA"
    assert normalize_state(None) is None
    assert normalize_city("St. Louis") == "saint louis"
    assert normalize_city("Ft Worth") == "fort worth"
    assert normalize_country("United States of America") == "US"
    assert normalize_country("usa") == "US"
