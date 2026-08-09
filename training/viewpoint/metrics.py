from __future__ import annotations

from typing import Iterable, Mapping
import numpy as np

from .data import ViewpointBatch
from .model import COMPONENTS, weighted_geometric_mean

Array = np.ndarray


def selected_components(batch: ViewpointBatch, indices: Array) -> Array:
    chosen = np.asarray(indices, np.int64)
    if chosen.shape != (len(batch.part_ids),):
        raise ValueError("one selected candidate index is required per example")
    if np.any(chosen < 0) or np.any(chosen >= batch.candidates.shape[1]):
        raise ValueError("selected candidate is out of range")
    if np.any(~batch.candidate_mask[np.arange(len(chosen)), chosen]):
        raise ValueError("selected candidate is masked")
    return batch.components[np.arange(len(chosen)), chosen]


def selection_scores(batch: ViewpointBatch, indices: Array) -> Array:
    return weighted_geometric_mean(selected_components(batch, indices))


def summarize_selection(batch: ViewpointBatch, indices: Array) -> dict[str, object]:
    components = selected_components(batch, indices)
    aggregate = weighted_geometric_mean(components)
    return {
        "examples": int(len(aggregate)),
        "aggregate": float(np.mean(aggregate)),
        "aggregate_quantiles": _quantiles(aggregate),
        "components": {name: float(np.mean(components[:, i])) for i, name in enumerate(COMPONENTS)},
        "component_saturation_rate": {
            name: float(np.mean(components[:, i] >= 1.0 - 1.0e-7))
            for i, name in enumerate(COMPONENTS)
        },
    }


def _quantiles(values: Array) -> dict[str, float]:
    quantiles = np.quantile(np.asarray(values, np.float64), (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0))
    return {
        name: float(value)
        for name, value in zip(("min", "p10", "p25", "median", "p75", "p90", "max"), quantiles)
    }


def _comparison(policy_score: Array, baseline_score: Array) -> dict[str, object]:
    delta = np.asarray(policy_score - baseline_score, np.float64)
    wins = delta > 0.0
    losses = delta < 0.0
    mean_delta = float(np.mean(delta))
    win_rate = float(np.mean(wins))
    if mean_delta > 0.0 and win_rate < 0.5:
        diagnosis = "higher mean from infrequent larger wins"
    elif mean_delta < 0.0 and win_rate > 0.5:
        diagnosis = "lower mean despite frequent smaller wins"
    elif mean_delta > 0.0:
        diagnosis = "higher mean with at least half strict wins"
    elif mean_delta < 0.0:
        diagnosis = "lower mean with at most half strict wins"
    else:
        diagnosis = "equal mean"
    return {
        "policy_aggregate": float(np.mean(policy_score)),
        "baseline_aggregate": float(np.mean(baseline_score)),
        "mean_delta": mean_delta,
        "strict_win_rate": win_rate,
        "strict_loss_rate": float(np.mean(losses)),
        "tie_rate": float(np.mean(delta == 0.0)),
        "mean_win_magnitude": float(np.mean(delta[wins])) if np.any(wins) else 0.0,
        "mean_loss_magnitude": float(np.mean(-delta[losses])) if np.any(losses) else 0.0,
        "delta_quantiles": _quantiles(delta),
        "diagnosis": diagnosis,
    }


def compare_selections(batch: ViewpointBatch, policy: Array, baselines: Mapping[str, Array]) -> dict[str, dict[str, object]]:
    policy_score = selection_scores(batch, policy)
    return {
        name: _comparison(policy_score, selection_scores(batch, indices))
        for name, indices in baselines.items()
    }


def _part_bootstrap_rows(batch: ViewpointBatch, *, samples: int, seed: int) -> Iterable[Array]:
    if samples <= 0:
        raise ValueError("bootstrap samples must be positive")
    unique_parts = np.asarray(sorted(set(batch.part_ids)), dtype=object)
    rows_by_part = {
        part: np.flatnonzero(np.asarray(batch.part_ids, dtype=object) == part)
        for part in unique_parts
    }
    rng = np.random.default_rng(seed)
    for _ in range(samples):
        drawn = rng.choice(unique_parts, size=len(unique_parts), replace=True)
        yield np.concatenate([rows_by_part[part] for part in drawn])


def _confidence_interval(values: Array, confidence: float) -> list[float]:
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between zero and one")
    tail = (1.0 - confidence) / 2.0
    return [float(value) for value in np.quantile(values, (tail, 1.0 - tail))]


def compute_equivalence_report(
    batch: ViewpointBatch,
    policy: Array,
    random_baselines: Mapping[int, Array],
    *,
    bootstrap_samples: int = 2000,
    confidence: float = 0.95,
    seed: int = 271828,
) -> dict[str, object]:
    """Compare one policy render with oracle-selected random-search budgets.

    Confidence intervals and crossover resampling operate on whole parts so
    multiple feature/task examples from one part are never treated as independent.
    """
    budgets = tuple(sorted(random_baselines))
    if not budgets or any(n <= 0 for n in budgets):
        raise ValueError("positive random-search budgets are required")
    policy_score = selection_scores(batch, policy)
    random_scores = {n: selection_scores(batch, random_baselines[n]) for n in budgets}
    comparisons = {
        n: _comparison(policy_score, random_scores[n])
        for n in budgets
    }
    bootstrap_delta = {n: [] for n in budgets}
    bootstrap_crossovers: list[int | None] = []
    for rows in _part_bootstrap_rows(batch, samples=bootstrap_samples, seed=seed):
        policy_mean = float(np.mean(policy_score[rows]))
        random_means = {n: float(np.mean(random_scores[n][rows])) for n in budgets}
        for n in budgets:
            bootstrap_delta[n].append(policy_mean - random_means[n])
        bootstrap_crossovers.append(next((n for n in budgets if random_means[n] >= policy_mean), None))
    for n in budgets:
        comparisons[n]["mean_delta_ci"] = _confidence_interval(
            np.asarray(bootstrap_delta[n]), confidence,
        )
    crossover = next(
        (n for n in budgets if comparisons[n]["baseline_aggregate"] >= comparisons[n]["policy_aggregate"]),
        None,
    )
    uncrossed = sum(value is None for value in bootstrap_crossovers)
    sentinel = budgets[-1] + 1
    encoded = np.asarray(
        [sentinel if value is None else value for value in bootstrap_crossovers],
        np.int64,
    )
    raw_crossover_ci = _confidence_interval(encoded, confidence)
    crossover_ci = {
        "lower": min(budgets, key=lambda n: abs(n - raw_crossover_ci[0])),
        "upper": (
            None
            if raw_crossover_ci[1] > budgets[-1]
            else min(budgets, key=lambda n: abs(n - raw_crossover_ci[1]))
        ),
        "upper_censored": raw_crossover_ci[1] > budgets[-1],
    }
    return {
        "render_budgets": list(budgets),
        "policy_renders": 1,
        "test_examples": len(batch.part_ids),
        "test_parts": len(set(batch.part_ids)),
        "confidence": confidence,
        "bootstrap_unit": "part_id",
        "bootstrap_samples": bootstrap_samples,
        "comparisons": {f"random@{n}": comparisons[n] for n in budgets},
        "crossover": crossover,
        "crossover_ci": crossover_ci,
        "crossover_censored_above_max_fraction": uncrossed / bootstrap_samples,
    }


def difficulty_report(
    batch: ViewpointBatch,
    policy: Array,
    baselines: Mapping[str, Array],
) -> dict[str, object]:
    labels = batch.difficulties or tuple("unlabeled" for _ in batch.part_ids)
    result: dict[str, object] = {}
    for difficulty in sorted(set(labels)):
        rows = np.flatnonzero(np.asarray(labels, dtype=object) == difficulty)
        sliced = batch.subset(rows)
        result[difficulty] = {
            "examples": len(rows),
            "parts": len(set(sliced.part_ids)),
            "comparisons": compare_selections(
                sliced,
                np.asarray(policy)[rows],
                {name: np.asarray(indices)[rows] for name, indices in baselines.items()},
            ),
        }
    return result


def evaluation_report(
    batch: ViewpointBatch,
    policy: Array,
    baselines: Mapping[str, Array],
    *,
    random_budgets: Mapping[int, Array] | None = None,
    bootstrap_samples: int = 2000,
    bootstrap_seed: int = 271828,
) -> dict[str, object]:
    comparisons = compare_selections(batch, policy, baselines)
    report: dict[str, object] = {
        "policy": summarize_selection(batch, policy),
        "baselines": {name: summarize_selection(batch, indices) for name, indices in baselines.items()},
        "comparisons": comparisons,
        "policy_win_rate": {name: values["strict_win_rate"] for name, values in comparisons.items()},
        "difficulty": difficulty_report(batch, policy, baselines),
    }
    if random_budgets is not None:
        report["compute_equivalence"] = compute_equivalence_report(
            batch, policy, random_budgets,
            bootstrap_samples=bootstrap_samples, seed=bootstrap_seed,
        )
    return report
