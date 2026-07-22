"""Mega-4 live harness-fix regressions."""
from __future__ import annotations


def test_feature_modeler_can_bootstrap_new_part():
    """BUILD must be able to create a part before describe/add_features."""
    from services.agent.roles import MAX_TOOLS_PER_ROLE, load_role

    card = load_role("feature_modeler")
    assert "new_part" in card.concrete_tools
    assert "new_part" in card.system_prompt
    assert "doc_id" in card.system_prompt
    assert len(card.tool_allowlist) <= MAX_TOOLS_PER_ROLE

def test_parse_xai_textual_tool_call_rendering():
    """Mega-4 n_spec_done text tool calls must enter the normal dispatch loop."""
    from services.agent.agent_turn import _parse_directive, run_turn

    reply = (
        "I'll audit the specs. ethan tool call: ledger_get with section is null "
        "budget_tokens is 2000 ethan tool call: buildgraph_status with run_id is current"
    )
    assert _parse_directive(reply) == {
        "tool": "ledger_get",
        "args": {"section": None, "budget_tokens": 2000},
    }

    class _Provider:
        name = "xai-text-fixture"

        def __init__(self):
            self.replies = [
                reply,
                '{"done": true, "handoff": {"status": "done", "summary": "audited"}}',
            ]

        def complete(self, system, messages, max_tokens=8000):
            return self.replies.pop(0)

    calls = []
    result = run_turn(
        "audit",
        _Provider(),
        allowed_tools=["ledger_get"],
        tool_dispatch=lambda tool, args: calls.append((tool, args)) or {"ok": True},
        max_tool_calls=2,
    )
    assert result.success
    assert calls == [("ledger_get", {"section": None, "budget_tokens": 2000})]



def test_live_ledger_aliases_and_criterion_result_section(tmp_path):
    """Mega-4 ledger vocabulary must resolve to canonical state without loss."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    ledger = Ledger("mega4_aliases", tmp_path / "ledger.json")
    dispatch = MissionDispatch(run_id=ledger.run_id, ledger=ledger)

    assumption = dispatch.ledger_note(
        kind="assumption",
        payload={
            "name": "fc_board_envelope",
            "value": "80 x 50 x 15 mm",
            "driver": "mission components",
            "needs_confirm": True,
        },
    )
    assert assumption["ok"] and assumption["kind"] == "decision"
    assert ledger.state["decisions"][0]["id"] == "fc_board_envelope"

    verification = dispatch.ledger_note(
        kind="verification",
        payload={"deliverable": "DL5", "verdict_overall": "FAIL"},
    )
    assert verification["ok"] and verification["kind"] == "artifact"
    assert ledger.state["artifacts"][0]["id"] == "DL5"
    assert ledger.state["artifacts"][0]["kind"] == "verification"

    assert dispatch.ledger_note(
        kind="criterion_result",
        payload={"id": "AC9", "verdict": "FAIL"},
    )["ok"]
    criterion = dispatch.ledger_get(section="criterion_result")
    assert criterion["ok"] and criterion["section"] == "criterion_results"
    assert criterion["data"] == [{"id": "AC9", "verdict": "FAIL"}]
    verification_section = dispatch.ledger_get(section="verification")
    assert verification_section["ok"]
    assert verification_section["data"] == criterion["data"]

    mission_brief = dispatch.ledger_get(section="mission_brief")
    assert mission_brief["ok"] and mission_brief["section"] == "brief"


def test_failed_plan_and_build_are_reconciled_but_not_completed(monkeypatch):
    """Mega-4 failed nodes must not receive fabricated successful exit gates."""
    # The mission driver's terminal mech/ecad product forces inspect AMBIENT
    # engine state (open docs from earlier tests) — order-dependent in a full
    # suite run. This test pins phase-reconciliation semantics only, so stub
    # the forces out (they have their own coverage elsewhere).
    monkeypatch.setattr(
        "services.agent.agent_turn.mech_deliver_gate",
        lambda *a, **k: {"allow_deliver": True, "had_mech": True,
                         "forced": False, "ok": True, "refusal": None,
                         "doc_id": None, "calls": []})
    monkeypatch.setattr(
        "services.agent.agent_turn.ecad_product_gate",
        lambda *a, **k: {"forced": False})
    from services.agent import loop
    from services.agent.pipeline.phases import Phase, PhaseMachine, PipelineFlags

    manifest = {
        "schema": "aicad.deliverables/1",
        "request": "mission",
        "deliverables": [{
            "id": "DL1",
            "name": "part",
            "kind": "part",
            "acceptance_criteria": [{
                "id": "AC1",
                "text": "exists",
                "check": {"tool": "describe_part", "params": {}},
            }],
        }],
    }
    graph = {"nodes": [
        {"id": "n_spec_DL1"},
        {"id": "n_spec_done"},
        {"id": "n_plan"},
        {"id": "n_build_DL1"},
        {"id": "n_verify_DL1"},
        {"id": "n_deliver_DL1"},
    ]}
    states = {
        "n_spec_DL1": {"status": "done"},
        "n_spec_done": {"status": "done"},
        "n_plan": {"status": "failed"},
        "n_build_DL1": {"status": "failed"},
        "n_verify_DL1": {"status": "done"},
        "n_deliver_DL1": {"status": "done"},
    }
    machine = PhaseMachine(flags=PipelineFlags(org_layer=False))
    machine.enter(Phase.INTAKE)
    machine.exit(
        Phase.INTAKE,
        machine.run_gate(Phase.INTAKE, {"manifest_findings": []}),
    )
    completed = ["intake"]
    reached, status, stop_reason = loop._drive_phases_from_dag(
        machine,
        manifest,
        graph,
        {"status": "failed", "nodes": states},
        completed,
        lambda *_: None,
        Phase,
        ledger=None,
    )
    assert reached == "correct"
    assert status == "needs_correction"
    assert stop_reason.startswith("verify_failed:")
    assert "specify" in completed
    assert "plan" not in completed
    assert "build" not in completed


def test_delegating_roles_use_dispatch_budget_tokens_contract():
    """Role guidance must name the same kwarg exposed by MissionDispatch."""
    from services.agent.roles import load_role

    for role_id in ("orchestrator", "spec_writer"):
        card = load_role(role_id)
        assert "delegate" in card.concrete_tools
        assert "delegate(role, brief, budget_tokens)" in card.system_prompt
        assert "delegate(role, brief, budget)" not in card.system_prompt


def test_buildgraph_plan_accepts_live_compact_buildgraph_shape():
    """Mega-3/4 planner output with format/title/deps must not become nodes=[]."""
    from services.agent.buildgraph.planner import build_graph

    payload = {
        "format": "aicad.buildgraph/1",
        "mission": "mega-4-fc-rocket-20260714",
        "nodes": [
            {
                "id": "N01_architecture",
                "role": "systems_architect",
                "title": "PLAN architecture interfaces budgets",
                "deps": [],
            },
            {
                "id": "N02_source_ics",
                "role": "sourcing_engineer",
                "title": "Source ICs connectors passives",
                "deps": ["N01_architecture"],
            },
            {
                "id": "N03_report",
                "role": "reviewer",
                "title": "Mission report",
                "deps": [],
            },
        ],
        "edges": [
            {"from": "N01_architecture", "to": "N02_source_ics"},
            {"from": "N02_source_ics", "to": "N03_report"},
        ],
    }

    graph, advisories = build_graph(payload, mission="mega-4-fc-rocket-20260714")

    assert graph["schema"] == "aicad.buildgraph/1"
    assert len(graph["nodes"]) == 3
    by_id = {node["id"]: node for node in graph["nodes"]}
    assert by_id["N01_architecture"]["label"] == "PLAN architecture interfaces budgets"
    assert by_id["N02_source_ics"]["requires"] == ["N01_architecture"]
    assert by_id["N02_source_ics"]["assigned"] == "sourcing_engineer"
    assert by_id["N03_report"]["requires"] == ["N02_source_ics"]
    checks = {
        item["check"]
        for advisory in advisories
        for item in advisory.get("items", [])
    }
    assert "unknown_plan_shape" not in checks
    assert "empty_graph" not in checks
