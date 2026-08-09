from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .baselines import fixed_isometric, random_from_n
from .corpus_cache import CachedCorpus, corpus_inputs, invalidate, load_or_build
from .data import ViewpointBatch, locked_expansion_part_indices
from .metrics import information_asymmetry_report
from .model import NumpyViewpointPolicy
from .observation_data import LIMITED_INFORMATION_LEVELS, LimitedCorpusConfig, generate_limited_corpus
from .runtime import DEFAULT_CACHE_DIR, default_device, generation_workers, report_device, resolve_device
from .sequential import SequentialConfig, evaluate_sequential_policy
from .train import fit_policy

DEFAULT_PART_LADDER = (200, 1000, 5000)




def _part_ids(indices: tuple[int, ...], seed: int) -> set[str]:
    return {f"mechanical_{int(seed) & 0xFFFFFFFF:08x}_{index:04d}" for index in indices}


def _subset(batch: ViewpointBatch, indices: tuple[int, ...], seed: int) -> ViewpointBatch:
    wanted = _part_ids(indices, seed)
    return batch.subset(row for row, part_id in enumerate(batch.part_ids) if part_id in wanted)


def run_information_experiment(
    *,
    train_parts: int = 5000,
    part_ladder: tuple[int, ...] = DEFAULT_PART_LADDER,
    seed: int = 1729,
    epochs: int = 120,
    weight_decay: float = 1.0e-4,
    workers: int = 6,
    device: str | None = None,
    fit_device: str | None = "cpu",
    bootstrap_samples: int = 2000,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    weights_dir: Path = Path("training/viewpoint"),
) -> dict[str, object]:
    if train_parts < max(part_ladder) or tuple(sorted(set(part_ladder))) != part_ladder:
        raise ValueError("train_parts must cover a unique increasing part ladder")
    actual_device = resolve_device(device)
    actual_workers = generation_workers(actual_device, workers)
    actual_fit_device = resolve_device(fit_device)
    fit_backend = "torch-cuda" if actual_fit_device.startswith("cuda") else "torch-cpu"
    cached_corpora: list[CachedCorpus] = []

    def cached(config: LimitedCorpusConfig) -> ViewpointBatch:
        result = load_or_build(
            corpus_inputs("limited", config, part_indices=config.part_indices),
            lambda: generate_limited_corpus(
                config, workers=actual_workers, device=actual_device,
            ),
            cache_dir=cache_dir,
        )
        cached_corpora.append(result)
        return result.batch
    train_indices, validation_indices, test_indices = locked_expansion_part_indices(train_parts, seed=seed)
    per_level: dict[str, object] = {}
    policies: dict[str, NumpyViewpointPolicy] = {}
    validation_curve: list[dict[str, object]] = []

    for level in LIMITED_INFORMATION_LEVELS:
        train_config = LimitedCorpusConfig(
            train_indices,
            level=level,
            examples_per_part=1,
            candidates=8,
            seed=seed,
            image_size=(96, 96),
        )
        validation_config = LimitedCorpusConfig(
            validation_indices,
            level=level,
            examples_per_part=6,
            candidates=16,
            seed=seed,
            image_size=(96, 96),
        )
        test_config = LimitedCorpusConfig(
            test_indices,
            level=level,
            examples_per_part=6,
            candidates=16,
            seed=seed,
            image_size=(96, 96),
        )
        train = cached(train_config)
        validation = cached(validation_config)
        test = cached(test_config)

        ladder = part_ladder if level == "depth_plus_prior" else (train_parts,)
        policy: NumpyViewpointPolicy | None = None
        training: dict[str, float | int] | None = None
        for count in ladder:
            subset = _subset(train, train_indices[:count], seed)
            policy, training = fit_policy(
                subset,
                validation,
                seed=seed,
                epochs=epochs,
                weight_decay=weight_decay,
                device=actual_fit_device,
            )
            if level == "depth_plus_prior":
                validation_curve.append({
                    "observation_level": level,
                    "train_parts": count,
                    "train_examples": len(subset.part_ids),
                    **training,
                })
        if policy is None or training is None:
            raise RuntimeError(f"no policy was trained for {level}")
        selected, _ = policy.select(**test.model_inputs())
        evaluation = information_asymmetry_report(
            test,
            policy=selected,
            random=random_from_n(test, test.candidates.shape[1], seed=seed),
            fixed_isometric=fixed_isometric(test),
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=seed + 100,
        )
        weights_path = weights_dir / f"limited_{level}_weights.npz"
        policy.weights.save(weights_path, policy.spec)
        policies[level] = policy
        per_level[level] = {
            "training": training,
            "evaluation": evaluation,
            "weights": str(weights_path),
        }

    sequential = {
        level: evaluate_sequential_policy(
            policies[level],
            SequentialConfig(
                test_indices,
                level=level,
                examples_per_part=6,
                candidates=8,
                budgets=(1, 2, 4, 8),
                seed=seed,
                image_size=(96, 96),
                bootstrap_samples=bootstrap_samples,
                device=actual_device,
            ),
        )
        for level in LIMITED_INFORMATION_LEVELS
    }
    return {
        "seed": seed,
        "split": {
            "unit": "part_id",
            "train_parts": len(train_indices),
            "validation_parts": len(validation_indices),
            "locked_test_parts": len(test_indices),
            "overlap": 0,
        },
        "information_boundary": {
            "policy_inputs": list(LIMITED_INFORMATION_LEVELS),
            "oracle_input": "full analytic mesh, exact feature, and all candidate evaluations",
            "oracle_is_not_weakened": True,
            "common_initial_observation": True,
        },
        "training": {
            "epochs": epochs,
            "weight_decay": weight_decay,
            "device": actual_device,
            "fit_backend": fit_backend,
            "fit_results_comparable_to_reference": fit_backend == "torch-cpu",
            "workers": actual_workers,
            "train_candidates": 8,
            "holdout_candidates": 16,
            "image_size": [96, 96],
        },
        "validation_regret_curve": validation_curve,
        "per_observation_level": per_level,
        "sequential_matched_budget": sequential,
        "corpus_cache": [
            {
                "status": "hit" if item.hit else "miss",
                "key": item.key,
                "path": str(item.path),
            }
            for item in cached_corpora
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate limited-information and sequential viewpoint policies")
    parser.add_argument("--train-parts", type=int, default=5000)
    parser.add_argument("--part-ladder", type=int, nargs="+", default=DEFAULT_PART_LADDER)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--weight-decay", type=float, default=1.0e-4)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--device", default=default_device(), help="actual compute device; defaults to CUDA when available")
    parser.add_argument(
        "--fit-device", default="cpu",
        help="optimizer device; CPU is the published reference, CUDA is exploratory",
    )
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--invalidate-cache", action="store_true")
    parser.add_argument("--weights-dir", type=Path, default=Path("training/viewpoint"))
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(argv)
    if args.invalidate_cache:
        invalidate(args.cache_dir)
    report = run_information_experiment(
        train_parts=args.train_parts,
        part_ladder=tuple(args.part_ladder),
        seed=args.seed,
        epochs=args.epochs,
        weight_decay=args.weight_decay,
        workers=args.workers,
        device=args.device,
        fit_device=args.fit_device,
        bootstrap_samples=args.bootstrap_samples,
        cache_dir=args.cache_dir,
        weights_dir=args.weights_dir,
    )
    report_device(
        str(report["training"]["device"]),
        str(report["training"]["fit_backend"]),
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output_report is not None:
        args.output_report.parent.mkdir(parents=True, exist_ok=True)
        args.output_report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
