from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from services.viewpoint.accel import evaluate_many
from services.viewpoint.observation import (
    CorruptionConfig,
    InformationLevel,
    ObservationConfig,
    SequentialObserver,
)
from services.viewpoint.parts import GENERATOR_VERSION, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .data import ViewpointBatch, encode_task, locked_expansion_part_indices
from .durable import durable_replace
from .metrics import sequential_evaluation_report
from .model import COMPONENTS, NumpyViewpointPolicy, PolicyWeights, weighted_geometric_mean
from .observation_data import (
    LIMITED_INFORMATION_LEVELS,
    context_camera,
    encode_observation,
    encode_observation_candidates,
)
from .runtime import default_device, generation_workers, oracle_device, report_device, resolve_device

ProgressCallback = Callable[[dict[str, object]], None]
Array = np.ndarray


@dataclass(frozen=True)
class SequentialConfig:
    part_indices: tuple[int, ...]
    level: InformationLevel = "depth_plus_prior"
    examples_per_part: int = 6
    candidates: int = 256
    budgets: tuple[int, ...] = (1, 2, 4, 8)
    workers: int = 1
    seed: int = 1729
    image_size: tuple[int, int] = (96, 96)
    observation: ObservationConfig = ObservationConfig(
        depth_height=12,
        depth_width=16,
        sparse_point_count=48,
        feature_region_quantum_px=4.0,
        feature_uncertainty_px=8.0,
        prior_voxel_size=0.08,
    )
    corruption: CorruptionConfig = CorruptionConfig()
    bootstrap_samples: int = 2000
    device: str | None = None

    def validate(self) -> "SequentialConfig":
        if not self.part_indices or any(int(index) < 0 for index in self.part_indices):
            raise ValueError("part_indices must contain non-negative values")
        if len(set(self.part_indices)) != len(self.part_indices):
            raise ValueError("part_indices must be unique")
        if self.level not in LIMITED_INFORMATION_LEVELS:
            raise ValueError("sequential evaluation requires a limited information level")
        if self.examples_per_part <= 0 or self.candidates <= 0 or self.workers <= 0:
            raise ValueError("examples_per_part, candidates, and workers must be positive")
        if tuple(sorted(set(self.budgets))) != self.budgets or not self.budgets:
            raise ValueError("budgets must be unique and increasing")
        if self.budgets[-1] > self.candidates:
            raise ValueError("the largest look budget cannot exceed the candidate pool")
        if self.bootstrap_samples <= 0:
            raise ValueError("bootstrap_samples must be positive")
        return self


def _candidate_seed(seed: int, part_index: int, example_index: int) -> int:
    return int(np.random.SeedSequence((seed, part_index, example_index, 0x1A11)).generate_state(1, dtype=np.uint32)[0])


def _sequence_seed(seed: int, part_index: int, example_index: int) -> int:
    return int(np.random.SeedSequence((seed, part_index, example_index, 0x5E90)).generate_state(1, dtype=np.uint64)[0])


def _evaluate_candidates(
    scene: object,
    target_id: str,
    poses: list[object],
    device: str,
) -> tuple[list[dict[str, float]], str]:
    labels, backend = evaluate_many(
        scene,  # type: ignore[arg-type]
        tuple((target_id, pose) for pose in poses),  # type: ignore[misc]
        device=oracle_device(device),
        return_info=True,
    )
    return [label.quality_components() for label in labels], backend.backend


def _one_row_inputs(
    task: Array,
    observation: object,
    candidates: list[object],
    available: Array,
) -> dict[str, Array]:
    geometry, geometry_mask, feature_mask = encode_observation(observation)  # type: ignore[arg-type]
    return {
        "task": np.asarray(task, np.float32)[None, :],
        "geometry": geometry[None, :, :],
        "geometry_mask": geometry_mask[None, :],
        "feature_mask": feature_mask[None, :],
        "candidates": encode_observation_candidates(observation, candidates)[None, :, :],  # type: ignore[arg-type]
        "candidate_mask": np.asarray(available, bool)[None, :],
    }


def evaluate_sequential_policy(
    policy: NumpyViewpointPolicy,
    config: SequentialConfig,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run adaptive K-look selection and a non-adaptive random schedule at equal cost."""
    config.validate()
    actual_device = resolve_device(config.device)
    actual_workers = generation_workers(actual_device, config.workers)
    oracle_backend = oracle_device(actual_device)
    part_ids: list[str] = []
    difficulties: list[str] = []
    tasks: list[Array] = []
    initial_geometries: list[Array] = []
    initial_geometry_masks: list[Array] = []
    initial_feature_masks: list[Array] = []
    initial_candidates: list[Array] = []
    component_rows: list[Array] = []
    policy_best = {budget: [] for budget in config.budgets}
    random_best = {budget: [] for budget in config.budgets}
    policy_indices: list[list[int]] = []
    random_indices: list[list[int]] = []

    for part_index in config.part_indices:
        examples = generate_part_examples(part_index, config.examples_per_part, seed=config.seed)
        for example_index, example in enumerate(examples):
            poses = generate_benchmark_candidate_set(
                example.scene,
                example.target_id,
                config.candidates,
                seed=_candidate_seed(config.seed, part_index, example_index),
                image_size=config.image_size,
            )
            labels, oracle_backend = _evaluate_candidates(
                example.scene, example.target_id, poses, actual_device,
            )
            components = np.asarray(
                [[label[name] for name in COMPONENTS] for label in labels],
                np.float32,
            )
            quality = weighted_geometric_mean(components)
            task = encode_task(example.feature_kind, example.question_kind)
            observer = SequentialObserver(
                example.scene,
                example.target_id,
                level=config.level,
                seed=_sequence_seed(config.seed, part_index, example_index),
                corruption=config.corruption,
                config=config.observation,
            )
            initial = observer.look(context_camera(example.scene, image_size=config.image_size))
            geometry, geometry_mask, feature_mask = encode_observation(initial)
            initial_geometries.append(geometry)
            initial_geometry_masks.append(geometry_mask)
            initial_feature_masks.append(feature_mask)
            initial_candidates.append(encode_observation_candidates(initial, poses))

            available = np.ones(config.candidates, bool)
            observation = initial
            chosen: list[int] = []
            realized: list[float] = []
            for look in range(1, config.budgets[-1] + 1):
                selected, _ = policy.select(**_one_row_inputs(task, observation, poses, available))
                index = int(selected[0])
                chosen.append(index)
                realized.append(float(quality[index]))
                available[index] = False
                if look < config.budgets[-1]:
                    observation = observer.look(poses[index])
                if look in policy_best:
                    policy_best[look].append(max(realized))
            rng = np.random.default_rng(_sequence_seed(config.seed + 1, part_index, example_index))
            random_order = rng.permutation(config.candidates).tolist()
            random_realized = [float(quality[index]) for index in random_order]
            for budget in config.budgets:
                random_best[budget].append(max(random_realized[:budget]))

            part_ids.append(example.part_id)
            difficulties.append(example.difficulty)
            tasks.append(task)
            component_rows.append(components)
            policy_indices.append(chosen)
            random_indices.append(random_order)
            if progress is not None:
                progress({
                    "complete": False,
                    "information_level": config.level,
                    "candidate_pool": config.candidates,
                    "device": actual_device,
                    "oracle_backend": oracle_backend,
                    "look_budgets": list(config.budgets),
                    "completed_examples": len(part_ids),
                    "expected_examples": len(config.part_indices) * config.examples_per_part,
                    "completed_part_ids": list(part_ids),
                    "policy_best_by_budget": {
                        str(budget): list(values) for budget, values in policy_best.items()
                    },
                    "one_shot_random_best_by_budget": {
                        str(budget): list(values) for budget, values in random_best.items()
                    },
                    "policy_selection_indices": list(policy_indices),
                    "one_shot_random_indices": list(random_indices),
                })

    batch = ViewpointBatch(
        part_ids=tuple(part_ids),
        task=np.stack(tasks),
        geometry=np.stack(initial_geometries),
        geometry_mask=np.stack(initial_geometry_masks),
        feature_mask=np.stack(initial_feature_masks),
        candidates=np.stack(initial_candidates),
        candidate_mask=np.ones((len(part_ids), config.candidates), bool),
        components=np.stack(component_rows),
        difficulties=tuple(difficulties),
    ).validate()
    report = sequential_evaluation_report(
        batch,
        policy_best_by_budget={budget: np.asarray(values) for budget, values in policy_best.items()},
        one_shot_random_best_by_budget={budget: np.asarray(values) for budget, values in random_best.items()},
        bootstrap_samples=config.bootstrap_samples,
        bootstrap_seed=config.seed + 2,
    )
    report.update({
        "information_level": config.level,
        "generator_version": GENERATOR_VERSION,
        "seed": config.seed,
        "device": actual_device,
        "oracle_backend": oracle_backend,
        "fit_backend": "torch-cpu",
        "fit_stage": "prefit_weights_loaded",
        "policy_backend": "numpy-cpu",
        "workers": actual_workers,
        "common_initial_observation": "fixed calibrated isometric camera; counted once for both methods",
        "common_initial_render_count": 1,
        "additional_render_budgets": list(config.budgets),
        "total_render_budgets": [1 + budget for budget in config.budgets],
        "render_accounting": {
            str(budget): {
                "common_initial": 1,
                "policy_additional": budget,
                "one_shot_random_additional": budget,
                "policy_total": 1 + budget,
                "one_shot_random_total": 1 + budget,
            }
            for budget in config.budgets
        },
        "policy_schedule": "adaptive; every next camera is scored from the accumulated observation",
        "one_shot_schedule": "seeded random permutation fixed before any render outcome",
        "candidate_pool": config.candidates,
        "candidate_pool_exhausted_at": config.candidates,
        "policy_selection_indices": policy_indices,
        "one_shot_random_indices": random_indices,
    })
    return report


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    durable_replace(temporary, path)


def run_locked_test_curves(
    *,
    weights_dir: Path = Path("training/viewpoint"),
    levels: tuple[InformationLevel, ...] = LIMITED_INFORMATION_LEVELS,
    train_parts: int = 5000,
    candidates: int = 256,
    budgets: tuple[int, ...] = (1, 2, 4, 8),
    examples_per_part: int = 6,
    workers: int = 1,
    seed: int = 1729,
    image_size: tuple[int, int] = (96, 96),
    bootstrap_samples: int = 2000,
    checkpoint_path: Path | None = None,
    device: str | None = None,
) -> dict[str, object]:
    """Evaluate checked-in limited-information policies on the locked test split."""
    _, _, test_indices = locked_expansion_part_indices(train_parts, seed=seed)
    actual_device = resolve_device(device)
    actual_workers = generation_workers(actual_device, workers)
    curves: dict[str, object] = {}
    checkpoint_levels: dict[str, object] = {}

    def checkpoint(level: str, payload: dict[str, object]) -> None:
        if checkpoint_path is None:
            return
        checkpoint_levels[level] = payload
        _write_json_atomic(checkpoint_path, {
            "complete": False,
            "seed": seed,
            "locked_test_parts": len(test_indices),
            "candidate_pool": candidates,
            "look_budgets": list(budgets),
            "levels": checkpoint_levels,
            "device": actual_device,
        })
    for level in levels:
        weights, spec = PolicyWeights.load(weights_dir / f"limited_{level}_weights.npz")
        curves[level] = evaluate_sequential_policy(
            NumpyViewpointPolicy(weights, spec),
            SequentialConfig(
                test_indices,
                level=level,
                examples_per_part=examples_per_part,
                candidates=candidates,
                budgets=budgets,
                seed=seed,
                workers=actual_workers,
                image_size=image_size,
                bootstrap_samples=bootstrap_samples,
                device=actual_device,
            ),
            progress=lambda payload, current_level=level: checkpoint(current_level, payload),
        )
        checkpoint_levels[level] = {"complete": True, "report": curves[level]}
        if checkpoint_path is not None:
            _write_json_atomic(checkpoint_path, {
                "complete": False,
                "seed": seed,
                "locked_test_parts": len(test_indices),
                "candidate_pool": candidates,
                "device": actual_device,
                "look_budgets": list(budgets),
                "levels": checkpoint_levels,
            })
    return {
        "seed": seed,
        "locked_test_parts": len(test_indices),
        "candidate_pool": candidates,
        "device": actual_device,
        "fit_backend": "torch-cpu",
        "fit_stage": "prefit_weights_loaded",
        "workers": actual_workers,
        "look_budgets": list(budgets),
        "levels": curves,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate sequential viewpoint policies on the locked test split")
    parser.add_argument("--weights-dir", type=Path, default=Path("training/viewpoint"))
    parser.add_argument("--levels", nargs="+", choices=LIMITED_INFORMATION_LEVELS, default=list(LIMITED_INFORMATION_LEVELS))
    parser.add_argument("--train-parts", type=int, default=5000)
    parser.add_argument("--candidates", type=int, default=256)
    parser.add_argument("--budgets", type=int, nargs="+", default=(1, 2, 4, 8))
    parser.add_argument("--examples-per-part", type=int, default=6)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--device", default=default_device(), help="actual oracle device; defaults to CUDA when available")
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--image-size", type=int, nargs=2, default=(96, 96), metavar=("WIDTH", "HEIGHT"))
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(argv)
    report = run_locked_test_curves(
        weights_dir=args.weights_dir,
        levels=tuple(args.levels),
        train_parts=args.train_parts,
        candidates=args.candidates,
        budgets=tuple(args.budgets),
        examples_per_part=args.examples_per_part,
        workers=args.workers,
        device=args.device,
        seed=args.seed,
        image_size=tuple(args.image_size),
        bootstrap_samples=args.bootstrap_samples,
        checkpoint_path=args.output_report,
    )
    report_device(str(report["device"]), str(report["fit_backend"]))
    if args.output_report is None:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _write_json_atomic(args.output_report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SequentialConfig", "evaluate_sequential_policy", "run_locked_test_curves"]
