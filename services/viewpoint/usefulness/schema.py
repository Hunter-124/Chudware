"""Leak-resistant schemas for objective viewpoint-usefulness trials."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import numpy as np

from ..pixels.schema import COMPONENTS

QUESTION_KINDS = (
    "visible_hole_count",
    "pocket_throughness",
    "boss_height_comparison",
    "counterbore_concentricity",
    "diameter_ratio",
)


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class PublicQuestion:
    """The complete model-facing question; it intentionally has no answer field."""

    question_id: str
    kind: str
    prompt: str
    choices: tuple[str, ...]
    marker_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.question_id, "question_id")
        if self.kind not in QUESTION_KINDS:
            raise ValueError(f"unknown question kind {self.kind!r}")
        _nonempty(self.prompt, "prompt")
        if len(self.choices) < 2 or len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must contain at least two unique answers")
        if any(not isinstance(choice, str) or not choice for choice in self.choices):
            raise ValueError("choices must be non-empty strings")
        if len(self.marker_labels) == 0 or any(not label for label in self.marker_labels):
            raise ValueError("marker_labels must identify at least one image marker")


@dataclass(frozen=True, slots=True)
class QuestionSpec:
    """Evaluation-only question metadata and geometry-derived ground truth."""

    public: PublicQuestion
    part_id: str
    part_index: int
    example_index: int
    feature_id: str
    difficulty: str
    correct_answer: str
    derivation: str
    measurements: tuple[tuple[str, float | str], ...]
    marker_feature_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.part_id, "part_id")
        _nonempty(self.feature_id, "feature_id")
        _nonempty(self.derivation, "derivation")
        if self.part_index < 0 or self.example_index < 0:
            raise ValueError("part_index/example_index must be non-negative")
        if self.difficulty not in ("easy", "medium", "hard"):
            raise ValueError(f"invalid difficulty {self.difficulty!r}")
        if self.correct_answer not in self.public.choices:
            raise ValueError("correct_answer must be one of the public choices")
        if not self.marker_feature_ids or len(self.marker_feature_ids) != len(self.public.marker_labels):
            raise ValueError("marker features and labels must have equal non-zero length")


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """Only data exposed to an answerer."""

    candidate_id: str
    image: np.ndarray

    def __post_init__(self) -> None:
        _nonempty(self.candidate_id, "candidate_id")
        image = np.asarray(self.image)
        if image.ndim != 3 or image.shape[-1] != 3 or image.dtype != np.uint8:
            raise ValueError("image must be uint8 HxWx3")


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Evaluation record pairing a public observation with frozen-oracle labels."""

    observation: CandidateObservation
    oracle_components: Mapping[str, float]
    aggregate: float

    def __post_init__(self) -> None:
        if tuple(self.oracle_components) != COMPONENTS:
            raise ValueError("oracle_components must use the canonical seven-component order")
        checked = {name: float(self.oracle_components[name]) for name in COMPONENTS}
        if any(not np.isfinite(value) or value < 0.0 or value > 1.0 for value in checked.values()):
            raise ValueError("oracle components must be finite values in [0, 1]")
        if not np.isfinite(self.aggregate) or not 0.0 <= self.aggregate <= 1.0:
            raise ValueError("aggregate must be finite and in [0, 1]")
        object.__setattr__(self, "oracle_components", MappingProxyType(checked))


@dataclass(frozen=True, slots=True)
class BenchmarkItem:
    question: QuestionSpec
    candidates: tuple[CandidateView, ...]

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("a benchmark item needs at least one candidate")
        ids = tuple(candidate.observation.candidate_id for candidate in self.candidates)
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique within an item")


__all__ = [
    "QUESTION_KINDS",
    "BenchmarkItem",
    "CandidateObservation",
    "CandidateView",
    "PublicQuestion",
    "QuestionSpec",
]
