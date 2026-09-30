"""Synthetic companies, enrichments and theses for the scoring, export and eval tests.

Every company here is fictional and every domain uses the reserved .test TLD.
"""

from __future__ import annotations

import json
import sqlite3

from dealsource.db import utcnow
from dealsource.score.thesis import Thesis

THESIS = {
    "name": "Synthetic: precision parts, Southeast",
    "sectors": {
        "naics_prefixes": ["3327", "3323"],
        "include_keywords": ["precision machining", "custom fabrication"],
        "end_markets": ["aerospace", "medical devices"],
    },
    "size": {"employees": {"min": 20, "max": 250}, "facilities": {"min": 1, "max": 8}},
    "geography": {"countries": ["US"], "states": ["GA", "NC"]},
    "ownership": {"prefer": ["founder_led", "family_owned"]},
    "exclusions": {
        "keywords": ["franchise", "staffing agency"],
        "ownership": ["pe_or_strategic_backed", "publicly_traded"],
        "domains": [],
    },
    "weights": {"sector": 0.4, "size": 0.2, "geography": 0.2, "ownership": 0.2},
    "shortlist_threshold": 60,
}


def thesis(**overrides) -> Thesis:
    data = json.loads(json.dumps(THESIS))
    for key, value in overrides.items():
        data[key] = value
    return Thesis.model_validate(data)


def extraction(**overrides) -> dict:
    data = {
        "summary": "Kestrel Ridge Machining does precision machining for aerospace customers.",
        "product_lines": ["CNC turned parts", "custom fabrication"],
        "end_markets": ["aerospace", "medical devices"],
        "business_model": "manufacturer",
        "size_signals": {
            "employee_count": 85,
            "employee_count_quote": "a team of 85 employees",
            "facility_count": 2,
            "facility_sqft_total": None,
            "founded_year": 1971,
        },
        "ownership": {
            "founder_led": "unknown",
            "family_owned": "yes",
            "generation": 2,
            "pe_or_strategic_backed": "unknown",
            "publicly_traded": "unknown",
        },
        "evidence": [
            {"claim": "family_owned", "page": "/about", "quote": "a family-owned shop since 1971"}
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


def add_company(
    conn: sqlite3.Connection,
    name: str,
    *,
    domain: str | None = None,
    city: str | None = None,
    state: str | None = "GA",
    country: str | None = "US",
    naics: str | None = "332710",
    employee_count: int | None = None,
    employee_count_source: str | None = None,
    revenue_usd_m: float | None = None,
    extraction: dict | None = None,
    status: str | None = None,
    source: str = "csv",
) -> int:
    """Insert a company (with one raw record) and, optionally, its enrichment row."""
    now = utcnow()
    with conn:
        cid = conn.execute(
            """INSERT INTO companies (canonical_name, domain, city, state, country, naics_codes,
                 employee_count, employee_count_source, revenue_usd_m, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                domain,
                city,
                state,
                country,
                naics,
                employee_count,
                employee_count_source,
                revenue_usd_m,
                now,
                now,
            ),
        ).lastrowid
        rid = conn.execute(
            """INSERT INTO raw_records (source, source_record_id, payload_json, payload_hash, ingested_at)
               VALUES (?, ?, '{}', '', ?)""",
            (source, f"synthetic-{cid}", now),
        ).lastrowid
        conn.execute(
            """INSERT INTO company_records (raw_record_id, company_id, match_method, evidence_json)
               VALUES (?, ?, 'singleton', '{}')""",
            (rid, cid),
        )
        if extraction is not None or status is not None:
            conn.execute(
                """INSERT INTO enrichments (company_id, status, extraction_json, pages_used_json, updated_at)
                   VALUES (?, ?, ?, '[]', ?)""",
                (
                    cid,
                    status or "ok",
                    json.dumps(extraction) if extraction is not None else None,
                    now,
                ),
            )
    return cid
