"""Bounded CUDA training and locked-split evaluation for rendered-pixel ranking."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import numpy as np

from .model import COMPONENTS, weighted_geometric_mean
from .pixel_model import ENCODER_PROVENANCE, PixelModelSpec, build_pixel_policy
from .runtime import report_device, resolve_device

DEFAULT_SEED = 1729
ESTABLISHED_BARS = {
    "depth_policy": 0.300661,
    "random": 0.122591,
    "fixed_isometric": 0.171608,
    "greedy_geometric": 0.169855,
    "full_information_oracle": 0.638062,
    "depth_visibility_correlation": 0.584,
}


def _device_inputs(inputs: Any, torch: Any, device: Any) -> dict[str, Any]:
    result = {
        "rgb": torch.as_tensor(inputs.rgb, dtype=torch.uint8, device=device),
        "cameras": torch.as_tensor(inputs.cameras, dtype=torch.float32, device=device),
        "coarse_prior": torch.as_tensor(inputs.coarse_prior, dtype=torch.float32, device=device),
        "task": torch.as_tensor(inputs.task, dtype=torch.float32, device=device),
        "candidate_mask": torch.as_tensor(inputs.candidate_mask, dtype=torch.bool, device=device),
    }
    if inputs.depth is not None:
        result["depth"] = torch.as_tensor(inputs.depth, dtype=torch.float32, device=device)
        result["depth_mask"] = torch.as_tensor(inputs.depth_mask, dtype=torch.bool, device=device)
    return result


def _take(values: Mapping[str, Any], indices: Any) -> dict[str, Any]:
    return {name: value.index_select(0, indices) for name, value in values.items()}


def _ranknet_loss(predicted: Any, quality: Any, mask: Any, torch: Any, temperature: float) -> Any:
    predicted_delta = predicted[:, :, None] - predicted[:, None, :]
    target_delta = quality[:, :, None] - quality[:, None, :]
    pair_mask = mask[:, :, None] & mask[:, None, :]
    pair_mask &= torch.triu(torch.ones_like(pair_mask), diagonal=1)
    pair_mask &= torch.abs(target_delta) > 1.0e-7
    pair_weight = torch.abs(target_delta)
    if not bool(pair_mask.any()):
        return predicted[mask].sum() * 0.0
    margin = torch.sign(target_delta) * predicted_delta / temperature
    losses = torch.nn.functional.softplus(-margin)
    return (losses[pair_mask] * pair_weight[pair_mask]).sum() / pair_weight[pair_mask].sum()


def _score_rows(model: Any, inputs: Mapping[str, Any], rows: Any, *, chunk_rows: int, torch: Any) -> np.ndarray:
    outputs: list[np.ndarray] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(rows), chunk_rows):
            selected = rows[start:start + chunk_rows]
            outputs.append(model(**_take(inputs, selected)).detach().cpu().numpy())
    return np.concatenate(outputs, axis=0)


def _selected_mean(scores: np.ndarray, components: np.ndarray, mask: np.ndarray) -> float:
    selected = np.argmax(np.where(mask, scores, -np.inf), axis=1)
    values = weighted_geometric_mean(components)[np.arange(len(selected)), selected]
    return float(np.mean(values))
def frozen_training_rows(corpus: Any) -> np.ndarray:
    """Match the .300661 depth run: one example for each of 50 training parts."""
    manifest = json.loads(Path(corpus.manifest_path).read_text(encoding="utf-8"))
    rows = np.asarray([
        index
        for index, entry in enumerate(manifest["entries"])
        if entry["split"] == "train" and int(entry["example_index"]) == 0
    ], np.int64)
    if len(rows) != 50 or len({corpus.part_ids[int(index)] for index in rows}) != 50:
        raise RuntimeError("pixel corpus does not contain the frozen 50-part training subset")
    return rows




def fit_pixel_policy(
    corpus: Any,
    *,
    train_rows_override: np.ndarray | None = None,
    seed: int = DEFAULT_SEED,
    epochs: int = 24,
    batch_rows: int = 4,
    learning_rate: float = 8.0e-4,
    weight_decay: float = 1.0e-4,
    rank_temperature: float = 0.10,
    device: str = "cuda",
) -> tuple[Any, dict[str, Any]]:
    """Train the unchanged consequence-weighted ranking objective on GPU-resident images."""
    if (
        epochs <= 0 or batch_rows <= 0 or learning_rate <= 0
        or weight_decay < 0 or rank_temperature <= 0
    ):
        raise ValueError("invalid pixel training hyperparameters")
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("pixel policy training requires optional Torch") from exc
    actual_device = torch.device(resolve_device(device))
    if actual_device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("the measured pixel experiment requires an available CUDA device")
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False

    split = np.asarray(corpus.splits, dtype=object)
    train_rows_np = (
        np.flatnonzero(split == "train")
        if train_rows_override is None
        else np.asarray(train_rows_override, np.int64)
    )
    validation_rows_np = np.flatnonzero(split == "validation")
    if not len(train_rows_np) or not len(validation_rows_np):
        raise ValueError("corpus needs non-empty train and validation splits")
    all_inputs = _device_inputs(corpus.model_inputs(), torch, actual_device)
    quality = torch.as_tensor(
        weighted_geometric_mean(corpus.targets.components), dtype=torch.float32, device=actual_device
    )
    train_rows = torch.as_tensor(train_rows_np, dtype=torch.long, device=actual_device)
    validation_rows = torch.as_tensor(validation_rows_np, dtype=torch.long, device=actual_device)
    model = build_pixel_policy(
        spec=PixelModelSpec(use_depth=corpus.inputs.depth is not None), seed=seed
    ).to(actual_device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    generator = torch.Generator(device="cpu").manual_seed(seed)
    curve: list[dict[str, float | int]] = []
    best_validation = -np.inf
    best_epoch = 0
    best_state: dict[str, Any] | None = None
    started = time.monotonic()

    for epoch in range(1, epochs + 1):
        model.train()
        permutation = torch.randperm(len(train_rows_np), generator=generator).to(actual_device)
        epoch_numerator = 0.0
        epoch_rows = 0
        for start in range(0, len(permutation), batch_rows):
            rows = train_rows.index_select(0, permutation[start:start + batch_rows])
            batch = _take(all_inputs, rows)
            optimizer.zero_grad(set_to_none=True)
            predicted = model(**batch)
            finite_predicted = predicted.masked_fill(~batch["candidate_mask"], 0.0)
            loss = _ranknet_loss(
                finite_predicted,
                quality.index_select(0, rows),
                batch["candidate_mask"],
                torch,
                rank_temperature,
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            count = len(rows)
            epoch_numerator += float(loss.detach()) * count
            epoch_rows += count

        train_scores = _score_rows(model, all_inputs, train_rows, chunk_rows=batch_rows, torch=torch)
        validation_scores = _score_rows(model, all_inputs, validation_rows, chunk_rows=batch_rows, torch=torch)
        train_aggregate = _selected_mean(
            train_scores, corpus.targets.components[train_rows_np], corpus.inputs.candidate_mask[train_rows_np]
        )
        validation_aggregate = _selected_mean(
            validation_scores,
            corpus.targets.components[validation_rows_np],
            corpus.inputs.candidate_mask[validation_rows_np],
        )
        row = {
            "epoch": epoch,
            "rank_loss": epoch_numerator / epoch_rows,
            "train_aggregate": train_aggregate,
            "validation_aggregate": validation_aggregate,
        }
        curve.append(row)
        if validation_aggregate >= best_validation:
            best_validation = validation_aggregate
            best_epoch = epoch
            best_state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError("training produced no validation checkpoint")
    model.load_state_dict(best_state)
    model.to(actual_device).eval()
    return model, {
        "seed": seed,
        "device": str(actual_device),
        "device_name": torch.cuda.get_device_name(actual_device),
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
        "epochs": epochs,
        "train_examples": len(train_rows_np),
        "batch_rows": batch_rows,
        "learning_rate": learning_rate,
        "weight_decay": weight_decay,
        "rank_temperature": rank_temperature,
        "best_epoch": best_epoch,
        "best_validation_aggregate": best_validation,
        "elapsed_seconds": time.monotonic() - started,
        "gpu_resident_corpus": True,
        "curve": curve,
    }


def _part_bootstrap(
    part_ids: Sequence[str], values: np.ndarray, *, samples: int, seed: int
) -> list[float]:
    parts = np.asarray(sorted(set(part_ids)), dtype=object)
    ids = np.asarray(part_ids, dtype=object)
    rows = {part: np.flatnonzero(ids == part) for part in parts}
    rng = np.random.default_rng(seed)
    means: list[float] = []
    for _ in range(samples):
        sampled = rng.choice(parts, size=len(parts), replace=True)
        selected = np.concatenate([rows[part] for part in sampled])
        means.append(float(np.mean(values[selected])))
    return means


def _summary_with_ci(part_ids: Sequence[str], values: np.ndarray, *, samples: int, seed: int) -> dict[str, Any]:
    boot = np.asarray(_part_bootstrap(part_ids, values, samples=samples, seed=seed), np.float64)
    return {
        "mean": float(np.mean(values)),
        "ci95": np.quantile(boot, (0.025, 0.975)).tolist(),
    }


def aligned_frozen_baseline_indices(
    corpus: Any,
    *,
    weights_path: Path = Path("training/viewpoint/limited_depth_only_weights.npz"),
    device: str = "cuda",
    workers: int = 6,
) -> dict[str, np.ndarray]:
    """Recreate all frozen baselines for exactly the pixel manifest's test rows."""
    from .baselines import fixed_isometric, greedy_geometric, random_from_n
    from .model import NumpyViewpointPolicy, PolicyWeights
    from .observation_data import LimitedCorpusConfig, generate_limited_corpus

    manifest = json.loads(Path(corpus.manifest_path).read_text(encoding="utf-8"))
    entries = manifest["entries"]
    test_rows = [index for index, entry in enumerate(entries) if entry["split"] == "test"]
    part_indices = tuple(sorted({int(entries[index]["part_index"]) for index in test_rows}))
    config = manifest["config"]
    candidate_count = int(config["candidates"])
    seed = int(config["seed"])
    depth_batch = generate_limited_corpus(
        LimitedCorpusConfig(
            part_indices=part_indices,
            level="depth_only",
            examples_per_part=int(config["examples_per_part"]),
            candidates=candidate_count,
            seed=seed,
            image_size=tuple(int(value) for value in config["oracle_image_size"]),
        ),
        workers=workers,
        device=device,
    )
    pixel_part_ids = tuple(corpus.part_ids[index] for index in test_rows)
    if pixel_part_ids != depth_batch.part_ids:
        raise RuntimeError("pixel and frozen depth test rows are not identically ordered")
    pixel_forward = corpus.inputs.cameras[test_rows, :, 3:6]
    if not np.allclose(pixel_forward, depth_batch.candidates[:, :, :3], atol=2.0e-6):
        raise RuntimeError("pixel corpus candidate cameras do not match the frozen depth benchmark")
    weights, spec = PolicyWeights.load(weights_path)
    depth_policy, _ = NumpyViewpointPolicy(weights, spec).select(**depth_batch.model_inputs())
    selected = {
        "depth_policy": depth_policy,
        "random": random_from_n(depth_batch, candidate_count, seed=seed),
        "fixed_isometric": fixed_isometric(depth_batch),
        "greedy_geometric": greedy_geometric(depth_batch),
    }
    result: dict[str, np.ndarray] = {}
    target_rows = np.asarray(test_rows, np.int64)
    for name, indices in selected.items():
        full = np.full(len(corpus.part_ids), -1, np.int64)
        full[target_rows] = indices
        result[name] = full
    return result


def validate_locked_bars(
    corpus: Any,
    aligned_indices: Mapping[str, np.ndarray],
    *,
    tolerance: float = 1.0e-5,
) -> dict[str, dict[str, Any]]:
    """Prove candidate/label parity before spending a training run."""
    test_rows = np.flatnonzero(np.asarray(corpus.splits, dtype=object) == "test")
    quality = weighted_geometric_mean(corpus.targets.components[test_rows])
    row = np.arange(len(test_rows))
    selections = {
        name: np.asarray(indices, np.int64)[test_rows]
        for name, indices in aligned_indices.items()
    }
    selections["full_information_oracle"] = np.argmax(quality, axis=1)
    observed = {
        name: float(np.mean(quality[row, indices]))
        for name, indices in selections.items()
    }
    result: dict[str, dict[str, Any]] = {}
    for name, value in observed.items():
        expected = ESTABLISHED_BARS[name]
        delta = value - expected
        matched = abs(delta) <= tolerance
        result[name] = {
            "observed": value, "established": expected, "delta": delta, "matched": matched
        }
        if not matched and name != "random":
            raise RuntimeError(
                f"locked split parity failed for {name}: observed {value:.9f}, expected {expected:.9f}"
            )
        if not matched:
            result[name]["note"] = (
                "checked-in .122591 random scalar is retained as the requested historical bar; "
                "its saved/current selection indices reproduce .165632, so no paired row vector is claimed"
            )
    return result


def evaluate_pixel_policy(
    model: Any,
    corpus: Any,
    *,
    device: str = "cuda",
    aligned_baseline_indices: Mapping[str, np.ndarray] | None = None,
    batch_rows: int = 4,
    bootstrap_samples: int = 2000,
    seed: int = DEFAULT_SEED,
) -> tuple[dict[str, Any], np.ndarray]:
    """Evaluate every system on the exact PixelCorpus test rows with whole-part resampling."""
    import torch

    actual_device = torch.device(resolve_device(device))
    all_inputs = _device_inputs(corpus.model_inputs(), torch, actual_device)
    test_rows_np = np.flatnonzero(np.asarray(corpus.splits, dtype=object) == "test")
    test_rows = torch.as_tensor(test_rows_np, dtype=torch.long, device=actual_device)
    predicted = _score_rows(model, all_inputs, test_rows, chunk_rows=batch_rows, torch=torch)
    components = corpus.targets.components[test_rows_np]
    mask = corpus.inputs.candidate_mask[test_rows_np]
    quality = weighted_geometric_mean(components)
    policy_index = np.argmax(np.where(mask, predicted, -np.inf), axis=1)
    oracle_index = np.argmax(np.where(mask, quality, -np.inf), axis=1)
    row = np.arange(len(test_rows_np))
    selections: dict[str, np.ndarray] = {"pixel_policy": policy_index, "full_information_oracle": oracle_index}
    for name, indices in corpus.baseline_indices.items():
        normalized = "random" if name == "seeded_random" else name
        selections[normalized] = np.asarray(indices, np.int64)[test_rows_np]
    if aligned_baseline_indices is not None:
        for name, indices in aligned_baseline_indices.items():
            selections[name] = np.asarray(indices, np.int64)[test_rows_np]
    required = {"depth_policy", "random", "fixed_isometric", "greedy_geometric"}
    missing = sorted(required - selections.keys())
    if missing:
        raise RuntimeError(f"evaluation lacks required aligned baseline indices: {missing}")
    values = {name: quality[row, indices] for name, indices in selections.items()}
    values["random_aligned_rerun"] = values.pop("random")
    values["random"] = np.full(len(test_rows_np), ESTABLISHED_BARS["random"], np.float32)
    selections["random_aligned_rerun"] = selections.pop("random")
    part_ids = tuple(corpus.part_ids[index] for index in test_rows_np)
    systems = {
        name: _summary_with_ci(part_ids, score, samples=bootstrap_samples, seed=seed + i * 101)
        for i, (name, score) in enumerate(values.items())
    }
    comparisons = {}
    for i, name in enumerate(("depth_policy", "random", "fixed_isometric", "greedy_geometric", "full_information_oracle")):
        delta = values["pixel_policy"] - values[name]
        comparisons[name] = _summary_with_ci(
            part_ids, delta, samples=bootstrap_samples, seed=seed + 1000 + i * 101
        )
        comparisons[name]["ci_kind"] = (
            "historical-reference-shifted whole-part pixel bootstrap"
            if name == "random"
            else "paired whole-part bootstrap"
        )
    labels = np.asarray([corpus.difficulties[index] for index in test_rows_np], dtype=object)
    difficulty = {}
    for label in sorted(set(labels.tolist())):
        selected = labels == label
        difficulty[label] = {
            "examples": int(selected.sum()),
            "parts": len(set(np.asarray(part_ids, dtype=object)[selected].tolist())),
            **{name: float(np.mean(score[selected])) for name, score in values.items()},
        }
    return {
        "test_examples": len(test_rows_np),
        "test_parts": len(set(part_ids)),
        "bootstrap_unit": "part_id",
        "bootstrap_samples": bootstrap_samples,
        "systems": systems,
        "pixel_minus": comparisons,
        "difficulty": difficulty,
        "random_reference_note": (
            "The requested .122591 historical random bar is reported as a fixed reference. "
            "Current and saved seeded selection indices on these labels yield .165632, so the "
            "random delta interval shifts the pixel policy's whole-part bootstrap rather than "
            "claiming a fabricated paired random vector."
        ),
        "selection_indices": {name: indices.tolist() for name, indices in selections.items()},
    }, predicted


def _probe_matrix(model: Any, inputs: Any, rows: np.ndarray, *, device: str, batch_rows: int) -> np.ndarray:
    import torch

    actual_device = torch.device(resolve_device(device))
    tensors = _device_inputs(inputs, torch, actual_device)
    result: list[np.ndarray] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(rows), batch_rows):
            index = torch.as_tensor(rows[start:start + batch_rows], dtype=torch.long, device=actual_device)
            batch = _take(tensors, index)
            result.append(model.probe_features(
                batch["rgb"], batch["coarse_prior"], depth=batch.get("depth"), depth_mask=batch.get("depth_mask")
            ).cpu().numpy())
    return np.concatenate(result, axis=0)


def visibility_probe_report(
    model: Any,
    training_corpus: Any,
    audit_corpus: Any,
    *,
    device: str = "cuda",
    batch_rows: int = 4,
    ridge: float = 0.01,
    train_rows_override: np.ndarray | None = None,
) -> dict[str, Any]:
    """Fit a frozen linear readout on train labels; evaluate only on the frozen audit."""
    audit_part_ids = set(audit_corpus.part_ids)
    split = np.asarray(training_corpus.splits, dtype=object)
    part_ids = np.asarray(training_corpus.part_ids, dtype=object)
    eligible = (
        np.flatnonzero(split == "train")
        if train_rows_override is None
        else np.asarray(train_rows_override, np.int64)
    )
    train_rows = eligible[~np.isin(part_ids[eligible], np.asarray(sorted(audit_part_ids), dtype=object))]
    train_features = _probe_matrix(
        model, training_corpus.model_inputs(), train_rows, device=device, batch_rows=batch_rows
    )
    audit_rows = np.arange(len(audit_corpus.part_ids), dtype=np.int64)
    audit_features = _probe_matrix(
        model, audit_corpus.model_inputs(), audit_rows, device=device, batch_rows=batch_rows
    )
    train_mask = training_corpus.inputs.candidate_mask[train_rows]
    audit_mask = audit_corpus.inputs.candidate_mask
    x_train = train_features[train_mask].astype(np.float64)
    y_train = training_corpus.targets.components[train_rows, :, COMPONENTS.index("visibility")][train_mask]
    x_audit = audit_features[audit_mask].astype(np.float64)
    y_audit = audit_corpus.targets.components[:, :, COMPONENTS.index("visibility")][audit_mask]
    mean = x_train.mean(axis=0)
    scale = x_train.std(axis=0)
    scale[scale < 1.0e-8] = 1.0
    standardized = (x_train - mean) / scale
    design = np.column_stack((np.ones(len(standardized)), standardized))
    penalty = np.eye(design.shape[1], dtype=np.float64) * ridge
    penalty[0, 0] = 0.0
    weights = np.linalg.solve(design.T @ design + penalty, design.T @ y_train)
    predictions = np.clip(
        np.column_stack((np.ones(len(x_audit)), (x_audit - mean) / scale)) @ weights, 0.0, 1.0
    )
    if np.std(predictions) <= 1.0e-12 or np.std(y_audit) <= 1.0e-12:
        correlation = 0.0
    else:
        correlation = float(np.corrcoef(predictions, y_audit)[0, 1])
    return {
        "probe": "fixed ridge linear readout over frozen mean/max/prior encoder features",
        "ridge": ridge,
        "train_samples": len(y_train),
        "audit_samples": len(y_audit),
        "audit_parts": len(set(audit_corpus.part_ids)),
        "probe_training_part_overlap": 0,
        "encoder_training_part_overlap": len(
            audit_part_ids & {
                part_id
                for part_id, row_split in zip(training_corpus.part_ids, training_corpus.splits)
                if row_split == "train"
            }
        ),
        "pearson_correlation": correlation,
        "depth_reference_correlation": ESTABLISHED_BARS["depth_visibility_correlation"],
        "beats_depth_reference": correlation > ESTABLISHED_BARS["depth_visibility_correlation"],
        "oracle_target": "frozen oracle visibility component; label-side only",
    }


def run_pixel_experiment(
    *,
    output_report: Path,
    output_weights: Path,
    parts: int = 72,
    examples_per_part: int = 6,
    candidates: int = 16,
    resolution: tuple[int, int] = (96, 96),
    epochs: int = 24,
    batch_rows: int = 4,
    workers: int = 6,
    device: str = "cuda",
    seed: int = DEFAULT_SEED,
    bootstrap_samples: int = 2000,
    cache_dir: Path = Path("scratch/viewpoint-pixels"),
) -> dict[str, Any]:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("pixel experiment requires optional Torch") from exc
    from services.viewpoint.pixels import PixelCorpusConfig, generate_pixel_corpus, generate_visibility_audit_corpus

    config = PixelCorpusConfig(
        parts=parts,
        examples_per_part=examples_per_part,
        candidates=candidates,
        seed=seed,
        resolution=resolution,
        include_depth=True,
        cache_dir=cache_dir,
    )
    corpus = generate_pixel_corpus(config, workers=workers, device=device)
    audit = generate_visibility_audit_corpus(seed=seed, device=device)
    aligned_baselines = aligned_frozen_baseline_indices(
        corpus, device=device, workers=workers
    )
    locked_bar_validation = validate_locked_bars(corpus, aligned_baselines)
    controlled_train_rows = frozen_training_rows(corpus)
    model, training = fit_pixel_policy(
        corpus, seed=seed, epochs=epochs, batch_rows=batch_rows, device=device,
        train_rows_override=controlled_train_rows,
    )
    evaluation, _ = evaluate_pixel_policy(
        model,
        corpus,
        device=device,
        aligned_baseline_indices=aligned_baselines,
        batch_rows=batch_rows,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
    )
    visibility = visibility_probe_report(
        model, corpus, audit, device=device, batch_rows=batch_rows,
        train_rows_override=controlled_train_rows,
    )
    torch.save({
        "schema": "training.viewpoint.pixel-policy/v1",
        "model_spec": asdict(PixelModelSpec(use_depth=True)),
        "state_dict": {name: value.detach().cpu() for name, value in model.state_dict().items()},
        "seed": seed,
    }, output_weights)
    pixel_mean = evaluation["systems"]["pixel_policy"]["mean"]
    verdict = {
        "same_information_bar": (
            "beats hand-computed depth observation" if pixel_mean > ESTABLISHED_BARS["depth_policy"]
            else "does not beat hand-computed depth observation"
        ),
        "teacher_bar": (
            "reaches full-information oracle ceiling" if pixel_mean >= ESTABLISHED_BARS["full_information_oracle"]
            else "remains below full-information oracle ceiling"
        ),
        "information_bar": (
            "beats .584 depth visibility correlation" if visibility["beats_depth_reference"]
            else "does not beat .584 depth visibility correlation"
        ),
    }
    report = {
        "schema": "training.viewpoint.pixel-experiment/v1",
        "architecture": {
            "trunk": "three-stage compact residual CNN with GroupNorm and SiLU; 32/64/96 channels",
            "channels": "RGB plus normalized depth and explicit valid-depth mask",
            "conditioning": "one projected 8-D coarse FeatureRegion2D prior token per candidate",
            "ranker": "existing task/query/cross-attention/fusion scalar candidate-ranking head",
            "objective": "unchanged consequence-weighted pairwise RankNet",
            "rationale": f"small synthetic corpus and {resolution[0]}x{resolution[1]} renders favor a compact from-scratch trunk; GroupNorm is stable at four rows (64 candidate images) per step; no mismatched natural-image pretraining is introduced",
            "license_provenance": ENCODER_PROVENANCE,
        },
        "corpus": {
            "config": asdict(config),
            "manifest_path": str(corpus.manifest_path),
            "renderer_version": corpus.renderer_version,
            "oracle_version": corpus.oracle_version,
            "generation_seconds": corpus.generation_seconds,
            "cache_hits": corpus.cache_hits,
        },
        "locked_bar_validation": locked_bar_validation,
        "device_line": f"device={training['device']} ({training['device_name']}); torch={training['torch_version']}; CUDA={training['cuda_version']}; gpu_resident_corpus=true",
        "training": training,
        "locked_test": evaluation,
        "established_bars": ESTABLISHED_BARS,
        "visibility_information": visibility,
        "verdict": verdict,
        "what_remains_unproven": "Synthetic rendered candidates do not establish generalization to real cameras, unseen topology families, materials, lighting, calibration drift, or downstream human/model answer quality.",
    }
    output_report.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-report", type=Path, default=Path("training/viewpoint/pixel_encoder_report.json"))
    parser.add_argument("--output-weights", type=Path, default=Path("training/viewpoint/pixel_encoder_weights.pt"))
    parser.add_argument("--parts", type=int, default=72)
    parser.add_argument("--examples-per-part", type=int, default=6)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--width", type=int, default=96)
    parser.add_argument("--height", type=int, default=96)
    parser.add_argument("--epochs", type=int, default=24)
    parser.add_argument("--batch-rows", type=int, default=4)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--cache-dir", type=Path, default=Path("scratch/viewpoint-pixels"))
    args = parser.parse_args(argv)
    report_device(args.device, "torch-cuda-conv-ranknet")
    report = run_pixel_experiment(
        output_report=args.output_report,
        output_weights=args.output_weights,
        parts=args.parts,
        examples_per_part=args.examples_per_part,
        candidates=args.candidates,
        resolution=(args.width, args.height),
        epochs=args.epochs,
        batch_rows=args.batch_rows,
        workers=args.workers,
        device=args.device,
        seed=args.seed,
        bootstrap_samples=args.bootstrap_samples,
        cache_dir=args.cache_dir,
    )
    print(report["device_line"])
    print(json.dumps({"verdict": report["verdict"], "locked_test": report["locked_test"]["systems"], "visibility_information": report["visibility_information"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
