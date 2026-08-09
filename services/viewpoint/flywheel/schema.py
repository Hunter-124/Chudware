"""Durable flywheel records layered around the frozen feedback schema."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping

from services.viewpoint.feedback import PreferenceRecord

SCHEMA_VERSION = "chudware.viewpoint.flywheel/1"
HARNESS_VERSION = "viewpoint-flywheel-1"


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_tokens(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or null")
    return value


@dataclass(frozen=True, slots=True)
class AnswerKey:
    """Exact, normalized answers kept out of the scorer prompt."""

    accepted_answers: tuple[str, ...]
    evaluator: str = "normalized_exact_v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_answers", tuple(self.accepted_answers))
        if not self.accepted_answers:
            raise ValueError("accepted_answers must not be empty")
        for answer in self.accepted_answers:
            _nonempty(answer, "accepted answer")
        _nonempty(self.evaluator, "evaluator")

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.strip().casefold().split())

    def check(self, answer: str) -> bool:
        actual = self._normalize(_nonempty(answer, "answer"))
        return actual in {self._normalize(item) for item in self.accepted_answers}

    def to_dict(self) -> dict[str, Any]:
        return {"accepted_answers": list(self.accepted_answers), "evaluator": self.evaluator}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnswerKey":
        return cls(tuple(value["accepted_answers"]), str(value["evaluator"]))


@dataclass(frozen=True, slots=True)
class UsageCost:
    """Per-judgement token and USD accounting; estimates are always marked."""

    requests: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    source: str
    estimated: bool
    input_usd_per_million: float
    output_usd_per_million: float

    def __post_init__(self) -> None:
        if type(self.requests) is not int or self.requests < 0:
            raise ValueError("requests must be a non-negative integer")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            _optional_tokens(getattr(self, name), name)
        if self.cost_usd is not None and (not math.isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("cost_usd must be finite, non-negative, or null")
        _nonempty(self.source, "source")
        if type(self.estimated) is not bool:
            raise ValueError("estimated must be boolean")
        for name in ("input_usd_per_million", "output_usd_per_million"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "requests", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd",
            "source", "estimated", "input_usd_per_million", "output_usd_per_million")}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UsageCost":
        return cls(*(value[name] for name in (
            "requests", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd",
            "source", "estimated", "input_usd_per_million", "output_usd_per_million")))


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    chosen_candidate_id: str
    preference_winner_id: str | None
    answer_text: str
    answer_correct: bool | None
    selection_matches_preference: bool | None
    chosen_oracle_inspectability: float | None

    def __post_init__(self) -> None:
        _nonempty(self.chosen_candidate_id, "chosen_candidate_id")
        _nonempty(self.answer_text, "answer_text")
        if self.preference_winner_id is not None:
            _nonempty(self.preference_winner_id, "preference_winner_id")
        if self.answer_correct is not None and type(self.answer_correct) is not bool:
            raise ValueError("answer_correct must be boolean or null")
        if self.selection_matches_preference is not None and type(
                self.selection_matches_preference) is not bool:
            raise ValueError("selection_matches_preference must be boolean or null")
        if self.chosen_oracle_inspectability is not None and not math.isfinite(
                float(self.chosen_oracle_inspectability)):
            raise ValueError("chosen_oracle_inspectability must be finite or null")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "chosen_candidate_id", "preference_winner_id", "answer_text", "answer_correct",
            "selection_matches_preference", "chosen_oracle_inspectability")}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnswerOutcome":
        return cls(*(value[name] for name in (
            "chosen_candidate_id", "preference_winner_id", "answer_text", "answer_correct",
            "selection_matches_preference", "chosen_oracle_inspectability")))


@dataclass(frozen=True, slots=True)
class FlywheelRecord:
    preference: PreferenceRecord
    render_provenance: Mapping[str, Any]
    question_provenance: Mapping[str, Any]
    candidate_oracle: Mapping[str, Any]
    outcome: AnswerOutcome
    usage: UsageCost
    provider_id: str
    model_id: str
    harness_version: str = HARNESS_VERSION
    schema: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_VERSION:
            raise ValueError(f"unsupported flywheel schema {self.schema!r}")
        if not isinstance(self.preference, PreferenceRecord):
            raise ValueError("preference must be PreferenceRecord")
        if self.preference.answer_correct != self.outcome.answer_correct:
            raise ValueError("preference and outcome correctness disagree")
        for name in ("render_provenance", "question_provenance", "candidate_oracle"):
            if not isinstance(getattr(self, name), Mapping):
                raise ValueError(f"{name} must be an object")
        _nonempty(self.provider_id, "provider_id")
        _nonempty(self.model_id, "model_id")
        _nonempty(self.harness_version, "harness_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "preference": self.preference.to_dict(),
            "render_provenance": dict(self.render_provenance),
            "question_provenance": dict(self.question_provenance),
            "candidate_oracle": dict(self.candidate_oracle),
            "outcome": self.outcome.to_dict(),
            "usage": self.usage.to_dict(),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "harness_version": self.harness_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FlywheelRecord":
        if value.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported flywheel schema {value.get('schema')!r}")
        return cls(
            preference=PreferenceRecord.from_dict(value["preference"]),
            render_provenance=value["render_provenance"],
            question_provenance=value["question_provenance"],
            candidate_oracle=value["candidate_oracle"],
            outcome=AnswerOutcome.from_dict(value["outcome"]),
            usage=UsageCost.from_dict(value["usage"]),
            provider_id=value["provider_id"],
            model_id=value["model_id"],
            harness_version=value["harness_version"],
            schema=value["schema"],
        )

    @classmethod
    def from_json(cls, value: str) -> "FlywheelRecord":
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("flywheel record must be an object")
        return cls.from_dict(payload)
