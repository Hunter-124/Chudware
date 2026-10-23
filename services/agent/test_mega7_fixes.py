"""Mega-7 live harness-fix regressions (Fixer7 = HomelessBadger).

Watch-list from mega-6 findings (owner cycle-7):
  2. Delegation regression — mega-5: 2 calls, mega-6: 0; role prompts must still
     prefer delegate and children route cheap (not suppress spawn).
  3. new_part empty solids — add_features must follow; thrash ≠ product success.
  4. live cheap-share 0% — mission nodes must stamp task_class so leaf/verify/
     deliver can auto_pick low-rank models at depth 0.

Item 1 of that list, DELIVER-phase soft-join mortality, is NOT covered here and no longer
can be: its subject `services/agent/loop.py::_drive_phases_from_dag` was deleted
and nothing re-implemented it, so `deliver_partial` has no producer left in the tree. The
`_drive` helper that monkeypatched it was never called — removed 2026-08-05. The
`_valid_manifest` fixture below survives because the task_class test at the bottom of this
file uses it.
"""
from __future__ import annotations

import pytest



# ---------------------------------------------------------------------------
# Shared manifest fixture (used by the task_class stamping test below)
# ---------------------------------------------------------------------------


def _valid_manifest():
    return {
        "request_verbatim": "Design a rocket plane with airframe + avionics.",
        "ambiguities": [],
        "deliverables": [
            {
                "id": "DL1",
                "name": "Airframe",
                "kind": "part",
                "description": "primary structure",
                "acceptance_criteria": [
                    {"id": "AC1", "text": "solid body exists",
                     "check": {"tool": "describe_part", "pass_rule": "bodies>0"}},
                ],
            },
            {
                "id": "DL2",
                "name": "Assembly",
                "kind": "assembly",
                "description": "mated airframe",
                "acceptance_criteria": [
                    {"id": "AC2", "text": "no interference",
                     "check": {"tool": "check_interference",
                               "pass_rule": "clean"}},
                ],
            },
            {
                "id": "DL3",
                "name": "Report",
                "kind": "report",
                "description": "mission report",
                "acceptance_criteria": [],
            },
            {
                "id": "DL4",
                "name": "Drawing pack",
                "kind": "drawing",
                "description": "drawing",
                "acceptance_criteria": [],
            },
            {
                "id": "DL5",
                "name": "CAM note",
                "kind": "report",
                "description": "cam note",
                "acceptance_criteria": [],
            },
        ],
    }


# ---------------------------------------------------------------------------
# 2. Delegation still preferred on role cards (not suppressed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_id",
    [
        "spec_writer",
        "feature_modeler",
        "layout_engineer",
        "orchestrator",
        "department_manager",
        "assembly_fit_engineer",
    ],
)
def test_delegate_roles_still_prefer_delegate(role_id):
    """mega-6 regression: 0 delegate calls — prompts must still require spawn."""
    from services.agent.roles import load_role

    card = load_role(role_id)
    tools = card.concrete_tools
    assert "delegate" in tools, (role_id, tools)
    prompt = card.system_prompt
    assert "delegate" in prompt, role_id
    assert "budget_tokens" in prompt, role_id
    # mega-7: stronger "must spawn" language for multi-slice work
    assert "cheap" in prompt.lower() or "MUST" in prompt or "prefer" in prompt.lower(), (
        role_id, prompt[:200]
    )


def test_feature_modeler_requires_add_features_after_new_part():
    from services.agent.roles import load_role

    card = load_role("feature_modeler")
    p = card.system_prompt
    assert "new_part alone is NOT done" in p or "FIRST tool after" in p
    assert "add_features" in p
    assert "preflight" in p.lower()


# ---------------------------------------------------------------------------
# 3. Empty solid / add_features thrash (rebind + stronger nudge)
# ---------------------------------------------------------------------------


def test_new_part_without_add_features_not_auto_done(tmp_path, monkeypatch):
    """mega-6 BUILD: new_part + preflight thrash must not product-complete."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega7_build_mut", path=tmp_path / "ledger.json")

    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return '```json\n{"tool":"new_part","args":{"name":"p"}}\n```'
            if seq["n"] <= 4:
                return '```json\n{"tool":"preflight_ops","args":{"ops":[]}}\n```'
            return "done thrashing without features"

    def _dispatch(tool, args):
        if tool == "new_part":
            return {"ok": True, "doc_id": "doc-test1", "name": "p"}
        if tool == "preflight_ops":
            return {"ok": True, "findings": []}
        return {"ok": True}

    res = at.run_turn(
        "Build the plate solid",
        _Prov(),
        name="BUILD DL1",
        ledger=ledger,
        allowed_tools=["new_part", "add_features", "preflight_ops", "ledger_get"],
        tool_dispatch=_dispatch,
        max_tool_calls=4,
        phase="build",
    )
    assert res.stop_reason in ("max_iters", "phase_step_cap", "no_code"), res.stop_reason
    if res.success:
        handoff = (res.result or {}).get("handoff") or {}
        assert handoff.get("status") != "done" or res.result.get("bootstrapped_only")
    else:
        assert res.result.get("bootstrapped_only") is True or any(
            c.get("tool") == "new_part" for c in (res.result or {}).get("calls") or []
        )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "new_part" in tools_used
    assert "add_features" not in tools_used


def test_new_part_then_add_features_auto_completes(tmp_path, monkeypatch):
    """Happy path: new_part → add_features is productive BUILD success."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega7_build_ok", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return '```json\n{"tool":"new_part","args":{"name":"p"}}\n```'
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"add_features","args":{"ops":'
                    '[{"op":"box","w":10,"h":10,"d":10}]}}\n```'
                )
            if seq["n"] == 3:
                return (
                    '```json\n{"tool":"verify_battery","args":{"doc_id":"doc-ok"}}\n```'
                )
            return "done thrashing"

    def _dispatch(tool, args):
        if tool == "new_part":
            return {"ok": True, "doc_id": "doc-ok", "name": "p"}
        if tool == "add_features":
            return {"ok": True, "features": ["box1"]}
        return {"ok": True}

    res = at.run_turn(
        "Build the plate solid",
        _Prov(),
        name="BUILD DL1",
        ledger=ledger,
        allowed_tools=["new_part", "add_features", "preflight_ops"],
        tool_dispatch=_dispatch,
        max_tool_calls=3,
        phase="build",
    )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "new_part" in tools_used
    assert "add_features" in tools_used
    assert res.success is True or (
        (res.result or {}).get("handoff") or {}
    ).get("status") == "done"


# ---------------------------------------------------------------------------
# 4. task_class stamp → cheap leaf routing at depth 0
# ---------------------------------------------------------------------------


def test_mission_nodes_stamp_task_class():
    """Every mission template node must carry inputs.task_class for auto_pick."""
    from services.dag.mission_template import build_mission_graph

    graph = build_mission_graph(_valid_manifest(), graph_id="m7_tc", org_layer=True)
    by_id = {n["id"]: n for n in graph["nodes"]}
    for n in graph["nodes"]:
        tc = (n.get("inputs") or {}).get("task_class")
        assert tc, (n["id"], n.get("inputs"))
    # Leaf / report classes for conservation targets
    assert by_id["n_intake"]["inputs"]["task_class"] == "leaf"
    assert by_id["n_verify_DL1"]["inputs"]["task_class"] == "leaf"
    assert by_id["n_deliver_DL1"]["inputs"]["task_class"] == "simple_part"
    assert by_id["n_deliver_DL3"]["inputs"]["task_class"] == "simple_part"
    # Build classes follow deliverable kind
    assert by_id["n_build_DL1"]["inputs"]["task_class"] == "multi_feature_part"
    assert by_id["n_build_DL2"]["inputs"]["task_class"] == "assembly"
    assert by_id["n_spec_DL1"]["inputs"]["task_class"] == "multi_feature_part"


def test_context_pack_promotes_task_class():
    from services.dag.context_pack import assemble

    node = {
        "id": "n_deliver_DL1",
        "type": "report",
        "name": "DELIVER DL1",
        "inputs": {
            "role": "reviewer",
            "task_class": "simple_part",
            "risk": "low",
            "depth": 0,
        },
    }
    pack = assemble(node, {})
    assert pack.get("task_class") == "simple_part"
    assert pack.get("risk") == "low"
    assert pack.get("role_id") == "reviewer"


def test_leaf_task_class_min_rank_is_one():
    from services.agent.role_models import min_rank_for_difficulty

    assert min_rank_for_difficulty(task_class="leaf", node_depth=0) == 1
    assert min_rank_for_difficulty(task_class="simple_part", node_depth=0) == 1
    assert min_rank_for_difficulty(task_class="simple", node_depth=0) == 1
    # risk=low demotes further but floor is 1
    assert min_rank_for_difficulty(
        task_class="leaf", node_depth=0, risk="low"
    ) == 1
    # unknown still hard at depth 0
    assert min_rank_for_difficulty(task_class=None, node_depth=0) == 3


def test_depth0_leaf_auto_pick_not_forced_frontier(monkeypatch):
    """With ollama+xai enabled, depth0 leaf/simple must prefer cheap ollama."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import auto_pick_model, min_rank_for_difficulty

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    # Sanity: leaf min_rank allows rank-1 ollama.
    assert min_rank_for_difficulty(task_class="leaf", node_depth=0) <= 1
    a = auto_pick_model(cfg=cfg, task_class="leaf", node_depth=0, risk="low")
    assert a.provider != "openrouter", a
    # Prefer non-xai when a cheap local is eligible.
    assert a.provider == "ollama" or (
        a.provider == "xai-sub" and a.rank is not None and a.rank >= 1
    ), a
    # Stronger: when min_rank=1, ollama rank1 must win over xai rank5.
    a2 = auto_pick_model(cfg=cfg, task_class="simple_part", node_depth=0)
    assert a2.provider == "ollama", a2


def test_role_aliases_part_designer_and_edit_part():
    """mega-7 attempt-2 spawned part_designer / edit-part — must map to geometry."""
    from services.agent.mission_dispatch import MissionDispatch

    assert MissionDispatch._ROLE_ALIASES["part_designer"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["edit-part"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["edit_part"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["part_fixer"] == "feature_modeler"
