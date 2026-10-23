"""test_pipeline.py — Mission pipeline glue.

Covers:
  * manifest validator rule-by-rule (a fixture per contract §16 rule);
  * phase gate enforcement (cannot enter BUILD with an unratified plan);
  * replan routing matrix (§17, incl. criteria-change → user, scope=org);
  * a mock-proven end-to-end dry run through all 8 phases on a single-part fixture
    (offline discipline: the mock exercises the REAL phase machine + gates + ledger,
    no all-skip).

Run with the gate venv (has yaml):
    ~/.local/share/Chudware/mcp-venv/bin/python -m pytest \
        services/agent/pipeline/test_pipeline.py -q
"""
from __future__ import annotations

import copy
import uuid

import pytest

from services.agent.pipeline import (
    validate_manifest, manifest_valid, blocking_ambiguities, BounceTracker,
    Phase, PHASE_ORDER, PhaseMachine, PhaseError, PipelineFlags,
    route_replan, apply_replan, validate_replan_request, ReplanError,
)
from services.dag.mission_template import build_mission_graph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _valid_manifest() -> dict:
    """A clean single-part manifest that passes every §16 rule."""
    return {
        "schema": "aicad.deliverables/1",
        "request_verbatim": "make a d20 bracket bore a sliding fit on the shaft",
        "deliverables": [
            {
                "id": "DL1", "kind": "part", "name": "bracket",
                "description": "bearing mount bracket",
                "acceptance_criteria": [
                    {"id": "AC1", "text": "bore is a sliding fit on the d20 shaft",
                     "check": {"tool": "fit_report", "params_hint": {},
                               "pass_rule": "clearance in [0.007,0.041]"}},
                ],
                "depends_on": [], "target_refs": {"doc_id": None, "features": []},
                "status": "pending",
            },
        ],
        "ambiguities": [], "assumptions": [],
    }


def _multi_manifest() -> dict:
    """part + drawing + cam_program (dependency chain) — the worked example shape."""
    m = _valid_manifest()
    m["deliverables"] += [
        {"id": "DL2", "kind": "drawing", "name": "drawing",
         "acceptance_criteria": [
             {"id": "AC2", "text": "views correct",
              "check": {"tool": "review_drawing", "pass_rule": "no errors"}}],
         "depends_on": ["DL1"]},
        {"id": "DL3", "kind": "cam_program", "name": "gcode",
         "acceptance_criteria": [
             {"id": "AC3", "text": "no gouges",
              "check": {"tool": "cam_simulate", "pass_rule": "gouges==0"}}],
         "depends_on": ["DL1"]},
    ]
    return m


# mega-8: the multi-domain PLAN/BUILD soft gate (services/dag/node_runner.py) refuses a
# bare auto-complete on gated nodes (n_plan) — the done-mock agent must carry a
# schema-valid typed refusal or n_plan escalates to failed and the PLAN phase
# soft-reconciles (entered, never exited) instead of recording its exit gate.
_TYPED_REFUSAL_RESULT = {
    "typed_refusal": {
        "reason": "single-part mechanical mission — no delegation needed",
        "domains_considered": ["mechanical"],
        "why_flat": "one deliverable, one domain; a flat plan is correct",
    }
}


# ===========================================================================
# 1. Manifest validator — rule by rule (contract §16)
# ===========================================================================

def test_valid_manifest_passes_clean():
    findings = validate_manifest(_valid_manifest())
    assert findings == [], [str(f) for f in findings]
    assert manifest_valid(findings)


def test_rule_wrong_schema():
    m = _valid_manifest(); m["schema"] = "wrong"
    assert any(f.rule == "schema" for f in validate_manifest(m))


def test_rule_missing_request_verbatim():
    m = _valid_manifest(); m["request_verbatim"] = ""
    assert any(f.rule == "request_verbatim" for f in validate_manifest(m))


def test_rule_empty_deliverables():
    m = _valid_manifest(); m["deliverables"] = []
    assert any(f.rule == "deliverables" for f in validate_manifest(m))


def test_rule_deliverable_needs_at_least_one_criterion():
    m = _valid_manifest(); m["deliverables"][0]["acceptance_criteria"] = []
    findings = validate_manifest(m)
    assert any(f.rule == "acceptance_criteria" for f in findings)
    assert not manifest_valid(findings)


def test_rule_criterion_must_name_a_check_tool():
    m = _valid_manifest()
    m["deliverables"][0]["acceptance_criteria"][0]["check"] = {"pass_rule": "x"}
    assert any(f.rule == "criterion_check_tool" for f in validate_manifest(m))


def test_rule_non_manual_check_needs_pass_rule():
    m = _valid_manifest()
    m["deliverables"][0]["acceptance_criteria"][0]["check"] = {"tool": "fit_report"}
    assert any(f.rule == "criterion_pass_rule" for f in validate_manifest(m))


def test_rule_manual_pairing_requires_needs_confirm_or_ask_user():
    m = _valid_manifest()
    m["deliverables"][0]["acceptance_criteria"][0]["check"] = {
        "tool": "manual", "pass_rule": "user confirms"}
    # No assumption / no question → manual pairing rule fails.
    assert any(f.rule == "manual_pairing" for f in validate_manifest(m))
    # Paired with a needs_confirm assumption → passes.
    m["assumptions"] = [{"text": "lid 6061", "basis": "defaults", "needs_confirm": True}]
    assert not any(f.rule == "manual_pairing" for f in validate_manifest(m))


def test_rule_invalid_kind():
    m = _valid_manifest(); m["deliverables"][0]["kind"] = "widget"
    assert any(f.rule == "kind" for f in validate_manifest(m))


def test_rule_depends_on_must_resolve():
    m = _valid_manifest(); m["deliverables"][0]["depends_on"] = ["DLX"]
    assert any(f.rule == "depends_on" for f in validate_manifest(m))


def test_rule_dependency_cycle_detected():
    m = _multi_manifest()
    # Introduce a cycle: DL1 depends on DL3 which depends on DL1.
    m["deliverables"][0]["depends_on"] = ["DL3"]
    findings = validate_manifest(m)
    assert any(f.rule == "dependency_cycle" for f in findings)
    assert not manifest_valid(findings)


def test_rule_modification_unresolved_target_is_blocking():
    m = _valid_manifest()
    m["deliverables"][0]["kind"] = "modification"
    m["deliverables"][0]["target_refs"] = {"doc_id": None, "features": []}
    findings = validate_manifest(m)
    assert any(f.rule == "modification_target" and f.level == "blocking"
               for f in findings)


def test_rule_modification_resolves_via_doc_resolver():
    m = _valid_manifest()
    m["deliverables"][0]["kind"] = "modification"
    m["deliverables"][0]["target_refs"] = {"doc_id": "doc7", "features": []}
    # resolver says the doc exists → no error.
    ok = validate_manifest(m, doc_resolver=lambda ref: ref.get("doc_id") == "doc7")
    assert not any(f.rule == "modification_target" for f in ok)
    # resolver says it does not → error.
    bad = validate_manifest(m, doc_resolver=lambda ref: False)
    assert any(f.rule == "modification_target" and f.level == "error" for f in bad)


def test_rule_modification_resolver_failure_is_harness_error_not_bounce():
    m = _valid_manifest()
    m["deliverables"][0]["kind"] = "modification"
    m["deliverables"][0]["target_refs"] = {"doc_id": "doc7", "features": []}

    def unavailable(_ref):
        raise ConnectionError("document service disconnected")

    findings = validate_manifest(m, doc_resolver=unavailable)
    failure = next(
        f for f in findings if f.rule == "modification_target_resolution")
    assert failure.level == "harness_error"
    assert "ConnectionError" in failure.message
    tracker = BounceTracker()
    assert tracker.record_bounce(findings) == "harness_error"
    assert tracker.attempts == 0


def test_blocking_ambiguity_surfaces_as_blocking_finding():
    m = _valid_manifest()
    m["ambiguities"] = [{"text": "which material?", "blocking": True,
                         "question_id": "q1"}]
    findings = validate_manifest(m)
    blk = blocking_ambiguities(findings)
    assert blk and all(f.level == "blocking" for f in blk)
    # A blocking ambiguity does NOT make the manifest invalid (it routes to CLARIFY).
    assert manifest_valid(findings)


def test_bounce_tracker_bounce_once_then_escalate():
    t = BounceTracker()  # max_attempts=2
    assert t.record_bounce() == "retry"
    assert not t.exhausted
    assert t.record_bounce() == "escalate"
    assert t.exhausted


# ===========================================================================
# 2. Phase machine + exit gates
# ===========================================================================

def test_phase_order_is_the_full_sequence():
    assert PHASE_ORDER[0] is Phase.INTAKE
    assert PHASE_ORDER[-1] is Phase.DELIVER
    assert Phase.ORGANIZE in PHASE_ORDER


def test_intake_gate_blocks_on_invalid_manifest():
    m = _valid_manifest(); m["deliverables"][0]["acceptance_criteria"] = []
    findings = validate_manifest(m)
    machine = PhaseMachine()
    machine.enter(Phase.INTAKE)
    gate = machine.run_gate(Phase.INTAKE, {"manifest_findings": findings})
    assert not gate.passed
    with pytest.raises(PhaseError):
        machine.exit(Phase.INTAKE, gate)


def test_phase_entry_requires_successful_exit_and_exact_adjacency():
    machine = PhaseMachine(flags=PipelineFlags(org_layer=False))
    assert machine.enter(Phase.INTAKE).ok
    with pytest.raises(PhaseError):
        machine.enter(Phase.DELIVER)
    with pytest.raises(PhaseError):
        machine.enter(Phase.SPECIFY)

    gate = machine.run_gate(Phase.INTAKE, {"manifest_findings": []})
    assert machine.exit(Phase.INTAKE, gate).ok
    assert machine.enter(Phase.SPECIFY).ok
    assert machine.current is Phase.SPECIFY


def test_enter_persistence_failure_does_not_mutate_state():
    class BrokenLedger:
        def log_stage(self, *_args, **_kwargs):
            raise OSError("ledger read-only")

    machine = PhaseMachine(ledger=BrokenLedger())
    result = machine.enter(Phase.INTAKE)
    assert not result.ok
    assert result.outcome_kind == "internal_error"
    assert machine.current is None
    assert machine.history == []


def test_plan_gate_blocks_when_not_ratified():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.PLAN, {
        "build_graph_blockers": [], "plan_ratified": False})
    assert not gate.passed
    assert any("ratif" in r for r in gate.reasons)


def test_cannot_enter_build_from_unratified_plan():
    """The load-bearing case: BUILD is unreachable while PLAN's gate fails."""
    machine = PhaseMachine()
    machine.enter(Phase.PLAN)
    plan_gate = machine.run_gate(Phase.PLAN, {
        "build_graph_blockers": ["cycle among ['a','b']"],
        "plan_ratified": False})
    assert not plan_gate.passed
    # exit() refuses, so the machine never advances to BUILD.
    with pytest.raises(PhaseError):
        machine.exit(Phase.PLAN, plan_gate)
    assert machine.current is Phase.PLAN


def test_plan_gate_passes_when_valid_and_ratified():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.PLAN, {
        "build_graph_blockers": [], "plan_ratified": True})
    assert gate.passed and gate.overseer


def test_build_gate_blocks_on_unresolved_node():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.BUILD, {"unresolved_nodes": ["n_build_DL1"]})
    assert not gate.passed


def test_verify_gate_waiver_by_name():
    machine = PhaseMachine()
    ctx = {
        "expected_ac_ids": ["AC1"],
        "criterion_results": {"AC1": "FAIL"},
        "waived_criteria": [],
    }
    assert not machine.run_gate(Phase.VERIFY, ctx).passed
    ctx["waived_criteria"] = ["AC1"]
    assert machine.run_gate(Phase.VERIFY, ctx).passed


def test_verify_gate_requires_evidence_for_every_expected_criterion():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.VERIFY, {
        "expected_ac_ids": ["AC1"],
        "criterion_results": {},
        "waived_criteria": [],
    })
    assert not gate.passed
    assert "AC1" in " ".join(gate.reasons)


def test_verify_gate_battery_guard_exception_is_harness_failure(monkeypatch):
    from services.agent import guardrails

    def broken_guard(_ctx):
        raise RuntimeError("guard state unavailable")

    monkeypatch.setattr(guardrails, "battery_missing_for_mission", broken_guard)
    gate = PhaseMachine().run_gate(Phase.VERIFY, {
        "expected_ac_ids": ["AC1"],
        "criterion_results": {"AC1": "PASS"},
        "waived_criteria": [],
    })
    assert not gate.passed
    assert gate.outcome_kind == "internal_error"
    assert "guard state unavailable" in " ".join(gate.reasons)


def test_deliver_gate_requires_ledger_closure_evidence():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.DELIVER, {
        "mission_report_emitted": True,
        "ledger_closed": False,
    })
    assert not gate.passed
    assert "ledger" in " ".join(gate.reasons)


def test_clarify_skipped_without_blocking_ambiguity():
    machine = PhaseMachine()
    nxt = machine.next_phase(Phase.INTAKE, {"has_blocking_ambiguity": False})
    assert nxt is Phase.SPECIFY  # CLARIFY skipped
    nxt2 = machine.next_phase(Phase.INTAKE, {"has_blocking_ambiguity": True})
    assert nxt2 is Phase.CLARIFY


def test_organize_skipped_unless_flag_on():
    # org_layer=True is the default. Explicit False skips ORGANIZE.
    off = PhaseMachine(flags=PipelineFlags(org_layer=False))
    assert off.next_phase(Phase.SPECIFY, {}) is Phase.PLAN  # ORGANIZE skipped
    on = PhaseMachine(flags=PipelineFlags(org_layer=True))
    assert on.next_phase(Phase.SPECIFY, {}) is Phase.ORGANIZE


def test_organize_is_default_on():
    """PipelineFlags() defaults to org_layer=True."""
    flags = PipelineFlags()
    assert flags.org_layer is True
    m = PhaseMachine()
    assert m.next_phase(Phase.SPECIFY, {}) is Phase.ORGANIZE


def test_build_council_gate_flagged():
    """PLAN gate requires council synthesis only when the council flag is on."""
    off = PhaseMachine(flags=PipelineFlags(build_council=False))
    assert off.run_gate(Phase.PLAN, {"plan_ratified": True}).passed
    on = PhaseMachine(flags=PipelineFlags(build_council=True))
    gate = on.run_gate(Phase.PLAN, {"plan_ratified": True,
                                    "council_synthesis_recorded": False})
    assert not gate.passed
    assert on.run_gate(Phase.PLAN, {"plan_ratified": True,
                                    "council_synthesis_recorded": True}).passed


def test_correct_loop_reentry_is_legal():
    machine = PhaseMachine()
    machine.enter(Phase.VERIFY)
    machine.exit(
        Phase.VERIFY,
        machine.run_gate(Phase.VERIFY, {
            "expected_ac_ids": ["AC1"],
            "criterion_results": {"AC1": "FAIL"},
            "waived_criteria": ["AC1"],
        }),
    )
    machine.enter(Phase.CORRECT)
    machine.exit(
        Phase.CORRECT,
        machine.run_gate(Phase.CORRECT, {"open_failure_cards": []}),
    )
    machine.enter(Phase.BUILD)
    assert machine.current is Phase.BUILD


# ===========================================================================
# 2b. Design-pipeline stages (flagged refinement, contract design_pipeline.md)
# ===========================================================================

def test_design_pipeline_default_off_specify_advances_as_before():
    """Flag off (default): SPECIFY → ORGANIZE/PLAN exactly as before — the four
    design-pipeline phases are skipped. The load-bearing flag-off invariant."""
    flags = PipelineFlags()
    assert flags.design_pipeline is False
    # org on (default) → SPECIFY → ORGANIZE, DP phases skipped
    assert PhaseMachine().next_phase(Phase.SPECIFY, {}) is Phase.ORGANIZE
    # org off too → SPECIFY → PLAN, DP phases still skipped
    off = PhaseMachine(flags=PipelineFlags(org_layer=False))
    assert off.next_phase(Phase.SPECIFY, {}) is Phase.PLAN


def test_design_pipeline_flag_on_visits_four_stages():
    """Flag on: SPECIFY → DECOMPOSE → GEOMETRY → LINKAGE → BUILDPATH → (ORGANIZE/PLAN)."""
    m = PhaseMachine(flags=PipelineFlags(design_pipeline=True, org_layer=False))
    assert m.next_phase(Phase.SPECIFY, {}) is Phase.DECOMPOSE
    assert m.next_phase(Phase.DECOMPOSE, {}) is Phase.GEOMETRY
    assert m.next_phase(Phase.GEOMETRY, {}) is Phase.LINKAGE
    assert m.next_phase(Phase.LINKAGE, {}) is Phase.BUILDPATH
    assert m.next_phase(Phase.BUILDPATH, {}) is Phase.PLAN  # org off → PLAN


def test_design_pipeline_exits_checkpoint_typed_ledger_story(tmp_path):
    """Every typed boundary cuts one attributed History entry."""
    from services.agent.ledger import Ledger
    from services.agent.plm_bridge import MockVault, PLMBridge

    ledger = Ledger("run_dp14", path=tmp_path / "ledger.json")
    vault = MockVault()
    vault.register_project("run_dp14", "project_dp14")
    bridge = PLMBridge("run_dp14", vault=vault)
    machine = PhaseMachine(
        ledger=ledger,
        flags=PipelineFlags(design_pipeline=True, org_layer=False),
        checkpoint_bridge=bridge,
    )
    item_artifacts = {"item_bracket": "bg_bracket"}

    feature = {
        "id": "FT1",
        "function": "mount to chassis rail",
        "driving_requirement": "R1",
    }
    ledger.append("feature_architect", "feature", feature)
    machine.enter(Phase.DECOMPOSE)
    machine.exit(
        Phase.DECOMPOSE,
        machine.run_gate(Phase.DECOMPOSE, {"features": [feature]}),
        item_artifacts=item_artifacts,
    )

    geometry = {
        "id": "G1",
        "realizes": ["FT1"],
        "shapes": [{"op": "extrude"}],
        "provenance": "derived from the recorded rail envelope",
    }
    ledger.append("geometry_designer", "geometry_spec", geometry)
    machine.enter(Phase.GEOMETRY)
    machine.exit(
        Phase.GEOMETRY,
        machine.run_gate(
            Phase.GEOMETRY,
            {"features": [feature], "geometry_specs": [geometry]},
        ),
        item_artifacts=item_artifacts,
    )

    linkage = {
        "id": "LINK_MAIN",
        "reference_edges": [{
            "from": "G1",
            "to": "G1",
            "relation": "references datum A",
            "why": "keeps the mounting face stable",
        }],
    }
    ledger.append("linkage_planner", "linkage", linkage)
    machine.enter(Phase.LINKAGE)
    machine.exit(
        Phase.LINKAGE,
        machine.run_gate(Phase.LINKAGE, {"linkages": [linkage]}),
        item_artifacts=item_artifacts,
    )

    build_node = {
        "id": "BN1",
        "op": "extrude",
        "elements": ["G1"],
        "why": "establish the mounting face first",
    }
    ledger.append("build_strategist", "build_node", build_node)
    machine.enter(Phase.BUILDPATH)
    machine.exit(
        Phase.BUILDPATH,
        machine.run_gate(
            Phase.BUILDPATH,
            {"geometry_specs": [geometry], "build_nodes": [build_node]},
        ),
        item_artifacts=item_artifacts,
    )

    checkpoints = vault.checkpoints
    assert len(checkpoints) == 4
    assert all(c["item_artifacts"] == item_artifacts for c in checkpoints)
    messages = [c["entry"]["message"] for c in checkpoints]
    assert "mount to chassis rail" in messages[0]
    assert "extrude" in messages[1]
    assert "keeps the mounting face stable" in messages[2]
    assert "establish the mounting face first" in messages[3]
    triggers = [
        ev["payload"]["trigger"]
        for ev in ledger.events
        if ev.get("kind") == "plmlink"
    ]
    assert triggers == [
        "stage_exit:decompose",
        "stage_exit:geometry",
        "stage_exit:linkage",
        "stage_exit:buildpath",
    ]


def test_dp_flag_from_env():
    import os
    os.environ["CHUDWARE_DESIGN_PIPELINE"] = "1"
    try:
        assert PipelineFlags.from_env().design_pipeline is True
    finally:
        del os.environ["CHUDWARE_DESIGN_PIPELINE"]
    assert PipelineFlags.from_env().design_pipeline is False


def test_decompose_gate_reads_ledger_features():
    m = PhaseMachine()
    assert not m.run_gate(Phase.DECOMPOSE, {"features": []}).passed
    g = m.run_gate(Phase.DECOMPOSE, {"features": [{"id": "FT1"}]})
    assert g.passed and g.gate == "features_present"


def test_geometry_gate_requires_coverage_and_provenance():
    m = PhaseMachine()
    features = [{"id": "FT1"}]
    # no specs → fail
    assert not m.run_gate(Phase.GEOMETRY,
                          {"features": features, "geometry_specs": []}).passed
    # spec realizes FT1 but has no provenance → fail
    bad = [{"id": "G1", "realizes": ["FT1"], "provenance": ""}]
    r = m.run_gate(Phase.GEOMETRY, {"features": features, "geometry_specs": bad})
    assert not r.passed and any("provenance" in x for x in r.reasons)
    # uncovered feature → fail
    uncov = [{"id": "G1", "realizes": ["FT9"], "provenance": "fit_calc"}]
    r2 = m.run_gate(Phase.GEOMETRY, {"features": features, "geometry_specs": uncov})
    assert not r2.passed and any("FT1" in str(x) for x in r2.reasons)
    # covered + provenance → pass, overseer flag set
    good = [{"id": "G1", "realizes": ["FT1"], "provenance": "fit_calc"}]
    r3 = m.run_gate(Phase.GEOMETRY, {"features": features, "geometry_specs": good})
    assert r3.passed and r3.overseer


def test_linkage_gate_reads_ledger():
    m = PhaseMachine()
    assert not m.run_gate(Phase.LINKAGE, {"linkages": []}).passed
    assert m.run_gate(Phase.LINKAGE, {"linkages": [{"id": "LINK_MAIN"}]}).passed


def test_linkage_context_validates_every_linkage():
    from services.agent.pipeline.linkage_integrity import gate_context_from_ledger

    state = {
        "geometry_specs": [{"id": "G1"}],
        "linkages": [
            {"id": "LINK_OK", "reference_edges": []},
            {"id": "LINK_SECOND", "reference_edges": [
                {"from": "G1", "to": "GHOST"},
            ]},
        ],
    }
    ctx = gate_context_from_ledger(state)
    gate = PhaseMachine().run_gate(Phase.LINKAGE, ctx)
    assert not gate.passed
    assert any("LINK_SECOND" in reason and "GHOST" in reason
               for reason in gate.reasons)


def test_linkage_reference_edge_rejects_empty_endpoint():
    from services.agent.pipeline.linkage_integrity import gate_context_from_ledger

    ctx = gate_context_from_ledger({
        "geometry_specs": [{"id": "G1"}],
        "linkages": [{"id": "LINK_EMPTY", "reference_edges": [
            {"from": "", "to": "G1"},
        ]}],
    })
    gate = PhaseMachine().run_gate(Phase.LINKAGE, ctx)
    assert not gate.passed
    assert any("LINK_EMPTY" in reason and ".from" in reason
               for reason in gate.reasons)


def test_buildpath_gate_rejects_dangling_element_ref():
    m = PhaseMachine()
    specs = [{"id": "G1"}]
    # no nodes → fail
    assert not m.run_gate(Phase.BUILDPATH,
                          {"build_nodes": [], "geometry_specs": specs}).passed
    # node references a geometry_spec that doesn't exist → fail
    dangling = [{"id": "BN1", "op": "extrude", "elements": ["G9"]}]
    r = m.run_gate(Phase.BUILDPATH,
                   {"build_nodes": dangling, "geometry_specs": specs})
    assert not r.passed and any("G9" in str(x) for x in r.reasons)
    # clean node → pass
    ok = [{"id": "BN1", "op": "extrude", "elements": ["G1"]}]
    r2 = m.run_gate(Phase.BUILDPATH,
                    {"build_nodes": ok, "geometry_specs": specs})
    assert r2.passed and r2.gate == "build_nodes_present"


# ===========================================================================
# 3. Replan routing matrix (contract §17)
# ===========================================================================

def _replan(scope, **extra):
    req = {"schema": "aicad.replan/1", "id": f"rp_{uuid.uuid4().hex[:6]}",
           "from_role": "feature_modeler", "scope": scope, "reason": "x",
           "urgency": "opportunistic"}
    req.update(extra)
    return req


def test_route_approach_to_orchestrator():
    d = route_replan(_replan("approach"))
    assert d.approvers == ("orchestrator",)
    assert not d.overseer_audit and not d.user_required


def test_route_buildgraph_orchestrator_plus_overseer():
    d = route_replan(_replan("buildgraph"))
    assert d.approvers == ("orchestrator", "overseer")
    assert d.overseer_audit


def test_route_spec_no_criteria_change_overseer_only():
    d = route_replan(_replan("spec", proposal={"spec_changes": [{"note": "reword"}]}))
    assert d.approvers == ("overseer",)
    assert not d.user_required


def test_route_spec_criteria_change_adds_user():
    d = route_replan(_replan("spec", proposal={
        "spec_changes": [{"ac_ref": "AC1", "changes_criteria": True}]}))
    assert d.approvers == ("overseer", "user")
    assert d.user_required


def test_route_requirement_always_user():
    d = route_replan(_replan("requirement"))
    assert d.approvers == ("user",)
    assert d.user_required


def test_route_org_orchestrator_plus_overseer():
    d = route_replan(_replan("org"))
    assert d.approvers == ("orchestrator", "overseer")
    assert d.overseer_audit


def test_invalid_replan_request_rejected():
    assert validate_replan_request({"scope": "bogus"})
    with pytest.raises(ReplanError):
        route_replan(_replan("nonsense"))


def test_apply_replan_denied_returns_reason_and_freezes_blocked_branch():
    req = _replan("approach", urgency="blocked", node_id="n_build_DL1")
    outcome = apply_replan(req, approved=False, denial_reason="not simpler")
    assert not outcome.approved
    assert outcome.returned_reason == "not simpler"
    assert outcome.frozen_branch == "n_build_DL1"
    assert outcome.ledger_event["verdict"] == "denied"


def test_apply_replan_approved_records_ledger_event():
    req = _replan("requirement",
                  proposal={"requirement_changes": [{"text": "add M4 holes"}]})
    outcome = apply_replan(req, approved=True)
    assert outcome.approved
    assert outcome.ledger_event["verdict"] == "approved"
    assert outcome.applied["kind"] == "requirement_change"


def test_apply_replan_dag_failure_is_not_an_approved_verdict(monkeypatch):
    from services.dag import replan as dag_replan_module

    def broken_replan(**_kwargs):
        raise RuntimeError("splice service unavailable")

    monkeypatch.setattr(dag_replan_module, "dag_replan", broken_replan)
    req = _replan("buildgraph", proposal={
        "buildgraph_changes": [{
            "op": "add",
            "node": {"id": "n_recovery", "role": "fixer"},
        }],
    })
    outcome = apply_replan(
        req,
        approved=True,
        run_id="run_broken",
        after_node_id="n_build",
    )
    assert not outcome.approved
    assert outcome.outcome_kind == "internal_error"
    assert outcome.ledger_event["verdict"] == "apply_failed"
    assert outcome.ledger_event["verdict"] != "approved"


def test_apply_replan_spec_impact_failure_is_not_approved(monkeypatch):
    from services.agent import derivation

    class RecordingLedger:
        def __init__(self):
            self.events = []

        def append(self, actor, kind, payload):
            self.events.append((actor, kind, payload))

    def broken_impact(*_args, **_kwargs):
        raise ConnectionError("derivation state unavailable")

    monkeypatch.setattr(derivation, "mark_faulty", broken_impact)
    ledger = RecordingLedger()
    req = _replan("spec", proposal={
        "spec_changes": [{"dim_ref": "D1", "why": "fit correction"}],
    })
    outcome = apply_replan(req, approved=True, ledger=ledger)
    assert not outcome.approved
    assert outcome.outcome_kind == "internal_error"
    assert outcome.ledger_event["verdict"] == "apply_failed"
    assert all(event[2]["verdict"] != "approved" for event in ledger.events)


# ===========================================================================
# 4. Mission DAG template — role assignment + fan-out
# ===========================================================================

def test_mission_graph_carries_roles_verbatim():
    g = build_mission_graph(_multi_manifest())
    intake = next(n for n in g["nodes"] if n["id"] == "n_intake")
    assert intake["inputs"]["role"] == "intake_analyst"
    # role_prompt is loaded verbatim from the card (non-empty for a real role).
    assert intake["inputs"]["role_prompt"].strip()
    # deliver of the cam_program uses the cam_programmer role.
    cam = next(n for n in g["nodes"] if n["id"] == "n_deliver_DL3")
    assert cam["inputs"]["role"] == "cam_programmer"


def test_mission_graph_fan_out_independent_builds_share_no_edge():
    """Two independent part builds must not depend on each other (parallel-ready)."""
    m = _valid_manifest()
    m["deliverables"].append(
        {"id": "DL9", "kind": "part", "name": "second part",
         "acceptance_criteria": [
             {"id": "AC9", "text": "ok",
              "check": {"tool": "verify_battery", "pass_rule": "PASS"}}],
         "depends_on": []})
    g = build_mission_graph(m)
    build_edges = [(e["from"], e["to"]) for e in g["edges"]
                   if e["to"].startswith("n_build_")]
    # each build depends only on the PLAN node, not on the other build.
    assert ("n_build_DL1", "n_build_DL9") not in build_edges
    assert ("n_build_DL9", "n_build_DL1") not in build_edges
    assert ("n_plan", "n_build_DL1") in build_edges
    assert ("n_plan", "n_build_DL9") in build_edges




def test_mission_graph_validates_against_dag_schema():
    # build_mission_graph calls validate_graph internally; a returned graph is valid.
    g = build_mission_graph(_valid_manifest())
    from services.dag.graph_loader import validate_graph
    validate_graph(g)  # must not raise


# ===========================================================================
# 5. Mock-proven end-to-end dry run through all 8 phases (offline discipline)
# ===========================================================================

def test_end_to_end_all_phases_mock(tmp_path):
    """Drive the REAL phase machine + gates + ledger through every phase on a
    single-part fixture, with mock role outputs.  No all-skip: every gate actually
    runs against constructed durable state, and each phase is a real ledger stage
    event.
    """
    from services.agent.ledger import Ledger

    run_id = f"t_w19_e2e_{uuid.uuid4().hex[:8]}"
    ledger = Ledger.load(run_id, path=tmp_path / "ledger.json")
    # Use org_layer=False for this focused 8-phase dry run (avoids needing a real
    # org chart fixture here; the org-layer e2e is covered in test_org.py).
    machine = PhaseMachine(ledger=ledger, flags=PipelineFlags(org_layer=False))

    manifest = _valid_manifest()

    # Phase 0 INTAKE — manifest produced + validated.
    machine.enter(Phase.INTAKE)
    findings = validate_manifest(manifest)
    assert manifest_valid(findings)
    machine.exit(Phase.INTAKE, machine.run_gate(
        Phase.INTAKE, {"manifest_findings": findings}))

    # No blocking ambiguity → CLARIFY skipped.
    assert machine.next_phase(Phase.INTAKE, {"has_blocking_ambiguity": False}) is Phase.SPECIFY

    # Phase 2 SPECIFY — spec audited, every AC covered.
    machine.enter(Phase.SPECIFY)
    machine.exit(Phase.SPECIFY, machine.run_gate(Phase.SPECIFY, {
        "spec_audited": True, "uncovered_criteria": []}))

    # ORGANIZE skipped (org_layer=False in this test) → PLAN.
    assert machine.next_phase(Phase.SPECIFY, {}) is Phase.PLAN

    # Phase 3 PLAN — build graph valid + ratified.
    machine.enter(Phase.PLAN)
    graph = build_mission_graph(manifest, graph_id=run_id)
    assert graph["nodes"]
    machine.exit(Phase.PLAN, machine.run_gate(Phase.PLAN, {
        "build_graph_blockers": [], "plan_ratified": True}))

    # Phase 4 BUILD — all nodes resolved.
    machine.enter(Phase.BUILD)
    machine.exit(Phase.BUILD, machine.run_gate(Phase.BUILD, {"unresolved_nodes": []}))

    # Phase 5 VERIFY — the single AC passes.
    machine.enter(Phase.VERIFY)
    machine.exit(Phase.VERIFY, machine.run_gate(Phase.VERIFY, {
        "expected_ac_ids": ["AC1"],
        "criterion_results": {"AC1": "PASS"},
        "waived_criteria": [],
    }))

    # No open failure cards → CORRECT skipped → DELIVER.
    assert machine.next_phase(Phase.VERIFY, {"open_failure_cards": []}) is Phase.DELIVER

    # Phase 7 DELIVER — mission report emitted, ledger closed.
    machine.enter(Phase.DELIVER)
    machine.exit(Phase.DELIVER, machine.run_gate(Phase.DELIVER, {
        "mission_report_emitted": True,
        "ledger_closed": True,
    }))
    assert machine.next_phase(Phase.DELIVER, {}) is None  # mission complete

    # The ledger stage_log recorded every phase we passed through, with exit gates.
    reloaded = Ledger.load(run_id, path=tmp_path / "ledger.json")
    stages = [s["stage"] for s in reloaded.state["stage_log"]]
    for expected in ("intake", "specify", "plan", "build", "verify", "deliver"):
        assert expected in stages, f"{expected} missing from stage_log {stages}"
    # every stage we exited carries its exit_gate name (state is durable / replayable).
    exited = [s for s in reloaded.state["stage_log"] if s.get("exited")]
    assert all(s.get("exit_gate") for s in exited)


# ---------------------------------------------------------------------------
# Honest VERIFY gate (reviewer results, not fabricated PASS)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WS0 — SPECIFY soft-join (n_spec_done is advisory)
# ---------------------------------------------------------------------------

