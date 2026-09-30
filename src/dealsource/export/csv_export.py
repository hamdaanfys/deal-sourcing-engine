"""Ranked, explained CSV of scored companies, plus a market-stats file alongside it (DESIGN.md §10).

The column list is fixed and has no contact columns. The export never contains label or split
information. Files are UTF-8 with a BOM so they open cleanly in Excel, and are never overwritten.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dealsource.resolve.normalize import US_STATES
from dealsource.score.scorer import latest_score_run
from dealsource.score.thesis import Thesis

COLUMNS = [
    "rank",
    "score",
    "confidence",
    "excluded",
    "company",
    "domain",
    "city",
    "state",
    "naics",
    "employees",
    "employees_source",
    "facilities",
    "revenue_usd_m",
    "business_model",
    "product_lines",
    "end_markets",
    "founder_led",
    "family_owned",
    "pe_backed",
    "reason",
    "sources",
    "company_id",
    "scored_at",
    "thesis_name",
]
MARKET_COLUMNS = [
    "naics",
    "naics_label",
    "year",
    "geo_level",
    "geo_code",
    "geo_name",
    "establishments",
    "employees",
    "annual_payroll_usd_k",
]
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_STATE_NAMES = {code: name.title() for name, code in US_STATES.items()}


class ExportRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportResult:
    path: Path
    market_path: Path | None
    rows: int
    excluded: int
    shortlisted: int
    market_rows: int
    score_run_id: str


def _open_new(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", newline="", encoding="utf-8-sig")


def _yes_no(own: dict, key: str) -> str:
    return own.get(key) or "unknown"


def export_ranked(
    conn: sqlite3.Connection, thesis: Thesis, thesis_hash: str, *, out_path: Path
) -> ExportResult:
    run_id = latest_score_run(conn, thesis_hash)
    if run_id is None:
        raise ExportRefused(
            "No scores for this thesis yet; run `dealsource score --thesis ...` with the same file first."
        )
    market_path = out_path.with_name(out_path.stem + ".market.csv")
    for p in (out_path, market_path):
        if p.exists():
            raise ExportRefused(f"{p} already exists; exports are never overwritten.")
    rows = conn.execute(
        """SELECT s.company_id, s.total, s.components_json, s.excluded, s.reason, s.confidence,
                  s.scored_at, c.canonical_name, c.domain, c.city, c.state, c.naics_codes,
                  c.revenue_usd_m, e.extraction_json, e.status,
                  (SELECT group_concat(src, '; ') FROM (
                      SELECT DISTINCT r.source AS src FROM company_records cr
                      JOIN raw_records r ON r.id = cr.raw_record_id
                      WHERE cr.company_id = c.id ORDER BY r.source)) AS sources
           FROM scores s JOIN companies c ON c.id = s.company_id
           LEFT JOIN enrichments e ON e.company_id = s.company_id
           WHERE s.run_id = ?""",
        (run_id,),
    ).fetchall()
    # Ranked companies first (score, then confidence, then id); excluded ones at the bottom.
    rows.sort(
        key=lambda r: (
            r["excluded"],
            -r["total"],
            _CONFIDENCE_ORDER.get(r["confidence"], 3),
            r["company_id"],
        )
    )
    shortlisted = excluded = 0
    with _open_new(out_path) as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        rank = 0
        for r in rows:
            comp = json.loads(r["components_json"])
            ex = (
                json.loads(r["extraction_json"])
                if r["status"] == "ok" and r["extraction_json"]
                else {}
            )
            own = ex.get("ownership") or {}
            if r["excluded"]:
                excluded += 1
                shown_rank = ""
            else:
                rank += 1
                shown_rank = rank
                shortlisted += r["total"] >= thesis.shortlist_threshold
            w.writerow(
                [
                    shown_rank,
                    f"{r['total']:.1f}",
                    r["confidence"],
                    "yes" if r["excluded"] else "no",
                    r["canonical_name"],
                    r["domain"] or "",
                    r["city"] or "",
                    r["state"] or "",
                    "; ".join(n for n in (r["naics_codes"] or "").split(",") if n),
                    "" if comp.get("employees") is None else comp["employees"],
                    comp.get("employees_source") or "",
                    "" if comp.get("facilities") is None else comp["facilities"],
                    "" if r["revenue_usd_m"] is None else r["revenue_usd_m"],
                    ex.get("business_model") or "",
                    "; ".join(ex.get("product_lines") or []),
                    "; ".join(ex.get("end_markets") or []),
                    _yes_no(own, "founder_led"),
                    _yes_no(own, "family_owned"),
                    _yes_no(own, "pe_or_strategic_backed"),
                    r["reason"],
                    r["sources"] or "",
                    r["company_id"],
                    r["scored_at"],
                    thesis.name,
                ]
            )
    market_rows = _write_market(conn, thesis, market_path)
    return ExportResult(
        path=out_path,
        market_path=market_path if market_rows else None,
        rows=len(rows),
        excluded=excluded,
        shortlisted=shortlisted,
        market_rows=market_rows,
        score_run_id=run_id,
    )


def _naics_matches(code: str, prefixes: list[str]) -> bool:
    """CBP codes can be zero-padded ('332300' for 3323), so compare without trailing zeros."""
    core = code.rstrip("0") or code
    return any(core.startswith(p) or p.startswith(core) for p in prefixes)


def _write_market(conn: sqlite3.Connection, thesis: Thesis, path: Path) -> int:
    """CBP stats for the thesis sectors and geographies (report-only; never used in scores)."""
    names = {_STATE_NAMES[s].lower() for s in thesis.geography.states if s in _STATE_NAMES}
    out = []
    for r in conn.execute(
        """SELECT naics, naics_label, year, geo_level, geo_code, geo_name, establishments, employees,
                  annual_payroll_usd_k FROM market_stats
           ORDER BY naics, year, geo_level, geo_code"""
    ):
        if not _naics_matches(r["naics"], thesis.sectors.naics_prefixes):
            continue
        geo = (r["geo_name"] or "").lower()
        if (
            r["geo_level"] != "us"
            and geo not in names
            and not any(geo.endswith(f", {n}") for n in names)
        ):
            continue
        out.append([r[c] if r[c] is not None else "" for c in MARKET_COLUMNS])
    if out:
        with _open_new(path) as f:
            w = csv.writer(f)
            w.writerow(MARKET_COLUMNS)
            w.writerows(out)
    return len(out)
