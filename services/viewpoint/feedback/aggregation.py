"""Turn raw Stage-2 records into de-biased, agreement-filtered preference examples."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
import json
from typing import Iterable

from .protocol import comparison_key, detect_position_bias
from .schema import PreferenceRecord, RenderCandidate


@dataclass(frozen=True)
class PreferenceExample:
    geometry_hash: str
    feature: str
    task_text: str
    winner: RenderCandidate
    loser: RenderCandidate
    rubric_margin: float
    mean_confidence: float
    pairwise_agreement: float
    source_record_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"geometry_hash": self.geometry_hash, "feature": self.feature,
                "task_text": self.task_text, "winner": self.winner.to_dict(),
                "loser": self.loser.to_dict(), "rubric_margin": self.rubric_margin,
                "mean_confidence": self.mean_confidence,
                "pairwise_agreement": self.pairwise_agreement,
                "source_record_ids": list(self.source_record_ids)}


@dataclass(frozen=True)
class AggregationResult:
    examples: tuple[PreferenceExample, ...]
    input_records: int
    unique_records: int
    duplicate_records: int
    abstained_records: int
    incorrect_records: int
    position_biased_records: int
    insufficient_rater_comparisons: int
    low_agreement_comparisons: int
    publication_ineligible_records: int

    def to_json(self) -> str:
        return json.dumps({"examples": [example.to_dict() for example in self.examples],
                           "counts": {name: getattr(self, name) for name in (
                               "input_records", "unique_records", "duplicate_records",
                               "abstained_records", "incorrect_records", "position_biased_records",
                               "insufficient_rater_comparisons", "low_agreement_comparisons",
                               "publication_ineligible_records")}},
                          sort_keys=True, separators=(",", ":"))


def publication_eligible(record: PreferenceRecord, *, approved_license_refs: Iterable[str],
                         approved_consent_refs: Iterable[str]) -> bool:
    """Machine-checkable release predicate; non-empty references are never self-approval."""
    consent = record.publication_consent
    if consent is None:
        return False
    if consent.export_restricted or not consent.redaction_applied:
        return False
    if consent.license_kind not in {"owned", "licensed", "synthetic"}:
        return False
    if not consent.model_training or not consent.public_redistribution:
        return False
    return (consent.license_ref in set(approved_license_refs)
            and consent.consent_ref in set(approved_consent_refs))


def _deduplicate(records: list[PreferenceRecord]) -> tuple[list[PreferenceRecord], int]:
    by_record_id: dict[str, str] = {}
    semantic_seen: set[tuple[str, str, str]] = set()
    unique: list[PreferenceRecord] = []
    duplicates = 0
    for record in records:
        payload = record.to_json()
        prior = by_record_id.get(record.record_id)
        if prior is not None:
            if prior != payload:
                raise ValueError(f"record_id {record.record_id!r} has conflicting payloads")
            duplicates += 1
            continue
        by_record_id[record.record_id] = payload
        semantic_key = (record.candidate_set.request_id, record.model_id,
                        json.dumps(record.scoring.to_dict(), sort_keys=True, separators=(",", ":")))
        if semantic_key in semantic_seen:
            duplicates += 1
            continue
        semantic_seen.add(semantic_key)
        unique.append(record)
    return unique, duplicates


def _agreement(observations: list[tuple[PreferenceRecord, object]]) -> float | None:
    labels = [score.preferred_id for _, score in observations]  # type: ignore[attr-defined]
    compared = 0
    agreed = 0
    for first, second in combinations(labels, 2):
        compared += 1
        agreed += first == second
    return None if compared == 0 else agreed / compared


def aggregate_preferences(records: Iterable[PreferenceRecord], *, min_agreement: float = 0.67,
                          min_raters: int = 2, position_bias_min_decisive: int = 8,
                          position_bias_alpha: float = 0.05,
                          require_correct_when_checkable: bool = True,
                          for_publication: bool = False,
                          approved_license_refs: Iterable[str] = (),
                          approved_consent_refs: Iterable[str] = ()) -> AggregationResult:
    """Aggregate pairwise votes; rubric values remain diagnostics/weights, never labels.

    The decisive pairwise majority supplies winner/loser. Observations from a scorer with a
    statistically detected first-slot bias, explicit abstentions, known-wrong downstream
    answers, low agreement, and (for release exports) ineligible provenance are excluded.
    """
    if not 0.0 <= min_agreement <= 1.0:
        raise ValueError("min_agreement must be in [0, 1]")
    if min_raters < 2:
        raise ValueError("min_raters must be at least two")

    raw = list(records)
    unique, duplicate_count = _deduplicate(raw)

    by_model: dict[str, list[PreferenceRecord]] = defaultdict(list)
    for record in unique:
        by_model[record.model_id].append(record)
    biased_models = {
        model_id for model_id, model_records in by_model.items()
        if detect_position_bias(model_records, min_decisive=position_bias_min_decisive,
                                alpha=position_bias_alpha).detected
    }

    approved_licenses = frozenset(approved_license_refs)
    approved_consents = frozenset(approved_consent_refs)
    abstained_records = 0
    incorrect_records = 0
    position_biased_records = 0
    publication_ineligible_records = 0
    observations: dict[tuple[str, ...], list[tuple[PreferenceRecord, object]]] = defaultdict(list)
    for record in unique:
        if record.model_id in biased_models:
            position_biased_records += 1
            continue
        if record.scoring.abstained or all(score.preferred_id is None
                                           for score in record.scoring.pairwise_scores):
            abstained_records += 1
            continue
        if require_correct_when_checkable and record.answer_correct is False:
            incorrect_records += 1
            continue
        if for_publication and not publication_eligible(
                record, approved_license_refs=approved_licenses,
                approved_consent_refs=approved_consents):
            publication_ineligible_records += 1
            continue
        for score in record.scoring.pairwise_scores:
            if score.preferred_id is not None:
                observations[comparison_key(record, score)].append((record, score))

    examples: list[PreferenceExample] = []
    insufficient = 0
    low_agreement = 0
    for key in sorted(observations):
        group = observations[key]
        if len(group) < min_raters:
            insufficient += 1
            continue
        agreement = _agreement(group)
        if agreement is None or agreement < min_agreement:
            low_agreement += 1
            continue
        winner_counts = Counter(score.preferred_id for _, score in group)  # type: ignore[attr-defined]
        ranked = winner_counts.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            low_agreement += 1
            continue
        winner_id = ranked[0][0]
        candidate_ids = {key[-2], key[-1]}
        loser_id = next(candidate_id for candidate_id in candidate_ids if candidate_id != winner_id)
        exemplar = group[0][0]
        winner = exemplar.candidate_set.candidate(winner_id)
        loser = exemplar.candidate_set.candidate(loser_id)
        margins: list[int] = []
        for record, _ in group:
            rubric = {score.candidate_id: score for score in record.scoring.rubric_scores}
            if winner_id in rubric and loser_id in rubric:
                margins.append(rubric[winner_id].task_usefulness - rubric[loser_id].task_usefulness)
        examples.append(PreferenceExample(
            geometry_hash=exemplar.geometry_hash,
            feature=exemplar.feature,
            task_text=exemplar.task_text,
            winner=winner,
            loser=loser,
            rubric_margin=0.0 if not margins else sum(margins) / len(margins),
            mean_confidence=sum(score.confidence for _, score in group) / len(group),  # type: ignore[attr-defined]
            pairwise_agreement=agreement,
            source_record_ids=tuple(sorted(record.record_id for record, _ in group)),
        ))

    return AggregationResult(
        examples=tuple(examples), input_records=len(raw), unique_records=len(unique),
        duplicate_records=duplicate_count, abstained_records=abstained_records,
        incorrect_records=incorrect_records, position_biased_records=position_biased_records,
        insufficient_rater_comparisons=insufficient, low_agreement_comparisons=low_agreement,
        publication_ineligible_records=publication_ineligible_records,
    )
