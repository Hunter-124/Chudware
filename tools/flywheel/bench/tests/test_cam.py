"""CAM bench (IP-4): fixture loading, gouge-gated scoring, deterministic op-plan template,
and the two-baseline / pending-residual run() contract."""
from __future__ import annotations

import pytest

from tools.flywheel.bench.benches import cam as CAM
from tools.flywheel.bench.schema import BenchResult


def _by_id(fixtures, example_id):
    return next(f for f in fixtures if f["id"] == example_id)


@pytest.fixture(scope="module")
def fixtures():
    fx = CAM.load_fixtures()
    assert len(fx) >= 6
    return fx


def test_fixtures_load_and_span_three_families(fixtures):
    families = {f["part_family"] for f in fixtures}
    assert families == {"pocketed_block", "drilled_plate", "contoured_part"}
    assert any(f["expected_sim"]["gouges"] > 0 for f in fixtures), \
        "at least one gouging fixture required"
    for f in fixtures:
        for key in ("id", "part_family", "material", "setup", "stock", "tools",
                    "op_plan", "expected_sim"):
            assert key in f, f"{f.get('id')} missing {key!r}"


def test_dataset_hash_is_stable():
    spec1 = CAM.freeze_dataset()
    spec2 = CAM.freeze_dataset()
    assert spec1.hash == spec2.hash
    assert spec1.hash.startswith("sha256:")
    assert spec1.verify()
    assert set(spec1.structures) == {"pocketed_block", "drilled_plate", "contoured_part"}


def test_gouging_example_scores_zero_even_with_high_fraction(fixtures):
    fx = _by_id(fixtures, "pocketed_block_gouge_02")
    assert fx["expected_sim"]["gouges"] > 0
    assert fx["expected_sim"]["machined_fraction"] >= 0.90  # deliberately high
    ex = CAM.score_example(fx)
    assert ex.score == 0.0
    assert ex.passed is False
    assert ex.label is False  # gouge-free label = False for this example
    assert ex.detail["gouges"] > 0


def test_clean_high_fraction_example_passes(fixtures):
    fx = _by_id(fixtures, "pocketed_block_clean_01")
    ex = CAM.score_example(fx)
    assert ex.detail["gouges"] == 0
    assert ex.score == pytest.approx(fx["expected_sim"]["machined_fraction"])
    assert ex.passed is True
    assert ex.label is True


def test_gouge_free_but_below_target_fails(fixtures):
    fx = _by_id(fixtures, "pocketed_block_low_fraction_03")
    ex = CAM.score_example(fx)
    assert ex.detail["gouges"] == 0
    assert ex.score > 0.0  # not gated to zero -- it's not a gouge
    assert ex.passed is False  # but below the completion bar


def test_deterministic_op_plan_produces_a_plan():
    stock = {"min": [-25, -25, -10], "max": [25, 25, 0]}
    plan = CAM.deterministic_op_plan("pocketed_block", stock)
    assert isinstance(plan, list) and len(plan) >= 1
    for op in plan:
        assert "type" in op and "tool_number" in op and "heights" in op and "geometry" in op

    drill_plan = CAM.deterministic_op_plan("drilled_plate", {"min": [-30, -20, -8], "max": [30, 20, 0]})
    assert drill_plan[0]["type"] == "drill"


def test_run_two_baselines_and_pending_residual(fixtures):
    spec = CAM.freeze_dataset()
    res = CAM.run(fixtures, spec.hash)
    agg = res.aggregate
    assert 0.0 < agg["gouge_free_rate"] < 1.0  # we deliberately included gouge fixtures
    assert 0.0 < agg["mean_machined_fraction"] <= 1.0
    assert agg["target_fraction"] == CAM.TARGET_FRACTION
    assert "gouge_free_and_fraction_ge_target" in agg["success_bar"]
    # gouge-free examples always outrank gouged ones -> perfect ranking by construction
    assert res.aggregate["roc_auc_gouge_free"] == 1.0

    kinds = {b.kind: b.status for b in res.baselines}
    assert kinds == {"deterministic": "measured", "frontier": "pending"}
    det_row = next(b for b in res.baselines if b.kind == "deterministic")
    assert det_row.mean_score is not None and det_row.pass_rate is not None
    assert res.residual["status"] == "pending"


def test_result_json_roundtrips(fixtures):
    spec = CAM.freeze_dataset()
    res = CAM.run(fixtures, spec.hash)
    back = BenchResult.from_json(res.to_json())
    assert back.bench_id == "cam"
    assert len(back.examples) == len(fixtures)
    assert back.examples[0].structure in {"pocketed_block", "drilled_plate", "contoured_part"}


def test_driver_registered_and_smoke_runs():
    res = CAM.DRIVER.run(True, "/tmp/unused-cam-bench-workdir")
    assert res.bench_id == "cam"
    assert res.n_examples == 4
    assert res.split == "smoke"
    # Scoring mode depends on whether libaicad is discoverable in this environment:
    # fixture mode without the core, live mode with it. Either is valid — what must
    # hold is that every example scored the SAME way (no silent per-example fallback).
    modes = {e.detail["mode"] for e in res.examples}
    assert len(modes) == 1 and modes <= {"fixture", "live"}
