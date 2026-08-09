import numpy as np
import pytest

from training.viewpoint.pixel_train import _part_bootstrap, _ranknet_loss


def test_whole_part_bootstrap_resamples_complete_groups():
    part_ids = ("a", "a", "b", "b")
    values = np.asarray((0.0, 0.0, 1.0, 1.0), np.float32)
    means = _part_bootstrap(part_ids, values, samples=200, seed=7)
    assert set(means) <= {0.0, 0.5, 1.0}
    assert 0.0 in means and 1.0 in means


torch = pytest.importorskip("torch", reason="optional Torch training dependency is not installed", exc_type=ImportError)


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
