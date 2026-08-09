import numpy as np

from services.perception.config import ModelSpec
from training.perception.synthetic import generate_synthetic_corpus


def test_synthetic_corpus_is_deterministic_valid_and_contains_events():
    spec = ModelSpec()
    first = generate_synthetic_corpus(spec=spec, windows=32, seed=55)
    second = generate_synthetic_corpus(spec=spec, windows=32, seed=55)
    first.windows.validate(spec)
    for name in first.windows.model_inputs():
        np.testing.assert_array_equal(first.windows.model_inputs()[name], second.windows.model_inputs()[name])
    np.testing.assert_array_equal(first.windows.target_latent, second.windows.target_latent)
    assert first.windows.event_label.any()
    assert (~first.windows.event_label).any()
    assert first.anomaly_ticks == second.anomaly_ticks
    assert first.regimes == second.regimes
    assert first.split_labels == second.split_labels
    assert set(first.regimes) == {"steady_cut", "material_change", "wear_ramp"}
    assert set(first.split_labels) == {"train", "calibration", "test_locked"}
