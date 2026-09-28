"""Export a sample of candidates for the analyst to label (DESIGN.md §11.0).

The file has exactly company_name, website, state and an empty decision column: nothing the
pipeline infers (scores, summaries, NAICS, sources) is shown, so labels are not influenced by
the tool. Only companies with a website, a state in the thesis and a NAICS code under the
thesis prefixes are eligible. The sample is spread across states (round-robin over seeded
shuffles) with a per-state cap.
"""

from __future__ import annotations

import csv
import math
import os
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dealsource.eval.labels import LabelsError, label_key, load_labels
from dealsource.score.thesis import Thesis

COLUMNS = ["company_name", "website", "state", "decision"]
DEFAULT_N = 200
DEFAULT_SEED = 20260927


class ExportRefused(RuntimeError):
    pass


@dataclass
class ExportResult:
    path: Path
    eligible: int
    in_thesis_states: int
    without_website: int
    naics_outside_thesis: int
    naics_unknown: int
    already_labeled: int
    sampled: int
    per_state_cap: int
    per_state: dict[str, int] = field(default_factory=dict)


def default_cap(n: int, states_with_candidates: int) -> int:
    """Even share per state, with 50% headroom so big states can fill in for small ones."""
    if states_with_candidates == 0:
        return 0
    return math.ceil(1.5 * n / states_with_candidates)


def spread_sample(by_state: dict[str, list[int]], *, n: int, cap: int, seed: int) -> list[int]:
    """Round-robin over states (alphabetical), each state's IDs shuffled with a seeded RNG,
    until n are picked or every state is exhausted or at its cap."""
    rng = random.Random(seed)
    queues = {}
    for state in sorted(by_state):
        ids = sorted(by_state[state])
        rng.shuffle(ids)
        queues[state] = ids
    picked: list[int] = []
    taken = defaultdict(int)
    while len(picked) < n:
        progressed = False
        for state in sorted(queues):
            if len(picked) >= n:
                break
            if taken[state] >= cap or not queues[state]:
                continue
            picked.append(queues[state].pop(0))
            taken[state] += 1
            progressed = True
        if not progressed:
            break
    return picked


def export_for_labeling(
    conn: sqlite3.Connection,
    thesis: Thesis,
    *,
    out_path: Path,
    labels_path: Path | None = None,
    n: int = DEFAULT_N,
    per_state_cap: int | None = None,
    seed: int = DEFAULT_SEED,
) -> ExportResult:
    if out_path.exists():
        raise ExportRefused(
            f"{out_path} already exists; it is never overwritten. Move or delete it first."
        )
    if n < 1:
        raise ValueError("n must be at least 1")

    already: set[str] = set()
    if labels_path is not None and labels_path.exists():
        try:
            already = {r.key for r in load_labels(labels_path)}
        except LabelsError as exc:
            raise ExportRefused(f"Existing labels file could not be read: {exc}") from exc

    states = set(thesis.geography.states)
    rows = conn.execute(
        "SELECT id, canonical_name, domain, state, naics_codes, country FROM companies ORDER BY id"
    ).fetchall()
    by_state: dict[str, list[int]] = defaultdict(list)
    info = {}
    counts = defaultdict(int)
    for r in rows:
        if r["state"] not in states or r["country"] not in (None, "US"):
            continue
        counts["in_states"] += 1
        if not r["domain"]:
            counts["no_website"] += 1
            continue
        if not r["naics_codes"]:
            counts["naics_unknown"] += 1  # e.g. USAspending-only records with no SAM match
            continue
        if not thesis.matches_naics(r["naics_codes"]):
            counts["naics_outside"] += 1
            continue
        if label_key(r["canonical_name"], r["domain"], r["state"]) in already:
            counts["labeled"] += 1
            continue
        by_state[r["state"]].append(r["id"])
        info[r["id"]] = r

    eligible = sum(len(v) for v in by_state.values())
    if eligible == 0:
        raise ExportRefused(
            f"No eligible companies: {counts['in_states']:,} in thesis states, of which "
            f"{counts['no_website']:,} have no website, {counts['naics_unknown']:,} have no NAICS code, "
            f"{counts['naics_outside']:,} are outside the thesis NAICS and {counts['labeled']:,} are "
            "already labeled. Nothing was written. (USAspending records get websites and NAICS "
            "codes by joining SAM.gov records; run `discover sam` too.)"
        )
    cap = per_state_cap if per_state_cap is not None else default_cap(n, len(by_state))
    picked = spread_sample(by_state, n=n, cap=cap, seed=seed)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for cid in sorted(picked, key=lambda i: (info[i]["canonical_name"].lower(), i)):
            r = info[cid]
            w.writerow([r["canonical_name"], r["domain"], r["state"], ""])

    per_state: dict[str, int] = defaultdict(int)
    for cid in picked:
        per_state[info[cid]["state"]] += 1
    return ExportResult(
        path=out_path,
        eligible=eligible,
        in_thesis_states=counts["in_states"],
        without_website=counts["no_website"],
        naics_outside_thesis=counts["naics_outside"],
        naics_unknown=counts["naics_unknown"],
        already_labeled=counts["labeled"],
        sampled=len(picked),
        per_state_cap=cap,
        per_state=dict(sorted(per_state.items())),
    )
