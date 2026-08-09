from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from services.viewpoint.observation import (
    CorruptionConfig,
    InformationLevel,
    ObservationConfig,
    SequentialObserver,
)
from services.viewpoint.oracle import evaluate
from services.viewpoint.parts import GENERATOR_VERSION, generate_part_examples
from services.viewpoint.sampling import generate_benchmark_candidate_set

from .data import ViewpointBatch, encode_task
from .metrics import sequential_evaluation_report
from .model import COMPONENTS, NumpyViewpointPolicy, weighted_geometric_mean
from .observation_data import (
    LIMITED_INFORMATION_LEVELS,
    context_camera,
    encode_observation,
    encode_observation_candidates,
)

Array = np.ndarray


@dataclass(frozen=True)
class SequentialConfig:
    part_indices: tuple[int, ...]
    level: InformationLevel = "depth_plus_prior"
    examples_per_part: int = 6
    candidates: int = 8
    budgets: tuple[int, ...] = (1, 2, 4, 8)
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

    def validate(self) -> "SequentialConfig":
        if not self.part_indices or any(int(index) < 0 for index in self.part_indices):
            raise ValueError("part_indices must contain non-negative values")
        if len(set(self.part_indices)) != len(self.part_indices):
            raise ValueError("part_indices must be unique")
        if self.level not in LIMITED_INFORMATION_LEVELS:
            raise ValueError("sequential evaluation requires a limited information level")
        if self.examples_per_part <= 0 or self.candidates <= 0:
            raise ValueError("examples_per_part and candidates must be positive")
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
) -> dict[str, object]:
    """Run adaptive K-look selection and a non-adaptive random schedule at equal cost."""
    config.validate()
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
            labels = [evaluate(example.scene, example.target_id, pose).quality_components() for pose in poses]
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
        "common_initial_observation": "fixed calibrated isometric camera; excluded from both additional-render budgets",
        "policy_schedule": "adaptive; every next camera is scored from the accumulated observation",
        "one_shot_schedule": "seeded random permutation fixed before any render outcome",
        "candidate_pool": config.candidates,
        "policy_selection_indices": policy_indices,
        "one_shot_random_indices": random_indices,
    })
    return report


__all__ = ["SequentialConfig", "evaluate_sequential_policy"]
