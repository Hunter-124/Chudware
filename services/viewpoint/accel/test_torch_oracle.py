from __future__ import annotations

import importlib

import numpy as np
import pytest

from services.viewpoint.accel import evaluate_many
from services.viewpoint.accel.torch_oracle import _RayBatch, _torch_ray_hits
from services.viewpoint.geometry import CameraPose, Feature, Mesh, Scene
from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import generate_part_corpus
from services.viewpoint.sampling import generate_benchmark_candidate_set, near_tangent_grazing


_COMPONENT_FIELDS = (
    "in_frame_coverage",
    "occlusion_fraction",
    "foreshortening_quality",
    "scale_quality",
    "silhouette_informativeness",
    "depth_validity",
    "ambiguity",
)


def _assert_label_parity(reference, actual, *, tolerance: float = 5e-12) -> None:
    for field in _COMPONENT_FIELDS:
        assert getattr(actual, field) == pytest.approx(getattr(reference, field), abs=tolerance, rel=0.0), field
    assert actual.apparent_scale_px == pytest.approx(reference.apparent_scale_px, abs=tolerance, rel=1e-13)
    if reference.foreshortening_angle_deg is None:
        assert actual.foreshortening_angle_deg is None
    else:
        assert actual.foreshortening_angle_deg == pytest.approx(reference.foreshortening_angle_deg, abs=tolerance)
    assert actual.ambiguous_feature_ids == reference.ambiguous_feature_ids
    assert actual.aggregate == pytest.approx(reference.aggregate, abs=tolerance, rel=1e-13)


def _randomized_requests():
    cases = []
    for part in generate_part_corpus(2, seed=8109):
        for feature_index, feature_id in enumerate(part.target_feature_ids):
            poses = generate_benchmark_candidate_set(
                part.scene,
                feature_id,
                3,
                seed=9100 + 100 * part.part_index + feature_index,
                image_size=(160, 120),
            )
            cases.append((part.scene, tuple((feature_id, pose) for pose in poses)))
    return cases


def test_torch_cpu_matches_frozen_oracle_across_randomized_parts_features_and_poses() -> None:
    pytest.importorskip("torch", exc_type=ImportError)
    for scene, requests in _randomized_requests():
        reference = tuple(evaluate(scene, feature_id, camera) for feature_id, camera in requests)
        actual, info = evaluate_many(scene, requests, device="cpu", return_info=True)
        assert info.backend == "torch-cpu"
        for expected, observed in zip(reference, actual):
            _assert_label_parity(expected, observed)


def test_cuda_matches_frozen_oracle_across_randomized_parts_features_and_poses() -> None:
    torch = pytest.importorskip("torch", exc_type=ImportError)
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable")
    for scene, requests in _randomized_requests():
        reference = tuple(evaluate(scene, feature_id, camera) for feature_id, camera in requests)
        actual, info = evaluate_many(scene, requests, device="cuda", return_info=True)
        assert info.backend == "cuda"
        assert info.ray_chunk_size > 0
        for expected, observed in zip(reference, actual):
            _assert_label_parity(expected, observed)


def test_degenerate_zero_area_grazing_and_fully_occluded_labels_match() -> None:
    pytest.importorskip("torch", exc_type=ImportError)
    vertices = np.asarray(
        (
            (-2.0, -2.0, 1.0),
            (2.0, -2.0, 1.0),
            (2.0, 2.0, 1.0),
            (-2.0, 2.0, 1.0),
        )
    )
    scene = Scene(
        Mesh(vertices, np.asarray(((0, 1, 2), (0, 2, 3)))),
        (
            Feature("occluded", "region", points=np.asarray(((0.0, 0.0, 0.0),))),
            Feature("zero_projection", "region", points=np.asarray(((0.0, 0.0, 1.0),))),
        ),
    )
    camera = CameraPose((0.0, 0.0, 5.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), vertical_fov_deg=60.0)
    grazing = near_tangent_grazing(
        np.asarray((0.0, 0.0, 0.0)),
        2,
        radius=5.0,
        seed=4,
        orientation=np.asarray((0.0, 0.0, 1.0)),
        grazing_angle_deg=1e-6,
    )
    requests = (
        ("occluded", camera),
        ("zero_projection", camera),
        ("occluded", grazing[0]),
        ("occluded", grazing[1]),
    )
    expected = tuple(evaluate(scene, feature_id, pose) for feature_id, pose in requests)
    actual = evaluate_many(scene, requests, device="cpu")
    assert expected[0].occlusion_fraction == 1.0
    assert expected[1].apparent_scale_px == 0.0
    for left, right in zip(expected, actual):
        _assert_label_parity(left, right)


def test_nan_ray_is_a_miss_like_frozen_predicate() -> None:
    torch = pytest.importorskip("torch", exc_type=ImportError)
    scene = Scene(
        Mesh(np.asarray(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))), np.asarray(((0, 1, 2),))),
        (Feature("face", "face", triangle_indices=(0,)),),
    )
    rays = _RayBatch.empty()
    rays.add(np.asarray((0.0, 0.0, 1.0)), np.asarray((np.nan, 0.0, -1.0)), 1e-7, 10.0, -1)
    hit, _, repaired = _torch_ray_hits(scene, rays, np.zeros((1, 1), dtype=bool), 1e-7, torch, torch.device("cpu"))
    assert hit.tolist() == [False]
    assert repaired == 1


def test_torch_import_failure_falls_back_to_frozen_numpy(monkeypatch) -> None:
    scene, requests = _randomized_requests()[0]
    expected = tuple(evaluate(scene, feature_id, camera) for feature_id, camera in requests)
    real_import = importlib.import_module

    def unavailable(name: str, package=None):
        if name == "torch":
            raise ImportError("blocked for optional-dependency test")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", unavailable)
    actual, info = evaluate_many(scene, requests, device="auto", return_info=True)
    assert info.backend == "numpy"
    assert actual == expected
