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


def _prefix_argmax(scores: Array, mask: Array, n: int) -> Array:
    result = np.empty(scores.shape[0], np.int64)
    for row in range(scores.shape[0]):
        valid = np.flatnonzero(mask[row])
        offered = valid[: min(n, len(valid))]
        result[row] = offered[int(np.argmax(scores[row, offered]))]
    return result


def _bootstrap_mean_ci(
    batch: ViewpointBatch,
    values: Array,
    *,
    samples: int,
    confidence: float,
    seed: int,
) -> list[float]:
    means = [
        float(np.mean(values[rows]))
        for rows in _part_bootstrap_rows(batch, samples=samples, seed=seed)
    ]
    return _confidence_interval(np.asarray(means, np.float64), confidence)


def deployed_evaluation_report(
    batch: ViewpointBatch,
    predicted_scores: Array,
    *,
    budgets: tuple[int, ...] = (1, 2, 4, 8, 16, 32),
    random_seed: int = 1729,
    bootstrap_samples: int = 2000,
    confidence: float = 0.95,
    bootstrap_seed: int = 271828,
) -> dict[str, object]:
    """Evaluate the scorer exactly as deployed on nested offered candidate sets."""
    scores = np.asarray(predicted_scores, np.float32)
    if scores.shape != batch.candidate_mask.shape:
        raise ValueError("predicted_scores must match the candidate mask")
    if tuple(sorted(set(budgets))) != budgets or any(n <= 0 for n in budgets):
        raise ValueError("budgets must be unique, positive, and increasing")
    actual = weighted_geometric_mean(batch.components)
    rows = np.arange(len(batch.part_ids))
    rngs = {
        n: np.random.default_rng(np.random.SeedSequence((random_seed, n, 0xA11CE)))
        for n in budgets
    }
    difficulty_labels = batch.difficulties or tuple("unlabeled" for _ in batch.part_ids)
    by_budget: dict[str, object] = {}
    selections: dict[str, dict[str, Array]] = {}

    for n in budgets:
        policy = _prefix_argmax(scores, batch.candidate_mask, n)
        oracle = _prefix_argmax(actual, batch.candidate_mask, n)
        random = np.empty(len(rows), np.int64)
        for row in rows:
            valid = np.flatnonzero(batch.candidate_mask[row])
            offered = valid[: min(n, len(valid))]
            random[row] = offered[int(rngs[n].integers(len(offered)))]
        policy_score = actual[rows, policy]
        random_score = actual[rows, random]
        oracle_score = actual[rows, oracle]
        delta = policy_score - random_score
        regret = oracle_score - policy_score
        vs_random = _comparison(policy_score, random_score)
        vs_random["mean_delta_ci"] = _bootstrap_mean_ci(
            batch,
            delta,
            samples=bootstrap_samples,
            confidence=confidence,
            seed=bootstrap_seed + n,
        )
        difficulty: dict[str, object] = {}
        labels = np.asarray(difficulty_labels, dtype=object)
        for label in sorted(set(difficulty_labels)):
            selected_rows = np.flatnonzero(labels == label)
            difficulty[label] = {
                "examples": int(len(selected_rows)),
                "parts": int(len(set(batch.part_ids[int(row)] for row in selected_rows))),
                "policy_aggregate": float(np.mean(policy_score[selected_rows])),
                "random_aggregate": float(np.mean(random_score[selected_rows])),
                "oracle_upper_bound": float(np.mean(oracle_score[selected_rows])),
                "mean_regret": float(np.mean(regret[selected_rows])),
                "strict_win_rate_vs_random": float(np.mean(delta[selected_rows] > 0.0)),
            }
        by_budget[str(n)] = {
            "policy_selected": summarize_selection(batch, policy),
            "random_selected": summarize_selection(batch, random),
            "oracle_upper_bound": summarize_selection(batch, oracle),
            "policy_vs_random": vs_random,
            "regret": {
                "mean": float(np.mean(regret)),
                "mean_ci": _bootstrap_mean_ci(
                    batch,
                    regret,
                    samples=bootstrap_samples,
                    confidence=confidence,
                    seed=bootstrap_seed + 1000 + n,
                ),
                "quantiles": _quantiles(regret),
                "oracle_match_rate": float(np.mean(regret == 0.0)),
            },
            "difficulty": difficulty,
        }
        selections[str(n)] = {"policy": policy, "random": random, "oracle": oracle}

    return {
        "render_budgets": list(budgets),
        "test_examples": len(batch.part_ids),
        "test_parts": len(set(batch.part_ids)),
        "bootstrap_unit": "part_id",
        "bootstrap_samples": bootstrap_samples,
        "confidence": confidence,
        "by_budget": by_budget,
        "selection_indices": {
            n: {name: values.tolist() for name, values in selection.items()}
            for n, selection in selections.items()
        },
    }


def _direct_score_summary(values: Array) -> dict[str, object]:
    scores = np.asarray(values, np.float64)
    if scores.ndim != 1 or not np.isfinite(scores).all():
        raise ValueError("score summaries require one finite value per example")
    return {
        "aggregate": float(np.mean(scores)),
        "quantiles": _quantiles(scores),
    }


def _direct_comparison_report(
    batch: ViewpointBatch,
    policy_score: Array,
    baseline_score: Array,
    *,
    bootstrap_samples: int,
    confidence: float,
    bootstrap_seed: int,
) -> dict[str, object]:
    comparison = _comparison(np.asarray(policy_score), np.asarray(baseline_score))
    comparison["mean_delta_ci"] = _bootstrap_mean_ci(
        batch,
        np.asarray(policy_score) - np.asarray(baseline_score),
        samples=bootstrap_samples,
        confidence=confidence,
        seed=bootstrap_seed,
    )
    return comparison


def _direct_difficulty_report(
    batch: ViewpointBatch,
    policy_score: Array,
    random_score: Array,
    fixed_score: Array,
    oracle_score: Array,
) -> dict[str, object]:
    labels = np.asarray(batch.difficulties or tuple("unlabeled" for _ in batch.part_ids), dtype=object)
    result: dict[str, object] = {}
    for label in sorted(set(labels.tolist())):
        rows = np.flatnonzero(labels == label)
        result[str(label)] = {
            "examples": int(len(rows)),
            "parts": int(len(set(batch.part_ids[int(row)] for row in rows))),
            "policy_aggregate": float(np.mean(np.asarray(policy_score)[rows])),
            "random_aggregate": float(np.mean(np.asarray(random_score)[rows])),
            "fixed_isometric_aggregate": float(np.mean(np.asarray(fixed_score)[rows])),
            "full_information_oracle": float(np.mean(np.asarray(oracle_score)[rows])),
            "policy_oracle_regret": float(np.mean(np.asarray(oracle_score)[rows] - np.asarray(policy_score)[rows])),
        }
    return result


def information_asymmetry_report(
    batch: ViewpointBatch,
    *,
    policy: Array,
    random: Array,
    fixed_isometric: Array,
    bootstrap_samples: int = 2000,
    confidence: float = 0.95,
    bootstrap_seed: int = 271828,
) -> dict[str, object]:
    """Compare limited-information choices with a full-information oracle.

    Only the labels used after selection enter this function.  The caller is
    responsible for producing ``policy`` from limited observations.
    """
    actual = weighted_geometric_mean(batch.components)
    rows = np.arange(len(batch.part_ids))
    selections = {
        "policy": np.asarray(policy, np.int64),
        "random": np.asarray(random, np.int64),
        "fixed_isometric": np.asarray(fixed_isometric, np.int64),
    }
    for name, chosen in selections.items():
        if chosen.shape != rows.shape:
            raise ValueError(f"{name} must contain one selection per example")
        if np.any(chosen < 0) or np.any(chosen >= actual.shape[1]):
            raise ValueError(f"{name} contains an out-of-range candidate")
        if np.any(~batch.candidate_mask[rows, chosen]):
            raise ValueError(f"{name} selected a masked candidate")
    oracle = _prefix_argmax(actual, batch.candidate_mask, actual.shape[1])
    policy_score = actual[rows, selections["policy"]]
    random_score = actual[rows, selections["random"]]
    fixed_score = actual[rows, selections["fixed_isometric"]]
    oracle_score = actual[rows, oracle]
    regret = oracle_score - policy_score
    return {
        "test_examples": len(batch.part_ids),
        "test_parts": len(set(batch.part_ids)),
        "bootstrap_unit": "part_id",
        "bootstrap_samples": bootstrap_samples,
        "confidence": confidence,
        "policy": _direct_score_summary(policy_score),
        "random": _direct_score_summary(random_score),
        "fixed_isometric": _direct_score_summary(fixed_score),
        "full_information_oracle": _direct_score_summary(oracle_score),
        "policy_vs_random": _direct_comparison_report(
            batch, policy_score, random_score,
            bootstrap_samples=bootstrap_samples, confidence=confidence,
            bootstrap_seed=bootstrap_seed,
        ),
        "policy_vs_fixed_isometric": _direct_comparison_report(
            batch, policy_score, fixed_score,
            bootstrap_samples=bootstrap_samples, confidence=confidence,
            bootstrap_seed=bootstrap_seed + 1,
        ),
        "full_information_oracle_regret": {
            "mean": float(np.mean(regret)),
            "mean_ci": _bootstrap_mean_ci(
                batch, regret,
                samples=bootstrap_samples, confidence=confidence,
                seed=bootstrap_seed + 2,
            ),
            "quantiles": _quantiles(regret),
        },
        "difficulty": _direct_difficulty_report(
            batch, policy_score, random_score, fixed_score, oracle_score,
        ),
        "selection_indices": {
            **{name: chosen.tolist() for name, chosen in selections.items()},
            "full_information_oracle": oracle.tolist(),
        },
    }


def sequential_evaluation_report(
    batch: ViewpointBatch,
    *,
    policy_best_by_budget: Mapping[int, Array],
    one_shot_random_best_by_budget: Mapping[int, Array],
    bootstrap_samples: int = 2000,
    confidence: float = 0.95,
    bootstrap_seed: int = 314159,
) -> dict[str, object]:
    """Compare adaptive accumulated looks with non-adaptive random search.

    Inputs are the best realized utility after each matched additional-render
    budget.  The upper bound is the best candidate under full geometry and all
    candidate evaluations; it is intentionally not weakened to the policy budget.
    """
    budgets = tuple(sorted(policy_best_by_budget))
    if not budgets or budgets != tuple(sorted(one_shot_random_best_by_budget)):
        raise ValueError("policy and one-shot reports need identical positive budgets")
    if any(value <= 0 for value in budgets):
        raise ValueError("look budgets must be positive")
    actual = weighted_geometric_mean(batch.components)
    oracle_score = np.max(np.where(batch.candidate_mask, actual, -np.inf), axis=1)
    by_budget: dict[str, object] = {}
    for budget in budgets:
        policy_score = np.asarray(policy_best_by_budget[budget], np.float64)
        random_score = np.asarray(one_shot_random_best_by_budget[budget], np.float64)
        expected = (len(batch.part_ids),)
        if policy_score.shape != expected or random_score.shape != expected:
            raise ValueError("sequential score arrays must contain one value per example")
        if not np.isfinite(policy_score).all() or not np.isfinite(random_score).all():
            raise ValueError("sequential score arrays must be finite")
        regret = oracle_score - policy_score
        fixed_score = random_score
        by_budget[str(budget)] = {
            "policy": _direct_score_summary(policy_score),
            "one_shot_random_best": _direct_score_summary(random_score),
            "full_information_oracle": _direct_score_summary(oracle_score),
            "policy_vs_one_shot_random": _direct_comparison_report(
                batch, policy_score, random_score,
                bootstrap_samples=bootstrap_samples, confidence=confidence,
                bootstrap_seed=bootstrap_seed + budget,
            ),
            "full_information_oracle_regret": {
                "mean": float(np.mean(regret)),
                "mean_ci": _bootstrap_mean_ci(
                    batch, regret,
                    samples=bootstrap_samples, confidence=confidence,
                    seed=bootstrap_seed + 1000 + budget,
                ),
                "quantiles": _quantiles(regret),
            },
            "difficulty": _direct_difficulty_report(
                batch, policy_score, random_score, fixed_score, oracle_score,
            ),
        }
    return {
        "look_budgets": list(budgets),
        "budget_unit": "additional camera render after the common initial observation",
        "oracle_information": "full geometry and all candidate labels",
        "test_examples": len(batch.part_ids),
        "test_parts": len(set(batch.part_ids)),
        "bootstrap_unit": "part_id",
        "bootstrap_samples": bootstrap_samples,
        "confidence": confidence,
        "by_budget": by_budget,
    }
