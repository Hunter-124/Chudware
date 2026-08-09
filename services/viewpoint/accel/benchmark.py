"""Reproducible profile, parity, and throughput evidence for the Torch oracle backend."""

from __future__ import annotations

import argparse
import cProfile
import importlib
from io import StringIO
import json
import pstats
from time import perf_counter

from . import evaluate_many
from ..oracle import evaluate
from ..parts import generate_part, generate_part_corpus
from ..sampling import generate_benchmark_candidate_set


def _requests(part, poses: int, seed: int, *, all_targets: bool):
    cameras = generate_benchmark_candidate_set(
        part.scene,
        part.target_feature_ids[0],
        poses,
        seed=seed + part.part_index,
        image_size=(320, 240),
    )
    targets = part.target_feature_ids if all_targets else part.target_feature_ids[:1]
    return tuple((feature_id, camera) for feature_id in targets for camera in cameras)


def profile_reference() -> None:
    def workload() -> int:
        count = 0
        for part in generate_part_corpus(8, seed=1729):
            for feature_index, feature_id in enumerate(part.target_feature_ids):
                cameras = generate_benchmark_candidate_set(
                    part.scene,
                    feature_id,
                    12,
                    seed=1729 + part.part_index * 100 + feature_index,
                )
                for camera in cameras:
                    evaluate(part.scene, feature_id, camera)
                    count += 1
        return count

    profiler = cProfile.Profile()
    profiler.enable()
    labels = workload()
    profiler.disable()
    stream = StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(30)
    print(f"labels={labels}")
    print(stream.getvalue())


def parity_report() -> None:
    torch = importlib.import_module("torch")
    fields = (
        "in_frame_coverage",
        "occlusion_fraction",
        "foreshortening_quality",
        "scale_quality",
        "silhouette_informativeness",
        "depth_validity",
        "ambiguity",
    )
    maximum = {field: 0.0 for field in fields}
    maximum.update(aggregate=0.0, apparent_scale_px=0.0)
    comparisons = 0
    id_mismatches = 0
    for part in generate_part_corpus(2, seed=8109):
        requests = []
        for feature_index, feature_id in enumerate(part.target_feature_ids):
            cameras = generate_benchmark_candidate_set(
                part.scene,
                feature_id,
                3,
                seed=9100 + 100 * part.part_index + feature_index,
                image_size=(160, 120),
            )
            requests.extend((feature_id, camera) for camera in cameras)
        reference = tuple(evaluate(part.scene, feature_id, camera) for feature_id, camera in requests)
        for backend in ("cpu", "cuda"):
            actual = evaluate_many(part.scene, requests, device=backend)
            if backend == "cuda" and torch.cuda.is_available():
                torch.cuda.synchronize()
            for expected, observed in zip(reference, actual):
                comparisons += 1
                for field in fields:
                    maximum[field] = max(maximum[field], abs(float(getattr(expected, field)) - float(getattr(observed, field))))
                maximum["aggregate"] = max(maximum["aggregate"], abs(expected.aggregate - observed.aggregate))
                maximum["apparent_scale_px"] = max(
                    maximum["apparent_scale_px"], abs(expected.apparent_scale_px - observed.apparent_scale_px)
                )
                id_mismatches += int(expected.ambiguous_feature_ids != observed.ambiguous_feature_ids)
    print(json.dumps({"backend_label_comparisons": comparisons, "component_max_abs_error": maximum, "ambiguous_id_mismatches": id_mismatches}, indent=2))


def throughput_report() -> None:
    torch = importlib.import_module("torch")
    part_count = 24
    pose_count = 8
    seed = 28173
    warm = generate_part(999, seed=seed)
    warm_requests = _requests(warm, pose_count, seed, all_targets=False)[:4]
    evaluate_many(warm.scene, warm_requests, device="cpu")
    evaluate_many(warm.scene, warm_requests, device="cuda")
    if torch.cuda.is_available():
        torch.cuda.synchronize()

    started = perf_counter()
    parts = generate_part_corpus(part_count, seed=seed)
    generation_seconds = perf_counter() - started
    result = {
        "parts": part_count,
        "poses_per_target": pose_count,
        "part_generation_seconds": generation_seconds,
        "part_generation_parts_per_second": part_count / generation_seconds,
    }
    for backend in ("numpy", "cpu", "cuda"):
        label_count = 0
        started = perf_counter()
        for part in parts:
            requests = _requests(part, pose_count, seed, all_targets=False)
            if backend == "numpy":
                tuple(evaluate(part.scene, feature_id, camera) for feature_id, camera in requests)
            else:
                evaluate_many(part.scene, requests, device=backend)
            label_count += len(requests)
        if backend == "cuda" and torch.cuda.is_available():
            torch.cuda.synchronize()
        elapsed = perf_counter() - started
        result[backend] = {
            "seconds": elapsed,
            "labels": label_count,
            "features_per_second": label_count / elapsed,
            "projected_5000_part_wall_seconds": 5000 * (elapsed + generation_seconds) / part_count,
        }
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("profile", "parity", "throughput"))
    mode = parser.parse_args().mode
    {"profile": profile_reference, "parity": parity_report, "throughput": throughput_report}[mode]()


if __name__ == "__main__":
    main()
