import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="optional Torch training dependency is not installed", exc_type=ImportError)

from training.viewpoint.data import ViewpointBatch, split_by_part
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


def test_pairwise_training_keeps_padded_candidate_gradients_finite():
    corpus = generate_oracle_corpus(
        CorpusConfig(parts=6, examples_per_part=1, candidates=4, seed=79, image_size=(80, 80)),
    )
    padded = ViewpointBatch(
        part_ids=corpus.part_ids,
        task=corpus.task,
        geometry=corpus.geometry,
        geometry_mask=corpus.geometry_mask,
        feature_mask=corpus.feature_mask,
        candidates=np.pad(corpus.candidates, ((0, 0), (0, 2), (0, 0))),
        candidate_mask=np.pad(corpus.candidate_mask, ((0, 0), (0, 2))),
        components=np.pad(corpus.components, ((0, 0), (0, 2), (0, 0))),
        difficulties=corpus.difficulties,
    ).validate()
    train, validation, _ = split_by_part(
        padded,
        seed=79,
        train_fraction=0.5,
        validation_fraction=0.2,
    )
    model, report = fit_policy(train, validation, seed=79, epochs=3)
    assert np.isfinite(report["loss"])
    assert all(np.isfinite(value).all() for value in model.weights.arrays().values())

def test_cuda_fit_repeats_bit_identically_at_fixed_seed():
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable")
    corpus = generate_oracle_corpus(
        CorpusConfig(parts=6, examples_per_part=1, candidates=4, seed=83, image_size=(64, 64)),
        device="cpu",
    )
    train, validation, _ = split_by_part(
        corpus, seed=83, train_fraction=0.5, validation_fraction=0.2,
    )
    first, first_report = fit_policy(train, validation, seed=83, epochs=3, device="cuda")
    second, second_report = fit_policy(train, validation, seed=83, epochs=3, device="cuda")
    assert first_report == second_report
    for name, first_value in first.weights.arrays().items():
        assert np.array_equal(first_value, second.weights.arrays()[name]), name
