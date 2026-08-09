"""Candidate presentation, scorer validation, bias controls, and durable JSONL storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
import math
import os
from pathlib import Path
import random
from typing import Iterable, Protocol
from uuid import uuid4

from .schema import (
    CandidateSetRequest,
    CandidateSetResponse,
    PairwiseScore,
    PreferenceRecord,
    PublicationConsent,
    RenderCandidate,
    ScoringResponse,
)


class Scorer(Protocol):
    """An adapter for the consuming model; production and scripted scorers share this seam."""

    def score(self, candidate_set: CandidateSetResponse) -> ScoringResponse: ...


def build_candidate_set(request: CandidateSetRequest,
                        renders: Iterable[RenderCandidate]) -> CandidateSetResponse:
    """Validate rendered outputs and apply a local, reproducible seeded permutation."""
    by_id: dict[str, RenderCandidate] = {}
    for render in renders:
        if render.candidate_id in by_id:
            raise ValueError(f"duplicate rendered candidate {render.candidate_id!r}")
        by_id[render.candidate_id] = render

    expected_ids = [candidate.candidate_id for candidate in request.candidate_poses]
    if set(by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(by_id))
        unknown = sorted(set(by_id) - set(expected_ids))
        raise ValueError(f"render ids do not match request (missing={missing}, unknown={unknown})")

    canonical: list[RenderCandidate] = []
    for requested in request.candidate_poses:
        rendered = by_id[requested.candidate_id]
        if rendered.pose != requested.pose:
            raise ValueError(f"render pose does not match request for {requested.candidate_id!r}")
        canonical.append(rendered)

    permutation = list(range(len(canonical)))
    random.Random(request.randomization_seed).shuffle(permutation)
    return CandidateSetResponse(
        request_id=request.request_id,
        geometry_hash=request.geometry_hash,
        feature=request.feature,
        task_text=request.task_text,
        canonical_candidates=tuple(canonical),
        permutation=tuple(permutation),
        randomization_seed=request.randomization_seed,
    )


def expected_pair_order(candidate_set: CandidateSetResponse) -> tuple[tuple[str, str], ...]:
    """Every unordered pair, oriented in the exact order shown to the scorer."""
    ids = tuple(candidate.candidate_id for candidate in candidate_set.ordered_candidates)
    return tuple(combinations(ids, 2))


def validate_scoring(candidate_set: CandidateSetResponse, scoring: ScoringResponse) -> None:
    """Reject incomplete, reordered, or foreign scores before they become provenance."""
    if scoring.request_id != candidate_set.request_id:
        raise ValueError("scoring request_id does not match candidate set")
    offered = {candidate.candidate_id for candidate in candidate_set.canonical_candidates}
    rubric_ids = {score.candidate_id for score in scoring.rubric_scores}
    if rubric_ids not in (set(), offered):
        raise ValueError("rubric_scores must cover either none or every offered candidate")
    if not scoring.abstained and rubric_ids != offered:
        raise ValueError("a decisive response requires one rubric score per candidate")
    if scoring.abstained:
        return

    actual_pairs = tuple((score.candidate_a_id, score.candidate_b_id)
                         for score in scoring.pairwise_scores)
    expected_pairs = expected_pair_order(candidate_set)
    if actual_pairs != expected_pairs:
        raise ValueError("pairwise_scores must cover every pair once in presentation order")


def score_candidate_set(candidate_set: CandidateSetResponse, scorer: Scorer, *, model_id: str,
                        harness_version: str, answer_correct: bool | None = None,
                        publication_consent: PublicationConsent | None = None,
                        timestamp: str | None = None, record_id: str | None = None) -> PreferenceRecord:
    """Run one scorer adapter and seal its response together with complete provenance."""
    scoring = scorer.score(candidate_set)
    validate_scoring(candidate_set, scoring)
    return PreferenceRecord(
        record_id=record_id or str(uuid4()),
        candidate_set=candidate_set,
        scoring=scoring,
        model_id=model_id,
        harness_version=harness_version,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        answer_correct=answer_correct,
        publication_consent=publication_consent,
    )


@dataclass(frozen=True)
class PositionBiasReport:
    decisive_comparisons: int
    first_wins: int
    first_win_rate: float | None
    two_sided_p_value: float | None
    detected: bool


def _two_sided_binomial_pvalue(successes: int, trials: int) -> float:
    """Exact two-sided binomial p-value under an unbiased p=0.5 null."""
    observed_probability = math.comb(trials, successes) / (2**trials)
    probability = sum(math.comb(trials, k) / (2**trials) for k in range(trials + 1)
                      if math.comb(trials, k) / (2**trials) <= observed_probability + 1e-15)
    return min(1.0, probability)


def detect_position_bias(records: Iterable[PreferenceRecord], *, min_decisive: int = 8,
                         alpha: float = 0.05) -> PositionBiasReport:
    """Detect preference for pairwise slot A, independent of randomized candidate identity."""
    if min_decisive < 1:
        raise ValueError("min_decisive must be positive")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    first_wins = 0
    decisive = 0
    for record in records:
        for comparison in record.scoring.pairwise_scores:
            if comparison.preferred_id is None:
                continue
            decisive += 1
            first_wins += comparison.preferred_id == comparison.candidate_a_id
    if decisive == 0:
        return PositionBiasReport(0, 0, None, None, False)
    p_value = _two_sided_binomial_pvalue(first_wins, decisive)
    return PositionBiasReport(decisive, first_wins, first_wins / decisive, p_value,
                              decisive >= min_decisive and p_value < alpha)


@dataclass(frozen=True)
class AgreementReport:
    agreeing_rater_pairs: int
    compared_rater_pairs: int
    agreement: float | None


def comparison_key(record: PreferenceRecord, score: PairwiseScore) -> tuple[str, ...]:
    """Identity of the same usefulness question across raters and randomized presentations."""
    a, b = sorted((score.candidate_a_id, score.candidate_b_id))
    return (record.geometry_hash, record.feature, record.task_text, a, b)


def inter_rater_agreement(records: Iterable[PreferenceRecord]) -> AgreementReport:
    """Observed pairwise agreement over decisive ratings of the same candidate pair.

    Abstentions are missing judgments, not disagreements. Each unordered rater pair is one
    trial, making 1.0 mean unanimous and 0.0 mean every comparable two-rater case disagrees.
    """
    labels: dict[tuple[str, ...], list[str]] = {}
    for record in records:
        for score in record.scoring.pairwise_scores:
            if score.preferred_id is not None:
                labels.setdefault(comparison_key(record, score), []).append(score.preferred_id)
    agreeing = 0
    compared = 0
    for decisions in labels.values():
        for first, second in combinations(decisions, 2):
            compared += 1
            agreeing += first == second
    return AgreementReport(agreeing, compared, None if compared == 0 else agreeing / compared)


class JsonlPreferenceStore:
    """Append-only JSONL store; each append is flushed and fsynced before returning."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, record: PreferenceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (record.to_json() + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("preference record append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def load(self) -> tuple[PreferenceRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[PreferenceRecord] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    records.append(PreferenceRecord.from_json(line))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid preference record at line {line_number}: {exc}") from exc
        return tuple(records)
