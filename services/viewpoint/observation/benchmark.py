"""Deterministic information audit for camera-limited observations.

The analytic oracle appears only on the evaluation side of this module.  It is never passed into an
observation or a sequential observer.
"""
from __future__ import annotations

import argparse
import json
from typing import Sequence

from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import generate_part_corpus
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .metrics import quantify_information
from .model import INFORMATION_LEVELS, CorruptionConfig, ObservationConfig
from .sensor import SequentialObserver, observe


def run_benchmark(*, parts: int, looks_per_part: int, seed: int) -> dict[str, object]:
    if parts <= 0 or looks_per_part <= 1:
        raise ValueError("parts must be positive and looks_per_part must exceed one")
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
            estimates["depth_plus_prior"].append(sequential.look(camera).visibility_estimate)

    return {
        "schema": "chudware.viewpoint.observation_information/1",
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
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=int, default=8)
    parser.add_argument("--looks-per-part", type=int, default=8)
    parser.add_argument("--seed", type=int, default=1729)
    args = parser.parse_args(argv)
    print(json.dumps(run_benchmark(parts=args.parts, looks_per_part=args.looks_per_part, seed=args.seed), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
