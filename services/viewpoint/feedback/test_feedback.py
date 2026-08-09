from __future__ import annotations

from dataclasses import replace

import pytest

from services.viewpoint.feedback import (
    CameraPose,
    CandidatePose,
    CandidateSetRequest,
    JsonlPreferenceStore,
    PairwiseScore,
    PreferenceRecord,
    PublicationConsent,
    RenderCandidate,
    RubricScore,
    ScoringResponse,
    aggregate_preferences,
    build_candidate_set,
    detect_position_bias,
    expected_pair_order,
    inter_rater_agreement,
    publication_eligible,
    score_candidate_set,
)

GEOMETRY_HASH = "a" * 64
POSE_A = CameraPose((4, 2, 3), (0, 0, 0), (0, 0, 1), 45)
POSE_B = CameraPose((-3, 4, 2), (0, 0, 0), (0, 0, 1), 50)
CANDIDATE_POSES = (CandidatePose("view-a", POSE_A), CandidatePose("view-b", POSE_B))
RENDERS = (
    RenderCandidate("view-a", POSE_A, "artifact://render-a", "1" * 64),
    RenderCandidate("view-b", POSE_B, "artifact://render-b", "2" * 64),
)


def make_offer(seed: int, request_id: str):
    request = CandidateSetRequest(
        request_id=request_id,
        geometry_hash=GEOMETRY_HASH,
        feature="counterbore-7",
        task_text="Determine whether the counterbore shoulder is complete.",
        candidate_poses=CANDIDATE_POSES,
        randomization_seed=seed,
    )
    return build_candidate_set(request, RENDERS)


def rubrics():
    return (
        RubricScore("view-a", 4, 4, 4, 4, 4),
        RubricScore("view-b", 2, 3, 2, 3, 2),
    )


class ScriptedScorer:
    def __init__(self, preferred_id: str):
        self.preferred_id = preferred_id

    def score(self, candidate_set):
        comparisons = []
        for first, second in expected_pair_order(candidate_set):
            assert self.preferred_id in {first, second}
            comparisons.append(PairwiseScore(first, second, self.preferred_id, 4, None))
        return ScoringResponse(candidate_set.request_id, rubrics(), tuple(comparisons))


class FirstPositionScorer:
    def score(self, candidate_set):
        comparisons = tuple(PairwiseScore(first, second, first, 4, None)
                            for first, second in expected_pair_order(candidate_set))
        return ScoringResponse(candidate_set.request_id, rubrics(), comparisons)


class AbstainingScorer:
    def score(self, candidate_set):
        return ScoringResponse(candidate_set.request_id, (), (), "both views hide the shoulder")


def make_record(index: int, preferred_id: str = "view-a", *, model_id: str | None = None,
                answer_correct: bool | None = True, consent: PublicationConsent | None = None):
    return score_candidate_set(
        make_offer(index, f"request-{index}"),
        ScriptedScorer(preferred_id),
        model_id=model_id or f"rater-{index}",
        harness_version="harness-3.2.1",
        answer_correct=answer_correct,
        publication_consent=consent,
        timestamp="2026-08-08T12:00:00+00:00",
        record_id=f"record-{index}-{preferred_id}",
    )


def test_schema_round_trip_and_missing_provenance_rejected():
    request = CandidateSetRequest(
        "schema-request", GEOMETRY_HASH, "hole-2", "Is the hole through?", CANDIDATE_POSES, 91)
    assert CandidateSetRequest.from_json(request.to_json()) == request

    record = make_record(1)
    assert PreferenceRecord.from_json(record.to_json()) == record
    payload = record.to_dict()
    payload.pop("model_id")
    with pytest.raises(ValueError, match="missing model_id"):
        PreferenceRecord.from_dict(payload)


def test_order_randomization_is_recorded_and_reproducible_from_seed():
    first = make_offer(1729, "request-one")
    second = make_offer(1729, "request-two")
    assert first.randomization_seed == 1729
    assert first.permutation == second.permutation
    assert tuple(candidate.candidate_id for candidate in first.ordered_candidates) == tuple(
        first.canonical_candidates[index].candidate_id for index in first.permutation)
    assert sorted(first.permutation) == [0, 1]


def test_first_position_biased_scorer_is_detected_despite_randomized_identity():
    records = []
    first_ids = set()
    for index in range(8):
        offer = make_offer(index, f"bias-request-{index}")
        first_ids.add(offer.ordered_candidates[0].candidate_id)
        records.append(score_candidate_set(
            offer, FirstPositionScorer(), model_id="first-slot-bot",
            harness_version="fake-1", timestamp="2026-08-08T12:00:00+00:00",
            record_id=f"bias-record-{index}"))
    assert first_ids == {"view-a", "view-b"}
    report = detect_position_bias(records)
    assert report.detected
    assert report.first_wins == report.decisive_comparisons == 8
    assert report.two_sided_p_value == pytest.approx(0.0078125)
    aggregated = aggregate_preferences(records)
    assert aggregated.examples == ()
    assert aggregated.position_biased_records == 8


def test_abstain_does_not_produce_a_preference_label():
    records = tuple(score_candidate_set(
        make_offer(index, f"abstain-request-{index}"), AbstainingScorer(),
        model_id=f"abstainer-{index}", harness_version="fake-1",
        timestamp="2026-08-08T12:00:00+00:00", record_id=f"abstain-{index}")
                    for index in range(2))
    result = aggregate_preferences(records)
    assert result.examples == ()
    assert result.abstained_records == 2


def test_inter_rater_agreement_on_agreeing_and_disagreeing_cases():
    first = make_record(11, "view-a")
    agrees = make_record(12, "view-a")
    disagrees = make_record(13, "view-b")
    agreeing_report = inter_rater_agreement((first, agrees))
    disagreeing_report = inter_rater_agreement((first, disagrees))
    assert agreeing_report.compared_rater_pairs == 1
    assert agreeing_report.agreement == 1.0
    assert disagreeing_report.compared_rater_pairs == 1
    assert disagreeing_report.agreement == 0.0


def test_low_agreement_is_excluded_and_unanimous_pair_is_training_ready():
    low = aggregate_preferences((make_record(21, "view-a"), make_record(22, "view-b")))
    assert low.examples == ()
    assert low.low_agreement_comparisons == 1

    high = aggregate_preferences((make_record(23, "view-a"), make_record(24, "view-a")))
    assert len(high.examples) == 1
    example = high.examples[0]
    assert example.winner.candidate_id == "view-a"
    assert example.loser.candidate_id == "view-b"
    assert example.pairwise_agreement == 1.0
    assert example.rubric_margin == 2.0


def test_publication_predicate_requires_consent_and_release_approval():
    no_consent = make_record(31)
    assert not publication_eligible(no_consent, approved_license_refs={"license-1"},
                                    approved_consent_refs={"consent-1"})

    consent = PublicationConsent("owned", "license-1", "consent-1", True, True, True, False)
    eligible = replace(no_consent, publication_consent=consent)
    assert publication_eligible(eligible, approved_license_refs={"license-1"},
                                approved_consent_refs={"consent-1"})
    assert not publication_eligible(eligible, approved_license_refs=set(),
                                    approved_consent_refs={"consent-1"})


def test_durable_jsonl_store_round_trip_and_semantic_dedup(tmp_path):
    record = make_record(41)
    store = JsonlPreferenceStore(tmp_path / "preferences.jsonl")
    store.append(record)
    assert store.load() == (record,)
    result = aggregate_preferences((record, record))
    assert result.unique_records == 1
    assert result.duplicate_records == 1
