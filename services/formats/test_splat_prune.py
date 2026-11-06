"""Pruning a phone-video-like Gaussian scene down to its subject."""
import numpy as np
import pytest

from services.formats.splat_io import GaussianCloud, prune_cloud
from services.formats.splat_prune import PruneOptions, prune_gaussians

_C0 = 0.28209479177387814
IDENTITY = np.array([1.0, 0.0, 0.0, 0.0])


def _scene(subject_opacity=0.12):
    """Flat orange floor, a dark sphere resting on it, haze, and isolated floaters."""
    rng = np.random.default_rng(3)
    parts = {}

    def add(name, means, scales, opacity, rgb):
        n = len(means)
        parts[name] = (means, np.tile(scales, (n, 1)), np.tile(IDENTITY, (n, 1)),
                       np.full(n, opacity), np.tile((np.array(rgb) - 0.5) / _C0, (n, 1)))

    floor = np.column_stack([rng.uniform(-5, 5, (6000, 2)), rng.normal(0, 0.01, 6000)])
    add("floor", floor, [0.06, 0.06, 0.004], 0.10, (0.9, 0.55, 0.2))
    direction = rng.normal(size=(1500, 3))
    direction /= np.linalg.norm(direction, axis=1, keepdims=True)
    add("subject", direction * 0.5 + [0, 0, 0.5], [0.03, 0.03, 0.03], subject_opacity, (0.1, 0.1, 0.1))
    add("haze", rng.uniform(-4, 4, (300, 3)) + [0, 0, 2], [2.5, 2.5, 2.5], 0.10, (0.8, 0.8, 0.9))
    add("floaters", rng.uniform(-4, 4, (40, 3)) * [1, 1, 0.5] + [0, 0, 3], [0.03, 0.03, 0.03], 0.3, (0.5, 0.5, 0.5))
    labels = np.concatenate([np.full(len(p[0]), i) for i, p in enumerate(parts.values())])
    cloud = GaussianCloud(*(np.concatenate([p[k] for p in parts.values()]) for k in range(5)),
                          sh_rest=None, source_format="ply", lossy=[])
    return cloud, labels, list(parts)


def _kept_fraction(keep, labels, names, name):
    mask = labels == names.index(name)
    return keep[mask].mean()


def test_prune_keeps_subject_and_drops_floor_haze_floaters():
    cloud, labels, names = _scene()
    keep, report = prune_gaussians(cloud)
    assert _kept_fraction(keep, labels, names, "subject") > 0.9
    assert _kept_fraction(keep, labels, names, "floor") < 0.02
    assert _kept_fraction(keep, labels, names, "haze") == 0
    assert _kept_fraction(keep, labels, names, "floaters") < 0.15
    assert [s["stage"] for s in report.stages] == ["opacity", "region", "shape", "floor", "floaters"]
    assert report.floor is not None and abs(abs(report.floor["normal"][2]) - 1) < 0.01


def test_fixed_half_opacity_cut_would_discard_everything():
    """The reason the cut is adaptive: low-opacity (Brush-like) subjects have none >= 0.5."""
    cloud, *_ = _scene(subject_opacity=0.12)
    assert (cloud.opacities >= 0.5).sum() == 0
    keep, report = prune_gaussians(cloud)
    assert keep.sum() > 1000 and report.min_opacity < 0.12


def test_floor_removal_can_be_disabled():
    cloud, labels, names = _scene()
    keep, report = prune_gaussians(cloud, PruneOptions(remove_floor=False))
    assert report.floor is None
    assert _kept_fraction(keep, labels, names, "floor") > 0.5


def test_prune_cloud_returns_aligned_subset():
    cloud, *_ = _scene()
    pruned, report = prune_cloud(cloud)
    assert len(pruned.means) == report["kept"] == len(pruned.opacities) == len(pruned.sh_dc)
    assert pruned.means[:, 2].min() > 0.0  # nothing at or below the floor plane z=0 remains.


def test_rejects_invalid_opacity():
    cloud, *_ = _scene()
    with pytest.raises(ValueError, match="min_opacity"):
        prune_gaussians(cloud, PruneOptions(min_opacity=1.5))
