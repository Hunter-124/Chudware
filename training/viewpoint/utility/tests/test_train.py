import numpy as np
import pytest

torch = pytest.importorskip("torch")

from training.viewpoint.utility.evaluate import part_bootstrap  # noqa: E402
from training.viewpoint.utility.tests.synth import planted_dataset  # noqa: E402
from training.viewpoint.utility.train import (  # noqa: E402
    TrainConfig, auc, binary_metrics, fit_hand_calibration, fit_utility, logloss, prior_probs,
    sigmoid, split_indices, trial_logits)

# The shipped regularisation defaults (weight decay + face-token embedding dropout).
CPU = TrainConfig(lr=3e-3, weight_decay=1.0, face_dropout=0.2, batch_trials=128,
                  max_epochs=60, patience=8, device="cpu")


@pytest.fixture(scope="module")
def data():
    ds = planted_dataset(seed=0)
    tr, va, _ = split_indices(ds)
    return ds, tr, va


def test_trainer_learns_planted_signal_from_soft_targets(data):
    ds, tr, va = data
    fit = fit_utility(ds, tr, va, seed=0, cfg=CPU)
    p = sigmoid(trial_logits(fit.net, ds, va))
    y = ds.y[va]
    prior = logloss(y, prior_probs(ds, tr, va))
    assert auc(ds.correct[va], p) > 0.72
    assert logloss(y, p) < prior - 0.05
    # The hand score in the synthetic set is pure noise: the calibrated baseline must not
    # find the planted signal, so the learned model beats it.
    hand = fit_hand_calibration(ds, tr)
    hand_p = sigmoid(hand.logits(ds.hand[va, None], ds.kind[va]))
    assert logloss(y, p) < logloss(y, hand_p) - 0.03


def test_shuffled_label_control_stays_at_prior(data):
    ds, tr, va = data
    fit = fit_utility(ds, tr, va, seed=0, cfg=CPU, shuffle_labels=True)
    p = sigmoid(trial_logits(fit.net, ds, va))
    y = ds.y[va]
    prior = logloss(y, prior_probs(ds, tr, va))
    assert abs(logloss(y, p) - prior) < 0.03
    assert auc(ds.correct[va], p) < 0.62


def test_normalisation_uses_train_sets_only(data):
    ds, tr, va = data
    fit = fit_utility(ds, tr, va, seed=1, cfg=TrainConfig(max_epochs=1, patience=1,
                                                          device="cpu"))
    train_sets = sorted(set(ds.set_index[tr].tolist()))
    faces = np.concatenate([ds.face_tokens[s][ds.face_mask[s]] for s in train_sets]).astype(
        np.float64)
    np.testing.assert_allclose(fit.net.norm_face_mean.numpy(), faces.mean(0), atol=1e-5)
    # Padded rows carry 6.0 in column 0; they must not leak into the statistics.
    assert fit.net.norm_face_mean[0].item() < 1.0


def test_metrics_edges():
    y = np.array([0, 0, 1, 1], np.float64)
    assert auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    m = binary_metrics(y, np.full(4, 0.5))
    assert m["logloss"] == pytest.approx(np.log(2.0))
    assert m["brier"] == pytest.approx(0.25)


def test_part_bootstrap_resamples_parts_not_trials():
    # One part with 100 identical trials and one with 1: a trial bootstrap would be tight
    # around 0.99, the part bootstrap must see that only two independent parts exist.
    parts = ["a"] * 100 + ["b"]
    vals = [1.0] * 100 + [0.0]
    b = part_bootstrap(parts, vals, n_boot=2000, seed=0)
    assert b["n_parts"] == 2
    assert b["ci95"][0] == pytest.approx(0.0)
    assert b["ci95"][1] == pytest.approx(1.0)
