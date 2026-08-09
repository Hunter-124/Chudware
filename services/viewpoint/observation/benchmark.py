"""Deterministic information audit for camera-limited observations.

The analytic oracle appears only on the evaluation side of this module. It is never passed into an
observation or a sequential observer.
"""
from __future__ import annotations

import argparse
import json
from typing import Sequence

import numpy as np

from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import generate_part_corpus
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .metrics import quantify_information
from .model import INFORMATION_LEVELS, CorruptionConfig, ObservationConfig
from .sensor import SequentialObserver, _feature_samples, observe

_CHECKPOINTS = (1, 2, 4, 8)
_SEQUENCE_NAMES = ("random", "oracle_best_next")


def _seed(seed: int, *coordinates: int) -> int:
    return int(np.random.SeedSequence((seed, *coordinates)).generate_state(1, dtype=np.uint64)[0])


def _feature_neighbourhood_coverage(
    scene: object,
    feature_id: str,
    points: np.ndarray,
    config: ObservationConfig,
) -> float:
    """Evaluation-only surface support within one coarse sensor-pixel footprint."""
    samples, weights = _feature_samples(scene, feature_id)  # type: ignore[arg-type]
    if not len(points):
        return 0.0
    bounds = scene.mesh.vertices  # type: ignore[attr-defined]
    diagonal = float(np.linalg.norm(bounds.max(axis=0) - bounds.min(axis=0)))
    radius = max(config.prior_voxel_size, diagonal / min(config.depth_height, config.depth_width))
    supported = np.zeros(len(samples), dtype=bool)
    for start in range(0, len(points), 512):
        delta = samples[:, None, :] - points[None, start : start + 512, :]
        supported |= np.min(np.einsum("ijk,ijk->ij", delta, delta), axis=1) <= radius * radius
    return float(np.sum(weights[supported]))


def _step_angles(cameras: list[object], order: np.ndarray, looks: int) -> list[float]:
    angles: list[float] = []
    for first, second in zip(order[: looks - 1], order[1:looks]):
        cosine = float(np.clip(np.dot(cameras[int(first)].forward, cameras[int(second)].forward), -1.0, 1.0))
        angles.append(float(np.degrees(np.arccos(cosine))))
    return angles


def _mean_by_checkpoint(values: dict[int, list[float]]) -> dict[str, float]:
    return {str(checkpoint): float(np.mean(values[checkpoint])) for checkpoint in _CHECKPOINTS}


def _run_accumulation_audit(
    *,
    corpus: list[object],
    looks_per_part: int,
    candidate_pool: int,
    targets_per_part: int,
    seed: int,
    config: ObservationConfig,
    corruption: CorruptionConfig,
) -> dict[str, object]:
    checkpoints = tuple(checkpoint for checkpoint in _CHECKPOINTS if checkpoint <= looks_per_part)
    if checkpoints != _CHECKPOINTS:
        raise ValueError("the accumulated-look audit requires at least eight looks")

    measurements = {
        sequence: {
            level: {checkpoint: [[], []] for checkpoint in checkpoints}
            for level in INFORMATION_LEVELS
        }
        for sequence in _SEQUENCE_NAMES
    }
    point_counts = {
        sequence: {checkpoint: [] for checkpoint in checkpoints}
        for sequence in _SEQUENCE_NAMES
    }
    neighbourhood_coverage = {
        sequence: {checkpoint: [] for checkpoint in checkpoints}
        for sequence in _SEQUENCE_NAMES
    }
    step_angles = {sequence: [] for sequence in _SEQUENCE_NAMES}
    unique_camera_fractions = {sequence: [] for sequence in _SEQUENCE_NAMES}
    all_points_finite = True
    sequence_samples = 0

    for part_index, part in enumerate(corpus):
        target_ids = part.target_feature_ids[:targets_per_part]
        for target_index, feature_id in enumerate(target_ids):
            cameras = generate_benchmark_candidate_set(
                part.scene,
                feature_id,
                candidate_pool,
                seed=_seed(seed, part_index, target_index, 0xCAAD),
                image_size=(160, 120),
            )
            oracle_visibility = np.asarray(
                [1.0 - evaluate(part.scene, feature_id, camera).occlusion_fraction for camera in cameras],
                dtype=np.float64,
            )
            estimates = {level: np.empty(candidate_pool, dtype=np.float64) for level in INFORMATION_LEVELS}
            for camera_index, camera in enumerate(cameras):
                observation_seed = _seed(seed, part_index, target_index, camera_index, 0x0B5E)
                for level in ("full", "depth_only", "sparse_points"):
                    estimates[level][camera_index] = observe(
                        part.scene,
                        feature_id,
                        camera,
                        level=level,
                        seed=observation_seed,
                        config=config,
                        corruption=corruption,
                    ).visibility_estimate

            random_rng = np.random.default_rng(_seed(seed, part_index, target_index, 0xA11D))
            orders = {
                "random": random_rng.permutation(candidate_pool),
                "oracle_best_next": np.argsort(-oracle_visibility, kind="stable"),
            }
            sequence_samples += 1
            for sequence, order in orders.items():
                selected_order = order[:looks_per_part]
                step_angles[sequence].extend(_step_angles(cameras, selected_order, looks_per_part))
                positions = np.stack([cameras[int(index)].position for index in selected_order])
                unique_camera_fractions[sequence].append(
                    len(np.unique(np.round(positions, decimals=10), axis=0)) / looks_per_part
                )

                prior_observer = SequentialObserver(
                    part.scene,
                    feature_id,
                    level="depth_plus_prior",
                    seed=_seed(seed, part_index, target_index, 0xF053),
                    config=config,
                    corruption=corruption,
                )
                prior_estimates: list[float] = []
                for look_index, camera_index in enumerate(selected_order, start=1):
                    result = prior_observer.look(cameras[int(camera_index)])
                    prior_estimates.append(result.visibility_estimate)
                    if look_index in checkpoints:
                        all_points_finite &= bool(np.all(np.isfinite(result.accumulated_points)))
                        point_counts[sequence][look_index].append(float(len(result.accumulated_points)))
                        neighbourhood_coverage[sequence][look_index].append(
                            _feature_neighbourhood_coverage(
                                part.scene,
                                feature_id,
                                result.accumulated_points,
                                config,
                            )
                        )

                ordered_truth = oracle_visibility[selected_order]
                for checkpoint in checkpoints:
                    truth = float(np.max(ordered_truth[:checkpoint]))
                    for level in ("full", "depth_only", "sparse_points"):
                        estimate = float(np.max(estimates[level][selected_order[:checkpoint]]))
                        measurements[sequence][level][checkpoint][0].append(estimate)
                        measurements[sequence][level][checkpoint][1].append(truth)
                    measurements[sequence]["depth_plus_prior"][checkpoint][0].append(
                        prior_estimates[checkpoint - 1]
                    )
                    measurements[sequence]["depth_plus_prior"][checkpoint][1].append(truth)

    sequence_reports = {
        sequence: {
            "levels": {
                level: {
                    str(checkpoint): quantify_information(
                        measurements[sequence][level][checkpoint][0],
                        measurements[sequence][level][checkpoint][1],
                        bins=8,
                    ).as_dict()
                    for checkpoint in checkpoints
                }
                for level in INFORMATION_LEVELS
            },
            "diagnostics": {
                "mean_consecutive_camera_angle_deg": float(np.mean(step_angles[sequence])),
                "minimum_consecutive_camera_angle_deg": float(np.min(step_angles[sequence])),
                "mean_unique_camera_fraction": float(np.mean(unique_camera_fractions[sequence])),
                "mean_accumulated_point_count": _mean_by_checkpoint(point_counts[sequence]),
                "mean_feature_neighbourhood_coverage": _mean_by_checkpoint(
                    neighbourhood_coverage[sequence]
                ),
            },
        }
        for sequence in _SEQUENCE_NAMES
    }

    random_depth = sequence_reports["random"]["levels"]["depth_only"]  # type: ignore[index]
    oracle_depth = sequence_reports["oracle_best_next"]["levels"]["depth_only"]  # type: ignore[index]
    random_eight_correlation = float(random_depth["8"]["pearson_correlation"])  # type: ignore[index]
    oracle_eight_correlation = float(oracle_depth["8"]["pearson_correlation"])  # type: ignore[index]
    oracle_correlations = {
        checkpoint: float(oracle_depth[str(checkpoint)]["pearson_correlation"])  # type: ignore[index]
        for checkpoint in checkpoints
    }
    best_oracle_checkpoint = max(oracle_correlations, key=oracle_correlations.__getitem__)
    best_oracle_correlation = oracle_correlations[best_oracle_checkpoint]
    policy_score, random_score, oracle_score = 0.301, 0.123, 0.638
    policy_gap = oracle_score - policy_score

    def attenuation_estimate(correlation: float) -> tuple[float, float]:
        score_ceiling = random_score + correlation * (oracle_score - random_score)
        return score_ceiling, oracle_score - score_ceiling

    best_policy_ceiling, best_observation_loss = attenuation_estimate(best_oracle_correlation)
    random_eight_policy_ceiling, random_eight_observation_loss = attenuation_estimate(
        random_eight_correlation
    )
    same_ray_resampling = any(
        any(fraction < 1.0 for fraction in unique_camera_fractions[sequence])
        for sequence in _SEQUENCE_NAMES
    )
    evidence_overwritten = any(
        any(
            later < earlier
            for earlier, later in zip(
                (np.mean(point_counts[sequence][checkpoint]) for checkpoint in checkpoints[:-1]),
                (np.mean(point_counts[sequence][checkpoint]) for checkpoint in checkpoints[1:]),
            )
        )
        for sequence in _SEQUENCE_NAMES
    )
    feature_coverage_increases = all(
        neighbourhood_coverage[sequence][8]
        and np.mean(neighbourhood_coverage[sequence][8])
        > np.mean(neighbourhood_coverage[sequence][1])
        for sequence in _SEQUENCE_NAMES
    )

    return {
        "definition": "running maximum of per-look visibility estimates versus running maximum frozen-oracle visibility",
        "checkpoints": list(checkpoints),
        "candidate_pool": candidate_pool,
        "targets_per_part": targets_per_part,
        "sequence_samples": sequence_samples,
        "sequences": sequence_reports,
        "fusion_diagnostics": {
            "same_ray_resampling": same_ray_resampling,
            "evidence_overwritten": evidence_overwritten,
            "nan_poisoning": not all_points_finite,
            "feature_neighbourhood_coverage_increases": feature_coverage_increases,
            "fix": (
                "SequentialObserver now returns the running-best visibility estimate while each "
                "VisitRecord remains a per-frame outcome; the former current-view scalar discarded "
                "the information represented by earlier looks. Point fusion remains a finite-only "
                "voxel union and never receives oracle labels."
            ),
        },
        "ceiling_estimate": {
            "random_eight_look_depth_correlation": random_eight_correlation,
            "oracle_ordered_eight_look_depth_correlation": oracle_eight_correlation,
            "best_oracle_ordered_depth_checkpoint": best_oracle_checkpoint,
            "best_oracle_ordered_depth_correlation": best_oracle_correlation,
            "policy_score": policy_score,
            "random_score": random_score,
            "full_information_oracle_score": oracle_score,
            "policy_to_oracle_gap": policy_gap,
            "best_correlation_scaled_policy_ceiling": best_policy_ceiling,
            "best_gap_attributed_to_observation_quality": best_observation_loss,
            "best_fraction_of_gap_attributed_to_observation_quality": best_observation_loss / policy_gap,
            "random_eight_correlation_scaled_policy_ceiling": random_eight_policy_ceiling,
            "random_eight_gap_attributed_to_observation_quality": random_eight_observation_loss,
            "random_eight_fraction_of_gap_attributed_to_observation_quality": (
                random_eight_observation_loss / policy_gap
            ),
            "method": (
                "diagnostic linear attenuation anchored at random, not an identified causal bound: "
                "random + r_depth * (oracle - random)"
            ),
        },
    }


def run_benchmark(
    *,
    parts: int,
    looks_per_part: int,
    seed: int,
    candidate_pool: int = 16,
    targets_per_part: int = 8,
) -> dict[str, object]:
    if parts <= 0 or looks_per_part < 8:
        raise ValueError("parts must be positive and looks_per_part must be at least eight")
    if candidate_pool < looks_per_part:
        raise ValueError("candidate_pool must be at least looks_per_part")
    if targets_per_part <= 0:
        raise ValueError("targets_per_part must be positive")
    corpus = generate_part_corpus(parts, seed=seed)
    estimates: dict[str, list[float]] = {level: [] for level in INFORMATION_LEVELS}
    truth: list[float] = []
    part_ids: list[str] = []
    config = ObservationConfig()
    corruption = CorruptionConfig()

    for part_index, part in enumerate(corpus):
        feature_id = part.target_feature_ids[part_index % len(part.target_feature_ids)]
        cameras = generate_benchmark_candidate_set(
            part.scene,
            feature_id,
            looks_per_part,
            seed=seed + 1009 * part_index,
            image_size=(160, 120),
        )
        sequential = SequentialObserver(
            part.scene,
            feature_id,
            level="depth_plus_prior",
            seed=seed + 7919 * part_index,
            config=config,
            corruption=corruption,
        )
        for look_index, camera in enumerate(cameras):
            oracle_visibility = 1.0 - evaluate(part.scene, feature_id, camera).occlusion_fraction
            truth.append(oracle_visibility)
            part_ids.append(part.part_id)
            for level in ("full", "depth_only", "sparse_points"):
                result = observe(
                    part.scene,
                    feature_id,
                    camera,
                    level=level,
                    seed=seed + 104729 * part_index + look_index,
                    config=config,
                    corruption=corruption,
                )
                estimates[level].append(result.visibility_estimate)
            estimates["depth_plus_prior"].append(sequential.look(camera).visits[-1].visibility_estimate)

    return {
        "schema": "chudware.viewpoint.observation_information/2",
        "seed": seed,
        "parts": parts,
        "part_ids": sorted(set(part_ids)),
        "part_overlap": 0,
        "grouping_unit": "part_id",
        "looks_per_part": looks_per_part,
        "samples": len(truth),
        "oracle_target": "1 - frozen geometric oracle occlusion_fraction",
        "levels": {
            level: quantify_information(estimates[level], truth, bins=8).as_dict()
            for level in INFORMATION_LEVELS
        },
        "accumulation": _run_accumulation_audit(
            corpus=corpus,
            looks_per_part=looks_per_part,
            candidate_pool=candidate_pool,
            targets_per_part=targets_per_part,
            seed=seed,
            config=config,
            corruption=corruption,
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=int, default=8)
    parser.add_argument("--looks-per-part", type=int, default=8)
    parser.add_argument("--candidate-pool", type=int, default=16)
    parser.add_argument("--targets-per-part", type=int, default=8)
    parser.add_argument("--seed", type=int, default=1729)
    args = parser.parse_args(argv)
    print(json.dumps(run_benchmark(
        parts=args.parts,
        looks_per_part=args.looks_per_part,
        candidate_pool=args.candidate_pool,
        targets_per_part=args.targets_per_part,
        seed=args.seed,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
