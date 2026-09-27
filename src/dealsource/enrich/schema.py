"""The structured output the LLM must produce (DESIGN.md §8.3).

Deliberately has no fields for people's names, titles, emails or phone numbers, and no revenue
field (revenue only ever comes from CSV inputs).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

YesNoUnknown = Literal["yes", "no", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SizeSignals(_Strict):
    employee_count: int | None = Field(
        None, ge=0, description="Only if the text states a number of employees"
    )
    employee_count_quote: str | None = Field(
        None, description="The exact phrase stating the employee count"
    )
    facility_count: int | None = Field(
        None, ge=0, description="Number of plants, branches or service locations"
    )
    facility_sqft_total: int | None = Field(
        None, ge=0, description="Total square footage if stated"
    )
    founded_year: int | None = Field(None, ge=1700, le=2100)


class OwnershipSignals(_Strict):
    founder_led: YesNoUnknown = "unknown"
    family_owned: YesNoUnknown = "unknown"
    generation: int | None = Field(None, ge=1, le=10, description="e.g. 'third-generation' -> 3")
    pe_or_strategic_backed: YesNoUnknown = Field(
        "unknown", description="Owned by a private equity firm or part of a larger company"
    )
    publicly_traded: YesNoUnknown = "unknown"


class Evidence(_Strict):
    claim: str = Field(description="Which field this supports, e.g. 'family_owned'")
    page: str = Field(description="The page path the quote comes from, e.g. /about")
    quote: str = Field(description="A short verbatim quote from the text")


class Extraction(_Strict):
    summary: str = Field(description="At most two sentences on what the company does")
    product_lines: list[str] = Field(default_factory=list)
    end_markets: list[str] = Field(
        default_factory=list, description="Industries or customers served"
    )
    business_model: Literal[
        "manufacturer", "distributor", "services", "software", "mixed", "unknown"
    ] = "unknown"
    size_signals: SizeSignals = Field(default_factory=SizeSignals)
    ownership: OwnershipSignals = Field(default_factory=OwnershipSignals)
    evidence: list[Evidence] = Field(default_factory=list)


def extraction_schema() -> dict:
    return Extraction.model_json_schema()


def schema_hash() -> str:
    return hashlib.sha256(json.dumps(extraction_schema(), sort_keys=True).encode()).hexdigest()[:16]


MAX_LIST_ITEMS = 10
_EMPTY_STRINGS = {"", "unknown", "n/a", "na", "none", "null", "not stated", "not specified"}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _blank_to_none(value: str | None) -> str | None:
    if value is None or value.strip().lower() in _EMPTY_STRINGS:
        return None
    return value.strip()


def _clean_list(items: list[str]) -> list[str]:
    out, seen = [], set()
    for item in items:
        item = (item or "").strip()
        key = item.lower()
        if not item or key in _EMPTY_STRINGS or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:MAX_LIST_ITEMS]


def normalize_extraction(data: Extraction) -> Extraction:
    """Deterministic cleanup after validation: small models fill optional fields with filler."""
    size = data.size_signals.model_copy()
    size.employee_count_quote = _blank_to_none(size.employee_count_quote)
    if size.employee_count is None:
        size.employee_count_quote = None
    sentences = _SENTENCE_END.split(data.summary.strip())
    return data.model_copy(
        update={
            "summary": " ".join(sentences[:2]),
            "product_lines": _clean_list(data.product_lines),
            "end_markets": _clean_list(data.end_markets),
            "size_signals": size,
            "evidence": [e for e in data.evidence if _blank_to_none(e.quote)][:6],
        }
    )


_WORKFORCE_RE = re.compile(
    r"\b(employ\w*|staff\w*|people|team members?|workforce|associates?|workers?|personnel|headcount)\b",
    re.I,
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _numbers(text: str) -> set[int]:
    return {
        int(n.replace(",", ""))
        for n in re.findall(r"\d[\d,]*", text)
        if n.replace(",", "").isdigit()
    }


def ground_extraction(data: Extraction, document: str) -> tuple[Extraction, list[str]]:
    """Drop claims the source text does not support. Returns (grounded data, what was dropped).

    - evidence quotes must appear verbatim (ignoring case and whitespace) in the text
    - an employee count needs a verbatim quote that contains that number and a workforce word
    - founded year and square footage must appear in the text as numbers
    """
    doc = _norm(document)
    doc_numbers = _numbers(document)
    dropped: list[str] = []

    evidence = [e for e in data.evidence if _norm(e.quote) in doc]
    if len(evidence) < len(data.evidence):
        dropped.append(f"evidence:{len(data.evidence) - len(evidence)}")

    size = data.size_signals.model_copy()
    if size.employee_count is not None:
        quote = size.employee_count_quote or ""
        supported = (
            quote
            and _norm(quote) in doc
            and size.employee_count in _numbers(quote)
            and _WORKFORCE_RE.search(quote)
        )
        if not supported:
            size.employee_count, size.employee_count_quote = None, None
            dropped.append("employee_count")
    if size.founded_year is not None and size.founded_year not in doc_numbers:
        size.founded_year = None
        dropped.append("founded_year")
    if size.facility_sqft_total is not None and size.facility_sqft_total not in doc_numbers:
        size.facility_sqft_total = None
        dropped.append("facility_sqft_total")

    # Evidence cited for a claim we just removed would be misleading on its own.
    removed_fields = {d for d in dropped if ":" not in d}
    evidence = [e for e in evidence if e.claim not in removed_fields]
    return data.model_copy(update={"evidence": evidence, "size_signals": size}), dropped
