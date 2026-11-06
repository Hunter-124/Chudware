"""Mega-7 live harness-fix regressions (Fixer7 = HomelessBadger).

Watch-list from mega-6 findings (owner cycle-7):
  3. new_part empty solids — add_features must follow; thrash ≠ product success.
  4. mission nodes must stamp task_class so leaf/verify/deliver carry their class.

Item 1 of that list, DELIVER-phase soft-join mortality, is NOT covered here and no longer
can be: its subject `services/agent/loop.py::_drive_phases_from_dag` was deleted
and nothing re-implemented it, so `deliver_partial` has no producer left in the tree. The
`_drive` helper that monkeypatched it was never called — removed 2026-08-05. The
`_valid_manifest` fixture below survives because the task_class test at the bottom of this
file uses it.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Shared manifest fixture (used by the task_class stamping test below)
# ---------------------------------------------------------------------------


def _valid_manifest():
    return {
        "request_verbatim": "Design a vehicle chassis with a controller board.",
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
    from services.agent import guardrails

    monkeypatch.setattr(guardrails, "_doc_tree", lambda *a, **kw: {"features": []})
    monkeypatch.setattr(guardrails, "_engine_body_count", lambda doc_id: 1)

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
                    '```json\n{"tool":"add_features","args":{"doc_id":"doc-ok","ops":'
                    '[{"op":"add_feature","feature":{"type":"box","name":"box1",'
                    '"params":{"length":10,"width":10,"height":10}}}]}}\n```'
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
        if tool == "verify_battery":
            return {"ok": True, "verdict": "pass",
                    "coverage": {"ran": ["is_manifold"]},
                    "scores": {"is_manifold": 1.0}, "findings": []}
        return {"ok": True}

    res = at.run_turn(
        "Build the plate solid",
        _Prov(),
        name="BUILD DL1",
        ledger=ledger,
        allowed_tools=["new_part", "add_features", "preflight_ops", "verify_battery"],
        tool_dispatch=_dispatch,
        max_tool_calls=3,
        phase="build",
    )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "new_part" in tools_used
    assert "add_features" in tools_used
    assert "verify_battery" in tools_used
    assert res.success is True, (res.stop_reason, res.result)
    assert res.result["handoff"]["status"] == "done"


# ---------------------------------------------------------------------------
# 4. task_class stamp → cheap leaf routing at depth 0
# ---------------------------------------------------------------------------


def test_mission_nodes_stamp_task_class():
    """Every mission template node must carry inputs.task_class."""
    from services.dag.mission_template import build_mission_graph

    graph = build_mission_graph(_valid_manifest(), graph_id="m7_tc")
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


def test_role_aliases_part_designer_and_edit_part():
    """mega-7 attempt-2 spawned part_designer / edit-part — must map to geometry."""
    from services.agent.mission_dispatch import MissionDispatch

    assert MissionDispatch._ROLE_ALIASES["part_designer"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["edit-part"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["edit_part"] == "feature_modeler"
    assert MissionDispatch._ROLE_ALIASES["part_fixer"] == "feature_modeler"
