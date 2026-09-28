"""Guess-and-verify website finder (DESIGN.md §6.4).

For a company without a website, build a few .com candidates from its name, keep those that
exist in DNS, fetch each homepage politely (robots.txt, rate limits, cache) and accept a domain
only if the page shows BOTH the company's name AND its city or state. Anything uncertain (name
without location, location without name, two different verified domains, parked pages,
redirects to unverifiable sites) gets no website. Each company's result is saved as soon as it
is done, so an interrupted run resumes where it stopped.

Companies can be checked by several worker threads, each with its own fetcher; the fetchers
share one SiteGate, so per-site politeness is the same as a sequential run.
"""

from __future__ import annotations

import hashlib
import json
import queue
import random
import re
import socket
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from dealsource.db import utcnow
from dealsource.enrich import fetcher as f
from dealsource.enrich.extract import extract_text
from dealsource.privacy import scrub_contact_info
from dealsource.resolve.matcher import name_similarity
from dealsource.resolve.normalize import (
    LEGAL_SUFFIXES,
    US_STATES,
    domain_key,
    name_key,
    name_tokens,
    normalize_city,
)
from dealsource.score.thesis import Thesis

# Words that don't identify a company on their own.
GENERIC_WORDS = frozenset(
    """
    industries industry services service enterprises enterprise group solutions solution systems
    system company technologies technology manufacturing products product international america
    american usa us national global general holdings holding partners associates corporation
    incorporated contracting construction supply supplies engineering consulting management
    resources sales southern southeast south north east west central united first
    """.split()
)
ABBREVIATIONS = {
    "manufacturing": "mfg",
    "company": "co",
    "international": "intl",
    "engineering": "eng",
    "technologies": "tech",
    "technology": "tech",
    "services": "svcs",
    "industries": "ind",
}
PARKED_PHRASES = (
    "domain is for sale",
    "domain may be for sale",
    "buy this domain",
    "domain for sale",
    "parked free",
    "domain parking",
    "hugedomains",
    "this domain is parked",
    "sedo domain",
)
MAX_CANDIDATES = 4
DEFAULT_SEED = 20260928

# Confidence for each accepted combination of name evidence and location evidence.
CONFIDENCE = {
    ("title", "city"): 0.95,
    ("title", "state"): 0.90,
    ("body", "city"): 0.85,
    ("body", "state"): 0.75,
}
TITLE_MATCH = 93.0

FOUND, NOT_FOUND, TOO_GENERIC, AMBIGUOUS, ERROR = (
    "found",
    "not_found",
    "too_generic",
    "ambiguous",
    "error",
)
_STATE_NAMES = {code: name for name, code in US_STATES.items()}


@dataclass(frozen=True)
class Target:
    company_id: int
    search_key: str
    name: str
    city: str | None
    state: str
    uei: str | None


def search_key(uei: str | None, name: str, state: str) -> str:
    return f"uei:{uei}" if uei else f"n:{name_key(name)}|{state.lower()}"


def distinctive_tokens(name: str) -> list[str]:
    toks = [t for t in name_tokens(name) if t not in {"and", "the"}]
    while toks and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    return [t for t in toks if t not in GENERIC_WORDS and len(t) >= 3 and not t.isdigit()]


def candidate_domains(name: str) -> list[str]:
    """Up to MAX_CANDIDATES .com guesses; [] if the name is too generic to guess safely."""
    toks = [t for t in name_tokens(name) if t not in {"and", "the"}]
    while toks and toks[-1] in LEGAL_SUFFIXES:
        toks.pop()
    if not toks or not any(len(t) >= 4 for t in distinctive_tokens(name)):
        return []
    labels = ["".join(toks), "-".join(toks), "".join(ABBREVIATIONS.get(t, t) for t in toks)]
    if len(toks) >= 3 and any(t in distinctive_tokens(name) for t in toks[:2]):
        labels.append("".join(toks[:2]))
    out: list[str] = []
    for label in labels:
        if 6 <= len(label) <= 40 and f"{label}.com" not in out:
            out.append(f"{label}.com")
    return out[:MAX_CANDIDATES]


def dns_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def _site_names(html: str, title: str) -> list[str]:
    names = [p.strip() for p in re.split(r"[|\-–—:•·]", title) if p.strip()]
    tree = HTMLParser(html)
    og = tree.css_first('meta[property="og:site_name"]')
    if og and og.attributes.get("content"):
        names.append(og.attributes["content"].strip())
    return names


def _snippet(text: str, start: int, end: int) -> str:
    s = text[max(0, start - 40) : min(len(text), end + 40)].replace("\n", " ")
    return scrub_contact_info(re.sub(r"\s+", " ", s).strip())


def verify_page(html: str, target: Target) -> tuple[float, dict]:
    """(confidence, evidence) if the page shows the company's name AND city/state; else (0, why)."""
    title, desc, text = extract_text(html, keep_footer=True)
    page = f"{title}\n{desc}\n{text}"
    low = page.lower()
    if any(p in low for p in PARKED_PHRASES):
        return 0.0, {"rejected": "parked domain"}

    key = name_key(target.name)
    title_score = max(
        (name_similarity(key, name_key(n)) for n in _site_names(html, title)), default=0.0
    )
    distinct = [t for t in key.split() if t not in GENERIC_WORDS and len(t) >= 3]
    body_hit = (
        bool(distinct)
        and (len(distinct) >= 2 or len(distinct[0]) >= 6)
        and all(re.search(rf"\b{re.escape(t)}", low) for t in distinct)
    )
    name_match = "title" if title_score >= TITLE_MATCH else "body" if body_hit else None

    loc_match, loc_span = None, None
    city = normalize_city(target.city) if target.city else None
    if city:
        m = re.search(rf"\b{re.escape(city)}\b", re.sub(r"[^a-z0-9 \n]", " ", low))
        if m:
            loc_match = "city"
            cm = re.search(rf"\b{re.escape(target.city)}\b", page, re.I)
            loc_span = (cm.start(), cm.end()) if cm else (m.start(), m.end())
    if loc_match is None:
        state_name = _STATE_NAMES.get(target.state, "")
        # State codes must be upper case ("Macon, GA", "GA 31201"); names are case-insensitive.
        patterns = [(rf",\s*{target.state}\b", 0), (rf"\b{target.state}\s+\d{{5}}\b", 0)]
        if state_name:
            patterns.append((rf"\b{re.escape(state_name)}\b", re.I))
        for pat, flags in patterns:
            m = re.search(pat, page, flags)
            if m:
                loc_match, loc_span = "state", (m.start(), m.end())
                break

    evidence = {
        "page_title": scrub_contact_info(title[:120]),
        "name_match": name_match,
        "name_score": round(title_score, 1),
        "name_tokens": distinct,
        "location_match": loc_match,
        "location_snippet": _snippet(page, *loc_span) if loc_span else None,
    }
    if not name_match or not loc_match:
        evidence["rejected"] = "no name evidence" if not name_match else "no city/state evidence"
        return 0.0, evidence
    return CONFIDENCE[(name_match, loc_match)], evidence


@dataclass
class FinderStats:
    total: int = 0
    already_done: int = 0
    checked: int = 0
    statuses: dict[str, int] = field(default_factory=dict)


def sample_order(targets_list: Iterable[Target], seed: int = DEFAULT_SEED) -> list[Target]:
    """Seeded random order, spread across states in proportion to their size.

    Each state's companies are shuffled (seeded per state, so adding a state leaves the others'
    order alone) and placed at evenly spaced positions (rank + random offset) / state size.
    Any prefix of the result holds each state's share, within about one company.
    """
    by_state: dict[str, list[Target]] = defaultdict(list)
    for t in sorted(targets_list, key=lambda t: t.search_key):
        by_state[t.state].append(t)
    keyed: list[tuple[float, str, Target]] = []
    for state, group in by_state.items():
        rng = random.Random(f"{seed}:{state}")
        rng.shuffle(group)
        offset = rng.random()
        keyed += [((i + offset) / len(group), state, t) for i, t in enumerate(group)]
    keyed.sort(key=lambda k: (k[0], k[1]))
    return [t for _, _, t in keyed]


def targets(
    conn: sqlite3.Connection, thesis: Thesis | None, seed: int = DEFAULT_SEED
) -> list[Target]:
    rows = conn.execute(
        """SELECT c.id, c.canonical_name, c.city, c.state, c.naics_codes, c.country,
                  (SELECT json_extract(r.payload_json, '$.extra.uei') FROM company_records cr
                   JOIN raw_records r ON r.id = cr.raw_record_id
                   WHERE cr.company_id = c.id AND json_extract(r.payload_json, '$.extra.uei') IS NOT NULL
                   LIMIT 1) AS uei
           FROM companies c WHERE c.domain IS NULL AND c.state IS NOT NULL ORDER BY c.id"""
    ).fetchall()
    out = []
    for r in rows:
        if r["country"] not in (None, "US"):
            continue
        if thesis is not None and (
            r["state"] not in thesis.geography.states or not thesis.matches_naics(r["naics_codes"])
        ):
            continue
        out.append(
            Target(
                r["id"],
                search_key(r["uei"], r["canonical_name"], r["state"]),
                r["canonical_name"],
                r["city"],
                r["state"],
                r["uei"],
            )
        )
    # A run that stops partway (or --limit) has covered states in proportion, and resuming
    # with the same seed continues in the same order.
    return sample_order(out, seed)


def check_company(
    target: Target, fetcher: f.PoliteFetcher, resolver: Callable[[str], bool]
) -> tuple[str, str | None, float | None, dict | None, list[dict]]:
    """Returns (status, domain, confidence, evidence, per-candidate log)."""
    cands = candidate_domains(target.name)
    if not cands:
        return TOO_GENERIC, None, None, None, []
    log: list[dict] = []
    verified: dict[str, tuple[float, dict]] = {}
    queue = list(cands)
    seen: set[str] = set()
    while queue:
        dom = queue.pop(0)
        if dom in seen:
            continue
        seen.add(dom)
        entry: dict = {"domain": dom}
        log.append(entry)
        if not resolver(dom):
            entry["dns"] = False
            continue
        entry["dns"] = True
        res = f.PageResult(dom, dom, f.CONNECTION_ERROR)
        for url in (f"https://{dom}/", f"https://www.{dom}/", f"http://{dom}/"):
            res = fetcher.fetch_page(url, dom)
            if res.status in (
                f.OK,
                f.BLOCKED_BY_ROBOTS,
                f.CRAWL_DELAY_TOO_LONG,
                f.OFFSITE_REDIRECT,
            ):
                break
        entry["fetch"] = res.status
        if res.status == f.OFFSITE_REDIRECT and res.detail:
            target_dom = domain_key(res.detail)  # None for platforms (facebook, wix, ...)
            entry["redirects_to"] = target_dom
            if target_dom and target_dom not in seen and len(seen) < MAX_CANDIDATES + 2:
                queue.append(target_dom)  # verify the destination on its own merits
            continue
        if res.status != f.OK:
            continue
        confidence, evidence = verify_page(res.html or "", target)
        entry["confidence"] = confidence
        entry["reason"] = evidence.get("rejected")
        if confidence > 0:
            verified[domain_key(res.final_url) or dom] = (confidence, evidence)
    if len(verified) > 1:
        return AMBIGUOUS, None, None, {"domains": sorted(verified)}, log
    if len(verified) == 1:
        ((dom, (confidence, evidence)),) = verified.items()
        return FOUND, dom, confidence, evidence, log
    return NOT_FOUND, None, None, None, log


def _check(target: Target, fetcher: f.PoliteFetcher, resolver: Callable[[str], bool]):
    try:
        return check_company(target, fetcher, resolver)
    except Exception as exc:  # one bad site must not stop an overnight run
        return ERROR, None, None, {"error": type(exc).__name__}, []


def _check_parallel(
    todo: list[Target], fetchers: Sequence[f.PoliteFetcher], resolver: Callable[[str], bool]
) -> Iterator[tuple[Target, tuple]]:
    """Yield (target, result) as companies finish, with one company per fetcher in flight."""
    free: queue.SimpleQueue = queue.SimpleQueue()
    for fetcher in fetchers:
        free.put(fetcher)

    def task(t: Target):
        fetcher = free.get()
        try:
            return t, _check(t, fetcher, resolver)
        finally:
            free.put(fetcher)

    it = iter(todo)
    with ThreadPoolExecutor(max_workers=len(fetchers), thread_name_prefix="websites") as ex:
        pending = {ex.submit(task, t) for _, t in zip(fetchers, it, strict=False)}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                nxt = next(it, None)
                if nxt is not None:
                    pending.add(ex.submit(task, nxt))
                yield fut.result()


def run_finder(
    conn: sqlite3.Connection,
    targets_list: list[Target],
    *,
    fetcher: f.PoliteFetcher | None = None,
    fetchers: Sequence[f.PoliteFetcher] | None = None,
    resolver: Callable[[str], bool] = dns_resolves,
    retry_errors: bool = False,
    limit: int | None = None,
    progress: Callable[[FinderStats], None] | None = None,
    progress_every: int = 10,
) -> FinderStats:
    """Check companies not yet in ``website_search``. With ``fetchers`` (one per worker, sharing
    a SiteGate) they are checked in parallel; results are saved on the calling thread."""
    done = {r[0]: r[1] for r in conn.execute("SELECT search_key, status FROM website_search")}
    stats = FinderStats(total=len(targets_list))
    todo = []
    for t in targets_list:
        status = done.get(t.search_key)
        if status is None or (retry_errors and status == ERROR):
            todo.append(t)
        else:
            stats.already_done += 1
    if limit is not None:
        todo = todo[:limit]
    pool = list(fetchers) if fetchers else [fetcher]
    if len(pool) == 1:
        results = ((t, _check(t, pool[0], resolver)) for t in todo)
    else:
        results = _check_parallel(todo, pool, resolver)
    for i, (t, (status, dom, conf, evidence, log)) in enumerate(results, start=1):
        _save(conn, t, status, dom, conf, evidence, log)
        stats.checked += 1
        stats.statuses[status] = stats.statuses.get(status, 0) + 1
        if progress and (i % progress_every == 0 or i == len(todo)):
            progress(stats)
    return stats


def _save(conn, t: Target, status, dom, conf, evidence, log) -> None:
    with conn:
        conn.execute(
            """INSERT INTO website_search (search_key, company_id, status, domain, confidence,
                 evidence_json, candidates_json, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(search_key) DO UPDATE SET company_id=excluded.company_id,
                 status=excluded.status, domain=excluded.domain, confidence=excluded.confidence,
                 evidence_json=excluded.evidence_json, candidates_json=excluded.candidates_json,
                 finished_at=excluded.finished_at""",
            (
                t.search_key,
                t.company_id,
                status,
                dom,
                conf,
                json.dumps(evidence, sort_keys=True) if evidence else None,
                json.dumps(log),
                utcnow(),
            ),
        )
        if status == FOUND:
            payload = {
                "name": t.name,
                "website": dom,
                "city": t.city,
                "state": t.state,
                "country": "US",
                "naics": None,
                "employees": None,
                "revenue_usd_m": None,
                "description": None,
                "extra": {
                    "uei": t.uei,
                    "website_confidence": conf,
                    "website_evidence": evidence,
                    "website_method": "guess_and_verify",
                },
            }
            body = json.dumps(payload, sort_keys=True)
            conn.execute(
                """INSERT INTO raw_records (source, source_record_id, payload_json, payload_hash, ingested_at)
                   VALUES ('websites', ?, ?, ?, ?)
                   ON CONFLICT(source, source_record_id) DO UPDATE SET payload_json=excluded.payload_json,
                     payload_hash=excluded.payload_hash, ingested_at=excluded.ingested_at""",
                (t.search_key, body, hashlib.sha256(body.encode()).hexdigest(), utcnow()),
            )


def found_rows(conn: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        """SELECT w.search_key, w.domain, w.confidence, w.evidence_json, c.canonical_name, c.city, c.state
           FROM website_search w LEFT JOIN companies c ON c.id = w.company_id
           WHERE w.status = 'found' ORDER BY w.search_key"""
    )
