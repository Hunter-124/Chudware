from dataclasses import replace

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from training.viewpoint.utility.tests.synth import planted_dataset
from training.viewpoint.utility.train import (
    TrainConfig, comparison_pairs, conditional_loss, fit_utility, pairwise_logloss,
    split_indices, trial_logits,
)


def grouped_data():
    ds = planted_dataset(n_sets=40, n_trials=160, seed=5)
    ds.rows = [{**r, "qid": f"{ds.part_ids[i]}:kind{ds.kind[i]}"}
               for i, r in enumerate(ds.rows)]
    ds.hand = np.sin(ds.set_index.astype(float))
    return ds


def test_comparisons_never_cross_query_part_or_cardinality():
    ds = grouped_data()
    ds.ks[::3] = 1
    left, right, offsets = comparison_pairs(ds, np.arange(len(ds)))
    assert len(left) > 0
    assert np.all(ds.part_ids[left] == ds.part_ids[right])
    assert np.all(ds.ks[left] == ds.ks[right])
    assert all(ds.rows[a]["qid"] == ds.rows[b]["qid"] for a, b in zip(left, right))
    assert np.all(ds.set_index[left] != ds.set_index[right])
    assert offsets[-1] == len(left)


def test_conditional_loss_prefers_correct_order_and_ignores_group_intercept():
    target = torch.tensor([0.6, -0.6])
    good = conditional_loss(torch.tensor([2., -2.]), target, .1)
    bad = conditional_loss(torch.tensor([-2., 2.]), target, .1)
    assert good < bad
    pairs = (np.array([0, 2]), np.array([1, 3]), np.array([0, 1, 2]))
    y = np.array([.9, .3, .2, .8])
    logits = np.array([2., -2., -2., 2.])
    assert pairwise_logloss(logits, y, pairs, .1) == pytest.approx(
        pairwise_logloss(logits + np.array([100., 100., -40., -40.]), y, pairs, .1))


def test_residual_ranking_training_resumes_exactly_after_interruption(tmp_path, monkeypatch):
    ds = grouped_data()
    tr, va, _ = split_indices(ds)
    cfg = TrainConfig(device="cpu", max_epochs=4, patience=8, batch_trials=32,
                      hand_residual=True, rank_weight=1., selection_metric="pairwise")
    uninterrupted = fit_utility(ds, tr, va, seed=4, cfg=cfg)
    checkpoint = tmp_path / "seed.pt"
    from training.viewpoint import durable
    original = durable.durable_replace
    saves = 0

    def interrupt_after_save(source, dest):
        nonlocal saves
        original(source, dest)
        saves += 1
        if saves == 2:
            raise RuntimeError("simulated hardware interruption")

    with monkeypatch.context() as patch:
        patch.setattr(durable, "durable_replace", interrupt_after_save)
        with pytest.raises(RuntimeError, match="hardware interruption"):
            fit_utility(ds, tr, va, seed=4, cfg=cfg, checkpoint_path=checkpoint)
    resumed = fit_utility(ds, tr, va, seed=4, cfg=cfg, checkpoint_path=checkpoint)
    np.testing.assert_array_equal(trial_logits(uninterrupted.net, ds, va),
                                  trial_logits(resumed.net, ds, va))
    assert resumed.best_epoch == uninterrupted.best_epoch
    shrunk = fit_utility(ds, tr, va, seed=4, cfg=replace(cfg, correction_scale=.25),
                         checkpoint_path=checkpoint)
    baseline = (float(uninterrupted.net.hand_slope) * ds.hand[va]
                + float(uninterrupted.net.hand_intercept)
                + uninterrupted.net.kind_bias.detach().numpy()[ds.kind[va]])
    np.testing.assert_allclose(
        trial_logits(shrunk.net, ds, va),
        baseline + .25 * (trial_logits(uninterrupted.net, ds, va) - baseline), atol=1e-7)
    changed = replace(cfg, rank_weight=2.)
    with pytest.raises(ValueError, match="different configuration"):
        fit_utility(ds, tr, va, seed=4, cfg=changed, checkpoint_path=checkpoint)
    relabelled = replace(ds, y=ds.y.copy())
    relabelled.y[tr[0]] += .1
    with pytest.raises(ValueError, match="different configuration or dataset"):
        fit_utility(relabelled, tr, va, seed=4, cfg=cfg, checkpoint_path=checkpoint)
