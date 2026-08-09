from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np

import pytest

from services.viewpoint.feedback import CameraPose, CandidatePose, CandidateSetRequest
from services.viewpoint.pixels.schema import COMPONENTS
from services.viewpoint.usefulness import (
    BenchmarkItem,
    CandidateObservation,
    CandidateView,
    PublicQuestion,
    QuestionSpec,
)

from .demo import DemoRenderBackend, demo_job
from .loop import FlywheelJob, JsonlFlywheelStore, run_flywheel
from .schema import AnswerKey
from .scorers import RealModelConfig, RealModelScorer, ScriptedScorer


def test_scripted_loop_renders_scores_persists_and_aggregates(tmp_path: Path) -> None:
    result = run_flywheel(
        (demo_job(),),
        scorer=(
            ScriptedScorer(ranking={"view-a": 4, "view-b": 2, "view-c": 1},
                           answer_text="yes", model_id="scripted-rater-a"),
            ScriptedScorer(ranking={"view-a": 4, "view-b": 2, "view-c": 1},
                           answer_text="yes", model_id="scripted-rater-b"),
        ),
        render_backend=DemoRenderBackend(),
        output_dir=tmp_path,
        raters_per_sheet=1,
    )
    assert len(result.records) == 2
    assert len(result.aggregation.examples) == 3
    assert {example.winner.candidate_id for example in result.aggregation.examples} == {
        "view-a", "view-b"}
    assert result.comparison.chosen_view_accuracy == 1.0
    assert result.comparison.preferred_and_correct_records == 2

    stored = JsonlFlywheelStore(tmp_path).load_records()
    assert stored == result.records
    assert stored[0].preference.candidate_set.permutation
    assert stored[0].render_provenance["renders"][0]["backend_name"] == "flywheel-demo-renderer"
    assert stored[0].question_provenance["answer_key"]["evaluator"] == "normalized_exact_v1"
    assert stored[0].usage.source == "scripted"
    aggregate_payload = json.loads((tmp_path / "aggregated.json").read_text(encoding="utf-8"))
    assert len(aggregate_payload["aggregation"]["examples"]) == 3


def test_first_position_control_is_caught_inside_full_loop(tmp_path: Path) -> None:
    jobs = tuple(demo_job(seed=index, request_id=f"bias-{index}") for index in range(8))
    result = run_flywheel(
        jobs,
        scorer=ScriptedScorer(first_position_biased=True,
                              model_id="scripted-first-position-control"),
        render_backend=DemoRenderBackend(),
        output_dir=tmp_path,
        raters_per_sheet=1,
    )
    assert len(result.records) == 8
    assert {record.preference.candidate_set.ordered_candidates[0].candidate_id
            for record in result.records} == {"view-a", "view-b", "view-c"}
    assert result.aggregation.examples == ()
    assert result.aggregation.position_biased_records == 8


def test_preference_and_correctness_remain_distinct_data(tmp_path: Path) -> None:
    correct = run_flywheel(
        (demo_job(request_id="correct"),),
        scorer=ScriptedScorer(ranking={"view-a": 4, "view-b": 2, "view-c": 1},
                              answer_text="yes", model_id="scripted-correct"),
        render_backend=DemoRenderBackend(), output_dir=tmp_path, raters_per_sheet=1,
    )
    assert correct.records[0].outcome.selection_matches_preference is True
    assert correct.records[0].outcome.answer_correct is True

    wrong_job = replace(demo_job(request_id="wrong"), answer_key=AnswerKey(("no",)))
    combined = run_flywheel(
        (wrong_job,),
        scorer=ScriptedScorer(ranking={"view-a": 4, "view-b": 2, "view-c": 1},
                              answer_text="yes", model_id="scripted-wrong"),
        render_backend=DemoRenderBackend(), output_dir=tmp_path, raters_per_sheet=1,
    )
    assert combined.records[0].outcome.selection_matches_preference is True
    assert combined.records[0].outcome.answer_correct is False
    assert combined.comparison.checkable_records == 2
    assert combined.comparison.preferred_and_correct_records == 1
    assert combined.comparison.preferred_but_incorrect_records == 1
    assert combined.comparison.preferred_view_accuracy == 0.5


def test_live_dry_run_estimates_requests_tokens_cost_without_provider(tmp_path: Path) -> None:
    config = RealModelConfig(
        enabled=True,
        provider="anthropic",
        model="claude-sonnet-4-5",
        max_output_tokens=1800,
        input_usd_per_million=3.0,
        output_usd_per_million=15.0,
        image_tokens_per_candidate=1200,
        chars_per_token=4.0,
    )
    scorer = RealModelScorer(config, allow_live=False)
    result = run_flywheel(
        (demo_job(),), scorer=scorer, render_backend=DemoRenderBackend(),
        output_dir=tmp_path, raters_per_sheet=3, dry_run=True,
    )
    assert result.records == ()
    manifest = result.dry_runs[0]
    estimate = manifest["would_spend"]
    assert estimate["requests"] == 3
    assert estimate["prompt_tokens"] > 3 * 3 * 1200
    assert estimate["completion_tokens"] == 5400
    assert estimate["total_tokens"] == estimate["prompt_tokens"] + 5400
    assert estimate["cost_usd"] > 0
    assert estimate["estimated"] is True
    assert manifest["question_provenance"]["answer_key"]["accepted_answers"] == ["yes"]
    assert manifest["publication_consent"] is None
    assert (tmp_path / "dry_runs.jsonl").exists()
    assert not (tmp_path / "records.jsonl").exists()


def test_live_backend_is_unreachable_under_pytest_even_when_fully_opted_in(tmp_path: Path) -> None:
    config = RealModelConfig(True, "anthropic", "claude-sonnet-4-5", 1000,
                             3.0, 15.0, 1200, 4.0)
    scorer = RealModelScorer(config, allow_live=True)
    dry = run_flywheel(
        (demo_job(),), scorer=scorer, render_backend=DemoRenderBackend(),
        output_dir=tmp_path, raters_per_sheet=1, dry_run=True,
    )
    candidate_set = dry.dry_runs[0]["candidate_set"]
    from services.viewpoint.feedback import CandidateSetResponse
    with pytest.raises(RuntimeError, match="disabled under pytest"):
        scorer.judge(CandidateSetResponse.from_dict(candidate_set))


def test_usefulness_item_adapter_keeps_answer_out_of_feedback_task() -> None:
    public = PublicQuestion(
        question_id="q-1",
        kind="visible_hole_count",
        prompt="How many holes are present?",
        choices=("2", "3", "4"),
        marker_labels=("A", "B", "C"),
    )
    question = QuestionSpec(
        public=public,
        part_id="part-1",
        part_index=2,
        example_index=3,
        feature_id="hole-7",
        difficulty="hard",
        correct_answer="3",
        derivation="marker count",
        measurements=(("count", 3.0),),
        marker_feature_ids=("hole-a", "hole-b", "hole-c"),
    )

    def candidate(candidate_id: str, aggregate: float) -> CandidateView:
        image = np.zeros((96, 96, 3), dtype=np.uint8)
        components = {name: aggregate for name in COMPONENTS}
        return CandidateView(CandidateObservation(candidate_id, image), components, aggregate)

    item = BenchmarkItem(question, (candidate("a", 0.8), candidate("b", 0.3)))
    poses = (
        CandidatePose("a", CameraPose((4, 0, 2), (0, 0, 0), (0, 0, 1), 45)),
        CandidatePose("b", CameraPose((-4, 0, 2), (0, 0, 0), (0, 0, 1), 45)),
    )
    job = FlywheelJob.from_usefulness(
        item, candidate_poses=poses, geometry_hash="b" * 64, doc_id="doc-1",
        randomization_seed=99,
    )
    assert job.answer_key.check("3")
    assert "marker count" not in job.request.task_text
    assert job.candidate_oracle["a"]["aggregate"] == 0.8
    assert job.question_provenance["source"] == "services.viewpoint.usefulness"
