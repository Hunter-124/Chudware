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


def test_temporal_encoder_is_strictly_causal():
    torch = pytest.importorskip("torch")
    corpus = generate_synthetic_corpus(windows=8, seed=29)
    inputs = corpus.windows.model_inputs()
    model = build_torch_model(spec=corpus.spec, seed=71).eval()
    encoded_inputs = {
        name: torch.from_numpy(np.asarray(inputs[name]))
        for name in ("cam_feat", "cam_mask", "proc_feat", "proc_mask")
    }
    split = corpus.spec.window // 2
    changed = {name: value.clone() for name, value in encoded_inputs.items()}
    changed["cam_feat"][:, :, split:] += 7.0
    changed["proc_feat"][:, split:] -= 5.0
    with torch.no_grad():
        original_tokens = model._encode(encoded_inputs)[0]
        changed_tokens = model._encode(changed)[0]
    torch.testing.assert_close(
        original_tokens[:, :split],
        changed_tokens[:, :split],
        rtol=0.0,
        atol=0.0,
    )
    assert not torch.equal(
        original_tokens[:, split:], changed_tokens[:, split:]
    )
