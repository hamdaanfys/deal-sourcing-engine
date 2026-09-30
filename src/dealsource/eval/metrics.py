"""Aggregate evaluation metrics with uncertainty (DESIGN.md §11.5-11.6).

Implemented directly (no numpy/scipy/scikit-learn): average precision, ROC AUC, precision@k
and recall@k, a confusion matrix at the shortlist threshold, Wilson score intervals for the
proportions, and a seeded, stratified bootstrap for AP, ROC AUC and F1.

Every function here works on anonymous items (score, label); nothing company-level is printed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

Z95 = 1.96
BOOTSTRAP_RESAMPLES = 2000
DEFAULT_BOOTSTRAP_SEED = 20260927
KS = (10, 25, 50)
SMALL_DENOMINATOR = 10  # Wilson lines below this are marked "(small n)"
SMALL_STRATUM = 5  # bootstrap intervals need at least this many groups per class


@dataclass(frozen=True)
class Item:
    """One labeled company: its score, whether it is excluded, and the analyst's decision."""

    score: float
    positive: bool
    excluded: bool = False
    tiebreak: int = 0  # company id, so equal scores rank deterministically
    exclusion_rule: str | None = None


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for successes / n; None when n is 0."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom))


def ranked(items: Sequence[Item]) -> list[Item]:
    """Best first: excluded companies last, then by score, then by company id."""
    return sorted(items, key=lambda i: (i.excluded, -i.score, i.tiebreak))


def average_precision(items: Sequence[Item]) -> float | None:
    order = ranked(items)
    hits, total = 0, 0.0
    for rank, item in enumerate(order, start=1):
        if item.positive:
            hits += 1
            total += hits / rank
    return total / hits if hits else None


def roc_auc(items: Sequence[Item]) -> float | None:
    """Probability a random positive outranks a random negative (ties count half).
    Excluded companies score 0, which is what the ranking gives them."""
    pos = [0.0 if i.excluded else i.score for i in items if i.positive]
    neg = [0.0 if i.excluded else i.score for i in items if not i.positive]
    if not pos or not neg:
        return None
    # Rank-sum (Mann-Whitney U) with average ranks for ties.
    values = sorted([(s, 1) for s in pos] + [(s, 0) for s in neg])
    rank_sum, i = 0.0, 0
    while i < len(values):
        j = i
        while j < len(values) and values[j][0] == values[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        rank_sum += avg_rank * sum(v[1] for v in values[i:j])
        i = j
    u = rank_sum - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def f1(self) -> float:
        return 2 * self.tp / (2 * self.tp + self.fp + self.fn) if self.tp else 0.0


def shortlisted(item: Item, threshold: float) -> bool:
    return not item.excluded and item.score >= threshold


def confusion(items: Sequence[Item], threshold: float) -> Confusion:
    tp = fp = fn = tn = 0
    for i in items:
        s = shortlisted(i, threshold)
        if i.positive:
            tp, fn = tp + s, fn + (not s)
        else:
            fp, tn = fp + s, tn + (not s)
    return Confusion(tp, fp, fn, tn)


def bootstrap_ci(
    items: Sequence[Item],
    statistic: Callable[[Sequence[Item]], float | None],
    *,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[float, float] | None:
    """95% percentile interval, resampling items with replacement within each decision class so
    every resample keeps the class balance. None when a class has fewer than SMALL_STRATUM items."""
    pos = [i for i in items if i.positive]
    neg = [i for i in items if not i.positive]
    if len(pos) < SMALL_STRATUM or len(neg) < SMALL_STRATUM:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        sample = [rng.choice(pos) for _ in pos] + [rng.choice(neg) for _ in neg]
        v = statistic(sample)
        if v is not None:
            values.append(v)
    if not values:
        return None
    values.sort()
    return (_percentile(values, 0.025), _percentile(values, 0.975))


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks."""
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


@dataclass
class Metrics:
    n: int
    positives: int
    negatives: int
    threshold: float
    ap: float | None
    ap_ci: tuple[float, float] | None
    auc: float | None
    auc_ci: tuple[float, float] | None
    at_k: list[dict] = field(default_factory=list)
    confusion: Confusion = field(default_factory=lambda: Confusion(0, 0, 0, 0))
    precision_ci: tuple[float, float] | None = None
    recall_ci: tuple[float, float] | None = None
    f1: float = 0.0
    f1_ci: tuple[float, float] | None = None
    excluded_positives: dict[str, int] = field(default_factory=dict)
    small_strata: bool = False
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED

    def to_dict(self) -> dict:
        c = self.confusion
        return {
            "n": self.n,
            "positives": self.positives,
            "negatives": self.negatives,
            "threshold": self.threshold,
            "ap": self.ap,
            "ap_ci": self.ap_ci,
            "auc": self.auc,
            "auc_ci": self.auc_ci,
            "at_k": self.at_k,
            "confusion": {"tp": c.tp, "fp": c.fp, "fn": c.fn, "tn": c.tn},
            "precision_ci": self.precision_ci,
            "recall_ci": self.recall_ci,
            "f1": self.f1,
            "f1_ci": self.f1_ci,
            "excluded_positives": self.excluded_positives,
            "bootstrap_seed": self.bootstrap_seed,
        }


def compute(
    items: Sequence[Item], *, threshold: float, seed: int = DEFAULT_BOOTSTRAP_SEED
) -> Metrics:
    positives = sum(i.positive for i in items)
    order = ranked(items)
    at_k = []
    for k in sorted({min(k, len(items)) for k in KS if len(items)}):
        top = sum(i.positive for i in order[:k])
        at_k.append(
            {
                "k": k,
                "hits": top,
                "precision": top / k,
                "precision_ci": wilson(top, k),
                "recall": top / positives if positives else None,
                "recall_ci": wilson(top, positives),
            }
        )
    conf = confusion(items, threshold)
    excluded_pos: dict[str, int] = {}
    for i in items:
        if i.positive and i.excluded:
            rule = i.exclusion_rule or "unknown"
            excluded_pos[rule] = excluded_pos.get(rule, 0) + 1
    return Metrics(
        n=len(items),
        positives=positives,
        negatives=len(items) - positives,
        threshold=threshold,
        ap=average_precision(items),
        ap_ci=bootstrap_ci(items, average_precision, seed=seed),
        auc=roc_auc(items),
        auc_ci=bootstrap_ci(items, roc_auc, seed=seed),
        at_k=at_k,
        confusion=conf,
        precision_ci=wilson(conf.tp, conf.tp + conf.fp),
        recall_ci=wilson(conf.tp, conf.tp + conf.fn),
        f1=conf.f1,
        f1_ci=bootstrap_ci(items, lambda s: confusion(s, threshold).f1, seed=seed),
        excluded_positives=dict(sorted(excluded_pos.items())),
        small_strata=positives < SMALL_STRATUM or len(items) - positives < SMALL_STRATUM,
        bootstrap_seed=seed,
    )


def _num(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _ci(ci: tuple[float, float] | None) -> str:
    return "[n/a]" if ci is None else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def _small(n: int) -> str:
    return " (small n)" if n < SMALL_DENOMINATOR else ""


def format_metrics(m: Metrics) -> list[str]:
    """Aggregate lines only: no company names, domains or per-row values."""
    boot_small = " (small n)" if m.small_strata else ""
    c = m.confusion
    lines = [
        f"average precision {_num(m.ap)} {_ci(m.ap_ci)}{boot_small}",
        f"ROC AUC           {_num(m.auc)} {_ci(m.auc_ci)}{boot_small}",
    ]
    for row in m.at_k:
        k = row["k"]
        lines.append(
            f"precision@{k:<3}    {row['precision']:.2f} {_ci(row['precision_ci'])} "
            f"(hits={row['hits']}, n={k}){_small(k)}"
        )
        lines.append(
            f"recall@{k:<3}       {_num(row['recall'])} {_ci(row['recall_ci'])} "
            f"(hits={row['hits']}, n={m.positives}){_small(m.positives)}"
        )
    shortlisted_n = c.tp + c.fp
    lines += [
        f"at shortlist threshold {m.threshold:g}: TP={c.tp} FP={c.fp} FN={c.fn} TN={c.tn}",
        f"  precision {_num(c.tp / shortlisted_n if shortlisted_n else None)} {_ci(m.precision_ci)} "
        f"(TP={c.tp}, n={shortlisted_n}){_small(shortlisted_n)}",
        f"  recall    {_num(c.tp / m.positives if m.positives else None)} {_ci(m.recall_ci)} "
        f"(TP={c.tp}, n={m.positives}){_small(m.positives)}",
        f"  F1        {m.f1:.2f} {_ci(m.f1_ci)}{boot_small}",
    ]
    removed = sum(m.excluded_positives.values())
    detail = ", ".join(f"{rule} {n}" for rule, n in m.excluded_positives.items())
    lines.append(
        f"positives removed by exclusion rules: {removed}" + (f" ({detail})" if detail else "")
    )
    lines.append(f"bootstrap: {BOOTSTRAP_RESAMPLES} stratified resamples, seed {m.bootstrap_seed}")
    return lines
