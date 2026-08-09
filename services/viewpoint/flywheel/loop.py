"""End-to-end render -> judge -> outcome -> aggregate viewpoint flywheel."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.viewpoint.feedback import (
    AggregationResult,
    CandidatePose,
    CandidateSetRequest,
    PreferenceRecord,
    PublicationConsent,
    aggregate_preferences,
    score_candidate_set,
)
from services.viewpoint.render import RenderBackend, RenderSettings, render_candidate_sheet

from .schema import AnswerKey, AnswerOutcome, FlywheelRecord, UsageCost
from .scorers import Judgement, ScorerBackend


@dataclass(frozen=True, slots=True)
class FlywheelJob:
    request: CandidateSetRequest
    doc_id: str
    answer_key: AnswerKey | None = None
    question_provenance: Mapping[str, Any] | None = None
    candidate_oracle: Mapping[str, Any] | None = None
    publication_consent: PublicationConsent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, CandidateSetRequest):
            raise ValueError("request must be CandidateSetRequest")
        if not isinstance(self.doc_id, str) or not self.doc_id.strip():
            raise ValueError("doc_id must be non-empty")
        object.__setattr__(self, "question_provenance", dict(self.question_provenance or {}))
        object.__setattr__(self, "candidate_oracle", dict(self.candidate_oracle or {}))

    @classmethod
    def from_usefulness(cls, item: Any, *, candidate_poses: Sequence[CandidatePose],
                        geometry_hash: str, doc_id: str, randomization_seed: int,
                        request_id: str | None = None,
                        publication_consent: PublicationConsent | None = None) -> "FlywheelJob":
        """Bind a usefulness ``BenchmarkItem`` to render poses without exposing its answer.

        The adapter intentionally accepts the public sibling objects by attributes. This keeps the
        flywheel coupled to the stable usefulness API rather than its storage implementation.
        """
        question = item.question
        public = question.public
        candidates = tuple(item.candidates)
        poses = tuple(candidate_poses)
        candidate_ids = tuple(candidate.observation.candidate_id for candidate in candidates)
        pose_ids = tuple(pose.candidate_id for pose in poses)
        if set(candidate_ids) != set(pose_ids):
            raise ValueError(
                "usefulness candidates and render candidate poses must have identical ids")
        by_pose = {pose.candidate_id: pose for pose in poses}
        ordered_poses = tuple(by_pose[candidate_id] for candidate_id in candidate_ids)
        choices = tuple(str(choice) for choice in public.choices)
        task_text = str(public.prompt)
        if choices:
            task_text += "\nChoices: " + " | ".join(choices)
        request = CandidateSetRequest(
            request_id=request_id or f"usefulness-{public.question_id}-{randomization_seed}",
            geometry_hash=geometry_hash,
            feature=str(question.feature_id),
            task_text=task_text,
            candidate_poses=ordered_poses,
            randomization_seed=randomization_seed,
        )
        oracle = {
            candidate.observation.candidate_id: {
                "aggregate": float(candidate.aggregate),
                "components": _jsonable(candidate.oracle_components),
            }
            for candidate in candidates
        }
        provenance = {
            "source": "services.viewpoint.usefulness",
            "question_id": str(public.question_id),
            "part_id": str(question.part_id),
            "part_index": int(question.part_index),
            "example_index": int(question.example_index),
            "feature_id": str(question.feature_id),
            "kind": str(public.kind),
            "difficulty": str(question.difficulty),
            "prompt": str(public.prompt),
            "choices": list(choices),
            "marker_labels": list(public.marker_labels),
            "derivation": str(question.derivation),
            "measurements": _jsonable(question.measurements),
            "marker_feature_ids": list(question.marker_feature_ids),
        }
        return cls(
            request=request,
            doc_id=doc_id,
            answer_key=AnswerKey(
                (str(question.correct_answer),), "usefulness_normalized_exact_v1"),
            question_provenance=provenance,
            candidate_oracle=oracle,
            publication_consent=publication_consent,
        )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class PreferenceCorrectnessComparison:
    checkable_records: int
    correct_records: int
    incorrect_records: int
    preferred_selection_records: int
    preferred_and_correct_records: int
    preferred_but_incorrect_records: int
    chosen_view_accuracy: float | None
    preferred_view_accuracy: float | None
    inspectability_correctness_pearson: float | None

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in (
            "checkable_records", "correct_records", "incorrect_records",
            "preferred_selection_records", "preferred_and_correct_records",
            "preferred_but_incorrect_records", "chosen_view_accuracy", "preferred_view_accuracy",
            "inspectability_correctness_pearson")}


@dataclass(frozen=True, slots=True)
class FlywheelRunResult:
    records: tuple[FlywheelRecord, ...]
    dry_runs: tuple[Mapping[str, Any], ...]
    aggregation: AggregationResult
    comparison: PreferenceCorrectnessComparison

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_written": len(self.records),
            "dry_runs_written": len(self.dry_runs),
            "dry_run_would_spend": (
                None if not self.dry_runs else _sum_estimates(tuple(
                    UsageCost.from_dict(manifest["would_spend"])
                    for manifest in self.dry_runs)).to_dict()
            ),
            "aggregation": json.loads(self.aggregation.to_json()),
            "preference_vs_correctness": self.comparison.to_dict(),
        }


class JsonlFlywheelStore:
    """Append-only, fsynced full-provenance records and dry-run manifests."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.records_path = self.root / "records.jsonl"
        self.dry_runs_path = self.root / "dry_runs.jsonl"
        self.aggregation_path = self.root / "aggregated.json"

    @staticmethod
    def _append(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("flywheel append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def append_record(self, record: FlywheelRecord) -> None:
        self._append(self.records_path, record.to_dict())

    def append_dry_run(self, manifest: Mapping[str, Any]) -> None:
        self._append(self.dry_runs_path, manifest)

    def load_records(self) -> tuple[FlywheelRecord, ...]:
        if not self.records_path.exists():
            return ()
        result: list[FlywheelRecord] = []
        with self.records_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    result.append(FlywheelRecord.from_json(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid flywheel record at line {line_number}: {exc}") from exc
        return tuple(result)

    def write_aggregation(self, aggregation: AggregationResult,
                          comparison: PreferenceCorrectnessComparison) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "aggregation": json.loads(aggregation.to_json()),
            "preference_vs_correctness": comparison.to_dict(),
        }
        staging = self.root / ".aggregated.json.new"
        with staging.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, self.aggregation_path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _preference_winner(judgement: Judgement) -> str | None:
    wins = Counter(score.preferred_id for score in judgement.scoring.pairwise_scores
                   if score.preferred_id is not None)
    if not wins:
        return None
    ranked = wins.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return str(ranked[0][0])


def _oracle_aggregate(job: FlywheelJob, candidate_id: str) -> float | None:
    value = (job.candidate_oracle or {}).get(candidate_id)
    if isinstance(value, Mapping):
        value = value.get("aggregate")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compare_preference_correctness(
        records: Iterable[FlywheelRecord]) -> PreferenceCorrectnessComparison:
    rows = tuple(records)
    checkable = tuple(record for record in rows if record.outcome.answer_correct is not None)
    correct = sum(record.outcome.answer_correct is True for record in checkable)
    preferred = tuple(record for record in checkable
                      if record.outcome.selection_matches_preference is True)
    preferred_correct = sum(record.outcome.answer_correct is True for record in preferred)
    pairs = [(float(record.outcome.chosen_oracle_inspectability),
              1.0 if record.outcome.answer_correct else 0.0)
             for record in checkable
             if record.outcome.chosen_oracle_inspectability is not None]
    pearson: float | None = None
    if len(pairs) >= 2:
        xs, ys = zip(*pairs)
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
        x_ss = sum((x - x_mean) ** 2 for x in xs)
        y_ss = sum((y - y_mean) ** 2 for y in ys)
        if x_ss > 0 and y_ss > 0:
            pearson = numerator / math.sqrt(x_ss * y_ss)
    return PreferenceCorrectnessComparison(
        checkable_records=len(checkable),
        correct_records=correct,
        incorrect_records=len(checkable) - correct,
        preferred_selection_records=len(preferred),
        preferred_and_correct_records=preferred_correct,
        preferred_but_incorrect_records=len(preferred) - preferred_correct,
        chosen_view_accuracy=None if not checkable else correct / len(checkable),
        preferred_view_accuracy=None if not preferred else preferred_correct / len(preferred),
        inspectability_correctness_pearson=pearson,
    )


def _scaled_estimate(usage: UsageCost, factor: int) -> UsageCost:
    def scale(value: int | None) -> int | None:
        return None if value is None else value * factor
    return UsageCost(
        usage.requests * factor,
        scale(usage.prompt_tokens),
        scale(usage.completion_tokens),
        scale(usage.total_tokens),
        None if usage.cost_usd is None else usage.cost_usd * factor,
        usage.source,
        usage.estimated,
        usage.input_usd_per_million,
        usage.output_usd_per_million,
    )

def _sum_estimates(estimates: Sequence[UsageCost]) -> UsageCost:
    def total(name: str) -> int | None:
        values = [getattr(item, name) for item in estimates]
        return None if any(value is None for value in values) else sum(values)
    costs = [item.cost_usd for item in estimates]
    return UsageCost(
        requests=sum(item.requests for item in estimates),
        prompt_tokens=total("prompt_tokens"),
        completion_tokens=total("completion_tokens"),
        total_tokens=total("total_tokens"),
        cost_usd=None if any(cost is None for cost in costs) else sum(costs),
        source="summed_per_scorer_estimates",
        estimated=any(item.estimated for item in estimates),
        input_usd_per_million=0.0,
        output_usd_per_million=0.0,
    )



class _OneShotScorer:
    def __init__(self, judgement: Judgement) -> None:
        self.judgement = judgement

    def score(self, _candidate_set):
        return self.judgement.scoring


def run_flywheel(jobs: Iterable[FlywheelJob], *,
                 scorer: ScorerBackend | Sequence[ScorerBackend],
                 render_backend: RenderBackend, output_dir: str | os.PathLike[str],
                 raters_per_sheet: int = 1, dry_run: bool = False,
                 render_settings: RenderSettings = RenderSettings(),
                 min_agreement: float = 0.67, min_raters: int = 2,
                 position_bias_min_decisive: int = 8) -> FlywheelRunResult:
    """Run the loop. Nothing invokes it at import time; live spending stays scorer-gated.

    A sequence represents independent raters/models. Feedback deliberately de-duplicates repeated
    identical responses from one model, so callers need at least ``min_raters`` distinct model ids
    before an agreement-filtered training example can exist.
    """
    if type(raters_per_sheet) is not int or raters_per_sheet < 1:
        raise ValueError("raters_per_sheet must be a positive integer")
    scorers = ((scorer,) if hasattr(scorer, "judge") else tuple(scorer))
    if not scorers or any(not hasattr(item, "judge") for item in scorers):
        raise ValueError("scorer must be a backend or a non-empty sequence of backends")
    root = Path(output_dir)
    store = JsonlFlywheelStore(root)
    written: list[FlywheelRecord] = []
    dry_manifests: list[Mapping[str, Any]] = []

    for job in jobs:
        sheet = render_candidate_sheet(
            job.request,
            doc_id=job.doc_id,
            output_dir=root / "renders" / job.request.request_id,
            backend=render_backend,
            settings=render_settings,
        )
        if dry_run:
            per_scorer = [
                _scaled_estimate(backend.estimate(sheet.candidate_set), raters_per_sheet)
                for backend in scorers
            ]
            estimate = _sum_estimates(per_scorer)
            manifest = {
                "schema": "chudware.viewpoint.flywheel.dry_run/1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "candidate_set": sheet.candidate_set.to_dict(),
                "render_provenance": sheet.provenance_dict(),
                "render_requests": [request.to_dict() for request in sheet.render_requests],
                "question_provenance": {
                    **dict(job.question_provenance or {}),
                    "answer_key": (
                        None if job.answer_key is None else job.answer_key.to_dict()),
                },
                "candidate_oracle": dict(job.candidate_oracle or {}),
                "publication_consent": (
                    None if job.publication_consent is None
                    else job.publication_consent.to_dict()),
                "scorers": [
                    {"provider_id": backend.provider_id, "model_id": backend.model_id,
                     "harness_version": backend.harness_version,
                     "would_spend": usage.to_dict()}
                    for backend, usage in zip(scorers, per_scorer)
                ],
                "raters_per_scorer": raters_per_sheet,
                "would_spend": estimate.to_dict(),
            }
            store.append_dry_run(manifest)
            dry_manifests.append(manifest)
            continue

        for backend in scorers:
            for _ in range(raters_per_sheet):
                judgement = backend.judge(sheet.candidate_set)
                offered = {candidate.candidate_id
                           for candidate in sheet.candidate_set.canonical_candidates}
                if judgement.chosen_candidate_id not in offered:
                    raise ValueError(
                        f"scorer chose unknown candidate {judgement.chosen_candidate_id!r}")
                preference_winner = _preference_winner(judgement)
                correctness = (None if job.answer_key is None
                               else job.answer_key.check(judgement.answer_text))
                outcome = AnswerOutcome(
                    chosen_candidate_id=judgement.chosen_candidate_id,
                    preference_winner_id=preference_winner,
                    answer_text=judgement.answer_text,
                    answer_correct=correctness,
                    selection_matches_preference=(
                        None if preference_winner is None
                        else preference_winner == judgement.chosen_candidate_id),
                    chosen_oracle_inspectability=_oracle_aggregate(
                        job, judgement.chosen_candidate_id),
                )
                preference = score_candidate_set(
                    sheet.candidate_set,
                    _OneShotScorer(judgement),
                    model_id=backend.model_id,
                    harness_version=backend.harness_version,
                    answer_correct=correctness,
                    publication_consent=job.publication_consent,
                )
                record = FlywheelRecord(
                    preference=preference,
                    render_provenance={
                        **sheet.provenance_dict(),
                        "render_requests": [
                            request.to_dict() for request in sheet.render_requests],
                    },
                    question_provenance={
                        **dict(job.question_provenance or {}),
                        "answer_key": (
                            None if job.answer_key is None else job.answer_key.to_dict()),
                    },
                    candidate_oracle=dict(job.candidate_oracle or {}),
                    outcome=outcome,
                    usage=judgement.usage,
                    provider_id=backend.provider_id,
                    model_id=backend.model_id,
                    harness_version=backend.harness_version,
                )
                store.append_record(record)
                written.append(record)

    all_records = store.load_records()
    aggregation = aggregate_preferences(
        (record.preference for record in all_records),
        min_agreement=min_agreement,
        min_raters=min_raters,
        position_bias_min_decisive=position_bias_min_decisive,
    )
    comparison = compare_preference_correctness(all_records)
    store.write_aggregation(aggregation, comparison)
    return FlywheelRunResult(tuple(written), tuple(dry_manifests), aggregation, comparison)
