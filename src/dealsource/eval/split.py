"""One-time, stratified, grouped dev/test split of the labels (DESIGN.md §11.3).

The split is written once to a read-only manifest and never changed. Everything this module
returns for display is aggregate counts; label keys stay in the manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dealsource.db import utcnow
from dealsource.eval.labels import PASS, PURSUE, Groups, group_labels, load_labels, write_conflicts

DEFAULT_SEED = 20260927
DEFAULT_TEST_FRACTION = 0.3
DEV, TEST = "dev", "test"


class SplitRefused(RuntimeError):
    """The split was not made; the message says why and contains no company-level data."""


def n_test_for(n: int, fraction: float) -> int:
    """Test groups for a stratum of n groups: fraction x n rounded half up, with at least one
    test group and one dev group whenever the stratum has two or more groups."""
    k = math.floor(fraction * n + 0.5)
    if n >= 2:
        k = min(max(k, 1), n - 1)
    return k


def assign(decisions: dict[str, str], *, seed: int, test_fraction: float) -> dict[str, str]:
    """Stratified by decision: within each stratum the sorted keys are shuffled with a seeded
    RNG and the first n_test go to test. Sorting first makes row order irrelevant."""
    rng = random.Random(seed)
    out: dict[str, str] = {}
    for stratum in (PURSUE, PASS):
        keys = sorted(k for k, d in decisions.items() if d == stratum)
        rng.shuffle(keys)
        k = n_test_for(len(keys), test_fraction)
        out.update({key: TEST for key in keys[:k]})
        out.update({key: DEV for key in keys[k:]})
    return out


def count(assignments: dict[str, str], decisions: dict[str, str]) -> dict[str, dict[str, int]]:
    counts = {s: {PURSUE: 0, PASS: 0} for s in (DEV, TEST)}
    for key, split in assignments.items():
        counts[split][decisions[key]] += 1
    return counts


# Stages whose output could influence labels (DESIGN.md §11.3). They may only run after the
# split exists. Discovery, ingest, resolution and the labeling export show the analyst nothing
# the pipeline inferred, so they are allowed before it.
POST_SPLIT_STAGES = frozenset({"enrich", "score", "export", "run", "eval"})


def stages_run(db_path: Path) -> set[str]:
    """Stages recorded in the runs table (read-only; an absent DB means none)."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if not exists:
            return set()
        return {r[0] for r in conn.execute("SELECT DISTINCT stage FROM runs")}
    finally:
        conn.close()


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text())


@dataclass(frozen=True)
class SplitResult:
    manifest_path: Path
    rows: int
    companies: int
    counts: dict[str, dict[str, int]]


def make_split(
    *,
    labels_path: Path,
    manifest_path: Path,
    db_path: Path,
    conflicts_path: Path,
    seed: int = DEFAULT_SEED,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    column_map: dict[str, str] | None = None,
) -> SplitResult:
    if not 0 < test_fraction < 1:
        raise ValueError("test fraction must be between 0 and 1")
    if manifest_path.exists():
        m = read_manifest(manifest_path)
        raise SplitRefused(
            f"A labels split already exists ({manifest_path}, created {m.get('created_at')}); "
            "it is never redone. " + format_counts(m["counts"])
        )
    if not labels_path.exists():
        raise SplitRefused(
            f"No labels file at {labels_path}. Create it first, then run this command."
        )
    prior = stages_run(db_path)
    blocked = sorted(prior & POST_SPLIT_STAGES)
    if blocked:
        raise SplitRefused(
            f"Pipeline stages that could influence labels have already run ({', '.join(blocked)}), "
            "so the split can no longer be made blind to pipeline output. Start from a data dir "
            "where only discovery/ingest/resolve/labels export have run (DESIGN.md §11.3)."
        )

    labels_bytes = labels_path.read_bytes()
    rows = load_labels(labels_path, column_map)
    groups: Groups = group_labels(rows)
    if groups.conflicts:
        write_conflicts(conflicts_path, groups.conflicts)
        raise SplitRefused(
            f"{len(groups.conflicts)} company(ies) have conflicting decisions across rows. "
            f"Fix them in the labels file (details: {conflicts_path}) and run again. No split was made."
        )

    assignments = assign(groups.decisions, seed=seed, test_fraction=test_fraction)
    counts = count(assignments, groups.decisions)
    manifest = {
        "version": 1,
        "created_at": utcnow(),
        "seed": seed,
        "test_fraction": test_fraction,
        "stratified_by": "decision",
        "grouped_by": "label_key",
        "labels_sha256": hashlib.sha256(labels_bytes).hexdigest(),
        "labels_rows": len(rows),
        "post_split_stages_had_run": False,
        "stages_before_split": sorted(prior),
        "rule_for_new_keys": "sha256(f'{seed}:{key}') / 2**256 < test_fraction",
        "counts": counts,
        "assignments": dict(sorted(assignments.items())),
        "decisions": dict(sorted(groups.decisions.items())),
    }
    _write_once(manifest_path, json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    return SplitResult(manifest_path, len(rows), len(assignments), counts)


def _write_once(path: Path, text: str) -> None:
    """Create the file exclusively (never overwrite), then make it read-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o444)


def format_counts(counts: dict[str, dict[str, int]]) -> str:
    total_pursue = counts[DEV][PURSUE] + counts[TEST][PURSUE]
    total_pass = counts[DEV][PASS] + counts[TEST][PASS]

    def line(label: str, pursue: int, pass_: int) -> str:
        return f"  {label:<6}{pursue + pass_:>6} companies   pursue {pursue:>5}   pass {pass_:>5}"

    return "\n".join(
        [
            "",
            line("total", total_pursue, total_pass),
            line("dev", counts[DEV][PURSUE], counts[DEV][PASS]),
            line("test", counts[TEST][PURSUE], counts[TEST][PASS]),
        ]
    )
