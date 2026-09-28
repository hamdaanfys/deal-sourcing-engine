"""Run entity resolution over all raw records and write companies to the database.

Resolution is recomputed from scratch each run (it is fast), but company IDs are kept stable:
a cluster reuses the oldest company ID any of its records already had.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from dealsource.db import utcnow
from dealsource.resolve.matcher import Clustering, MatchRecord, cluster
from dealsource.resolve.normalize import (
    domain_key,
    name_key,
    normalize_city,
    normalize_country,
    normalize_state,
)


@dataclass
class LoadedRecord:
    match: MatchRecord
    source: str
    payload: dict


def load_records(conn: sqlite3.Connection) -> list[LoadedRecord]:
    rows = conn.execute(
        "SELECT id, source, source_record_id, payload_json FROM raw_records ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        p = json.loads(row["payload_json"])
        out.append(
            LoadedRecord(
                match=MatchRecord(
                    id=row["id"],
                    ref=f"{row['source']}:{row['source_record_id']}",
                    name=p["name"],
                    key=name_key(p["name"]),
                    domain=domain_key(p.get("website")),
                    state=normalize_state(p.get("state")),
                    city=normalize_city(p.get("city")),
                    country=normalize_country(p.get("country")),
                    uei=((p.get("extra") or {}).get("uei") or None),
                ),
                source=row["source"],
                payload=p,
            )
        )
    return out


def load_overrides(
    path: Path, refs: dict[str, int]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    """Read ``merge`` / ``split`` record-ref pairs. Returns (merges, splits, unknown ref count)."""
    if not path.exists():
        return [], [], 0
    data = yaml.safe_load(path.read_text()) or {}
    unknown = 0

    def pairs(kind: str) -> list[tuple[int, int]]:
        nonlocal unknown
        out = []
        for pair in data.get(kind) or []:
            if not isinstance(pair, list | tuple) or len(pair) != 2:
                raise ValueError(f"overrides.yaml: each {kind} entry must be a pair of record refs")
            if pair[0] in refs and pair[1] in refs:
                out.append((refs[pair[0]], refs[pair[1]]))
            else:
                unknown += 1
        return out

    return pairs("merge"), pairs("split"), unknown


def canonical_fields(records: list[LoadedRecord], source_priority: tuple[str, ...]) -> dict:
    def rank(r: LoadedRecord) -> tuple:
        prio = (
            source_priority.index(r.source) if r.source in source_priority else len(source_priority)
        )
        completeness = sum(1 for v in r.payload.values() if v not in (None, "", {}, []))
        return (prio, -completeness, r.match.id)

    ordered = sorted(records, key=rank)

    def first(fn):
        return next((v for r in ordered if (v := fn(r)) not in (None, "")), None)

    located = next((r for r in ordered if r.match.state), None)
    employee_rec = next((r for r in ordered if r.payload.get("employees") is not None), None)
    naics = sorted({r.payload["naics"] for r in ordered if r.payload.get("naics")})
    return {
        "canonical_name": ordered[0].payload["name"],
        "domain": first(lambda r: r.match.domain),
        "city": located.payload.get("city") if located else None,
        "state": located.match.state if located else None,
        "country": first(lambda r: r.match.country),
        "naics_codes": ",".join(naics) or None,
        "employee_count": employee_rec.payload["employees"] if employee_rec else None,
        "employee_count_source": employee_rec.source if employee_rec else None,
        "revenue_usd_m": first(lambda r: r.payload.get("revenue_usd_m")),
    }


def persist(
    conn: sqlite3.Connection,
    loaded: list[LoadedRecord],
    result: Clustering,
    source_priority: tuple[str, ...],
) -> dict[str, int]:
    by_id = {r.match.id: r for r in loaded}
    previous = {
        row["raw_record_id"]: row["company_id"]
        for row in conn.execute("SELECT raw_record_id, company_id FROM company_records")
    }
    now = utcnow()
    claimed: set[int] = set()
    kept_ids: set[int] = set()
    stats = {"companies": 0, "new_companies": 0, "multi_record_companies": 0}

    with conn:
        conn.execute("DELETE FROM company_records")
        for members in result.clusters:
            fields = canonical_fields([by_id[i] for i in members], source_priority)
            candidates = sorted({previous[i] for i in members if i in previous} - claimed)
            if candidates:
                company_id = candidates[0]
                conn.execute(
                    """UPDATE companies SET canonical_name=?, domain=?, city=?, state=?, country=?,
                         naics_codes=?, employee_count=?, employee_count_source=?, revenue_usd_m=?,
                         updated_at=? WHERE id=?""",
                    (*fields.values(), now, company_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO companies (canonical_name, domain, city, state, country, naics_codes,
                         employee_count, employee_count_source, revenue_usd_m, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*fields.values(), now, now),
                )
                company_id = cur.lastrowid
                stats["new_companies"] += 1
            claimed.add(company_id)
            kept_ids.add(company_id)
            stats["companies"] += 1
            if len(members) > 1:
                stats["multi_record_companies"] += 1

            for rid in members:
                edges = result.edges_by_record.get(rid, [])
                best = max(edges, key=lambda d: d.strength, default=None)
                evidence = [
                    {
                        "other": by_id[d.b if d.a == rid else d.a].match.ref,
                        "method": d.method,
                        "name_similarity": round(d.score, 1),
                        "reason": d.reason,
                    }
                    for d in edges
                ]
                conn.execute(
                    "INSERT INTO company_records (raw_record_id, company_id, match_method, match_score, evidence_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        rid,
                        company_id,
                        best.method if best else "singleton",
                        round(best.score, 1) if best else None,
                        json.dumps(evidence, sort_keys=True),
                    ),
                )
        existing = {row[0] for row in conn.execute("SELECT id FROM companies")}
        for stale in existing - kept_ids:
            conn.execute("DELETE FROM companies WHERE id = ?", (stale,))
        stats["removed_companies"] = len(existing - kept_ids)
    return stats


REVIEW_COLUMNS = [
    "kind",
    "reason",
    "name_similarity",
    "ref_a",
    "name_a",
    "domain_a",
    "state_a",
    "city_a",
    "ref_b",
    "name_b",
    "domain_b",
    "state_b",
    "city_b",
]


def write_review(path: Path, loaded: list[LoadedRecord], result: Clustering) -> int:
    by_id = {r.match.id: r.match for r in loaded}
    rows = []

    def row(kind, d, reason):
        a, b = by_id[d.a], by_id[d.b]
        return [
            kind,
            reason,
            round(d.score, 1),
            a.ref,
            a.name,
            a.domain,
            a.state,
            a.city,
            b.ref,
            b.name,
            b.domain,
            b.state,
            b.city,
        ]

    for d in result.review:
        if d.decision == "merge":
            kind = "merged_on_domain"
        elif d.method == "domain_conflict":
            kind = "different_domains"
        else:
            kind = "possible_match"
        rows.append(row(kind, d, d.reason))
    for d, why in result.rejected:
        rows.append(row("merge_blocked", d, why))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(REVIEW_COLUMNS)
        w.writerows(rows)
    return len(rows)


def resolve(
    conn: sqlite3.Connection,
    *,
    source_priority: tuple[str, ...],
    overrides_path: Path | None = None,
    review_path: Path | None = None,
) -> dict[str, int]:
    loaded = load_records(conn)
    refs = {r.match.ref: r.match.id for r in loaded}
    merges, splits, unknown = ([], [], 0)
    if overrides_path is not None:
        merges, splits, unknown = load_overrides(overrides_path, refs)
    result = cluster([r.match for r in loaded], force_merge=merges, force_split=splits)
    stats = persist(conn, loaded, result, source_priority)
    stats.update(
        records=len(loaded),
        review_items=len(result.review) + len(result.rejected),
        merges_blocked=len(result.rejected),
        unknown_override_refs=unknown,
        oversized_blocks=result.oversized_blocks,
    )
    if review_path is not None:
        write_review(review_path, loaded, result)
    return stats
