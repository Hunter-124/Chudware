from __future__ import annotations

from dataclasses import fields

import numpy as np
import pytest

from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import generate_part
from services.viewpoint.sampling import generate_benchmark_candidate_set
from .benchmark import run_benchmark

from . import (
    CorruptionConfig,
    DepthOnlyObservation,
    DepthPlusPriorObservation,
    FullObservation,
    INFORMATION_LEVELS,
    ObservationConfig,
    SequentialObserver,
    SparsePointsObservation,
    observe,
    quantify_information,
)


def _case() -> tuple[object, str, list[object]]:
    part = generate_part(3, seed=1729)
    feature_id = part.target_feature_ids[0]
    cameras = generate_benchmark_candidate_set(part.scene, feature_id, 4, seed=1941, image_size=(160, 120))
    return part.scene, feature_id, cameras


@pytest.mark.parametrize(
    ("level", "expected_type", "specific_fields"),
    (
        ("full", FullObservation, {"mesh_vertices", "mesh_triangles", "depth_map", "normal_map"}),
        ("depth_only", DepthOnlyObservation, {"depth_map", "normal_map"}),
        (
            "depth_plus_prior",
            DepthPlusPriorObservation,
            {"depth_map", "normal_map", "accumulated_points", "accumulated_normals"},
        ),
        ("sparse_points", SparsePointsObservation, {"points", "normals"}),
    ),
)
def test_information_levels_expose_only_declared_fields(level: str, expected_type: type, specific_fields: set[str]) -> None:
    scene, feature_id, cameras = _case()
    result = observe(scene, feature_id, cameras[0], level=level, seed=17)
    common = {
        "level",
        "camera",
        "workspace_bounds",
        "feature_region",
        "visits",
        "visibility_estimate",
        "valid_return_fraction",
    }
    assert type(result) is expected_type
    assert {field.name for field in fields(result)} == common | specific_fields

    forbidden = {
        "scene",
        "feature",
        "feature_id",
        "triangle_indices",
        "orientation",
        "occlusion_fraction",
        "ambiguous_feature_ids",
        "aggregate",
        "quality_components",
        "component_labels",
    }
    assert forbidden.isdisjoint(dir(result))
    assert not hasattr(result, "__dict__")
    assert result.workspace_bounds.as_vector().shape == (6,)
    assert result.feature_region.as_vector().shape == (8,)
    if level != "full":
        assert not hasattr(result, "mesh_vertices")
        assert not hasattr(result, "mesh_triangles")


def test_observation_is_deterministic_under_seed() -> None:
    scene, feature_id, cameras = _case()
    corruption = CorruptionConfig(
        depth_noise_std=0.03,
        grazing_dropout_probability=0.6,
        grazing_cosine_threshold=0.8,
        calibration_position_std=0.02,
        calibration_angle_std_deg=0.5,
    )
    first = observe(scene, feature_id, cameras[0], level="sparse_points", seed=991, corruption=corruption)
    second = observe(scene, feature_id, cameras[0], level="sparse_points", seed=991, corruption=corruption)
    third = observe(scene, feature_id, cameras[0], level="sparse_points", seed=992, corruption=corruption)
    np.testing.assert_array_equal(first.points, second.points)
    np.testing.assert_array_equal(first.normals, second.normals)
    np.testing.assert_array_equal(first.feature_region.as_vector(), second.feature_region.as_vector())
    assert first.visibility_estimate == second.visibility_estimate
    assert not np.array_equal(first.points, third.points)


def test_sequential_observer_accumulates_k_looks_and_outcomes() -> None:
    scene, feature_id, cameras = _case()
    observer = SequentialObserver(scene, feature_id, level="depth_plus_prior", seed=81)
    results = observer.looks(cameras[:3])
    assert [len(result.visits) for result in results] == [1, 2, 3]
    assert observer.visit_count == 3
    assert all(isinstance(result, DepthPlusPriorObservation) for result in results)
    point_counts = [len(result.accumulated_points) for result in results]
    assert point_counts == sorted(point_counts)
    assert point_counts[-1] > point_counts[0]
    assert all(
        result.visibility_estimate == max(record.visibility_estimate for record in result.visits)
        for result in results
    )
    assert results[-1].visits[-1].valid_return_fraction == results[-1].valid_return_fraction


def test_sequential_visibility_retains_a_better_earlier_look() -> None:
    part = generate_part(0, seed=1729)
    feature_id = part.target_feature_ids[0]
    cameras = generate_benchmark_candidate_set(
        part.scene, feature_id, 8, seed=1729, image_size=(160, 120)
    )
    first, second = SequentialObserver(
        part.scene, feature_id, level="depth_only", seed=81
    ).looks(cameras[:2])
    assert second.visits[-1].visibility_estimate < first.visibility_estimate
    assert second.visibility_estimate == first.visibility_estimate


def test_each_corruption_is_independently_measurable() -> None:
    scene, feature_id, cameras = _case()
    camera = cameras[1]
    config = ObservationConfig(depth_height=30, depth_width=40)
    clean = observe(scene, feature_id, camera, level="depth_only", seed=701, config=config)

    noisy = observe(
        scene,
        feature_id,
        camera,
        level="depth_only",
        seed=701,
        config=config,
        corruption=CorruptionConfig(depth_noise_std=0.08),
    )
    shared = np.isfinite(clean.depth_map) & np.isfinite(noisy.depth_map)
    assert np.count_nonzero(shared) > 10
    assert float(np.mean(np.abs(clean.depth_map[shared] - noisy.depth_map[shared]))) > 0.01
    np.testing.assert_array_equal(np.isfinite(clean.depth_map), np.isfinite(noisy.depth_map))

    grazing = observe(
        scene,
        feature_id,
        camera,
        level="depth_only",
        seed=701,
        config=config,
        corruption=CorruptionConfig(grazing_dropout_probability=1.0, grazing_cosine_threshold=1.0),
    )
    assert np.count_nonzero(np.isfinite(grazing.depth_map)) < np.count_nonzero(np.isfinite(clean.depth_map))

    calibrated = observe(
        scene,
        feature_id,
        camera,
        level="depth_only",
        seed=701,
        config=config,
        corruption=CorruptionConfig(calibration_position_std=0.4, calibration_angle_std_deg=2.0),
    )
    same_returns = np.isfinite(clean.depth_map) & np.isfinite(calibrated.depth_map)
    changed_support = not np.array_equal(np.isfinite(clean.depth_map), np.isfinite(calibrated.depth_map))
    changed_depth = bool(np.any(np.abs(clean.depth_map[same_returns] - calibrated.depth_map[same_returns]) > 1e-5))
    assert changed_support or changed_depth

    grazing_with_noise = observe(
        scene,
        feature_id,
        camera,
        level="depth_only",
        seed=701,
        config=config,
        corruption=CorruptionConfig(
            depth_noise_std=0.08,
            grazing_dropout_probability=1.0,
            grazing_cosine_threshold=1.0,
        ),
    )
    np.testing.assert_array_equal(np.isfinite(grazing.depth_map), np.isfinite(grazing_with_noise.depth_map))


def test_visibility_readout_uses_each_levels_available_geometry() -> None:
    full: list[float] = []
    depth: list[float] = []
    sparse: list[float] = []
    oracle_visibility: list[float] = []
    for part_index in range(4):
        part = generate_part(part_index, seed=1729)
        feature_id = part.target_feature_ids[part_index % len(part.target_feature_ids)]
        cameras = generate_benchmark_candidate_set(
            part.scene, feature_id, 3, seed=300 + part_index, image_size=(160, 120)
        )
        for camera_index, camera in enumerate(cameras):
            observation_seed = 500 + 10 * part_index + camera_index
            full.append(observe(part.scene, feature_id, camera, level="full", seed=observation_seed).visibility_estimate)
            depth.append(observe(part.scene, feature_id, camera, level="depth_only", seed=observation_seed).visibility_estimate)
            sparse.append(observe(part.scene, feature_id, camera, level="sparse_points", seed=observation_seed).visibility_estimate)
            oracle_visibility.append(1.0 - evaluate(part.scene, feature_id, camera).occlusion_fraction)

    np.testing.assert_allclose(full, oracle_visibility, atol=1e-12)
    assert full != depth
    assert depth != sparse


def test_information_metric_quantifies_visibility_signal_without_leaking_label() -> None:
    scene, feature_id, cameras = _case()
    estimates = []
    truth = []
    for index, camera in enumerate(cameras):
        result = observe(scene, feature_id, camera, level="depth_only", seed=100 + index)
        estimates.append(result.visibility_estimate)
        truth.append(1.0 - evaluate(scene, feature_id, camera).occlusion_fraction)
    report = quantify_information(estimates, truth, bins=4)
    assert report.samples == len(cameras)
    assert np.isfinite(report.pearson_correlation)
    assert report.mutual_information_bits >= 0.0
    assert set(report.as_dict()) == {
        "samples",
        "pearson_correlation",
        "mutual_information_bits",
        "estimate_mean",
        "oracle_mean",
    }


def test_accumulated_information_audit_reports_all_levels_and_sequence_budgets() -> None:
    report = run_benchmark(
        parts=1,
        looks_per_part=8,
        candidate_pool=8,
        targets_per_part=2,
        seed=1729,
    )
    accumulation = report["accumulation"]
    assert accumulation["checkpoints"] == [1, 2, 4, 8]
    assert accumulation["sequence_samples"] == 2
    for sequence in ("random", "oracle_best_next"):
        levels = accumulation["sequences"][sequence]["levels"]
        assert set(levels) == set(INFORMATION_LEVELS)
        assert all(set(by_budget) == {"1", "2", "4", "8"} for by_budget in levels.values())
        assert all(
            by_budget["pearson_correlation"] == pytest.approx(1.0)
            for by_budget in levels["full"].values()
        )
    diagnostics = accumulation["fusion_diagnostics"]
    assert diagnostics["same_ray_resampling"] is False
    assert diagnostics["evidence_overwritten"] is False
    assert diagnostics["nan_poisoning"] is False
    assert diagnostics["feature_neighbourhood_coverage_increases"] is True
