import numpy as np

from services.perception.config import ModelSpec
from services.perception.reference import NumpyMLDP, surprise_score
from training.perception.synthetic import generate_synthetic_corpus


def test_numpy_forward_is_deterministic_and_has_static_shapes():
    spec = ModelSpec()
    batch = generate_synthetic_corpus(spec=spec, windows=12, seed=4).windows
    inputs = batch.model_inputs()
    first = NumpyMLDP(spec, seed=91).forward(inputs)
    second = NumpyMLDP(spec, seed=91).forward(inputs)
    assert set(first) == {
        "z_hat", "z_logvar", "snr_hat", "snr_logvar", "state_logit",
        "anomaly", "event_bnd", "importance", "cam_rel", "retain",
    }
    for name in first:
        assert first[name].dtype == np.float32
        np.testing.assert_array_equal(first[name], second[name])
    b, k, c = 12, len(spec.horizons), spec.cameras
    assert first["z_hat"].shape == (b, k, c, spec.latent_features)
    assert first["z_logvar"].shape == first["z_hat"].shape
    assert first["snr_hat"].shape == (b, k, spec.sensor_features)
    assert first["state_logit"].shape == (b, k, spec.state_labels)
    assert first["event_bnd"].shape == (b, spec.window, 3)
    assert first["cam_rel"].shape == (b, c)
    np.testing.assert_allclose(first["cam_rel"].sum(axis=1), 1.0, atol=1e-6)


def test_temporal_encoder_is_strictly_causal():
    spec = ModelSpec(window=8, dilations=(1, 2, 4))
    batch = generate_synthetic_corpus(spec=spec, windows=8, seed=19).windows.subset(np.array([0]))
    inputs = {key: value.copy() for key, value in batch.model_inputs().items()}
    model = NumpyMLDP(spec, seed=12)
    baseline = model.encode_sequence(inputs)[0]
    cutoff = 4
    inputs["cam_feat"][:, :, cutoff + 1 :] += 1000.0
    inputs["proc_feat"][:, cutoff + 1 :] -= 1000.0
    changed = model.encode_sequence(inputs)[0]
    np.testing.assert_array_equal(baseline[:, : cutoff + 1], changed[:, : cutoff + 1])
    assert not np.array_equal(baseline[:, -1], changed[:, -1])


def test_surprise_uses_prediction_error_and_predicted_variance():
    mean = np.zeros((2, 1, 1, 2), np.float32)
    observed = np.array([[[[1.0, 1.0]]], [[[4.0, 4.0]]]], np.float32)
    logvar = np.zeros_like(mean)
    score = surprise_score(observed, mean, logvar)
    np.testing.assert_allclose(score, [0.5, 8.0])
    larger_variance = surprise_score(observed[1:], mean[1:], np.full_like(mean[1:], np.log(16.0)))
    assert larger_variance[0] < score[1]
