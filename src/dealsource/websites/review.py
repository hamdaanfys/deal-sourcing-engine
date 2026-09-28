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


def write_sample(
    conn: sqlite3.Connection, out_path: Path, *, n: int = 30, seed: int = 20260927
) -> SampleResult:
    if out_path.exists():
        raise SampleRefused(
            f"{out_path} already exists; it is never overwritten. Move or delete it first."
        )
    rows = list(found_rows(conn))
    if not rows:
        raise SampleRefused(
            "No websites have been found yet; run `dealsource websites find` first."
        )
    picked = random.Random(seed).sample(rows, min(n, len(rows)))
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
    return SampleResult(out_path, len(rows), len(picked), dict(sorted(by_conf.items())))
