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
        # www., other subdomains, scheme, port, path, case and trailing dots all normalize away
        ("https://www.Acme-Mfg.test/about?x=1", "acme-mfg.test"),
        ("acme-mfg.test", "acme-mfg.test"),
        ("HTTPS://WWW.ACME.COM:443/", "acme.com"),
        ("www2.acme.com", "acme.com"),
        ("shop.acme.com", "acme.com"),
        ("https://portal.eu.acme.com/login", "acme.com"),
        ("acme.com.", "acme.com"),
        ("http://shop.acme.co.uk", "acme.co.uk"),  # multi-part public suffix
        # platforms and shared hosting: never a company's identifying domain
        ("https://www.facebook.com/oakmontvalve", None),
        ("m.facebook.com/harborline", None),
        ("linkedin.com/company/acme", None),
        ("https://www.yelp.com/biz/acme-macon", None),
        ("acme.wixsite.com/home", None),
        ("wixsite.com", None),
        ("acme.godaddysites.com", None),
        ("acme.business.site", None),
        ("acme.squarespace.com", None),
        ("acme.github.io", None),  # public suffix list, private section
        ("acme.myshopify.com", None),
        ("sites.google.com/view/acme", None),
        # not websites at all
        ("jo@acme-mfg.test", None),
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
