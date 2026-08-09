import numpy as np
import pytest

pytest.importorskip("torch", reason="optional Torch training dependency is not installed", exc_type=ImportError)

from training.viewpoint.data import split_by_part
from training.viewpoint.model import ModelSpec, PolicyWeights
from training.viewpoint.synthetic import CorpusConfig, generate_oracle_corpus
from training.viewpoint.train import fit_policy


def test_stage1_updates_real_network_weights_end_to_end():
    corpus = generate_oracle_corpus(CorpusConfig(parts=6, examples_per_part=1, candidates=5, seed=73, image_size=(80, 80)))
    train, validation, _ = split_by_part(corpus, seed=73, train_fraction=0.5, validation_fraction=0.2)
    model, report = fit_policy(train, validation, seed=73, epochs=3)
    assert np.isfinite(report["loss"])
    assert np.isfinite(report["train_aggregate"])
    assert np.isfinite(report["validation_aggregate"])
    assert report["auxiliary_regression_weight"] == 0.0
    initial = PolicyWeights.random(ModelSpec(), seed=73).arrays()
    trained = model.weights.arrays()
    changed = sum(np.any(trained[name] != initial[name]) for name in initial)
    assert changed >= 8
