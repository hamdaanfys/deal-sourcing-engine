"""Enrich companies: fetch their site politely, extract text, run local-LLM extraction.

Every company ends with an ``enrichments`` row and a status; no single failure (a timeout, a
blocked site, bad JSON, Ollama being down) stops the run.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass

from dealsource.clock import Clock, SystemClock
from dealsource.db import utcnow
from dealsource.enrich import fetcher as f
from dealsource.enrich.extract import build_document, page_text, select_links
from dealsource.enrich.mask import MASK_VERSION, mask_extraction
from dealsource.enrich.prompts import SYSTEM, user_prompt
from dealsource.enrich.schema import (
    Extraction,
    ground_extraction,
    normalize_extraction,
    schema_hash,
)
from dealsource.llm.base import LLMUnavailable
from dealsource.llm.cache import LLM_UNAVAILABLE, OK, LLMRunner

# Company-level statuses (beyond the LLM ones in llm.cache)
NO_WEBSITE = "no_website"
NO_TEXT = "no_text"
ERROR = "error"

DEFAULT_BUDGET_CHARS = 24_000  # ~6k tokens
DEFAULT_PER_PAGE_CHARS = 8_000


@dataclass
class EnrichConfig:
    max_pages: int = 5
    budget_chars: int = DEFAULT_BUDGET_CHARS
    per_page_chars: int = DEFAULT_PER_PAGE_CHARS


def homepage_candidates(domain: str) -> list[str]:
    return [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]


def fetch_site(
    fetcher: f.PoliteFetcher, domain: str, max_pages: int
) -> tuple[list[f.PageResult], f.PageResult]:
    """Fetch the homepage (trying common variants) and up to max_pages-1 relevant pages.

    Returns (successful pages, homepage result)."""
    home = None
    for url in homepage_candidates(domain):
        home = fetcher.fetch_page(url, domain)
        if home.status in (f.OK, f.BLOCKED_BY_ROBOTS, f.CRAWL_DELAY_TOO_LONG, f.OFFSITE_REDIRECT):
            break
    assert home is not None
    if home.status != f.OK:
        return [], home
    pages = [home]
    for url in select_links(home.html or "", home.final_url, domain, max_pages - 1):
        result = fetcher.fetch_page(url, domain)
        if result.status == f.OK and result.final_url not in {p.final_url for p in pages}:
            pages.append(result)
    return pages, home


def _write(conn: sqlite3.Connection, company_id: int, **fields) -> None:
    cols = {
        "status": None,
        "detail": None,
        "extraction_json": None,
        "pages_used_json": "[]",
        "text_sha256": None,
        "llm_cache_key": None,
        "mask_version": None,
        "fetch_ms": None,
        "llm_ms": None,
        "total_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    cols.update(fields)
    with conn:
        conn.execute(
            f"""INSERT INTO enrichments (company_id, {", ".join(cols)}, updated_at)
                VALUES (?, {", ".join("?" for _ in cols)}, ?)
                ON CONFLICT(company_id) DO UPDATE SET {", ".join(f"{c}=excluded.{c}" for c in cols)},
                  updated_at=excluded.updated_at""",
            (company_id, *cols.values(), utcnow()),
        )


def enrich_company(
    conn: sqlite3.Connection,
    company: sqlite3.Row,
    *,
    fetcher: f.PoliteFetcher,
    runner: LLMRunner,
    config: EnrichConfig,
    clock: Clock,
    llm_available: bool,
) -> str:
    cid, domain = company["id"], company["domain"]
    start = clock.monotonic()
    if not domain:
        _write(conn, cid, status=NO_WEBSITE, total_ms=0.0)
        return NO_WEBSITE

    pages, home = fetch_site(fetcher, domain, config.max_pages)
    fetch_ms = (clock.monotonic() - start) * 1000
    if not pages:
        _write(
            conn, cid, status=home.status, detail=home.detail, fetch_ms=fetch_ms, total_ms=fetch_ms
        )
        return home.status

    texts = [page_text(p.final_url, p.html or "") for p in pages]
    document = build_document(
        texts, budget_chars=config.budget_chars, per_page_chars=config.per_page_chars
    )
    pages_used = [t.path for t in texts]
    if not document.strip():
        _write(
            conn,
            cid,
            status=NO_TEXT,
            pages_used_json=json.dumps(pages_used),
            fetch_ms=fetch_ms,
            total_ms=fetch_ms,
        )
        return NO_TEXT
    text_hash = hashlib.sha256(document.encode()).hexdigest()

    if not llm_available:
        _write(
            conn,
            cid,
            status=LLM_UNAVAILABLE,
            pages_used_json=json.dumps(pages_used),
            text_sha256=text_hash,
            fetch_ms=fetch_ms,
            total_ms=fetch_ms,
        )
        return LLM_UNAVAILABLE

    outcome = runner.extract(
        company_id=cid,
        stage="enrich",
        system=SYSTEM,
        user=user_prompt(company["canonical_name"], domain, document),
        model_cls=Extraction,
        schema_hash=schema_hash(),
    )
    total_ms = (clock.monotonic() - start) * 1000
    extraction = None
    detail = outcome.error
    if outcome.status == OK and outcome.data is not None:
        cleaned = normalize_extraction(Extraction.model_validate(outcome.data))
        grounded, dropped = ground_extraction(cleaned, document)
        if dropped:
            detail = "unsupported by page text: " + ", ".join(dropped)
        extraction = mask_extraction(
            grounded.model_dump(), company_name=company["canonical_name"], city=company["city"]
        )
    _write(
        conn,
        cid,
        status=outcome.status,
        detail=detail,
        extraction_json=json.dumps(extraction, sort_keys=True) if extraction else None,
        pages_used_json=json.dumps(pages_used),
        text_sha256=text_hash,
        llm_cache_key=outcome.cache_key,
        mask_version=MASK_VERSION if extraction else None,
        fetch_ms=fetch_ms,
        llm_ms=outcome.latency_ms,
        total_ms=total_ms,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
    )
    return outcome.status


def enrich(
    conn: sqlite3.Connection,
    *,
    fetcher: f.PoliteFetcher,
    runner: LLMRunner,
    config: EnrichConfig | None = None,
    clock: Clock | None = None,
    company_ids: list[int] | None = None,
    limit: int | None = None,
) -> dict:
    config = config or EnrichConfig()
    clock = clock or SystemClock()
    sql = "SELECT id, canonical_name, domain, city, state FROM companies"
    params: list = []
    if company_ids:
        sql += f" WHERE id IN ({', '.join('?' for _ in company_ids)})"
        params = list(company_ids)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    companies = conn.execute(sql, params).fetchall()

    llm_available, llm_problem = True, None
    try:
        runner.backend.check()
    except LLMUnavailable as exc:
        llm_available, llm_problem = False, str(exc)

    statuses: Counter[str] = Counter()
    for company in companies:
        try:
            status = enrich_company(
                conn,
                company,
                fetcher=fetcher,
                runner=runner,
                config=config,
                clock=clock,
                llm_available=llm_available,
            )
        except Exception as exc:  # one bad company must never stop the run
            status = ERROR
            _write(conn, company["id"], status=ERROR, detail=type(exc).__name__)
        if status == LLM_UNAVAILABLE and llm_available:
            llm_available, llm_problem = False, "the model stopped responding during the run"
        statuses[status] += 1

    ids = [c["id"] for c in companies]
    rows = (
        conn.execute(
            f"SELECT total_ms, prompt_tokens, completion_tokens FROM enrichments WHERE company_id IN ({', '.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        if ids
        else []
    )
    times = [r["total_ms"] for r in rows if r["total_ms"] is not None]
    return {
        "companies": len(companies),
        "statuses": dict(statuses),
        "llm_problem": llm_problem,
        "requests": fetcher.requests_made,
        "fetch_cache_hits": fetcher.cache_hits,
        "median_ms": statistics.median(times) if times else None,
        "max_ms": max(times) if times else None,
        "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
    }
