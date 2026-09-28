"""Contact-information patterns and scrubbing, shared by CSV import and website extraction."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Separators are required so bare numbers (revenue, IDs) are not mistaken for phone numbers.
_DIGIT_PHONE = r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
# Numbers spelled with capital letters ("1-800-FLOWERS", "800-GOT-JUNK", "(478) 555-FIXX",
# "555-FIXX"). At least one letter is required, and the separators must be "-" or "." (a space
# only after "(478)"), so all-caps prose like "100 PERCENT" is left alone. Plain digits without
# an area code ("555-1234") are not matched: they clash with ranges like "250-1000".
_VANITY_PHONE = (
    r"(?<![\w-])(?:"
    r"(?:\+?1[.-])?(?:\(\d{3}\)\s?|\d{3}[.-])(?=[\d.-]*[A-Z])[0-9A-Z]{3}[.-]?[0-9A-Z]{4}"
    r"|\d{3}[.-](?=\d*[A-Z])[0-9A-Z]{4}"
    r")(?![\w-])"
)
PHONE_RE = re.compile(f"{_DIGIT_PHONE}|{_VANITY_PHONE}")
LINKEDIN_PERSON_RE = re.compile(r"linkedin\.com/in/", re.IGNORECASE)

REMOVED = "[removed]"


def scrub_contact_info(text: str) -> str:
    """Replace email addresses and phone numbers with a placeholder."""
    text = EMAIL_RE.sub(REMOVED, text)
    return PHONE_RE.sub(REMOVED, text)


def contains_contact_info(text: str) -> bool:
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))
