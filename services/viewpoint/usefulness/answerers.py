"""Deterministic answerer interface and model-free benchmark controls."""
from __future__ import annotations

from hashlib import sha256
from typing import Mapping, Protocol, Sequence

from .schema import BenchmarkItem, CandidateObservation, PublicQuestion


def _stable_unit(*parts: object) -> float:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(sha256(payload).digest()[:8], "big") / float(1 << 64)


class Answerer(Protocol):
    """A real adapter receives exactly the same prompt, choices, markers and image."""

    name: str

    def answer(self, question: PublicQuestion, observation: CandidateObservation) -> str:
        ...


class OracleAnswerer:
    """Positive control whose hidden lookup contains the geometry-derived answers."""

    name = "scripted_oracle"

    def __init__(self, answers: Mapping[str, str]) -> None:
        self._answers = dict(answers)

    @classmethod
    def from_items(cls, items: Sequence[BenchmarkItem]) -> "OracleAnswerer":
        return cls({item.question.public.question_id: item.question.correct_answer for item in items})

    def answer(self, question: PublicQuestion, observation: CandidateObservation) -> str:
        del observation
        return self._answers[question.question_id]


class BlindAnswerer:
    """Negative control sampling declared per-kind answer priors without reading pixels."""

    name = "blind_prior"

    def __init__(self, priors: Mapping[str, Mapping[str, float]] | None = None, *, seed: int = 1729) -> None:
        self._priors = {kind: dict(values) for kind, values in (priors or {}).items()}
        self._seed = int(seed)

    def answer(self, question: PublicQuestion, observation: CandidateObservation) -> str:
        weights = self._priors.get(question.kind)
        if weights is None:
            ordered = tuple((choice, 1.0) for choice in question.choices)
        else:
            if set(weights) != set(question.choices):
                raise ValueError(f"prior for {question.kind!r} must name every answer choice")
            ordered = tuple((choice, float(weights[choice])) for choice in question.choices)
        if any(weight < 0.0 for _, weight in ordered) or sum(weight for _, weight in ordered) <= 0.0:
            raise ValueError("prior weights must be non-negative with positive total")
        draw = _stable_unit("blind", self._seed, question.question_id, observation.candidate_id)
        threshold = draw * sum(weight for _, weight in ordered)
        cumulative = 0.0
        for choice, weight in ordered:
            cumulative += weight
            if threshold < cumulative:
                return choice
        return ordered[-1][0]


class ComponentDegradedAnswerer:
    """Positive instrument control with accuracy explicitly driven by one component.

    For a question with ``K`` choices and named component quality ``q``, the probability of
    returning the correct answer is ``1/K + (1 - 1/K) q``.  Stable hashing replaces runtime
    randomness so repeated benchmark runs are byte-for-byte reproducible.
    """

    def __init__(
        self,
        *,
        component: str,
        correct_answers: Mapping[str, str],
        component_values: Mapping[tuple[str, str], float],
        seed: int = 1729,
    ) -> None:
        self.component = component
        self.name = f"degraded:{component}"
        self._correct_answers = dict(correct_answers)
        self._component_values = dict(component_values)
        self._seed = int(seed)

    @classmethod
    def from_items(
        cls, items: Sequence[BenchmarkItem], *, component: str, seed: int = 1729
    ) -> "ComponentDegradedAnswerer":
        if not items or component not in items[0].candidates[0].oracle_components:
            raise ValueError(f"unknown inspectability component {component!r}")
        correct = {item.question.public.question_id: item.question.correct_answer for item in items}
        values = {
            (item.question.public.question_id, candidate.observation.candidate_id): candidate.oracle_components[component]
            for item in items for candidate in item.candidates
        }
        return cls(component=component, correct_answers=correct, component_values=values, seed=seed)

    def answer(self, question: PublicQuestion, observation: CandidateObservation) -> str:
        key = (question.question_id, observation.candidate_id)
        correct = self._correct_answers[question.question_id]
        quality = self._component_values[key]
        chance = 1.0 / len(question.choices)
        probability_correct = chance + (1.0 - chance) * quality
        if _stable_unit("degraded-correct", self._seed, *key) < probability_correct:
            return correct
        wrong = tuple(choice for choice in question.choices if choice != correct)
        index = min(int(_stable_unit("degraded-wrong", self._seed, *key) * len(wrong)), len(wrong) - 1)
        return wrong[index]


__all__ = ["Answerer", "BlindAnswerer", "ComponentDegradedAnswerer", "OracleAnswerer"]
