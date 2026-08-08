import numpy as np
import pytest

from services.perception.reference import NumpyMLDP
from training.perception.model import OUTPUT_NAMES, build_torch_model
from training.perception.synthetic import generate_synthetic_corpus


def test_torch_matches_numpy_reference_when_installed():
    torch = pytest.importorskip("torch")
    corpus = generate_synthetic_corpus(windows=8, seed=23)
    inputs = corpus.windows.model_inputs()
    reference = NumpyMLDP(corpus.spec, seed=61)
    expected = reference.forward(inputs)
    model = build_torch_model(reference).eval()
    tensors = [torch.from_numpy(np.asarray(inputs[name])) for name in (
        "cam_feat", "cam_mask", "cam_age", "proc_feat", "proc_mask", "proc_age",
        "cmd_future", "cmd_mask",
    )]
    with torch.no_grad():
        actual = model(*tensors)
    for name, value in zip(OUTPUT_NAMES, actual):
        np.testing.assert_allclose(value.cpu().numpy(), expected[name], rtol=1e-5, atol=1e-5)
