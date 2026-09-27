"""Name, domain and location normalization used for matching (and for keying labels later)."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from urllib.parse import urlsplit

import tldextract

# Dropped from the end of a name for matching only (display names keep them).
LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "lllp",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "lp",
        "llp",
        "plc",
        "pllc",
        "holdings",
        "holding",
        "group",
    }
)

ABBREVIATIONS = {
    "mfg": "manufacturing",
    "mfr": "manufacturing",
    "mfrs": "manufacturing",
    "manuf": "manufacturing",
    "intl": "international",
    "natl": "national",
    "svc": "services",
    "svcs": "services",
    "srvcs": "services",
    "bros": "brothers",
    "eng": "engineering",
    "engr": "engineering",
    "assoc": "associates",
    "equip": "equipment",
    "dist": "distribution",
    "distr": "distribution",
    "elec": "electric",
    "ctr": "center",
    "tech": "technology",
    "mech": "mechanical",
    "fab": "fabrication",
    "mfring": "manufacturing",
}

# Platforms, directories, site builders and email providers. A URL on one of these (a Facebook
# page, acme.wixsite.com, a Yelp listing) says nothing about which company it is, so the record
# is treated as having no website. Hosts in the public suffix list's private section
# (wixsite.com, github.io, myshopify.com, blogspot.com, ...) are caught separately.
PLATFORM_DOMAINS = frozenset(
    {
        # social and professional networks
        "facebook.com",
        "fb.com",
        "linkedin.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "tiktok.com",
        "pinterest.com",
        "nextdoor.com",
        # directories, reviews and data vendors
        "yelp.com",
        "bbb.org",
        "manta.com",
        "mapquest.com",
        "yellowpages.com",
        "angi.com",
        "homeadvisor.com",
        "thumbtack.com",
        "houzz.com",
        "zoominfo.com",
        "dnb.com",
        "crunchbase.com",
        "bizapedia.com",
        "opencorporates.com",
        "thomasnet.com",
        # site builders and link pages
        "google.com",
        "g.page",
        "goo.gl",
        "wix.com",
        "wixsite.com",
        "squarespace.com",
        "weebly.com",
        "business.site",
        "godaddysites.com",
        "wordpress.com",
        "webs.com",
        "site123.me",
        "jimdosite.com",
        "square.site",
        "carrd.co",
        "linktr.ee",
        # marketplaces
        "amazon.com",
        "etsy.com",
        "ebay.com",
        # email providers
        "gmail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "aol.com",
        "icloud.com",
    }
)


US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "puerto rico": "PR",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_STATE_CODES = frozenset(US_STATES.values())

# Bundled public suffix list only: no network fetch, no on-disk cache.
_extract = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _singular(token: str) -> str:
    if len(token) <= 3 or token.endswith(("ss", "us", "is")):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("s"):
        return token[:-1]
    return token


def name_tokens(name: str) -> list[str]:
    text = ascii_fold(name).lower().replace("&", " and ").replace("+", " and ")
    text = text.replace(".", "")  # "L.L.C." -> "llc", "Mfg." -> "mfg"
    return re.findall(r"[a-z0-9]+", text)


@lru_cache(maxsize=100_000)
def name_key(name: str) -> str:
    """Matching key: folded, lowercased, abbreviations expanded, legal suffixes and a leading
    'the' removed, plurals singularized. 'Acme Mfg. LLC' and 'ACME Manufacturing, Inc.' both
    become 'acme manufacturing'."""
    tokens = [ABBREVIATIONS.get(t, t) for t in name_tokens(name)]
    stripped = list(tokens)
    while stripped and stripped[-1] in LEGAL_SUFFIXES:
        stripped.pop()
    if stripped and stripped[0] == "the":
        stripped.pop(0)
    if not stripped:  # the name was nothing but suffixes; keep what we had
        stripped = tokens
    return " ".join(_singular(t) for t in stripped)


def domain_key(url: str | None) -> str | None:
    """The company's registrable domain for a URL or bare host.

    ``https://WWW.Acme.com:443/about`` and ``shop.acme.com`` both give ``acme.com``. Returns None
    for missing values, email addresses and platform/shared-hosting URLs.
    """
    if not url or not url.strip():
        return None
    raw = url.strip().lower()
    if "@" in raw and "/" not in raw:  # an email address, not a website
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = (urlsplit(raw).hostname or "").strip(".")
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = re.sub(r"^www\d*\.", "", host)
    parts = _extract(host)
    if parts.is_private:  # PSL private section: a platform handing out subdomains
        return None
    registrable = parts.top_domain_under_public_suffix
    if not registrable:
        # Suffix not on the public list (e.g. reserved test TLDs): use the last two labels.
        registrable = ".".join(host.split(".")[-2:])
    if registrable in PLATFORM_DOMAINS:
        return None
    return registrable


def normalize_state(state: str | None) -> str | None:
    if not state or not state.strip():
        return None
    s = re.sub(r"[^a-z ]", "", ascii_fold(state).lower()).strip()
    s = re.sub(r"\s+", " ", s)
    if s.upper() in _STATE_CODES:
        return s.upper()
    return US_STATES.get(s, state.strip().upper())


def normalize_city(city: str | None) -> str | None:
    if not city or not city.strip():
        return None
    c = re.sub(r"[^a-z0-9 ]", " ", ascii_fold(city).lower())
    c = re.sub(r"\bst\b", "saint", c)
    c = re.sub(r"\bft\b", "fort", c)
    return re.sub(r"\s+", " ", c).strip() or None


def normalize_country(country: str | None) -> str | None:
    if not country or not country.strip():
        return None
    c = re.sub(r"[^a-z]", "", country.lower())
    return (
        "US"
        if c in {"us", "usa", "unitedstates", "unitedstatesofamerica"}
        else country.strip().upper()
    )
