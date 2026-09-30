"""Evaluation metrics and intervals (DESIGN.md §11.5-11.6), on anonymous synthetic items."""

import itertools
import random

import pytest

from dealsource.eval import metrics as m
from dealsource.eval.metrics import Item


def items(labels, scores=None):
    scores = scores if scores is not None else [100 - i for i in range(len(labels))]
    return [
        Item(s, bool(y), tiebreak=i) for i, (y, s) in enumerate(zip(labels, scores, strict=True))
    ]


# --- Wilson ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [
        (5, 10, 0.2366, 0.7634),  # published reference values
        (0, 10, 0.0, 0.2775),  # p = 0
        (10, 10, 0.7225, 1.0),  # p = 1
        (18, 29, 0.4400, 0.7731),
    ],
)
def test_wilson_matches_reference_values(k, n, lo, hi):
    got = m.wilson(k, n)
    assert got == pytest.approx((lo, hi), abs=1e-4)


def test_wilson_empty_denominator():
    assert m.wilson(0, 0) is None


# --- ranking metrics ------------------------------------------------------------------------------


def test_average_precision_known_value():
    assert m.average_precision(items([1, 0, 1, 0])) == pytest.approx((1 + 2 / 3) / 2)
    assert m.average_precision(items([1, 1, 0, 0])) == 1.0
    assert m.average_precision(items([0, 0])) is None


def test_roc_auc_known_values_and_ties():
    assert m.roc_auc(items([1, 1, 0, 0])) == 1.0
    assert m.roc_auc(items([0, 0, 1, 1])) == 0.0
    assert m.roc_auc(items([1, 0], scores=[50, 50])) == 0.5
    assert m.roc_auc(items([1, 1])) is None


def test_roc_auc_equals_pairwise_count():
    rng = random.Random(7)
    labels = [rng.random() < 0.4 for _ in range(40)]
    scores = [rng.choice([10, 20, 30, 40, 50]) for _ in range(40)]
    its = items(labels, scores)
    pairs = [
        1.0 if p.score > q.score else 0.5 if p.score == q.score else 0.0
        for p, q in itertools.product(
            [i for i in its if i.positive], [i for i in its if not i.positive]
        )
    ]
    assert m.roc_auc(its) == pytest.approx(sum(pairs) / len(pairs))


def test_excluded_items_rank_last():
    its = [Item(0.0, True, excluded=True, tiebreak=1), Item(0.0, False, tiebreak=2)]
    assert [i.tiebreak for i in m.ranked(its)] == [2, 1]


def test_precision_and_recall_at_k_are_clipped_to_n():
    res = m.compute(items([1, 0, 1, 0, 0]), threshold=60)
    assert [row["k"] for row in res.at_k] == [5]
    row = res.at_k[0]
    assert (row["hits"], row["precision"], row["recall"]) == (2, 0.4, 1.0)


def test_confusion_at_threshold_and_f1():
    its = items([1, 1, 0, 1, 0], scores=[90, 70, 65, 40, 10])
    c = m.confusion(its, 60)
    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 1, 1)
    assert c.f1 == pytest.approx(2 * 2 / (2 * 2 + 1 + 1))


def test_excluded_positive_is_never_shortlisted_and_counted_by_rule():
    its = [
        Item(0.0, True, excluded=True, tiebreak=1, exclusion_rule="keyword:franchise"),
        Item(0.0, True, excluded=True, tiebreak=2, exclusion_rule="keyword:franchise"),
        Item(80.0, True, tiebreak=3),
        Item(20.0, False, tiebreak=4),
    ]
    res = m.compute(its, threshold=0)
    assert res.confusion.tp == 1 and res.excluded_positives == {"keyword:franchise": 2}


# --- bootstrap ------------------------------------------------------------------------------------


def big_sample(seed=3, n=60):
    rng = random.Random(seed)
    return [Item(rng.uniform(0, 100), rng.random() < 0.35, tiebreak=i) for i in range(n)]


def test_bootstrap_is_deterministic_for_a_seed():
    its = big_sample()
    a = m.bootstrap_ci(its, m.roc_auc, seed=20260927, resamples=300)
    b = m.bootstrap_ci(its, m.roc_auc, seed=20260927, resamples=300)
    c = m.bootstrap_ci(its, m.roc_auc, seed=1, resamples=300)
    assert a == b and a != c
    assert a[0] <= m.roc_auc(its) <= a[1]


def test_bootstrap_keeps_stratum_sizes():
    its = big_sample()
    pos = sum(i.positive for i in its)
    seen = set()

    def stat(sample):
        seen.add((len(sample), sum(i.positive for i in sample)))
        return 0.0

    m.bootstrap_ci(its, stat, resamples=50)
    assert seen == {(len(its), pos)}


def test_bootstrap_is_na_for_small_strata():
    its = items([1, 1, 1, 1, 0, 0, 0, 0, 0, 0])  # 4 positives
    assert m.bootstrap_ci(its, m.roc_auc) is None
    res = m.compute(its, threshold=60)
    assert res.small_strata and res.auc_ci is None


# --- formatting -----------------------------------------------------------------------------------


def test_format_marks_small_n_and_na():
    lines = m.format_metrics(m.compute(items([1, 0, 1, 0, 0]), threshold=60))
    text = "\n".join(lines)
    assert "[n/a] (small n)" in text  # bootstrap with fewer than 5 per class
    assert "precision@5" in text and "(hits=2, n=5) (small n)" in text
    assert "positives removed by exclusion rules: 0" in text


def test_format_full_example():
    res = m.compute(big_sample(n=120), threshold=60)
    text = "\n".join(m.format_metrics(res))
    assert "(small n)" not in text.split("precision@")[0]  # AP/AUC lines have real intervals
    assert "precision@10" in text and "precision@50" in text
    assert "bootstrap: 2000 stratified resamples, seed 20260927" in text
