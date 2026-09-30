"""Evaluate a score run against one split of the analyst's labels (DESIGN.md §11.4-11.5).

- Only the requested split's labels are kept; `split` is a required argument, and only the
  `--set test --final` path passes TEST.
- Labels are matched to scored companies by domain, then by name (similarity >= 93) in the
  same state. Unmatched labels are left out of the ranking metrics and counted; if more than
  MAX_UNMATCHED of the split's labeled companies are unmatched, the evaluation refuses.
- The terminal gets aggregate metrics only. Company-level rows go to a report file under the
  private evals directory.
- The test split is evaluated once: its metrics are appended to an append-only log, and a
  second attempt reprints them instead.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dealsource.db import utcnow
from dealsource.eval.labels import PURSUE, LabelRow, group_labels, load_labels, write_conflicts
from dealsource.eval.metrics import Item, Metrics, compute, format_metrics, ranked, shortlisted
from dealsource.eval.split import DEV, TEST, read_manifest
from dealsource.resolve.matcher import name_similarity
from dealsource.resolve.normalize import domain_key, name_key, normalize_state
from dealsource.score.scorer import latest_score_run
from dealsource.score.thesis import Thesis

NAME_MATCH = 93.0
MAX_UNMATCHED = 0.05
REPORT_COLUMNS = [
    "label_key",
    "company_name",
    "domain",
    "decision",
    "matched",
    "company_id",
    "score",
    "rank",
    "shortlisted",
    "excluded",
    "exclusion_rule",
    "confidence",
    "components",
    "reason",
    "disagreement",
]
_DISAGREEMENT_ORDER = {
    "false_negative": 0,
    "false_positive": 1,
    "excluded_positive": 2,
    "unmatched": 3,
    "": 4,
}


class EvalRefused(RuntimeError):
    """The evaluation did not run; the message has no company-level data."""


def split_for_key(key: str, manifest: dict) -> str:
    """The recorded assignment, or the manifest's stable hash rule for keys added later."""
    assigned = manifest.get("assignments", {}).get(key)
    if assigned:
        return assigned
    seed = manifest.get("seed", 0)
    fraction = manifest.get("test_fraction", 0.3)
    h = int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(), 16)
    return TEST if h / 2**256 < fraction else DEV


def load_split_labels(
    labels_path: Path, manifest: dict, split: str, conflicts_path: Path
) -> dict[str, list[LabelRow]]:
    """Label groups (key -> rows) of one split. Other splits' decisions are dropped here."""
    if split not in (DEV, TEST):
        raise ValueError(f"split must be {DEV!r} or {TEST!r}")
    groups = group_labels(load_labels(labels_path))
    if groups.conflicts:
        write_conflicts(conflicts_path, groups.conflicts)
        raise EvalRefused(
            f"{len(groups.conflicts)} company(ies) have conflicting decisions across rows; fix "
            f"them in the labels file (details: {conflicts_path})."
        )
    return {
        key: rows
        for key, rows in groups.rows_per_key.items()
        if split_for_key(key, manifest) == split
    }


@dataclass(frozen=True)
class Scored:
    company_id: int
    name_key: str
    name: str
    domain: str | None
    state: str | None
    total: float
    excluded: bool
    exclusion_rule: str | None
    confidence: str
    components: str
    reason: str


def _scored(conn: sqlite3.Connection, run_id: str) -> list[Scored]:
    return [
        Scored(
            r["company_id"],
            name_key(r["canonical_name"]),
            r["canonical_name"],
            r["domain"],
            r["state"],
            r["total"],
            bool(r["excluded"]),
            r["exclusion_rule"],
            r["confidence"],
            r["components_json"],
            r["reason"],
        )
        for r in conn.execute(
            """SELECT s.company_id, s.total, s.excluded, s.exclusion_rule, s.confidence,
                      s.components_json, s.reason, c.canonical_name, c.domain, c.state
               FROM scores s JOIN companies c ON c.id = s.company_id WHERE s.run_id = ?
               ORDER BY s.company_id""",
            (run_id,),
        )
    ]


def match_labels(
    groups: dict[str, list[LabelRow]], scored: list[Scored]
) -> dict[str, tuple[Scored | None, str]]:
    """key -> (company or None, method). Domain first; then a unique best name match (>= 93)
    among companies in the label's state. A tie for best, or no state, leaves it unmatched."""
    by_domain = {s.domain: s for s in sorted(scored, key=lambda s: -s.company_id) if s.domain}
    by_state: dict[str, list[Scored]] = defaultdict(list)
    for s in scored:
        if s.state:
            by_state[s.state].append(s)
    out: dict[str, tuple[Scored | None, str]] = {}
    for key, rows in groups.items():
        hit = None
        for r in rows:
            d = domain_key(r.website)
            if d and d in by_domain:
                hit = (by_domain[d], "domain")
                break
        if hit is None:
            for r in rows:
                state = normalize_state(r.state) if r.state else None
                if not state:
                    continue
                nk = name_key(r.company_name)
                scores = [(name_similarity(nk, s.name_key), s) for s in by_state.get(state, [])]
                scores = [(v, s) for v, s in scores if v >= NAME_MATCH]
                if not scores:
                    continue
                best = max(v for v, _ in scores)
                top = [s for v, s in scores if v == best]
                if len(top) == 1:
                    hit = (top[0], "name_state")
                    break
        out[key] = hit or (None, "unmatched")
    return out


@dataclass
class EvalResult:
    eval_id: str
    split: str
    labeled: int  # companies (label groups) in the split
    matched: int
    unmatched: int
    positives_unmatched: int
    metrics: Metrics
    summary_lines: list[str]
    report_path: Path
    score_run_id: str


def _disagreement(positive: bool, item: Item | None, threshold: float) -> str:
    if item is None:
        return "unmatched"
    if positive and item.excluded:
        return "excluded_positive"
    s = shortlisted(item, threshold)
    if positive and not s:
        return "false_negative"
    if not positive and s:
        return "false_positive"
    return ""


def summary_lines(
    split: str, labeled: int, matched: int, unmatched: int, positives_unmatched: int, m: Metrics
) -> list[str]:
    pct = 100 * matched / labeled if labeled else 0.0
    lines = [
        f"split: {split}",
        f"labeled companies: {labeled}; matched to scored companies: {matched} ({pct:.1f}%)",
        f"UNMATCHED: {unmatched} ({positives_unmatched} pursue, {unmatched - positives_unmatched} pass)"
        " - left out of every metric below",
        f"metrics on {m.n} matched companies: pursue {m.positives}, pass {m.negatives}",
    ]
    return lines + format_metrics(m)


def _unmatched_path(evals_dir: Path, split: str) -> Path:
    return evals_dir / f"unmatched_{split}.csv"


def evaluate(
    conn: sqlite3.Connection,
    thesis: Thesis,
    thesis_hash: str,
    *,
    split: str,
    labels_path: Path,
    manifest_path: Path,
    evals_dir: Path,
    code_version: str | None,
    bootstrap_seed: int,
) -> EvalResult:
    run_id = latest_score_run(conn, thesis_hash)
    if run_id is None:
        raise EvalRefused(
            "No scores for this thesis yet; run `dealsource score --thesis ...` with the same file first."
        )
    if not labels_path.exists():
        raise EvalRefused(f"No labels file at {labels_path}.")
    manifest = read_manifest(manifest_path)
    groups = load_split_labels(labels_path, manifest, split, evals_dir / "label_conflicts.csv")
    if not groups:
        raise EvalRefused(f"The {split} split has no labeled companies.")
    decisions = {k: rows[0].decision == PURSUE for k, rows in groups.items()}
    matches = match_labels(groups, _scored(conn, run_id))

    unmatched = sorted(k for k, (s, _) in matches.items() if s is None)
    if len(unmatched) > MAX_UNMATCHED * len(groups):
        path = _unmatched_path(evals_dir, split)
        _write_unmatched(path, unmatched, groups)
        raise EvalRefused(
            f"{len(unmatched)} of {len(groups)} labeled companies in the {split} split "
            f"({100 * len(unmatched) / len(groups):.1f}%) match no scored company; the limit is "
            f"{100 * MAX_UNMATCHED:g}%. No metrics were computed. Check them in {path} "
            "(e.g. run `resolve` and `score` again, or fix the websites in the labels file)."
        )

    items: dict[str, Item] = {}
    for key, (s, _) in matches.items():
        if s is not None:
            items[key] = Item(s.total, decisions[key], s.excluded, s.company_id, s.exclusion_rule)
    metrics = compute(
        list(items.values()), threshold=thesis.shortlist_threshold, seed=bootstrap_seed
    )
    pos_unmatched = sum(decisions[k] for k in unmatched)
    lines = summary_lines(split, len(groups), len(items), len(unmatched), pos_unmatched, metrics)

    eval_id = f"{utcnow().replace(':', '').replace('-', '')[:15]}_{uuid.uuid4().hex[:6]}"
    report_path = evals_dir / f"{eval_id}_{split}.csv"
    _write_report(report_path, lines, groups, matches, items, decisions, thesis.shortlist_threshold)
    with conn:
        conn.execute(
            """INSERT INTO eval_runs (eval_id, split, thesis_hash, score_run_id, code_version,
                 bootstrap_seed, metrics_json, report_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eval_id,
                split,
                thesis_hash,
                run_id,
                code_version,
                bootstrap_seed,
                json.dumps(
                    {**metrics.to_dict(), "labeled": len(groups), "unmatched": len(unmatched)},
                    sort_keys=True,
                ),
                str(report_path),
                utcnow(),
            ),
        )
    return EvalResult(
        eval_id,
        split,
        len(groups),
        len(items),
        len(unmatched),
        pos_unmatched,
        metrics,
        lines,
        report_path,
        run_id,
    )


def _open_private(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", newline="", encoding="utf-8")


def _write_unmatched(path: Path, keys: list[str], groups: dict[str, list[LabelRow]]) -> None:
    with _open_private(path) as f:
        w = csv.writer(f)
        w.writerow(["label_key", "company_name", "website", "state", "decision"])
        for k in keys:
            r = groups[k][0]
            w.writerow([k, r.company_name, r.website or "", r.state or "", r.decision])


def _write_report(
    path: Path,
    lines: list[str],
    groups: dict[str, list[LabelRow]],
    matches: dict[str, tuple[Scored | None, str]],
    items: dict[str, Item],
    decisions: dict[str, bool],
    threshold: float,
) -> None:
    """Company-level detail for the analyst's own review, with the aggregate block on top."""
    rank_of = {id(item): n for n, item in enumerate(ranked(list(items.values())), start=1)}
    rows = []
    for key, label_rows in groups.items():
        s, method = matches[key]
        item = items.get(key)
        positive = decisions[key]
        dis = _disagreement(positive, item, threshold)
        rows.append(
            [
                key,
                s.name if s else label_rows[0].company_name,
                (s.domain if s else domain_key(label_rows[0].website)) or "",
                "pursue" if positive else "pass",
                method if s else "no",
                s.company_id if s else "",
                f"{s.total:.1f}" if s else "",
                rank_of[id(item)] if item else "",
                ("yes" if shortlisted(item, threshold) else "no") if item else "",
                ("yes" if s.excluded else "no") if s else "",
                (s.exclusion_rule or "") if s else "",
                s.confidence if s else "",
                s.components if s else "",
                s.reason if s else "",
                dis,
            ]
        )
    rows.sort(key=lambda r: (_DISAGREEMENT_ORDER[r[-1]], r[7] if r[7] != "" else 10**9, r[0]))
    with _open_private(path) as f:
        for line in lines:
            f.write(f"# {line}\n")
        w = csv.writer(f)
        w.writerow(REPORT_COLUMNS)
        w.writerows(rows)


# --- the held-out test set: evaluated once -----------------------------------------------------


def read_test_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_test_log(
    path: Path, result: EvalResult, thesis_hash: str, code_version: str | None
) -> None:
    """Append-only: opened in append mode and never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": utcnow(),
        "eval_id": result.eval_id,
        "thesis_hash": thesis_hash,
        "code_version": code_version,
        "score_run_id": result.score_run_id,
        "metrics": {
            **result.metrics.to_dict(),
            "labeled": result.labeled,
            "unmatched": result.unmatched,
        },
        "summary_lines": result.summary_lines,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
