import numpy as np
import pytest

from training.viewpoint.model import ModelSpec, NumpyViewpointPolicy
from training.viewpoint.torch_model import build_torch_policy


torch = pytest.importorskip("torch", reason="optional Torch training dependency is not installed", exc_type=ImportError)


def _inputs():
    rng = np.random.default_rng(17)
    return {
        "task": rng.normal(size=(2, 14)).astype(np.float32),
        "geometry": rng.normal(size=(2, 4, 18)).astype(np.float32),
        "geometry_mask": np.asarray([[1, 1, 1, 0], [1, 1, 1, 1]], bool),
        "feature_mask": np.asarray([[0, 1, 0, 0], [0, 0, 1, 0]], bool),
        "candidates": rng.normal(size=(2, 6, ModelSpec().candidate_dim)).astype(np.float32),
        "candidate_mask": np.asarray([[1, 1, 1, 1, 0, 0], [1, 1, 1, 1, 1, 1]], bool),
    }


def test_torch_and_numpy_forward_are_equivalent():
    numpy_model = NumpyViewpointPolicy.random(seed=23)
    torch_model = build_torch_policy(reference=numpy_model)
    inputs = _inputs()
    expected = numpy_model.forward(**inputs)
    actual = torch_model(**{
        name: torch.as_tensor(value, dtype=torch.bool if value.dtype == bool else torch.float32)
        for name, value in inputs.items()
    }).detach().numpy()
    np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=3e-6)
