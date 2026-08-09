"""Accuracy, dependence, and difficulty analysis for usefulness trials."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from ..pixels.schema import COMPONENTS
from .answerers import Answerer
from .schema import BenchmarkItem


@dataclass(frozen=True, slots=True)
class TrialResult:
    question_id: str
    candidate_id: str
    kind: str
    difficulty: str
    answer: str
    correct: bool
    components: tuple[tuple[str, float], ...]
    aggregate: float


@dataclass(frozen=True, slots=True)
class DependenceStats:
    samples: int
    accuracy: float
    pearson_correlation: float
    mutual_information_bits: float
    bin_accuracy: tuple[tuple[float, float, int, float], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "accuracy": self.accuracy,
            "pearson_correlation": self.pearson_correlation,
            "mutual_information_bits": self.mutual_information_bits,
            "bin_accuracy": [
                {"lower": lower, "upper": upper, "count": count, "accuracy": accuracy}
                for lower, upper, count, accuracy in self.bin_accuracy
            ],
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    answerer: str
    trials: tuple[TrialResult, ...]
    accuracy: float
    chance_accuracy: float
    by_signal: Mapping[str, DependenceStats]
    by_difficulty: Mapping[str, tuple[int, float]]
    chosen_view_accuracy: float | None

    def as_dict(self, *, include_trials: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "answerer": self.answerer,
            "samples": len(self.trials),
            "accuracy": self.accuracy,
            "chance_accuracy": self.chance_accuracy,
            "chosen_view_accuracy": self.chosen_view_accuracy,
            "by_signal": {name: stats.as_dict() for name, stats in self.by_signal.items()},
            "by_difficulty": {
                difficulty: {"count": count, "accuracy": accuracy}
                for difficulty, (count, accuracy) in self.by_difficulty.items()
            },
        }
        if include_trials:
            result["trials"] = [
                {
                    "question_id": trial.question_id,
                    "candidate_id": trial.candidate_id,
                    "kind": trial.kind,
                    "difficulty": trial.difficulty,
                    "answer": trial.answer,
                    "correct": trial.correct,
                    "components": dict(trial.components),
                    "aggregate": trial.aggregate,
                }
                for trial in self.trials
            ]
        return result


def _dependence(signal: np.ndarray, correct: np.ndarray, *, bins: int) -> DependenceStats:
    if len(signal) != len(correct) or len(signal) < 2:
        raise ValueError("dependence analysis needs equal arrays with at least two samples")
    if bins < 2:
        raise ValueError("bins must be at least two")
    signal_std = float(np.std(signal))
    correct_std = float(np.std(correct))
    correlation = 0.0 if signal_std <= 1.0e-15 or correct_std <= 1.0e-15 else float(np.corrcoef(signal, correct)[0, 1])
    edges = np.linspace(0.0, 1.0, bins + 1)
    signal_bins = np.minimum(np.searchsorted(edges, signal, side="right") - 1, bins - 1)
    signal_bins = np.maximum(signal_bins, 0)
    joint = np.zeros((bins, 2), dtype=np.float64)
    for index, outcome in zip(signal_bins, correct.astype(np.int64)):
        joint[int(index), int(outcome)] += 1.0
    joint /= joint.sum()
    marginal_signal = joint.sum(axis=1)
    marginal_correct = joint.sum(axis=0)
    independent = marginal_signal[:, None] * marginal_correct[None, :]
    occupied = joint > 0.0
    mutual_information = float(np.sum(joint[occupied] * np.log2(joint[occupied] / independent[occupied])))
    bin_accuracy: list[tuple[float, float, int, float]] = []
    for index in range(bins):
        selected = signal_bins == index
        count = int(np.count_nonzero(selected))
        bin_accuracy.append((float(edges[index]), float(edges[index + 1]), count, float(np.mean(correct[selected])) if count else 0.0))
    return DependenceStats(len(signal), float(np.mean(correct)), correlation, mutual_information, tuple(bin_accuracy))


def highest_aggregate_selections(items: Sequence[BenchmarkItem]) -> dict[str, str]:
    return {
        item.question.public.question_id: max(item.candidates, key=lambda candidate: candidate.aggregate).observation.candidate_id
        for item in items
    }


def evaluate_answerer(
    items: Sequence[BenchmarkItem],
    answerer: Answerer,
    *,
    selections: Mapping[str, str] | None = None,
    bins: int = 8,
) -> EvaluationReport:
    if not items:
        raise ValueError("at least one benchmark item is required")
    trials: list[TrialResult] = []
    chance_sum = 0.0
    for item in items:
        question = item.question
        for candidate in item.candidates:
            answer = answerer.answer(question.public, candidate.observation)
            if answer not in question.public.choices:
                raise ValueError(f"answerer returned non-choice answer {answer!r} for {question.public.question_id}")
            trials.append(TrialResult(
                question.public.question_id,
                candidate.observation.candidate_id,
                question.public.kind,
                question.difficulty,
                answer,
                answer == question.correct_answer,
                tuple((name, candidate.oracle_components[name]) for name in COMPONENTS),
                candidate.aggregate,
            ))
            chance_sum += 1.0 / len(question.public.choices)
    correct = np.asarray([trial.correct for trial in trials], dtype=np.float64)
    by_signal = {
        name: _dependence(
            np.asarray([dict(trial.components)[name] for trial in trials], dtype=np.float64), correct, bins=bins
        )
        for name in COMPONENTS
    }
    by_signal["aggregate"] = _dependence(
        np.asarray([trial.aggregate for trial in trials], dtype=np.float64), correct, bins=bins
    )
    by_difficulty: dict[str, tuple[int, float]] = {}
    for difficulty in ("easy", "medium", "hard"):
        selected = np.asarray([trial.difficulty == difficulty for trial in trials], dtype=bool)
        count = int(np.count_nonzero(selected))
        by_difficulty[difficulty] = (count, float(np.mean(correct[selected])) if count else 0.0)
    chosen_accuracy: float | None = None
    if selections is not None:
        chosen: list[bool] = []
        for item in items:
            question_id = item.question.public.question_id
            if question_id not in selections:
                raise ValueError(f"selection missing question {question_id!r}")
            matching = [trial.correct for trial in trials if trial.question_id == question_id and trial.candidate_id == selections[question_id]]
            if len(matching) != 1:
                raise ValueError(f"selection for {question_id!r} is not one of its candidates")
            chosen.append(matching[0])
        chosen_accuracy = float(np.mean(chosen))
    return EvaluationReport(
        getattr(answerer, "name", type(answerer).__name__), tuple(trials), float(np.mean(correct)),
        chance_sum / len(trials), by_signal, by_difficulty, chosen_accuracy,
    )


__all__ = [
    "DependenceStats",
    "EvaluationReport",
    "TrialResult",
    "evaluate_answerer",
    "highest_aggregate_selections",
]
