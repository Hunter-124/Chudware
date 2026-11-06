"""Done guards retain observed sketch failures without introducing mandatory RPCs."""
from __future__ import annotations

import json

import pytest

from services.agent import guardrails


def _call(sketch, *, tool="check_sketch", doc="d1", **changes):
    return {"tool": tool, "ok": True, "args": {"doc_id": doc, "sketch": sketch},
            "result": {"ok": True, "doc_id": doc, "sketch": sketch,
                       "status": "full", "dof": 0, "fully_defined": True,
                       "pinned_only": False, **changes}}


def _done(calls, handoff=None, **kwargs):
    return guardrails.check_done(handoff or {}, calls=calls, doc_id="d1",
                                tree_provider=lambda doc: {},
                                skip={"verify_battery", "check_interference", "orphan_sketch"},
                                **kwargs)


@pytest.mark.parametrize("tool", ["check_sketch", "sketch_health"])
def test_done_rejects_pinned_only_despite_dof_zero_and_full_status(tool):
    verdict = _done([_call("f7", tool=tool, pinned_only=True)])
    assert verdict.ok is False
    assert verdict.name == "sketch_quality"
    assert "f7" in verdict.message and "pinned" in verdict.message


def test_done_does_not_lose_health_quality_finding():
    finding = {"kind": "under_dimensioned", "severity": "warning",
               "message": "Add a driving radius to e3"}
    call = _call("f9", tool="sketch_health", findings=[finding])
    verdict = _done([call])
    assert verdict.ok is False
    assert "Add a driving radius to e3" in verdict.message


def test_clean_first_sketch_does_not_clear_other_failed_sketch():
    calls = [_call("f7", fully_defined=False, dof=2, status="under"), _call("f1")]
    assert _done(calls).ok is False
    calls.append(_call("f7"))
    assert _done(calls).ok is True


def test_clean_recheck_from_battery_clears_observed_failure():
    calls = [_call("f7", pinned_only=True), {
        "tool": "verify_battery", "ok": True, "args": {"doc_id": "d1"},
        "result": {"scores": {"check_sketch": 1.0}, "findings": [],
                   "check_results": {"check_sketch": {
                       "doc_id": "d1", "fully_defined": True, "pinned_only": False,
                       "sketches": [_call("f7")["result"]],
                   }}},
    }]
    assert _done(calls).ok is True


def test_known_failures_require_reasoned_waiver_not_missing_checks():
    call = _call("f7", pinned_only=True)
    assert _done([call], {"waivers": [{"check": "sketch_quality:f7", "reason": ""}]}).ok is False
    handoff = {"waivers": [{"check": "sketch_quality:f7", "reason": "intentional fixture import"}]}
    assert _done([call], handoff).ok is True
    assert _done([]).ok is True


def test_per_sketch_waiver_does_not_waive_a_different_sketch():
    calls = [_call("f7", pinned_only=True), _call("f9", pinned_only=True)]
    verdict = _done(calls, {"waivers": [
        {"check": "sketch_quality:f7", "reason": "intentional fixture import"},
    ]})
    assert verdict.ok is False
    assert "f9" in verdict.message


def test_sketch_quality_is_scoped_to_document_and_surviving_feature():
    calls = [_call("f7", pinned_only=True), _call("f8", doc="other", pinned_only=True)]
    assert _done(calls, tree={"features": [{"id": "f1", "type": "box"}]}).ok is True
    assert _done([calls[1]]).ok is True


def test_serialized_health_findings_in_ledger_are_retained():
    from types import SimpleNamespace

    result = _call("f7", pinned_only=True)
    result["result"] = json.dumps(result["result"])
    ledger = SimpleNamespace(events=[{"kind": "tool_call", "payload": result}])
    assert _done([], ledger=ledger).ok is False


@pytest.mark.parametrize("quality", [
    {"ok": True, "fully_defined": True, "pinned_only": True},
    {"ok": True, "fully_defined": False, "pinned_only": False},
    {"ok": True, "findings": [{"severity": "error", "message": "open profile"}]},
])
def test_battery_pass_evidence_cannot_override_explicit_bad_sketch_quality(quality):
    result = {"ok": True, "verdict": "pass", "coverage": {"ran": ["check_sketch"]},
              "scores": {"check_sketch": 1.0}, "findings": [],
              "check_results": {"check_sketch": quality}}
    assert guardrails._battery_result_ok(result) is False


def test_advisory_dimension_findings_are_not_an_unjustified_new_hard_gate():
    call = _call("f7", findings=[{"kind": "no_driving_dimensions", "severity": "warning",
                                 "message": "Consider critical dimensions"}])
    assert _done([call]).ok is True


@pytest.mark.parametrize("usage,accepted", [
    ("open_path", True), ("unknown", True),
    ("closed_profile", False), ("mixed", False), ("", False),
])
def test_open_endpoints_are_valid_only_for_open_or_unused_paths(usage, accepted):
    closure = {"closed_chains_ok": False, "open_endpoints": ["e1.end"]}
    if usage:
        closure["usage"] = usage
    assert _done([_call("f7", closure=closure)]).ok is accepted


def test_sketch_pass_before_later_edit_is_stale_until_rechecked():
    calls = [_call("f7", pinned_only=True), _call("f7"),
             {"tool": "add_features", "ok": True, "args": {"doc_id": "d1"},
              "result": {"ok": True}}]
    verdict = _done(calls)
    assert not verdict.ok and verdict.name == "sketch_quality"
    assert "f7" in verdict.message and "recheck" in verdict.message
    calls.append(_call("f7"))
    assert _done(calls).ok


def test_other_document_edit_does_not_stale_sketch_pass():
    calls = [_call("f7"), {"tool": "add_features", "ok": True,
                            "args": {"doc_id": "other"}, "result": {"ok": True}}]
    assert _done(calls).ok


@pytest.mark.parametrize("current_revision,stale,expected", [
    (3, False, True),
    (4, False, False),
    (3, True, False),
    (None, False, False),
])
def test_sketch_pass_must_match_current_document_revision(current_revision, stale, expected):
    calls = [_call("f7", revision=3, revision_stale=stale)]
    tree = {"revision": current_revision,
            "features": [{"id": "f7", "type": "sketch"}]}
    verdict = guardrails.check_sketch_quality_before_done(
        {}, calls=calls, doc_id="d1", tree_provider=lambda doc: tree)
    assert verdict.ok is expected
    if not expected:
        assert verdict.name == "sketch_quality" and "recheck" in verdict.message


@pytest.mark.parametrize("changes", [
    {"status": "under", "dof": 2, "fully_defined": False},
    {"status": "over", "conflicting": ["c1"]},
    {"converged": False},
    {"skipped_constraints": ["c2"]},
])
def test_3d_sketch_solver_failures_block_done_until_rechecked(changes):
    calls = [_call("f7", tool="check_sketch3d", **changes)]
    verdict = _done(calls)
    assert not verdict.ok and verdict.name == "sketch_quality"
    assert "f7" in verdict.message
    calls.append(_call("f7", tool="check_sketch3d"))
    assert _done(calls).ok


def test_clean_3d_sketch_check_becomes_stale_after_edit():
    calls = [_call("f7", tool="check_sketch3d"),
             {"tool": "add_features", "ok": True, "args": {"doc_id": "d1"},
              "result": {"ok": True}}]
    verdict = _done(calls)
    assert not verdict.ok and "recheck" in verdict.message
    calls.append(_call("f7", tool="check_sketch3d"))
    assert _done(calls).ok



def test_individual_fix_with_driving_dimensions_remains_advisory():
    call = _call("f7", findings=[{
        "kind": "pinned_instead_of_constrained", "severity": "warning",
        "message": "Prefer a geometric origin relation over a single anchor fix",
    }])
    assert _done([call]).ok is True


def test_fully_define_mutation_requires_verification_before_done():
    verdict = guardrails.check_done(
        {}, calls=[{"tool": "sketch_fully_define", "ok": True,
                    "args": {"doc_id": "d1", "sketch": "f7"},
                    "result": {"ok": True, "dof": 0}}],
        doc_id="d1", tree={"features": []},
        skip={"check_interference", "orphan_sketch"})
    assert not verdict.ok and verdict.name == "verify_battery"
