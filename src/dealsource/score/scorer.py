"""Deterministic, rule-based scoring against a thesis (DESIGN.md §9). No LLM, and never labels.

Each company gets four components in 0-1 (sector, size, geography, ownership), a total of
100 x the weighted sum, a confidence level from how many components rest on evidence, and a
written reason. Exclusion rules come first: an excluded company scores 0 and its reason names
the rule and the evidence.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field

from dealsource.db import utcnow
from dealsource.resolve.normalize import domain_key
from dealsource.score.thesis import Range, Thesis

# Sector: a thesis NAICS code is strong evidence; keyword and end-market mentions add the rest.
NAICS_WEIGHT = 0.6
TEXT_WEIGHT = 0.4
TEXT_HITS_FOR_FULL = 2
FACILITY_CAP = 0.8  # facility count is a weaker size proxy than employees (§9.3)
UNKNOWN = 0.5
OTHER_STATE = 0.25
OWNERSHIP_NO = 0.25
CONFIDENCE_LEVELS = {4: "high", 3: "medium"}  # evidenced components -> level; fewer is "low"

_OWNERSHIP_PHRASES = {
    "founder_led": "founder-led",
    "family_owned": "family-owned",
    "pe_or_strategic_backed": "PE- or strategic-backed",
    "publicly_traded": "publicly traded",
}


@dataclass(frozen=True)
class CompanyFacts:
    company_id: int
    name: str
    domain: str | None
    state: str | None
    country: str | None
    naics_codes: list[str]
    employee_count: int | None  # from source records (e.g. a CSV)
    employee_count_source: str | None
    revenue_usd_m: float | None  # CSV-supplied only
    enrichment_status: str | None  # None: not enriched yet
    extraction: dict | None  # masked, grounded extraction (only when status is ok)


@dataclass
class ScoreResult:
    total: float
    components: dict[str, float]
    evidenced: list[str]
    excluded: bool
    exclusion_rule: str | None
    confidence: str
    reason: str
    details: dict = field(default_factory=dict)  # employees, facilities etc. for the export


def phrase_in(phrase: str, text: str) -> bool:
    """Whole-word, case-insensitive; a plural of the last word counts ('medical devices')."""
    words = phrase.lower().split()
    if not words:
        return False
    pat = r"\s+".join(re.escape(w) for w in words)
    return re.search(rf"(?<![a-z0-9]){pat}(?:s|es)?(?![a-z0-9])", text) is not None


def range_fit(x: float, r: Range) -> float:
    """1 inside the range, falling linearly to 0 at 50% beyond either bound."""
    if r.min is not None and x < r.min:
        return max(0.0, 1 - (r.min - x) / (0.5 * r.min)) if r.min > 0 else 0.0
    if r.max is not None and x > r.max:
        return max(0.0, 1 - (x - r.max) / (0.5 * r.max)) if r.max > 0 else 0.0
    return 1.0


def _range_text(r: Range) -> str:
    if r.min is not None and r.max is not None:
        return f"{r.min:g}-{r.max:g}"
    return f"at least {r.min:g}" if r.min is not None else f"at most {r.max:g}"


def _position(x: float, r: Range) -> str:
    if r.min is not None and x < r.min:
        return f"below the {_range_text(r)} range"
    if r.max is not None and x > r.max:
        return f"above the {_range_text(r)} range"
    return f"within {_range_text(r)}"


def _text_fields(ex: dict | None) -> dict[str, str]:
    """The extracted text the sector and exclusion rules search, by field (lower case)."""
    if not ex:
        return {}
    return {
        "product lines": "; ".join(ex.get("product_lines") or []).lower(),
        "end markets": "; ".join(ex.get("end_markets") or []).lower(),
        "summary": (ex.get("summary") or "").lower(),
        "evidence": " ".join(e.get("quote") or "" for e in ex.get("evidence") or []).lower(),
    }


def _quote_for(ex: dict | None, *, claim: str | None = None, phrase: str | None = None):
    """(quote, page) of the first evidence item for a claim or containing a phrase."""
    for e in (ex or {}).get("evidence") or []:
        quote = e.get("quote") or ""
        if quote and (
            (claim and e.get("claim") == claim) or (phrase and phrase_in(phrase, quote.lower()))
        ):
            return e.get("quote"), e.get("page")
    return None, None


def _cite(quote: str | None, page: str | None) -> str:
    if not quote:
        return ""
    return f" — '{quote}'" + (f" ({page})" if page else "")


def _exclusion(c: CompanyFacts, thesis: Thesis) -> tuple[str, str] | None:
    """(rule name, reason sentence) for the first exclusion that applies, else None."""
    ex = c.extraction
    blocked = {domain_key(d) or d.lower() for d in thesis.exclusions.domains}
    if c.domain and c.domain in blocked:
        return "domain", "Excluded: domain is on the thesis exclusion list."
    own = (ex or {}).get("ownership") or {}
    for sig in thesis.exclusions.ownership:
        if own.get(sig) == "yes":
            quote, page = _quote_for(ex, claim=sig)
            return (
                f"ownership:{sig}",
                f"Excluded: ownership signal {sig}{_cite(quote, page)}.",
            )
    fields = {"company name": c.name.lower(), **_text_fields(ex)}
    for kw in thesis.exclusions.keywords:
        where = [f for f, text in fields.items() if phrase_in(kw, text)]
        if where:
            quote, page = _quote_for(ex, phrase=kw)
            cite = _cite(quote, page) or f" (in {where[0]})"
            return f"keyword:{kw}", f"Excluded: keyword '{kw}'{cite}."
    return None


def _sector(c: CompanyFacts, thesis: Thesis) -> tuple[float, bool, str]:
    naics = [n for n in c.naics_codes if thesis.matches_naics([n])]
    text = " | ".join(_text_fields(c.extraction).values())
    hits = [
        p
        for p in dict.fromkeys(thesis.sectors.include_keywords + thesis.sectors.end_markets)
        if text and phrase_in(p, text)
    ]
    score = NAICS_WEIGHT * bool(naics) + TEXT_WEIGHT * min(1.0, len(hits) / TEXT_HITS_FOR_FULL)
    mentions = f"mentions {', '.join(hits[:4])}" if hits else ""
    if naics and hits:
        why = f"Sector fit: NAICS {naics[0]}; {mentions}."
    elif naics:
        why = f"Sector: NAICS {naics[0]} is in the thesis; no thesis keywords found" + (
            " in the website data." if c.extraction else " (no website data)."
        )
    elif hits:
        why = f"Sector: {mentions}; NAICS " + (
            "outside the thesis." if c.naics_codes else "unknown."
        )
    else:
        why = (
            "No sector evidence: NAICS "
            + ("outside the thesis" if c.naics_codes else "unknown")
            + " and no thesis keywords found."
        )
    return score, bool(naics or hits), why


def _size(c: CompanyFacts, thesis: Thesis) -> tuple[float, bool, str, dict]:
    size = (c.extraction or {}).get("size_signals") or {}
    details: dict = {"employees": None, "employees_source": None, "facilities": None}
    if c.employee_count is not None:
        emp, src, quote = c.employee_count, c.employee_count_source or "records", None
    elif size.get("employee_count") is not None:
        emp, src, quote = size["employee_count"], "website", size.get("employee_count_quote")
    else:
        emp, src, quote = None, None, None
    facilities = size.get("facility_count")
    details.update(employees=emp, employees_source=src, facilities=facilities)

    parts: list[str] = []
    primary = None
    r = thesis.size
    if emp is not None and r.employees:
        primary = range_fit(emp, r.employees)
        cite = f" (site: '{quote}')" if quote else f" ({src})"
        parts.append(f"~{emp:,} employees{cite}, {_position(emp, r.employees)}")
    elif facilities is not None and r.facilities:
        primary = min(FACILITY_CAP, range_fit(facilities, r.facilities))
        parts.append(
            f"{facilities} facilit{'y' if facilities == 1 else 'ies'} (no employee count), "
            f"{_position(facilities, r.facilities)}"
        )
    revenue = None
    if c.revenue_usd_m is not None and r.revenue_usd_m:
        revenue = range_fit(c.revenue_usd_m, r.revenue_usd_m)
        parts.append(
            f"${c.revenue_usd_m:,.1f}M revenue (CSV), {_position(c.revenue_usd_m, r.revenue_usd_m)}"
        )
    if primary is None and revenue is None:
        return UNKNOWN, False, "Size unknown.", details
    score = primary if revenue is None else revenue if primary is None else (primary + revenue) / 2
    return score, True, "; ".join(parts) + ".", details


def _geography(c: CompanyFacts, thesis: Thesis) -> tuple[float, bool, str]:
    if c.country and c.country not in thesis.geography.countries:
        return 0.0, True, f"Outside the thesis countries ({c.country})."
    if not c.state:
        return UNKNOWN, False, "State unknown."
    if c.state in thesis.geography.states:
        return 1.0, True, f"In-geography ({c.state})."
    return OTHER_STATE, True, f"Outside the thesis states ({c.state})."


def _ownership(c: CompanyFacts, thesis: Thesis) -> tuple[float, bool, str]:
    prefer = thesis.ownership.prefer
    own = (c.extraction or {}).get("ownership") or {}
    if not prefer:
        return UNKNOWN, False, ""
    values = {p: own.get(p, "unknown") for p in prefer}
    yes = [p for p, v in values.items() if v == "yes"]
    if yes:
        text = ", ".join(_OWNERSHIP_PHRASES[p] for p in yes)
        gen = own.get("generation")
        if "family_owned" in yes and gen:
            text += f" ({_ordinal(gen)} generation)"
        quote, page = _quote_for(c.extraction, claim=yes[0])
        return 1.0, True, text[0].upper() + text[1:] + _cite(quote, page) + "."
    if all(v == "no" for v in values.values()):
        return OWNERSHIP_NO, True, "Not " + " or ".join(_OWNERSHIP_PHRASES[p] for p in prefer) + "."
    return UNKNOWN, False, "Ownership unknown."


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def score_company(c: CompanyFacts, thesis: Thesis) -> ScoreResult:
    sector, sector_ev, sector_why = _sector(c, thesis)
    size, size_ev, size_why, details = _size(c, thesis)
    geo, geo_ev, geo_why = _geography(c, thesis)
    own, own_ev, own_why = _ownership(c, thesis)
    components = {
        "sector": round(sector, 4),
        "size": round(size, 4),
        "geography": round(geo, 4),
        "ownership": round(own, 4),
    }
    evidenced = [
        k
        for k, ok in (
            ("sector", sector_ev),
            ("size", size_ev),
            ("geography", geo_ev),
            ("ownership", own_ev),
        )
        if ok
    ]
    confidence = CONFIDENCE_LEVELS.get(len(evidenced), "low")
    if c.enrichment_status is None:
        note = "Website not analyzed yet."
    elif c.enrichment_status != "ok":
        note = f"Website not analyzed ({c.enrichment_status})."
    else:
        note = ""
    excl = _exclusion(c, thesis)
    if excl:
        rule, why = excl
        return ScoreResult(0.0, components, evidenced, True, rule, confidence, why, details)
    total = round(100 * sum(thesis.weights[k] * v for k, v in components.items()), 1)
    reason = " ".join(p for p in (sector_why, geo_why, size_why, own_why, note) if p)
    return ScoreResult(total, components, evidenced, False, None, confidence, reason, details)


def load_facts(conn: sqlite3.Connection) -> list[CompanyFacts]:
    rows = conn.execute(
        """SELECT c.id, c.canonical_name, c.domain, c.state, c.country, c.naics_codes,
                  c.employee_count, c.employee_count_source, c.revenue_usd_m,
                  e.status, e.extraction_json
           FROM companies c LEFT JOIN enrichments e ON e.company_id = c.id ORDER BY c.id"""
    ).fetchall()
    return [
        CompanyFacts(
            company_id=r["id"],
            name=r["canonical_name"],
            domain=r["domain"],
            state=r["state"],
            country=r["country"],
            naics_codes=[n for n in (r["naics_codes"] or "").split(",") if n],
            employee_count=r["employee_count"],
            employee_count_source=r["employee_count_source"],
            revenue_usd_m=r["revenue_usd_m"],
            enrichment_status=r["status"],
            extraction=json.loads(r["extraction_json"])
            if r["status"] == "ok" and r["extraction_json"]
            else None,
        )
        for r in rows
    ]


@dataclass
class ScoreRunStats:
    run_id: str
    scored: int = 0
    excluded: int = 0
    shortlisted: int = 0
    confidence: dict[str, int] = field(default_factory=dict)
    exclusions: dict[str, int] = field(default_factory=dict)  # rule names come from the thesis


def score_all(conn: sqlite3.Connection, thesis: Thesis, thesis_hash: str) -> ScoreRunStats:
    """Score every company and store the results as one new score run."""
    stats = ScoreRunStats(run_id=uuid.uuid4().hex)
    now = utcnow()
    confidence: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    rows = []
    for c in load_facts(conn):
        res = score_company(c, thesis)
        stats.scored += 1
        confidence[res.confidence] += 1
        if res.excluded:
            stats.excluded += 1
            exclusions[res.exclusion_rule or ""] += 1
        elif res.total >= thesis.shortlist_threshold:
            stats.shortlisted += 1
        components = {**res.components, "evidenced": res.evidenced, **res.details}
        rows.append(
            (
                stats.run_id,
                c.company_id,
                thesis_hash,
                res.total,
                json.dumps(components, sort_keys=True),
                int(res.excluded),
                res.exclusion_rule,
                res.reason,
                res.confidence,
                now,
            )
        )
    with conn:
        conn.executemany(
            """INSERT INTO scores (run_id, company_id, thesis_hash, total, components_json, excluded,
                 exclusion_rule, reason, confidence, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    stats.confidence = dict(sorted(confidence.items()))
    stats.exclusions = dict(sorted(exclusions.items()))
    return stats


def latest_score_run(conn: sqlite3.Connection, thesis_hash: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM scores WHERE thesis_hash = ? ORDER BY scored_at DESC, rowid DESC LIMIT 1",
        (thesis_hash,),
    ).fetchone()
    return row[0] if row else None
