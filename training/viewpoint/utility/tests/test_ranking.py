import math
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("torch")

from training.viewpoint.utility.evaluate import within_group_ranking  # noqa: E402


def dataset(parts, queries, sets, targets, ks=None):
    return SimpleNamespace(
        part_ids=np.asarray(parts, object),
        rows=[{"qid": q} for q in queries],
        set_index=np.asarray(sets, np.int64),
        y=np.asarray(targets, np.float64),
        ks=np.asarray(ks if ks is not None else [4] * len(parts), np.int64),
    )


def test_true_and_predicted_ties_have_distinct_meanings():
    ds = dataset(["p"] * 3, ["q"] * 3, [0, 1, 2], [0.1, 0.1, 0.9])
    report = within_group_ranking(ds, [0, 1, 2], [0, 1, 1], n_boot=20)
    assert report["comparable_pairs"] == 2  # The target tie is not comparable.
    assert report["pairwise_accuracy"]["estimate"] == pytest.approx(0.75)
    assert report["kendall_tau_b"]["estimate"] == pytest.approx(0.5)
    # Selecting both tied maxima has mean target 0.5, not the lucky winner's 0.9.
    assert report["top1_regret"]["estimate"] == pytest.approx(0.4)


def test_constant_rankings_preserve_regret_and_exclude_undefined_tau():
    ds = dataset(["p"] * 4, ["true_tie"] * 2 + ["pred_tie"] * 2,
                 [0, 1, 2, 3], [0.4, 0.4, 0.2, 0.8])
    report = within_group_ranking(ds, range(4), [0, 1, 0, 0], n_boot=0)
    assert report["groups"] == 2
    assert report["comparable_pairs"] == 1
    assert report["pairwise_accuracy"]["estimate"] == 0.5
    assert report["pairwise_accuracy"]["n"] == 1
    assert math.isnan(report["kendall_tau_b"]["estimate"])
    assert report["kendall_tau_b"]["n"] == 0
    assert report["top1_regret"]["estimate"] == pytest.approx(0.15)
    assert report["top1_regret"]["n"] == 2


def test_groups_are_isolated_deduplicated_and_macro_averaged():
    ds = dataset(["a"] * 4 + ["b"] * 2 + ["c"] * 2,
                 ["shared"] * 3 + ["singleton"] + ["shared"] * 4,
                 [0, 1, 2, 3, 4, 5, 6, 6], [0, 0.5, 1, 1, 0.2, 0.8, 0, 0])
    # An index may repeat: it is still one DISTINCT labelled set, never a new pair.
    idx = [0, 1, 2, 0, 3, 4, 5, 6, 7]
    scores = [2, 1, 0, 2, 100, 0, 1, 0, 0]
    report = within_group_ranking(ds, idx, scores, n_boot=0)
    assert report["groups"] == 2
    assert report["parts"] == 2
    assert report["sets"] == 5
    assert report["labelled_sets"] == 5
    assert report["comparable_pairs"] == 4
    # Equal group weight, not pair weight: the wrong 3-set group has three pairs.
    assert report["pairwise_accuracy"]["estimate"] == 0.5
    assert report["kendall_tau_b"]["estimate"] == 0
    assert report["top1_regret"]["estimate"] == 0.5


def test_k4_filter_runs_before_group_eligibility_and_uses_aligned_scores():
    ds = dataset(["a"] * 3 + ["b"] * 2, ["q"] * 5,
                 [0, 1, 2, 3, 4], [0, 0.5, 1, 0, 1], [1, 4, 4, 1, 4])
    idx = np.asarray([4, 2, 0, 3, 1])
    scores = np.asarray([1, 0, 0, 0, 1])
    all_k = within_group_ranking(ds, idx, scores, n_boot=0)
    k4 = within_group_ranking(ds, idx, scores, k=4, n_boot=0)
    assert all_k["groups"] == 2
    assert all_k["comparable_pairs"] == 4
    assert all_k["pairwise_accuracy"]["estimate"] == 0.75
    assert k4["groups"] == 1
    assert k4["parts"] == 1
    assert k4["sets"] == 2
    assert k4["comparable_pairs"] == 1
    assert k4["pairwise_accuracy"]["estimate"] == 0
    assert k4["kendall_tau_b"]["estimate"] == -1
    assert k4["top1_regret"]["estimate"] == 0.5


def test_bootstrap_clusters_queries_by_part():
    ds = dataset(["a"] * 20 + ["b"] * 2,
                 [f"q{i // 2}" for i in range(20)] + ["q0"] * 2,
                 range(22), [0, 1] * 11)
    report = within_group_ranking(ds, range(22), [0, 1] * 10 + [1, 0], n_boot=2000)
    accuracy = report["pairwise_accuracy"]
    assert accuracy["estimate"] == pytest.approx(10 / 11)
    assert accuracy["n"] == 11
    assert accuracy["n_parts"] == 2
    assert accuracy["ci95"] == [0, 1]
