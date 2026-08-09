from types import SimpleNamespace

import numpy as np
import pytest

from training.viewpoint.pixel_train import (
    _augment_training_batch,
    _part_bootstrap,
    _ranknet_loss,
    full_training_rows,
)


def test_whole_part_bootstrap_resamples_complete_groups():
    part_ids = ("a", "a", "b", "b")
    values = np.asarray((0.0, 0.0, 1.0, 1.0), np.float32)
    means = _part_bootstrap(part_ids, values, samples=200, seed=7)
    assert set(means) <= {0.0, 0.5, 1.0}
    assert 0.0 in means and 1.0 in means

def test_full_training_rows_uses_every_train_example_without_touching_locked_splits():
    corpus = SimpleNamespace(
        splits=("train", "validation", "train", "test", "train", "validation")
    )
    np.testing.assert_array_equal(full_training_rows(corpus), np.asarray((0, 2, 4)))


try:
    import torch
except ImportError:
    torch = None


@pytest.mark.skipif(torch is None, reason="optional Torch training dependency is not installed")
def test_ranknet_loss_rewards_the_correct_candidate_order():
    quality = torch.tensor([[0.1, 0.8, 0.4]], dtype=torch.float32)
    mask = torch.ones((1, 3), dtype=torch.bool)
    correct = torch.tensor([[-2.0, 3.0, 0.0]], requires_grad=True)
    reversed_scores = -correct.detach()
    correct_loss = _ranknet_loss(correct, quality, mask, torch, 0.1)
    reversed_loss = _ranknet_loss(reversed_scores, quality, mask, torch, 0.1)
    assert correct_loss < reversed_loss
    correct_loss.backward()
    assert torch.isfinite(correct.grad).all()


@pytest.mark.skipif(torch is None, reason="optional Torch training dependency is not installed")
def test_seeded_augmentation_keeps_rgb_depth_geometry_aligned_without_flip_or_rotation():
    height = width = 8
    ramp = torch.arange(height * width, dtype=torch.float32).reshape(1, 1, height, width)
    rgb = torch.stack((ramp, ramp, ramp), dim=-1).to(torch.uint8)
    batch = {
        "rgb": rgb,
        "depth": ramp / float(height * width - 1),
        "depth_mask": torch.ones((1, 1, height, width), dtype=torch.bool),
        "cameras": torch.zeros((1, 1, 16)),
    }
    first = _augment_training_batch(
        batch,
        torch,
        torch.Generator(device="cpu").manual_seed(41),
        photometric_jitter=0.08,
        max_crop_pixels=2,
    )
    second = _augment_training_batch(
        batch,
        torch,
        torch.Generator(device="cpu").manual_seed(41),
        photometric_jitter=0.08,
        max_crop_pixels=2,
    )
    assert first["rgb"].shape == rgb.shape
    assert torch.equal(first["depth_mask"], batch["depth_mask"])
    assert torch.equal(first["cameras"], batch["cameras"])
    assert torch.equal(first["rgb"], second["rgb"])
    assert torch.equal(first["depth"], second["depth"])
    assert torch.all(torch.diff(first["depth"][0, 0], dim=0) >= 0)
    assert torch.all(torch.diff(first["depth"][0, 0], dim=1) >= 0)
