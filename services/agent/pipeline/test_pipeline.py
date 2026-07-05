"""test_pipeline.py — T-W19 mission pipeline glue.

Covers, per the task DoD:
  * manifest validator rule-by-rule (a fixture per contract §16 rule);
  * phase gate enforcement (cannot enter BUILD with an unratified plan);
  * replan routing matrix (§17, incl. criteria-change → user, scope=org);
  * a mock-proven end-to-end dry run through all 8 phases on a single-part fixture
    (offline discipline: the mock exercises the REAL phase machine + gates + ledger,
    no all-skip).

Run with the gate venv (has yaml):
    ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest \
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


def test_plan_gate_blocks_when_not_ratified():
    machine = PhaseMachine()
    gate = machine.run_gate(Phase.PLAN, {
        "build_graph_blockers": [], "plan_ratified": False})
    assert not gate.passed
    assert any("ratif" in r for r in gate.reasons)


def test_cannot_enter_build_from_unratified_plan():
    """The load-bearing DoD case: BUILD is unreachable while PLAN's gate fails."""
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
    ctx = {"criterion_results": {"AC1": "FAIL"}, "waived_criteria": []}
    assert not machine.run_gate(Phase.VERIFY, ctx).passed
    ctx["waived_criteria"] = ["AC1"]
    assert machine.run_gate(Phase.VERIFY, ctx).passed


def test_clarify_skipped_without_blocking_ambiguity():
    machine = PhaseMachine()
    nxt = machine.next_phase(Phase.INTAKE, {"has_blocking_ambiguity": False})
    assert nxt is Phase.SPECIFY  # CLARIFY skipped
    nxt2 = machine.next_phase(Phase.INTAKE, {"has_blocking_ambiguity": True})
    assert nxt2 is Phase.CLARIFY


def test_organize_skipped_unless_flag_on():
    off = PhaseMachine(flags=PipelineFlags(org_layer=False))
    assert off.next_phase(Phase.SPECIFY, {}) is Phase.PLAN  # ORGANIZE skipped
    on = PhaseMachine(flags=PipelineFlags(org_layer=True))
    assert on.next_phase(Phase.SPECIFY, {}) is Phase.ORGANIZE


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
    machine.current = Phase.CORRECT  # simulate being in CORRECT
    # re-entering BUILD from CORRECT is the documented loop
    machine.enter(Phase.BUILD)
    assert machine.current is Phase.BUILD


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
    machine = PhaseMachine(ledger=ledger, flags=PipelineFlags())

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

    # ORGANIZE skipped (flag off) → PLAN.
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
        "criterion_results": {"AC1": "PASS"}, "waived_criteria": []}))

    # No open failure cards → CORRECT skipped → DELIVER.
    assert machine.next_phase(Phase.VERIFY, {"open_failure_cards": []}) is Phase.DELIVER

    # Phase 7 DELIVER — mission report emitted, ledger closed.
    machine.enter(Phase.DELIVER)
    machine.exit(Phase.DELIVER, machine.run_gate(Phase.DELIVER, {
        "mission_report_emitted": True}))
    assert machine.next_phase(Phase.DELIVER, {}) is None  # mission complete

    # The ledger stage_log recorded every phase we passed through, with exit gates.
    reloaded = Ledger.load(run_id, path=tmp_path / "ledger.json")
    stages = [s["stage"] for s in reloaded.state["stage_log"]]
    for expected in ("intake", "specify", "plan", "build", "verify", "deliver"):
        assert expected in stages, f"{expected} missing from stage_log {stages}"
    # every stage we exited carries its exit_gate name (state is durable / replayable).
    exited = [s for s in reloaded.state["stage_log"] if s.get("exited")]
    assert all(s.get("exit_gate") for s in exited)


def test_run_mission_blocks_on_invalid_manifest(tmp_path, monkeypatch):
    """run_mission (loop.py front door) blocks at INTAKE when intake yields an
    invalid manifest, bouncing once then escalating — never fabricating a graph."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.providers.mock_provider import MockProvider

    bad = _valid_manifest()
    bad["deliverables"][0]["acceptance_criteria"] = []  # invalid: no criterion

    result = loop.run_mission(
        "make a bracket", MockProvider([]), name=f"t_w19_block_{uuid.uuid4().hex[:6]}",
        intake_fn=lambda req: copy.deepcopy(bad), execute_dag=False)
    assert result.status == "blocked"
    assert result.stop_reason == "manifest_invalid_escalated"
    assert result.graph is None  # no mission graph fabricated from a bad manifest


def test_run_mission_plans_valid_manifest(tmp_path, monkeypatch):
    """A valid manifest drives INTAKE→PLAN and compiles the role-assigned graph
    (execute_dag=False stops before the DAG runs — the planning half is deterministic)."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.providers.mock_provider import MockProvider

    good = _valid_manifest()
    result = loop.run_mission(
        "make a bracket", MockProvider([]), name=f"t_w19_plan_{uuid.uuid4().hex[:6]}",
        intake_fn=lambda req: copy.deepcopy(good), execute_dag=False)
    assert result.status == "running"
    assert result.phase == "plan"
    assert "intake" in result.phases_completed
    assert result.graph is not None and result.graph["nodes"]
    # the intake node carries the intake_analyst role prompt verbatim.
    intake = next(n for n in result.graph["nodes"] if n["id"] == "n_intake")
    assert intake["inputs"]["role"] == "intake_analyst"


def test_run_mission_execute_dag_records_all_phases_in_ledger(monkeypatch):
    """The load-bearing integration case (advisor-flagged): run_mission with
    execute_dag=True drives EVERY phase onto the ledger stage_log, so a real
    mission_status shows all 8 phases — not just intake.

    A mock agent_fn drives every DAG node to done; the phase machine then advances
    in lockstep and mission_status reads the full stage_log from the ledger.
    """
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.providers.mock_provider import MockProvider

    class _DoneResult:
        stop_reason = "confirmed"
        success = True
        final_code = None
        result = None

    def _mock_agent(request, context_pack):
        return _DoneResult()

    run_id = f"t_w19_dag_{uuid.uuid4().hex[:8]}"
    good = _valid_manifest()
    result = loop.run_mission(
        "make a bracket", MockProvider([]), name=run_id,
        intake_fn=lambda req: copy.deepcopy(good),
        execute_dag=True, agent_fn=_mock_agent,
        autonomy_default="auto")  # auto so gate nodes don't pause the DAG

    assert result.status == "done", (result.status, result.stop_reason)
    assert result.phase == "deliver"
    # Every phase reached the ledger stage_log — this is what mission_status renders.
    from services.agent.ledger import Ledger
    stages = [s["stage"] for s in Ledger.load(run_id).state["stage_log"]]
    for expected in ("intake", "specify", "plan", "build", "verify", "deliver"):
        assert expected in stages, f"{expected} missing from live stage_log {stages}"
    # phases_completed (the MissionRun handle) matches.
    for expected in ("intake", "specify", "plan", "build", "verify", "deliver"):
        assert expected in result.phases_completed


def test_mission_status_tool_renders_all_phases(monkeypatch):
    """The mission_status MCP tool renders the full phase progression from the
    ledger after a completed run_mission (the GUI / mission-control surface)."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.agent import loop
    from services.agent.providers.mock_provider import MockProvider
    from services.mcp import mission_tools

    class _DoneResult:
        stop_reason = "confirmed"; success = True; final_code = None; result = None

    run_id = f"t_w19_status_{uuid.uuid4().hex[:8]}"
    loop.run_mission(
        "make a bracket", MockProvider([]), name=run_id,
        intake_fn=lambda req: copy.deepcopy(_valid_manifest()),
        execute_dag=True, agent_fn=lambda r, c: _DoneResult(),
        autonomy_default="auto")

    # Capture the registered mission_status closure and call it.
    captured = {}

    class _Mcp:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    mission_tools.register_mission_tools(_Mcp())
    status = captured["mission_status"](run_id)
    assert status["ok"]
    for expected in ("intake", "specify", "plan", "build", "verify", "deliver"):
        assert expected in status["phases_complete"], status["phases_complete"]
    assert status["current_phase"] is None  # deliver exited → nothing open
