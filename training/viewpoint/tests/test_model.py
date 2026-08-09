import numpy as np

from training.viewpoint.model import ModelSpec, NumpyViewpointPolicy, PolicyWeights


def _inputs(seed=3):
    rng = np.random.default_rng(seed)
    return {
        "task": rng.normal(size=(2, 14)).astype(np.float32),
        "geometry": rng.normal(size=(2, 5, 18)).astype(np.float32),
        "geometry_mask": np.asarray([[1, 1, 1, 0, 0], [1, 1, 1, 1, 1]], bool),
        "feature_mask": np.asarray([[0, 1, 0, 0, 0], [0, 0, 1, 0, 0]], bool),
        "candidates": rng.normal(size=(2, 7, ModelSpec().candidate_dim)).astype(np.float32),
        "candidate_mask": np.asarray([[1, 1, 1, 1, 1, 0, 0], [1, 1, 1, 1, 1, 1, 1]], bool),
    }


def test_numpy_forward_is_finite_and_selects_only_valid_candidates():
    model = NumpyViewpointPolicy.random(seed=7)
    inputs = _inputs()
    selected, scores = model.select(**inputs)
    assert scores.shape == (2, 7)
    assert np.isfinite(scores[inputs["candidate_mask"]]).all()
    assert np.all(np.isneginf(scores[~inputs["candidate_mask"]]))
    assert inputs["candidate_mask"][np.arange(2), selected].all()


def test_geometry_token_permutation_does_not_change_output():
    model = NumpyViewpointPolicy.random(seed=11)
    inputs = _inputs()
    original = model.forward(**inputs)
    permutation = np.asarray((2, 0, 4, 1, 3))
    inputs["geometry"] = inputs["geometry"][:, permutation]
    inputs["geometry_mask"] = inputs["geometry_mask"][:, permutation]
    inputs["feature_mask"] = inputs["feature_mask"][:, permutation]
    permuted = model.forward(**inputs)
    np.testing.assert_allclose(permuted, original, atol=2e-6, rtol=2e-6)


def test_weight_archive_round_trip(tmp_path):
    spec = ModelSpec(hidden_dim=24)
    weights = PolicyWeights.random(spec, seed=19)
    path = tmp_path / "policy.npz"
    weights.save(path, spec)
    loaded, loaded_spec = PolicyWeights.load(path)
    assert loaded_spec == spec
    for name, value in weights.arrays().items():
        np.testing.assert_array_equal(loaded.arrays()[name], value)
