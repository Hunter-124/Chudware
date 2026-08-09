"""Scripted and opt-in repository-provider scorer backends."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlparse

from services.viewpoint.feedback import (
    CandidateSetResponse,
    PairwiseScore,
    RubricScore,
    ScoringResponse,
    expected_pair_order,
    validate_scoring,
)

from .schema import HARNESS_VERSION, UsageCost

_SYSTEM_PROMPT = """You are scoring which rendered CAD view is useful for answering the supplied
part question. Candidate identity is canonical; presentation order is candidate_set.permutation.
Inspect every image. Return one JSON object and no prose. The `scoring` value MUST conform exactly
to services.viewpoint.feedback ScoringResponse v1: every candidate gets integer rubric values 0..4
for feature_visible, unoccluded, unforeshortened, adequate_scale, and task_usefulness; every
unordered pair appears exactly once and in the shown order, with confidence 1..4 (or a zero-confidence
abstention). Also return chosen_candidate_id and answer_text. Answer using only the chosen view.
Do not claim visibility for evidence absent from the pixels."""


@dataclass(frozen=True, slots=True)
class Judgement:
    scoring: ScoringResponse
    chosen_candidate_id: str
    answer_text: str
    usage: UsageCost

    def __post_init__(self) -> None:
        if not isinstance(self.scoring, ScoringResponse):
            raise ValueError("scoring must be ScoringResponse")
        if not isinstance(self.chosen_candidate_id, str) or not self.chosen_candidate_id.strip():
            raise ValueError("chosen_candidate_id must be non-empty")
        if not isinstance(self.answer_text, str) or not self.answer_text.strip():
            raise ValueError("answer_text must be non-empty")


class ScorerBackend(Protocol):
    provider_id: str
    model_id: str
    harness_version: str

    def estimate(self, candidate_set: CandidateSetResponse) -> UsageCost: ...

    def judge(self, candidate_set: CandidateSetResponse) -> Judgement: ...


class ScriptedScorer:
    """Deterministic scorer for tests and offline smoke runs.

    ``first_position_biased`` is the adversarial control: each comparison picks slot A even when
    seeded permutations change candidate identity. Feedback's exact-binomial detector must reject it.
    """

    provider_id = "scripted"
    harness_version = HARNESS_VERSION

    def __init__(self, *, ranking: Mapping[str, int] | None = None, answer_text: str = "yes",
                 model_id: str = "scripted-usefulness-v1",
                 first_position_biased: bool = False) -> None:
        self.ranking = dict(ranking or {})
        self.answer_text = answer_text
        self.model_id = model_id
        self.first_position_biased = first_position_biased

    def estimate(self, candidate_set: CandidateSetResponse) -> UsageCost:
        del candidate_set
        return UsageCost(1, 0, 0, 0, 0.0, "scripted", False, 0.0, 0.0)

    def _rank(self, candidate_id: str, canonical_index: int) -> int:
        return int(self.ranking.get(candidate_id, max(0, 4 - canonical_index)))

    def judge(self, candidate_set: CandidateSetResponse) -> Judgement:
        canonical = candidate_set.canonical_candidates
        rank = {candidate.candidate_id: self._rank(candidate.candidate_id, index)
                for index, candidate in enumerate(canonical)}
        rubrics = tuple(RubricScore(
            candidate.candidate_id,
            max(0, min(4, rank[candidate.candidate_id])),
            max(0, min(4, rank[candidate.candidate_id])),
            max(0, min(4, rank[candidate.candidate_id])),
            4,
            max(0, min(4, rank[candidate.candidate_id])),
        ) for candidate in canonical)
        pairs: list[PairwiseScore] = []
        for first, second in expected_pair_order(candidate_set):
            if self.first_position_biased:
                preferred = first
            else:
                preferred = first if rank[first] >= rank[second] else second
            pairs.append(PairwiseScore(first, second, preferred, 4, None))
        if self.first_position_biased:
            chosen = candidate_set.ordered_candidates[0].candidate_id
        else:
            chosen = max((candidate.candidate_id for candidate in canonical),
                         key=lambda candidate_id: (rank[candidate_id], candidate_id))
        scoring = ScoringResponse(candidate_set.request_id, rubrics, tuple(pairs))
        validate_scoring(candidate_set, scoring)
        return Judgement(scoring, chosen, self.answer_text, self.estimate(candidate_set))


@dataclass(frozen=True, slots=True)
class RealModelConfig:
    """Explicit live-model selection plus auditable pricing/token assumptions."""

    enabled: bool
    provider: str
    model: str
    max_output_tokens: int
    input_usd_per_million: float
    output_usd_per_million: float
    image_tokens_per_candidate: int
    chars_per_token: float = 4.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for name in ("provider", "model"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        for name in ("max_output_tokens", "image_tokens_per_candidate"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("input_usd_per_million", "output_usd_per_million", "chars_per_token"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealModelConfig":
        expected = {"enabled", "provider", "model", "max_output_tokens",
                    "input_usd_per_million", "output_usd_per_million",
                    "image_tokens_per_candidate", "chars_per_token"}
        if set(value) != expected:
            raise ValueError(f"invalid live model config fields: expected {sorted(expected)}")
        return cls(*(value[name] for name in (
            "enabled", "provider", "model", "max_output_tokens", "input_usd_per_million",
            "output_usd_per_million", "image_tokens_per_candidate", "chars_per_token")))

    @classmethod
    def from_json_file(cls, path: str | os.PathLike[str]) -> "RealModelConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("live model config must be a JSON object")
        return cls.from_dict(payload)


class RealModelScorer:
    """Vision scorer routed through ``services.agent.config.get_provider``.

    Live execution has two independent opt-ins: ``config.enabled`` and ``allow_live``. ``judge``
    also hard-refuses whenever pytest is imported or advertises a current test. Provider creation is
    lazy, so construction and dry-run estimation cannot start a sidecar or touch the network.
    """

    harness_version = HARNESS_VERSION

    def __init__(self, config: RealModelConfig, *, allow_live: bool = False) -> None:
        self.config = config
        self.allow_live = allow_live
        self.provider_id = config.provider
        self.model_id = config.model
        self._provider: Any = None

    @staticmethod
    def _image_path(uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            return unquote(parsed.path)
        if not parsed.scheme:
            return uri
        raise ValueError(f"live scorer requires local file renders, got {uri!r}")

    @classmethod
    def _messages(cls, candidate_set: CandidateSetResponse) -> list[dict[str, Any]]:
        from services.agent.providers.base import image_part, text_part, user

        response_shape = {
            "scoring": {
                "schema_version": "1.0",
                "request_id": candidate_set.request_id,
                "rubric_scores": "one exact RubricScore object per candidate",
                "pairwise_scores": [
                    {"candidate_a_id": first, "candidate_b_id": second,
                     "preferred_id": "one id or null", "confidence": "0..4",
                     "abstain_reason": "string or null"}
                    for first, second in expected_pair_order(candidate_set)
                ],
                "overall_abstain_reason": None,
            },
            "chosen_candidate_id": "candidate id used to answer",
            "answer_text": "answer to task_text using only that view",
        }
        parts = [text_part(
            "candidate_set (exact feedback schema):\n" + candidate_set.to_json()
            + "\nrequired response shape:\n"
            + json.dumps(response_shape, sort_keys=True, separators=(",", ":"))
        )]
        for position, candidate in enumerate(candidate_set.ordered_candidates):
            parts.append(text_part(
                f"Presented position {position + 1}; candidate_id={candidate.candidate_id}; "
                f"render_sha256={candidate.render_sha256}"))
            parts.append(image_part(cls._image_path(candidate.render_uri), "image/png"))
        return [user(parts)]

    def estimate(self, candidate_set: CandidateSetResponse) -> UsageCost:
        messages = self._messages(candidate_set)
        text_chars = len(_SYSTEM_PROMPT)
        for message in messages:
            for part in message["content"]:
                if part.get("type") == "text":
                    text_chars += len(str(part.get("text") or ""))
        prompt_tokens = math.ceil(text_chars / self.config.chars_per_token)
        prompt_tokens += len(candidate_set.canonical_candidates) * self.config.image_tokens_per_candidate
        completion_tokens = self.config.max_output_tokens
        cost = (prompt_tokens * self.config.input_usd_per_million
                + completion_tokens * self.config.output_usd_per_million) / 1_000_000.0
        return UsageCost(1, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens,
                         cost, "configured_dry_run_estimate", True,
                         self.config.input_usd_per_million,
                         self.config.output_usd_per_million)

    @staticmethod
    def _parse_reply(reply: str) -> Mapping[str, Any]:
        text = reply.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            last_fence = text.rfind("```")
            if first_newline < 0 or last_fence <= first_newline:
                raise ValueError("model returned an invalid fenced JSON response")
            text = text[first_newline + 1:last_fence].strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("model response must be a JSON object")
        if set(payload) != {"scoring", "chosen_candidate_id", "answer_text"}:
            raise ValueError("model response must contain exactly scoring, chosen_candidate_id, answer_text")
        return payload

    def _resolve_provider(self) -> Any:
        if self._provider is None:
            from services.agent import config as agent_config

            repo_config = agent_config.load_config()
            repo_config["model"] = self.config.model
            self._provider = agent_config.get_provider(self.config.provider, cfg=repo_config)
        return self._provider

    def judge(self, candidate_set: CandidateSetResponse) -> Judgement:
        if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
            raise RuntimeError("live viewpoint scoring is disabled under pytest")
        if not self.config.enabled or not self.allow_live:
            raise RuntimeError(
                "live viewpoint scoring requires config enabled=true and the explicit allow_live flag")
        provider = self._resolve_provider()
        if not bool(getattr(provider, "supports_vision", True)):
            raise RuntimeError(f"configured provider {self.provider_id!r} does not support vision")

        from services.agent.providers import token_usage

        token_usage.clear_last_usage()
        reply = provider.complete(_SYSTEM_PROMPT, self._messages(candidate_set),
                                  max_tokens=self.config.max_output_tokens)
        payload = self._parse_reply(reply)
        scoring = ScoringResponse.from_dict(payload["scoring"])
        validate_scoring(candidate_set, scoring)
        chosen = str(payload["chosen_candidate_id"])
        offered = {candidate.candidate_id for candidate in candidate_set.canonical_candidates}
        if chosen not in offered:
            raise ValueError(f"model chose unknown candidate {chosen!r}")

        reported = token_usage.get_last_usage()
        if reported and int(reported.get("total_tokens") or 0) > 0:
            prompt = int(reported.get("prompt_tokens") or 0)
            completion = int(reported.get("completion_tokens") or 0)
            total = int(reported.get("total_tokens") or (prompt + completion))
            cost = (prompt * self.config.input_usd_per_million
                    + completion * self.config.output_usd_per_million) / 1_000_000.0
            usage = UsageCost(1, prompt, completion, total, cost,
                              "provider_reported_tokens_configured_pricing", False,
                              self.config.input_usd_per_million,
                              self.config.output_usd_per_million)
        else:
            estimate = self.estimate(candidate_set)
            usage = UsageCost(1, estimate.prompt_tokens, estimate.completion_tokens,
                              estimate.total_tokens, estimate.cost_usd,
                              "provider_usage_unavailable_fallback_estimate", True,
                              estimate.input_usd_per_million, estimate.output_usd_per_million)
        return Judgement(scoring, chosen, str(payload["answer_text"]), usage)
