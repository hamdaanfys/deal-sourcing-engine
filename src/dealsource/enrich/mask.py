"""Mask person names in LLM output (evidence quotes, summary) as [PERSON] (DESIGN.md §8.4).

Best-effort and biased toward over-masking. The company's own name, US states and the
company's city are protected, so "Smith & Sons" or "Georgia" are never masked.
"""

from __future__ import annotations

import re

from dealsource.resolve.normalize import US_STATES

MASK_VERSION = 1
PERSON = "[PERSON]"

GIVEN_NAMES = frozenset(
    """
    aaron adam alan albert alex alexander alice allen amanda amy andrew angela ann anna anne
    anthony arthur barbara betty beverly bill billy bob bobby brandon brenda brian bruce bryan
    carl carol carolyn catherine charles charlie cheryl chris christina christine christopher
    cindy craig cynthia dan daniel danny david deborah debra dennis diana diane donald donna
    doris dorothy doug douglas earl edward elizabeth emily emma eric ernest eugene evelyn frank
    fred gary george gerald gloria grace greg gregory harold harry heather helen henry howard
    jack jacob james jane janet janice jason jean jeff jeffrey jennifer jeremy jerry jessica jim
    jimmy joan joe john johnny jonathan jose joseph joshua joyce juan judith judy julia julie
    justin karen katherine kathleen kathy keith kelly kenneth kevin kimberly larry laura
    lawrence linda lisa lori louis margaret maria marie marilyn mark martha martin mary matthew
    melissa michael michelle mike nancy nicholas nicole pamela patricia patrick paul peter
    philip phillip rachel ralph randy raymond rebecca richard rick robert roger ronald rose
    roy russell ruth ryan samuel sandra sara sarah scott sean sharon shirley stephanie stephen
    steve steven susan teresa terry thomas timothy tina todd tom tony victoria vincent walter
    wayne william willie
    """.split()
)

_NAME = r"[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.)?(?:\s+(?:[A-Z][a-zA-Z'\-]+|Jr\.?|Sr\.?|III|II))*"
_CUES = (
    r"founded by|started by|owned by|led by|run by|established by|headed by|managed by|"
    r"son of|daughter of|wife of|husband of|grandson of|granddaughter of|nephew of|"
    r"founder|co-founder|owner|president|ceo|chairman|chairwoman|principal"
)
_ROLES = r"CEO|President|Owner|Founder|Co-Founder|Chairman|Chairwoman|Principal|Vice President|COO|CFO|CTO|Director|Manager"

PATTERNS = (
    # Mr. John Smith / Dr. Smith
    re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+(?P<name>{_NAME})"),
    # founded by John Smith / CEO John Smith
    re.compile(rf"(?i:\b(?:{_CUES}))[,:]?\s+(?:and\s+)?(?P<name>{_NAME})"),
    # John Smith, President / John Smith (CEO)
    re.compile(rf"(?P<name>{_NAME})\s*[,(\-–]\s*(?:our\s+)?(?:{_ROLES})\b"),
)
_TOKEN = re.compile(r"\b[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.)?\s+[A-Z][a-zA-Z'\-]+\b")


def _protected_tokens(company_name: str, city: str | None) -> set[str]:
    words = re.findall(r"[A-Za-z']+", company_name.lower())
    places = {w for s in US_STATES for w in s.split()} | set(
        re.findall(r"[a-z]+", (city or "").lower())
    )
    return set(words) | places


def mask_names(text: str, *, company_name: str = "", city: str | None = None) -> str:
    if not text:
        return text
    protected = _protected_tokens(company_name, city)

    def masked(span: str) -> bool:
        words = [
            w for w in re.findall(r"[A-Za-z']+", span.lower()) if w not in {"jr", "sr", "ii", "iii"}
        ]
        return bool(words) and not all(w in protected for w in words)

    for pattern in PATTERNS:

        def repl(m: re.Match) -> str:
            name = m.group("name")
            if not masked(name):
                return m.group(0)
            start, end = m.span("name")
            s0 = m.start()
            return m.group(0)[: start - s0] + PERSON + m.group(0)[end - s0 :]

        text = pattern.sub(repl, text)

    def given_name(m: re.Match) -> str:
        span = m.group(0)
        first = span.split()[0].lower()
        return PERSON if first in GIVEN_NAMES and masked(span) else span

    text = _TOKEN.sub(given_name, text)
    return re.sub(r"(?:\[PERSON\](?:\s+and\s+|\s*,\s*)?)+\[PERSON\]", PERSON, text)


def mask_extraction(data: dict, *, company_name: str, city: str | None) -> dict:
    """Apply masking to the free-text fields of an Extraction dict (returns a new dict)."""
    out = {**data}
    kw = {"company_name": company_name, "city": city}
    out["summary"] = mask_names(out.get("summary") or "", **kw)
    size = {**(out.get("size_signals") or {})}
    if size.get("employee_count_quote"):
        size["employee_count_quote"] = mask_names(size["employee_count_quote"], **kw)
    out["size_signals"] = size
    out["evidence"] = [
        {**e, "quote": mask_names(e.get("quote") or "", **kw)} for e in out.get("evidence") or []
    ]
    return out
