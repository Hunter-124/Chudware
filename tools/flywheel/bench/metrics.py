"""Stdlib metric helpers for the bench suite.

Example-level metrics (ROC-AUC, precision/recall, coverage) so benches report ranking
quality, not just a corpus mean. No numpy — the suite must run parity-clean on any box
with only the stdlib, so results are reproducible on a second machine/OS.
"""
from __future__ import annotations

from typing import Optional, Sequence


def mean(xs: Sequence[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def pass_rate(passed: Sequence[bool]) -> float:
    passed = list(passed)
    return sum(1 for p in passed if p) / len(passed) if passed else 0.0


def roc_auc(labels: Sequence[bool], scores: Sequence[float]) -> Optional[float]:
    """Area under the ROC curve via the Mann-Whitney U statistic (tie-aware).

    Returns None when only one class is present (AUC undefined). O(n log n).
    AUC = P(score(random positive) > score(random negative)), ties counted as 0.5.
    """
    pairs = list(zip(labels, scores))
    n_pos = sum(1 for l, _ in pairs if l)
    n_neg = len(pairs) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    # Rank scores (average ranks for ties), 1-based.
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][1])
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pairs[order[j + 1]][1] == pairs[order[i]][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # average of 1-based ranks in the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    sum_ranks_pos = sum(ranks[i] for i in range(len(pairs)) if pairs[i][0])
    u_pos = sum_ranks_pos - n_pos * (n_pos + 1) / 2.0
    return u_pos / (n_pos * n_neg)


def precision_recall(labels: Sequence[bool], predicted: Sequence[bool]) -> dict[str, float]:
    tp = sum(1 for l, p in zip(labels, predicted) if l and p)
    fp = sum(1 for l, p in zip(labels, predicted) if (not l) and p)
    fn = sum(1 for l, p in zip(labels, predicted) if l and (not p))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def coverage(found: Sequence[bool]) -> float:
    """Fraction of required slots/items that were covered."""
    found = list(found)
    return sum(1 for f in found if f) / len(found) if found else 0.0


def residual(candidate_mean: float, baseline_mean: float) -> dict[str, float]:
    """Residual: the margin by which a candidate beats the deterministic baseline.

    A model-backed path is only justified over the deterministic one when this residual
    is a real, measured positive margin on the frozen set.
    """
    return {
        "candidate_mean": candidate_mean,
        "baseline_mean": baseline_mean,
        "residual": candidate_mean - baseline_mean,
    }
