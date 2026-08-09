import numpy as np
import pytest

from training.viewpoint.pixel_model import ENCODER_PROVENANCE, PixelModelSpec, build_pixel_policy


def test_pixel_contract_is_dependency_light_and_has_explicit_provenance():
    spec = PixelModelSpec().validate()
    assert spec.input_channels == 5
    assert spec.ranker.candidate_dim == 48
    assert ENCODER_PROVENANCE["pretrained_weights"] is None
    assert ENCODER_PROVENANCE["torch_runtime_license"] == "BSD-3-Clause"


torch = pytest.importorskip("torch", reason="optional Torch training dependency is not installed", exc_type=ImportError)


def _inputs(batch=2, candidates=4, height=32, width=40):
    rng = np.random.default_rng(19)
    depth_mask = rng.random((batch, candidates, height, width)) > 0.2
    depth = rng.random((batch, candidates, height, width), dtype=np.float32)
    depth[~depth_mask] = np.nan
    return {
        "task": torch.tensor(rng.normal(size=(batch, 14)), dtype=torch.float32),
        "rgb": torch.tensor(rng.integers(0, 256, (batch, candidates, height, width, 3), dtype=np.uint8)),
        "depth": torch.tensor(depth),
        "depth_mask": torch.tensor(depth_mask),
        "cameras": torch.tensor(rng.normal(size=(batch, candidates, 16)), dtype=torch.float32),
        "coarse_prior": torch.tensor(rng.normal(size=(batch, candidates, 8)), dtype=torch.float32),
        "candidate_mask": torch.tensor([[1, 1, 1, 0], [1, 1, 1, 1]], dtype=torch.bool),
    }


def test_pixel_encoder_feeds_existing_ranker_and_backpropagates():
    model = build_pixel_policy(seed=19)
    values = _inputs()
    scores = model(**values)
    assert scores.shape == (2, 4)
    assert torch.isneginf(scores[0, 3])
    loss = scores[values["candidate_mask"]].sum()
    loss.backward()
    assert model.stem[0].weight.grad is not None
    assert torch.isfinite(model.stem[0].weight.grad).all()
    assert model.ranker.out_w.grad is not None


def test_probe_features_depend_on_pixels_and_never_accept_oracle_targets():
    model = build_pixel_policy(seed=23).eval()
    values = _inputs(batch=1)
    with torch.no_grad():
        first = model.probe_features(
            values["rgb"], values["coarse_prior"], depth=values["depth"], depth_mask=values["depth_mask"]
        )
        changed_rgb = values["rgb"].clone()
        changed_rgb[:, :, :8, :8] = 255 - changed_rgb[:, :, :8, :8]
        second = model.probe_features(
            changed_rgb, values["coarse_prior"], depth=values["depth"], depth_mask=values["depth_mask"]
        )
    assert first.shape == (1, 4, 54)
    assert not torch.allclose(first, second)
    with pytest.raises(TypeError):
        model(**values, components=torch.zeros((1, 4, 7)))
