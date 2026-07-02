"""IP-18 migration gate: the frozen criteria load, are pre-registered, and the executable gate
matches the documented rules (within 5pts of frontier, <1/50th cost, never below deterministic,
zero intent drift)."""
import pytest

from tools.flywheel.bench import migration as M


def test_criteria_are_frozen_before_training():
    c = M.load_criteria()
    assert c["frozen"] is True and c["frozen_before_training"] is True
    assert c["pre_registered_frontier_target"]["frontier_pct_fixed_min"] == 0.70
    assert c["pre_registered_frontier_target"]["frontier_intent_drift_max"] == 0
    # the local model is NOT wired in this task
    assert c["training_recipe"]["gated_phase"] is True


REF = M.Reference(frontier_pct_fixed=0.80, frontier_cost_per_fix=1.0,
                  deterministic_pct_fixed=0.40)


def test_gate_passes_when_all_criteria_met():
    cand = M.Candidate(pct_fixed=0.78, intent_drift=0, cost_per_fix=0.01)  # within 5pts, 100x cheaper
    res = M.evaluate(cand, REF)
    assert res.passed and all(res.checks.values()) and not res.reasons


def test_gate_fails_when_more_than_5pts_below_frontier():
    cand = M.Candidate(pct_fixed=0.70, intent_drift=0, cost_per_fix=0.01)  # 10pts below 0.80
    res = M.evaluate(cand, REF)
    assert not res.passed and not res.checks["within_5pts_of_frontier"]


def test_gate_fails_when_not_cheap_enough():
    cand = M.Candidate(pct_fixed=0.78, intent_drift=0, cost_per_fix=0.5)  # only 2x cheaper
    res = M.evaluate(cand, REF)
    assert not res.passed and not res.checks["cheaper_than_frontier_50x"]


def test_gate_fails_below_deterministic_killswitch():
    ref = M.Reference(frontier_pct_fixed=0.42, frontier_cost_per_fix=1.0,
                      deterministic_pct_fixed=0.40)
    cand = M.Candidate(pct_fixed=0.39, intent_drift=0, cost_per_fix=0.001)  # within 5pts of frontier
    res = M.evaluate(cand, ref)                                             # but below deterministic
    assert not res.passed and not res.checks["never_below_deterministic"]


def test_gate_fails_on_any_intent_drift():
    cand = M.Candidate(pct_fixed=0.79, intent_drift=1, cost_per_fix=0.001)
    res = M.evaluate(cand, REF)
    assert not res.passed and not res.checks["zero_intent_drift"]
