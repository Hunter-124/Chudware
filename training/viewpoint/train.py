from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from .baselines import RANDOM_RENDER_BUDGETS, all_baselines
from .data import ViewpointBatch, split_by_part
from .metrics import deployed_evaluation_report, evaluation_report
from .model import NumpyViewpointPolicy, weighted_geometric_mean
from .synthetic import GENERATOR_VERSION, CorpusConfig, generate_oracle_corpus
from .torch_model import build_torch_policy

DEFAULT_SEED = 1729


def _torch_inputs(batch: ViewpointBatch, torch: Any, device: Any) -> dict[str, Any]:
    return {
        "task": torch.as_tensor(batch.task, dtype=torch.float32, device=device),
        "geometry": torch.as_tensor(batch.geometry, dtype=torch.float32, device=device),
        "geometry_mask": torch.as_tensor(batch.geometry_mask, dtype=torch.bool, device=device),
        "feature_mask": torch.as_tensor(batch.feature_mask, dtype=torch.bool, device=device),
        "candidates": torch.as_tensor(batch.candidates, dtype=torch.float32, device=device),
        "candidate_mask": torch.as_tensor(batch.candidate_mask, dtype=torch.bool, device=device),
    }


def _chosen_oracle_mean(model: Any, batch: ViewpointBatch, torch: Any, device: Any) -> float:
    with torch.no_grad():
        predicted = model(**_torch_inputs(batch, torch, device)).cpu().numpy()
    selected = np.argmax(predicted, axis=1)
    actual = batch.components[np.arange(len(selected)), selected]
    return float(np.mean(weighted_geometric_mean(actual)))


def _oracle_upper_mean(batch: ViewpointBatch) -> float:
    quality = weighted_geometric_mean(batch.components)
    quality = np.where(batch.candidate_mask, quality, -np.inf)
    return float(np.mean(np.max(quality, axis=1)))


def fit_policy(
    train: ViewpointBatch,
    validation: ViewpointBatch,
    *,
    seed: int = DEFAULT_SEED,
    epochs: int = 500,
    learning_rate: float = 2.0e-3,
    weight_decay: float = 1.0e-5,
    rank_temperature: float = 0.10,
    device: str = "cpu",
) -> tuple[NumpyViewpointPolicy, dict[str, float | int]]:
    """Fit candidate ordering with a consequence-weighted pairwise RankNet loss."""
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("Stage-1 training requires optional Torch; NumPy inference does not") from exc
    if epochs <= 0 or learning_rate <= 0 or rank_temperature <= 0:
        raise ValueError("epochs, learning_rate, and rank_temperature must be positive")
    torch.manual_seed(seed)
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"requested {device!r}, but Torch reports no CUDA device")
    actual_device = torch.device(device)
    if actual_device.type == "cpu":
        torch.set_num_threads(max(1, min(4, torch.get_num_threads())))
    model = build_torch_policy(seed=seed).to(actual_device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    inputs = _torch_inputs(train, torch, actual_device)
    target_quality = torch.as_tensor(
        weighted_geometric_mean(train.components),
        dtype=torch.float32,
        device=actual_device,
    )
    mask = torch.as_tensor(train.candidate_mask, dtype=torch.bool, device=actual_device)
    target_delta = target_quality[:, :, None] - target_quality[:, None, :]
    pair_mask = mask[:, :, None] & mask[:, None, :]
    pair_mask &= torch.triu(torch.ones_like(pair_mask), diagonal=1)
    pair_mask &= torch.abs(target_delta) > 1.0e-7
    pair_weight = torch.abs(target_delta)

    best_validation = -np.inf
    best_state: dict[str, Any] | None = None
    best_epoch = 0
    final_loss = np.inf
    for epoch in range(epochs):
        optimizer.zero_grad(set_to_none=True)
        predicted = model(**inputs)
        predicted_delta = predicted[:, :, None] - predicted[:, None, :]
        signed_margin = torch.sign(target_delta) * predicted_delta / rank_temperature
        pair_loss = torch.nn.functional.softplus(-signed_margin)
        loss = (pair_loss[pair_mask] * pair_weight[pair_mask]).sum() / pair_weight[pair_mask].sum()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()
        final_loss = float(loss.detach())
        validation_score = _chosen_oracle_mean(model, validation, torch, actual_device)
        if validation_score >= best_validation:
            best_validation = validation_score
            best_epoch = epoch + 1
            best_state = {name: value.detach().clone() for name, value in model.state_dict().items()}
    if best_state is None:
        raise RuntimeError("training did not produce a finite validation checkpoint")
    model.load_state_dict(best_state)
    train_aggregate = _chosen_oracle_mean(model, train, torch, actual_device)
    validation_aggregate = _chosen_oracle_mean(model, validation, torch, actual_device)
    return model.to_numpy_reference(), {
        "loss": final_loss,
        "best_epoch": best_epoch,
        "train_aggregate": train_aggregate,
        "train_oracle_upper_bound": _oracle_upper_mean(train),
        "train_regret": _oracle_upper_mean(train) - train_aggregate,
        "validation_aggregate": validation_aggregate,
        "validation_oracle_upper_bound": _oracle_upper_mean(validation),
        "validation_regret": _oracle_upper_mean(validation) - validation_aggregate,
        "train_validation_gap": train_aggregate - validation_aggregate,
        "best_validation_aggregate": best_validation,
        "auxiliary_regression_weight": 0.0,
    }


def run_stage1(
    *,
    corpus: CorpusConfig = CorpusConfig(),
    epochs: int = 500,
    seed: int = DEFAULT_SEED,
    output_weights: str | Path | None = None,
    device: str = "cpu",
    bootstrap_samples: int = 2000,
) -> dict[str, object]:
    batch = generate_oracle_corpus(corpus)
    train, validation, test = split_by_part(batch, seed=seed)
    policy, training = fit_policy(train, validation, seed=seed, epochs=epochs, device=device)
    predicted_scores = policy.forward(**test.model_inputs())
    selected = np.argmax(predicted_scores, axis=1)
    baselines = all_baselines(test, random_budgets=RANDOM_RENDER_BUDGETS)
    report = {
        "deployed": deployed_evaluation_report(
            test,
            predicted_scores,
            budgets=RANDOM_RENDER_BUDGETS,
            random_seed=seed,
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=seed + 1,
        ),
        "legacy_policy_at_32_candidates": evaluation_report(
            test,
            selected,
            baselines,
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=seed + 1,
        ),
    }
    report.update({
        "seed": seed,
        "split": {
            "unit": "part_id",
            "train_parts": len(set(train.part_ids)),
            "validation_parts": len(set(validation.part_ids)),
            "test_parts": len(set(test.part_ids)),
            "test_examples": len(test.part_ids),
            "overlap": 0,
        },
        "corpus": {
            "generator_version": GENERATOR_VERSION,
            "parts": corpus.parts,
            "examples_per_part": corpus.examples_per_part,
            "candidates": corpus.candidates,
        },
        "training": training,
        "device": device,
    })
    if output_weights is not None:
        policy.weights.save(output_weights, policy.spec)
        report["weights"] = str(output_weights)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the Stage-1 task-conditioned viewpoint ranker")
    parser.add_argument("--parts", type=int, default=72)
    parser.add_argument("--examples-per-part", type=int, default=6)
    parser.add_argument("--candidates", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--image-size", type=int, default=320, help="square oracle image size")
    parser.add_argument("--device", default="cpu", help="Torch optimizer device; oracle generation remains CPU")
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--output-weights", type=Path)
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(argv)
    report = run_stage1(
        corpus=CorpusConfig(
            parts=args.parts, examples_per_part=args.examples_per_part,
            candidates=args.candidates, seed=args.seed,
            image_size=(args.image_size, args.image_size),
        ),
        epochs=args.epochs, seed=args.seed, output_weights=args.output_weights, device=args.device,
        bootstrap_samples=args.bootstrap_samples,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output_report is not None:
        args.output_report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
