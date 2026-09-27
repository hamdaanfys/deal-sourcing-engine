"""Contact-information patterns and scrubbing, shared by CSV import and website extraction."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Separators are required so bare numbers (revenue, IDs) are not mistaken for phone numbers.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)")
LINKEDIN_PERSON_RE = re.compile(r"linkedin\.com/in/", re.IGNORECASE)

REMOVED = "[removed]"


def scrub_contact_info(text: str) -> str:
    """Replace email addresses and phone numbers with a placeholder."""
    text = EMAIL_RE.sub(REMOVED, text)
    return PHONE_RE.sub(REMOVED, text)


def contains_contact_info(text: str) -> bool:
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))
