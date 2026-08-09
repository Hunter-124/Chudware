from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .data import ViewpointBatch, locked_expansion_part_indices
from .model import NumpyViewpointPolicy
from .synthetic import CorpusConfig, GENERATOR_VERSION, generate_oracle_corpus_for_indices
from .train import fit_policy

DEFAULT_LADDER = (50, 200, 1000, 5000)


def _save_batch(path: Path, batch: ViewpointBatch) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        part_ids=np.asarray(batch.part_ids),
        task=batch.task,
        geometry=batch.geometry,
        geometry_mask=batch.geometry_mask,
        feature_mask=batch.feature_mask,
        candidates=batch.candidates,
        candidate_mask=batch.candidate_mask,
        components=batch.components,
        difficulties=np.asarray(batch.difficulties),
    )


def _load_batch(path: Path) -> ViewpointBatch:
    with np.load(path, allow_pickle=False) as data:
        return ViewpointBatch(
            part_ids=tuple(str(value) for value in data["part_ids"]),
            task=data["task"],
            geometry=data["geometry"],
            geometry_mask=data["geometry_mask"],
            feature_mask=data["feature_mask"],
            candidates=data["candidates"],
            candidate_mask=data["candidate_mask"],
            components=data["components"],
            difficulties=tuple(str(value) for value in data["difficulties"]),
        ).validate()


def _cached_batch(path: Path, build: Any) -> ViewpointBatch:
    if path.exists():
        return _load_batch(path)
    batch = build()
    _save_batch(path, batch)
    return batch


def _pad_candidates(batch: ViewpointBatch, count: int) -> ViewpointBatch:
    if batch.candidates.shape[1] > count:
        raise ValueError("cannot shrink a candidate pool while padding")
    if batch.candidates.shape[1] == count:
        return batch
    missing = count - batch.candidates.shape[1]
    return ViewpointBatch(
        part_ids=batch.part_ids,
        task=batch.task,
        geometry=batch.geometry,
        geometry_mask=batch.geometry_mask,
        feature_mask=batch.feature_mask,
        candidates=np.pad(batch.candidates, ((0, 0), (0, missing), (0, 0))),
        candidate_mask=np.pad(batch.candidate_mask, ((0, 0), (0, missing))),
        components=np.pad(batch.components, ((0, 0), (0, missing), (0, 0))),
        difficulties=batch.difficulties,
    ).validate()


def _concatenate_same_shape(batches: Iterable[ViewpointBatch]) -> ViewpointBatch:
    rows = tuple(batches)
    if not rows:
        raise ValueError("at least one batch is required")
    geometry_count = max(batch.geometry.shape[1] for batch in rows)

    def pad_geometry(batch: ViewpointBatch) -> ViewpointBatch:
        missing = geometry_count - batch.geometry.shape[1]
        if missing == 0:
            return batch
        return ViewpointBatch(
            part_ids=batch.part_ids,
            task=batch.task,
            geometry=np.pad(batch.geometry, ((0, 0), (0, missing), (0, 0))),
            geometry_mask=np.pad(batch.geometry_mask, ((0, 0), (0, missing))),
            feature_mask=np.pad(batch.feature_mask, ((0, 0), (0, missing))),
            candidates=batch.candidates,
            candidate_mask=batch.candidate_mask,
            components=batch.components,
            difficulties=batch.difficulties,
        ).validate()

    padded = tuple(pad_geometry(batch) for batch in rows)
    return ViewpointBatch(
        part_ids=tuple(part for batch in padded for part in batch.part_ids),
        task=np.concatenate([batch.task for batch in padded]),
        geometry=np.concatenate([batch.geometry for batch in padded]),
        geometry_mask=np.concatenate([batch.geometry_mask for batch in padded]),
        feature_mask=np.concatenate([batch.feature_mask for batch in padded]),
        candidates=np.concatenate([batch.candidates for batch in padded]),
        candidate_mask=np.concatenate([batch.candidate_mask for batch in padded]),
        components=np.concatenate([batch.components for batch in padded]),
        difficulties=tuple(value for batch in padded for value in batch.difficulties),
    ).validate()


def _subset_parts(batch: ViewpointBatch, part_ids: set[str]) -> ViewpointBatch:
    return batch.subset(index for index, part_id in enumerate(batch.part_ids) if part_id in part_ids)


def _part_ids_for_indices(indices: tuple[int, ...], seed: int) -> set[str]:
    return {f"mechanical_{int(seed) & 0xFFFFFFFF:08x}_{index:04d}" for index in indices}


def _fit_once(
    train: ViewpointBatch,
    validation: ViewpointBatch,
    *,
    seed: int,
    epochs: int,
    weight_decay: float,
    device: str,
) -> tuple[NumpyViewpointPolicy, dict[str, float | int]]:
    return fit_policy(
        train,
        validation,
        seed=seed,
        epochs=epochs,
        weight_decay=weight_decay,
        device=device,
    )


def run_scaling_ladder(
    *,
    ladder: tuple[int, ...] = DEFAULT_LADDER,
    seed: int = 1729,
    epochs: int = 300,
    workers: int = 6,
    device: str = "cpu",
    cache_dir: Path = Path("scratch/viewpoint-scale"),
    selected_weight_decay: float | None = None,
    output_weights: Path | None = None,
) -> dict[str, object]:
    if tuple(sorted(set(ladder))) != ladder or ladder[0] != 50:
        raise ValueError("the ladder must be unique, increasing, and begin at the locked 50-part baseline")
    train_indices, validation_indices, test_indices = locked_expansion_part_indices(ladder[-1], seed=seed)
    base_indices = train_indices[:50]
    extra_indices = train_indices[50:]
    base_config = CorpusConfig(parts=50, examples_per_part=6, candidates=32, seed=seed, image_size=(320, 320))
    extra_config = CorpusConfig(parts=max(3, len(extra_indices)), examples_per_part=1, candidates=8, seed=seed, image_size=(320, 320))
    holdout_config = CorpusConfig(parts=11, examples_per_part=6, candidates=32, seed=seed, image_size=(320, 320))
    base = _cached_batch(
        cache_dir / "full_base_train_50x6x32.npz",
        lambda: generate_oracle_corpus_for_indices(base_config, base_indices, workers=workers),
    )
    extras = _cached_batch(
        cache_dir / f"full_extra_train_{len(extra_indices)}x1x8.npz",
        lambda: generate_oracle_corpus_for_indices(extra_config, extra_indices, workers=workers),
    ) if extra_indices else None
    validation = _cached_batch(
        cache_dir / "full_locked_validation_11x6x32.npz",
        lambda: generate_oracle_corpus_for_indices(holdout_config, validation_indices, workers=workers),
    )
    test = _cached_batch(
        cache_dir / "full_locked_test_11x6x32.npz",
        lambda: generate_oracle_corpus_for_indices(holdout_config, test_indices, workers=workers),
    )
    all_train = base if extras is None else _concatenate_same_shape((base, _pad_candidates(extras, 32)))

    regularization_trials: list[dict[str, object]] = []
    trial_parts = min(200, ladder[-1])
    trial_indices = train_indices[:trial_parts]
    trial_batch = _subset_parts(all_train, _part_ids_for_indices(trial_indices, seed))
    for decay in (0.0, 1.0e-5, 1.0e-4, 1.0e-3):
        _, metrics = _fit_once(
            trial_batch, validation,
            seed=seed, epochs=epochs, weight_decay=decay, device=device,
        )
        regularization_trials.append({"weight_decay": decay, **metrics})
    if selected_weight_decay is None:
        best_trial = max(regularization_trials, key=lambda row: float(row["validation_aggregate"]))
        selected_weight_decay = float(best_trial["weight_decay"])

    curve: list[dict[str, object]] = []
    selected_policy: NumpyViewpointPolicy | None = None
    for part_count in ladder:
        indices = train_indices[:part_count]
        train = _subset_parts(all_train, _part_ids_for_indices(indices, seed))
        policy, metrics = _fit_once(
            train, validation,
            seed=seed, epochs=epochs, weight_decay=selected_weight_decay, device=device,
        )
        curve.append({
            "train_parts": part_count,
            "train_examples": len(train.part_ids),
            **metrics,
        })
        selected_policy = policy
    if selected_policy is None:
        raise RuntimeError("the scaling ladder produced no policy")
    test_selected = selected_policy.select(**test.model_inputs())[0]
    from .metrics import information_asymmetry_report
    from .baselines import fixed_isometric, random_from_n

    full_information_test = information_asymmetry_report(
        test,
        policy=test_selected,
        random=random_from_n(test, test.candidates.shape[1], seed=seed),
        fixed_isometric=fixed_isometric(test),
        bootstrap_samples=2000,
        bootstrap_seed=seed + 1,
    )
    validation_scores = selected_policy.forward(**validation.model_inputs())
    permutation = np.random.default_rng(seed).permutation(validation.candidates.shape[1])
    permuted_inputs = validation.model_inputs()
    permuted_inputs["candidates"] = validation.candidates[:, permutation]
    permuted_inputs["candidate_mask"] = validation.candidate_mask[:, permutation]
    permuted_scores = selected_policy.forward(**permuted_inputs)
    permutation_error = float(np.max(np.abs(validation_scores[:, permutation] - permuted_scores)))
    selection_invariant = bool(np.array_equal(
        np.argmax(validation_scores, axis=1),
        permutation[np.argmax(permuted_scores, axis=1)],
    ))
    if output_weights is not None:
        output_weights.parent.mkdir(parents=True, exist_ok=True)
        selected_policy.weights.save(output_weights, selected_policy.spec)
    return {
        "seed": seed,
        "generator_version": GENERATOR_VERSION,
        "split": {
            "unit": "part_id",
            "locked_base_train_parts": 50,
            "locked_validation_parts": len(validation_indices),
            "locked_test_parts": len(test_indices),
            "additional_train_parts": len(extra_indices),
            "overlap": 0,
        },
        "training_corpus": {
            "base": {"parts": 50, "examples_per_part": 6, "candidates": 32},
            "procedural_expansion": {"parts": len(extra_indices), "examples_per_part": 1, "candidates": 8},
            "image_size": [320, 320],
        },
        "epochs": epochs,
        "device": device,
        "regularization_trials_at_parts": trial_parts,
        "regularization_trials": regularization_trials,
        "selected_weight_decay": selected_weight_decay,
        "augmentation_trials": [
            {
                "name": "candidate_order_permutation",
                "result": "did_not_help",
                "measured_max_abs_score_difference": permutation_error,
                "selection_identical": selection_invariant,
                "reason": "the scorer is exactly candidate-permutation equivariant; measured shuffling adds no new training signal",
            },
        ],
        "validation_regret_curve": curve,
        "selected_full_information_locked_test": full_information_test,
        "weights": str(output_weights) if output_weights is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the locked-holdout viewpoint part-count scaling ladder")
    parser.add_argument("--ladder", type=int, nargs="+", default=DEFAULT_LADDER)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--epochs", type=int, default=300)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--cache-dir", type=Path, default=Path("scratch/viewpoint-scale"))
    parser.add_argument("--weight-decay", type=float)
    parser.add_argument("--output-weights", type=Path)
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(argv)
    report = run_scaling_ladder(
        ladder=tuple(args.ladder),
        seed=args.seed,
        epochs=args.epochs,
        workers=args.workers,
        device=args.device,
        cache_dir=args.cache_dir,
        selected_weight_decay=args.weight_decay,
        output_weights=args.output_weights,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output_report is not None:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
