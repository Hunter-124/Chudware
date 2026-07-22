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
