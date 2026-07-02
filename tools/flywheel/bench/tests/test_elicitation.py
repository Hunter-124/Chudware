"""elicitation bench: gold corpus, deterministic extractor, and scoring rubric."""
import copy

import pytest

from tools.flywheel.bench.benches import elicitation as EL
from tools.flywheel.bench.schema import BenchResult


def test_gold_loads_and_spans_four_families():
    examples = EL.load_gold()
    assert len(examples) >= 8
    families = {e["structure"] for e in examples}
    assert families == {"bracket", "shaft", "enclosure", "plate"}
    for e in examples:
        assert e["conversation"]
        assert e["brief"]["schema"] == "aicad.req/1"
        assert 2 <= len(e["brief"]["requirements"]) <= 4


def test_gold_hash_is_stable():
    spec1 = EL.freeze_spec()
    spec2 = EL.freeze_spec()
    assert spec1.hash == spec2.hash
    assert spec1.verify()
    assert spec1.n_examples == len(EL.load_gold())
    assert set(spec1.structures) == {"bracket", "shaft", "enclosure", "plate"}


def test_gold_briefs_are_all_schema_valid():
    for e in EL.load_gold():
        valid, errors = EL.schema_valid(e["brief"])
        assert valid, (e["id"], errors)


def test_deterministic_baseline_produces_partial_brief():
    e = EL.load_gold()[0]
    draft = EL.deterministic_extract(e["conversation"])
    assert draft["schema"] == "aicad.req/1"
    valid, errors = EL.schema_valid(draft)
    assert valid, errors
    # decent-but-imperfect: it should find *something* but not read the gold brief, so it
    # must not trivially reproduce every gold requirement's full slot set.
    assert len(draft["requirements"]) >= 1
    slots = EL.score_slots(e["brief"], draft)
    assert 0.0 < slots["coverage"] < 1.0 or 0.0 < slots["precision"] < 1.0


def test_perfect_candidate_scores_full_coverage_and_schema_valid():
    e = EL.load_gold()[0]
    score = EL.score_example(e["id"], e["structure"], e["conversation"], e["brief"],
                             candidate_brief=e["brief"])
    assert score.detail["schema_valid"] is True
    assert score.detail["coverage"] == 1.0
    assert score.detail["precision"] == 1.0
    assert score.passed is True
    assert score.score == 1.0


def test_invalid_status_scores_zero():
    e = EL.load_gold()[0]
    bad = copy.deepcopy(e["brief"])
    bad["requirements"][0]["status"] = "not_a_real_status"
    score = EL.score_example(e["id"], e["structure"], e["conversation"], e["brief"],
                             candidate_brief=bad)
    assert score.score == 0.0
    assert score.passed is False
    assert score.detail["schema_valid"] is False


def test_invalid_kind_and_verifier_also_score_zero():
    e = EL.load_gold()[0]
    bad_kind = copy.deepcopy(e["brief"])
    bad_kind["requirements"][0]["kind"] = "not_a_kind"
    assert EL.score_example(e["id"], e["structure"], e["conversation"], e["brief"],
                            candidate_brief=bad_kind).score == 0.0

    bad_verifier = copy.deepcopy(e["brief"])
    bad_verifier["requirements"][0]["verify_by"] = [{"verifier": "not_a_verifier"}]
    assert EL.score_example(e["id"], e["structure"], e["conversation"], e["brief"],
                            candidate_brief=bad_verifier).score == 0.0

    empty_verify_by = copy.deepcopy(e["brief"])
    empty_verify_by["requirements"][0]["verify_by"] = []
    assert EL.score_example(e["id"], e["structure"], e["conversation"], e["brief"],
                            candidate_brief=empty_verify_by).score == 0.0


def test_matching_rule_pairs_by_param_then_kind():
    gold = [
        {"id": "R1", "kind": "dimensional", "quantity": {"param": "bore_dia", "op": "==", "value": 10, "unit": "mm"}, "verify_by": [{"verifier": "measure_part"}]},
        {"id": "R2", "kind": "material", "quantity": None, "verify_by": [{"verifier": "dfm_check"}]},
    ]
    cand = [
        {"id": "C2", "kind": "material", "quantity": None, "verify_by": [{"verifier": "dfm_check"}]},
        {"id": "C1", "kind": "dimensional", "quantity": {"param": "bore_dia", "op": "==", "value": 10, "unit": "mm"}, "verify_by": [{"verifier": "measure_part"}]},
    ]
    pairs = EL.match_requirements(gold, cand)
    assert len(pairs) == 2
    matched = {(g["id"] if g else None, c["id"] if c else None) for g, c in pairs}
    assert ("R1", "C1") in matched
    assert ("R2", "C2") in matched


def test_run_returns_two_baseline_rows_and_pending_residual():
    res = EL.run()
    assert isinstance(res, BenchResult)
    assert res.bench_id == "elicitation"
    assert res.n_examples == len(EL.load_gold())
    kinds = {b.kind: b.status for b in res.baselines}
    assert kinds["deterministic"] == "measured"
    assert kinds["frontier"] == "pending"
    assert res.residual["status"] == "pending"
    assert 0.0 <= res.aggregate["coverage"] <= 1.0
    assert res.aggregate["schema_valid_rate"] == 1.0  # deterministic extractor always emits valid enums
    assert "success_bar" in res.aggregate


def test_run_smoke_limits_examples():
    res = EL.run(split="smoke", limit=4)
    assert res.n_examples == 4
    assert res.split == "smoke"


def test_result_json_roundtrips():
    res = EL.run(limit=5)
    back = BenchResult.from_json(res.to_json())
    assert back.bench_id == "elicitation"
    assert len(back.examples) == 5
    assert back.examples[0].structure in {"bracket", "shaft", "enclosure", "plate"}


def test_driver_is_exposed():
    assert EL.DRIVER.bench_id == "elicitation"
    result = EL.DRIVER.run(True, "/tmp")
    assert result.n_examples == 4
