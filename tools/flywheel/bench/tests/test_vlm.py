"""vlm render-review bench (IP-21): claims-vs-known-geometry scoring (fixture mode)."""
import os

import pytest

from tools.flywheel.bench.benches import vlm as V
from tools.flywheel.bench.dataset import DatasetSpec


def test_fixtures_load_and_hash_is_stable():
    fixtures = V.load_fixtures(V.SCENES_DIR)
    assert len(fixtures) >= 8
    families = {fx["structure"] for fx in fixtures}
    assert len(families) >= 4
    ids = {fx["example_id"] for fx in fixtures}
    assert len(ids) == len(fixtures)  # unique example ids

    spec1 = DatasetSpec.freeze("vlm_scenes", V.SCENES_DIR, n_examples=len(fixtures))
    spec2 = DatasetSpec.freeze("vlm_scenes", V.SCENES_DIR, n_examples=len(fixtures))
    assert spec1.hash == spec2.hash
    assert spec1.verify()


def test_spec_json_matches_committed_scenes():
    spec = DatasetSpec.load(os.path.join(V.FIXTURES_DIR, "spec.json"), V.SCENES_DIR)
    assert spec.verify(), "committed spec.json hash is stale vs scenes/ contents"
    assert spec.n_examples == len(V.load_fixtures(V.SCENES_DIR))


def test_hallucinated_claim_drops_precision_and_fails():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "bracket_hallucinated_thread")
    score = V.score_example(fx)
    assert score.detail["hallucinations"] >= 1
    assert score.detail["precision"] < 1.0
    assert score.passed is False


def test_perfect_claims_fixture_passes():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "bracket_perfect")
    score = V.score_example(fx)
    assert score.detail["precision"] == 1.0
    assert score.detail["recall"] == 1.0
    assert score.detail["hallucinations"] == 0
    assert score.passed is True
    assert score.score == pytest.approx(1.0)


def test_missed_fact_fixture_has_recall_below_one_but_no_hallucination():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "flange_missed_boss")
    score = V.score_example(fx)
    assert score.detail["recall"] < 1.0
    assert score.detail["precision"] == 1.0  # nothing claimed was wrong, just incomplete
    assert score.detail["hallucinations"] == 0


def test_wrong_attribute_claim_counts_as_hallucination_not_a_match():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "plate_miscounted_holes")
    score = V.score_example(fx)
    # claimed hole count (6) does not match the fact's count (8) -> ungrounded claim,
    # and the real hole fact goes unmatched -> both precision and recall suffer.
    assert score.detail["hallucinations"] == 1
    assert score.detail["precision"] < 1.0
    assert score.detail["recall"] < 1.0
    assert score.passed is False


def test_deterministic_baseline_scores_near_perfect_precision():
    for fx in V.load_fixtures(V.SCENES_DIR):
        det_claims = V.deterministic_claims(fx["known_geometry"])
        result = V._score_claims(det_claims, fx["known_geometry"])
        assert result["precision"] == 1.0, fx["example_id"]  # it only ever echoes a fact
        assert result["recall"] <= 1.0


def test_deterministic_baseline_has_modest_recall_when_facts_are_hidden():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "flange_missed_boss")
    det_claims = V.deterministic_claims(fx["known_geometry"])
    # the boss fact is visible: false -> the non-vision fallback never proposes it
    assert not any(c["kind"] == "boss" for c in det_claims)
    result = V._score_claims(det_claims, fx["known_geometry"])
    assert result["recall"] < 1.0


def test_run_returns_two_baselines_and_pending_residual():
    spec = DatasetSpec.load(os.path.join(V.FIXTURES_DIR, "spec.json"), V.SCENES_DIR)
    res = V.run(V.SCENES_DIR, spec.hash)
    assert res.n_examples == len(V.load_fixtures(V.SCENES_DIR))

    kinds = {b.name: b for b in res.baselines}
    assert kinds["deterministic"].status == "measured"
    assert kinds["deterministic"].mean_score is not None
    assert kinds["frontier"].status == "pending"
    assert kinds["frontier"].mean_score is None

    assert res.residual["status"] == "pending"
    assert "baseline_mean" in res.residual


def test_run_aggregate_hallucination_count_matches_example_sum():
    spec = DatasetSpec.load(os.path.join(V.FIXTURES_DIR, "spec.json"), V.SCENES_DIR)
    res = V.run(V.SCENES_DIR, spec.hash)
    assert res.aggregate["hallucination_count"] == sum(
        e.detail["hallucinations"] for e in res.examples)
    assert res.aggregate["hallucination_count"] >= 1  # at least the hallucination fixture
    # precision is king: the suite is not vacuously perfect -> success bar honestly fails
    assert res.aggregate["success_bar"]["precision==1.0"] is False


def test_run_examples_carry_structure_and_label_for_roc():
    spec = DatasetSpec.load(os.path.join(V.FIXTURES_DIR, "spec.json"), V.SCENES_DIR)
    res = V.run(V.SCENES_DIR, spec.hash, limit=5)
    assert len(res.examples) == 5
    for e in res.examples:
        assert e.structure  # held-out structure recorded
        assert e.label in (True, False)


def test_result_json_roundtrips():
    spec = DatasetSpec.load(os.path.join(V.FIXTURES_DIR, "spec.json"), V.SCENES_DIR)
    res = V.run(V.SCENES_DIR, spec.hash)
    from tools.flywheel.bench.schema import BenchResult
    back = BenchResult.from_json(res.to_json())
    assert back.bench_id == "vlm"
    assert len(back.examples) == len(res.examples)


def test_live_check_failure_is_recorded_but_never_changes_offline_score():
    fx = next(f for f in V.load_fixtures(V.SCENES_DIR)
              if f["example_id"] == "bracket_perfect")
    baseline_score = V.score_example(fx)

    def _boom(_facts):
        raise RuntimeError("no core on this box")

    guarded_score = V.score_example(fx, live_check=_boom)
    assert guarded_score.score == baseline_score.score
    assert guarded_score.passed == baseline_score.passed
    assert "live_error" in guarded_score.detail
    assert guarded_score.detail["mode"] == "fixture"


def test_driver_is_exposed():
    assert V.DRIVER.bench_id == "vlm"
    assert callable(V.DRIVER.run)
