"""Correctness pins for the stdlib bench metrics (no numpy to cross-check against)."""
import math

from tools.flywheel.bench import metrics as M


def test_roc_auc_perfect_separation():
    labels = [True, True, False, False]
    scores = [0.9, 0.8, 0.7, 0.6]
    assert M.roc_auc(labels, scores) == 1.0


def test_roc_auc_interleaved():
    # positives at 0.9 and 0.6; negatives at 0.8 and 0.7.
    # pos>neg pairs: (0.9>0.8),(0.9>0.7) = 2 of 4 => 0.5
    labels = [True, True, False, False]
    scores = [0.9, 0.6, 0.8, 0.7]
    assert M.roc_auc(labels, scores) == 0.5


def test_roc_auc_ties_count_half():
    labels = [True, False]
    scores = [0.5, 0.5]
    assert M.roc_auc(labels, scores) == 0.5


def test_roc_auc_single_class_is_none():
    assert M.roc_auc([True, True], [0.1, 0.9]) is None
    assert M.roc_auc([False, False], [0.1, 0.9]) is None


def test_precision_recall():
    labels = [True, True, False, False]
    predicted = [True, False, True, False]
    pr = M.precision_recall(labels, predicted)
    assert pr["tp"] == 1 and pr["fp"] == 1 and pr["fn"] == 1
    assert math.isclose(pr["precision"], 0.5)
    assert math.isclose(pr["recall"], 0.5)
    assert math.isclose(pr["f1"], 0.5)


def test_coverage_and_pass_rate():
    assert M.coverage([True, True, False, True]) == 0.75
    assert M.pass_rate([True, False, False, False]) == 0.25


def test_residual():
    r = M.residual(0.8, 0.5)
    assert math.isclose(r["residual"], 0.3)
