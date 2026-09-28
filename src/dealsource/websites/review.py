"""Random sample of found websites for the analyst to hand-check before labeling.

Company-level detail goes to a private file; callers print counts only.
"""

from __future__ import annotations

import csv
import json
import os
import random
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dealsource.resolve.normalize import domain_key
from dealsource.websites.finder import found_rows

COLUMNS = [
    "company_name",
    "city",
    "state",
    "website",
    "confidence",
    "name_match",
    "location_match",
    "page_title",
    "location_snippet",
    "correct",
]


class SampleRefused(RuntimeError):
    pass


@dataclass
class SampleResult:
    path: Path
    found: int
    sampled: int
    by_confidence: dict[str, int]
    excluded: int = 0


def read_excluded(paths: list[Path]) -> tuple[set[str], set[tuple[str, str]]]:
    """Websites and (company name, state) pairs from earlier sample files, to leave out."""
    domains: set[str] = set()
    names: set[tuple[str, str]] = set()
    for path in paths:
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if dom := domain_key(r.get("website")):
                    domains.add(dom)
                if r.get("company_name"):
                    names.add((r["company_name"], r.get("state") or ""))
    return domains, names


def write_sample(
    conn: sqlite3.Connection,
    out_path: Path,
    *,
    n: int = 30,
    seed: int = 20260927,
    exclude: list[Path] | None = None,
) -> SampleResult:
    """Random sample of found websites. Companies in ``exclude`` (earlier sample files) are left
    out, so a sample drawn after a rule change was tuned on one isn't measured on it again."""
    if out_path.exists():
        raise SampleRefused(
            f"{out_path} already exists; it is never overwritten. Move or delete it first."
        )
    rows = list(found_rows(conn))
    if not rows:
        raise SampleRefused(
            "No websites have been found yet; run `dealsource websites find` first."
        )
    ex_domains, ex_names = read_excluded(exclude or [])
    eligible = [
        r
        for r in rows
        if r["domain"] not in ex_domains and (r["canonical_name"], r["state"] or "") not in ex_names
    ]
    excluded = len(rows) - len(eligible)
    picked = random.Random(seed).sample(eligible, min(n, len(eligible)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in sorted(picked, key=lambda r: (r["canonical_name"] or "").lower()):
            ev = json.loads(r["evidence_json"] or "{}")
            w.writerow(
                [
                    r["canonical_name"],
                    r["city"] or "",
                    r["state"] or "",
                    f"https://{r['domain']}",
                    r["confidence"],
                    ev.get("name_match"),
                    ev.get("location_match"),
                    ev.get("page_title"),
                    ev.get("location_snippet"),
                    "",
                ]
            )
    by_conf = Counter(f"{r['confidence']:.2f}" for r in picked)
    return SampleResult(
        out_path, len(rows), len(picked), dict(sorted(by_conf.items())), excluded=excluded
    )


EVIDENCE_COLUMNS = (
    "confidence",
    "name_match",
    "location_match",
    "page_title",
    "location_snippet",
)
NO_LONGER_FOUND = "(no longer found)"


@dataclass
class RefreshResult:
    rows: int
    changed: int
    no_longer_found: int


def refresh_sample(conn: sqlite3.Connection, path: Path) -> RefreshResult:
    """Rewrite a sample file's evidence columns from the current database, e.g. after stored
    evidence was re-scrubbed. Rows, their order and every other column (including a filled-in
    ``correct``) are kept; the file is replaced atomically and stays private (mode 600)."""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    missing = [c for c in ("company_name", "website", *EVIDENCE_COLUMNS) if c not in header]
    if missing:
        raise SampleRefused(f"{path} is not a website sample (missing {', '.join(missing)}).")
    by_name: dict[tuple[str, str], sqlite3.Row] = {}
    by_domain: dict[str, list[sqlite3.Row]] = {}
    for r in found_rows(conn):
        by_name[(r["domain"], r["canonical_name"])] = r
        by_domain.setdefault(r["domain"], []).append(r)
    changed = gone = 0
    for row in rows:
        dom = domain_key(row["website"])
        found = by_name.get((dom, row["company_name"]))
        if found is None and len(by_domain.get(dom, [])) == 1:
            found = by_domain[dom][0]
        if found is None:
            new = dict.fromkeys(EVIDENCE_COLUMNS, "")
            new["location_snippet"] = NO_LONGER_FOUND
            gone += 1
        else:
            ev = json.loads(found["evidence_json"] or "{}")
            new = {
                "confidence": str(found["confidence"]),
                "name_match": ev.get("name_match") or "",
                "location_match": ev.get("location_match") or "",
                "page_title": ev.get("page_title") or "",
                "location_snippet": ev.get("location_snippet") or "",
            }
        if any(row[c] != new[c] for c in EVIDENCE_COLUMNS):
            changed += 1
        row.update(new)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    return RefreshResult(len(rows), changed, gone)
