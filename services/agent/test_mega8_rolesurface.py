"""Mega-8 RoleSurface regressions (FitGecko).

Covers mega-7 TOP-5 items owned by RoleSurface:
  1. Capability separation — PLAN cards lose leaf exec tools (add_features).
  2. Typed-refusal soft gate — free-form ledger_note does NOT satisfy;
     valid {reason, domains_considered, why_flat} does; delegate call does.
  3. Cheap-leaf one-shot envelope — validates, falls back, increments leaf_miss.
  4. Clean terminal status — soft-join deliver → done|blocked + stop_reason
     deliver_partial (never running + empty).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Capability separation (advisor Q2 primary)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_id",
    ["orchestrator", "layout_engineer", "department_manager"],
)
def test_plan_roles_have_no_add_features(role_id):
    """PLAN / multi-domain coordinator cards must not expose leaf exec tools."""
    from services.agent.roles import load_role

    card = load_role(role_id)
    tools = card.concrete_tools
    assert "add_features" not in tools, (role_id, tools)
    assert "new_part" not in tools, (role_id, tools)
    assert "preflight_ops" not in tools, (role_id, tools)
    # Still able to coordinate.
    assert "delegate" in tools, (role_id, tools)
    assert "ledger_note" in tools or "ledger_get" in tools, (role_id, tools)


def test_leaf_worker_feature_modeler_keeps_exec_tools():
    """Leaf/worker BUILD role keeps geometry mutators."""
    from services.agent.roles import load_role

    card = load_role("feature_modeler")
    tools = card.concrete_tools
    assert "add_features" in tools
    assert "new_part" in tools
    assert "delegate" in tools


def test_plan_role_tools_slot_resolves_without_add_features():
    """Acceptance: PLAN role card resolves WITHOUT add_features in tools_slot."""
    from services.agent.roles import load_role

    for rid in ("orchestrator", "layout_engineer"):
        card = load_role(rid)
        assert "add_features" not in card.concrete_tools
        # tools_slot would render concrete_tools; assert the source of truth.
        names = [t.tool for t in card.tool_allowlist if t.tool]
        assert "add_features" not in names
        assert "delegate" in names


# ---------------------------------------------------------------------------
# 2. Typed refusal schema + soft gate
# ---------------------------------------------------------------------------


def test_typed_refusal_schema_strict():
    from services.agent.role_models import (
        extract_typed_refusal,
        validate_typed_refusal,
    )

    ok = {
        "reason": "single integrated solid",
        "domains_considered": ["geometry", "assembly"],
        "why_flat": "interfaces share one doc; spawn would thrash mates",
    }
    assert validate_typed_refusal(ok) == []
    assert extract_typed_refusal({"typed_refusal": ok}) == {
        "reason": ok["reason"],
        "domains_considered": ["geometry", "assembly"],
        "why_flat": ok["why_flat"],
    }

    # Free-form ledger-ish note does NOT pass.
    free = {"kind": "decision", "text": "will do it flat", "id": "x"}
    assert validate_typed_refusal(free)
    assert extract_typed_refusal({"handoff": {"outputs": free}}) is None

    # Missing keys.
    assert validate_typed_refusal({"reason": "x", "why_flat": "y"})
    assert validate_typed_refusal({
        "reason": "x",
        "domains_considered": [],
        "why_flat": "y",
    })
    assert validate_typed_refusal("not an object")


def test_typed_refusal_gate_blocks_free_form():
    """Multi-domain PLAN auto-complete blocks without delegate or typed refusal."""
    from services.dag import node_runner as nr

    node = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "domain_tags": ["geometry", "assembly", "cam"],
            "depth": 0,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "multi_feature_part",
        "domain_tags": ["geometry", "assembly", "cam"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "auto_complete": True,
            "calls": [
                {"tool": "ledger_note", "ok": True},
                {"tool": "buildgraph_plan", "ok": True},
            ],
            "handoff": {
                "status": "done",
                "summary": "planned flat",
                "outputs": {"note": "doing it myself"},
            },
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    # Soft gate → retry (or escalate if budget 0); not clean done.
    assert verdict != nr.VERDICT_DONE, (verdict, out)
    assert out["data"]["stop_reason"] == "delegate_or_typed_refusal_required"
    gate = (out["data"].get("agent_result") or {}).get("delegate_gate") or {}
    assert gate.get("blocked") is True
    assert gate.get("reason") == "missing_delegate_or_typed_refusal"


def test_typed_refusal_gate_accepts_valid_envelope():
    from services.dag import node_runner as nr

    node = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "domain_tags": ["geometry", "assembly"],
            "depth": 0,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "multi_feature_part",
        "domain_tags": ["geometry", "assembly"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }
    refusal = {
        "reason": "single body",
        "domains_considered": ["geometry", "assembly"],
        "why_flat": "shared datum tree; children would race the same doc",
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "calls": [{"tool": "ledger_note", "ok": True}],
            "handoff": {
                "status": "done",
                "summary": "flat with typed refusal",
                "outputs": {"typed_refusal": refusal},
            },
            "typed_refusal": refusal,
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)


def test_typed_refusal_gate_accepts_delegate_call():
    from services.dag import node_runner as nr

    node = {
        "id": "n_build_DL1",
        "type": "design",
        "name": "BUILD DL1",
        "role": "feature_modeler",
        "inputs": {
            "role": "feature_modeler",
            "task_class": "assembly",
            "domain_tags": ["geometry", "assembly"],
            "depth": 0,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "feature_modeler",
        "task_class": "assembly",
        "domain_tags": ["geometry", "assembly"],
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "calls": [
                {"tool": "delegate", "ok": True},
                {"tool": "ledger_note", "ok": True},
            ],
            "handoff": {"status": "done", "summary": "spawned child"},
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)


def test_typed_refusal_gate_skips_at_max_depth():
    from services.dag import node_runner as nr

    node = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN",
        "role": "orchestrator",
        "inputs": {
            "role": "orchestrator",
            "task_class": "multi_feature_part",
            "domain_tags": ["geometry", "assembly"],
            "depth": 8,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "orchestrator",
        "task_class": "multi_feature_part",
        "domain_tags": ["geometry", "assembly"],
        "depth": 8,
        "capability_letter": "C",  # max 8
        "retry_remaining": 2,
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {"calls": [], "handoff": {"status": "done"}}

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)


# ---------------------------------------------------------------------------
# 3. Cheap-leaf one-shot envelope
# ---------------------------------------------------------------------------


def test_leaf_envelope_validates():
    from services.agent.role_models import (
        parse_leaf_envelope_text,
        validate_leaf_envelope,
    )

    ok_ops = {
        "goal": "summarise DL1 deliverable",
        "ops": [{"op": "note", "text": "built"}],
        "artifacts": [{"kind": "report", "id": "R1"}],
    }
    assert validate_leaf_envelope(ok_ops) == []

    ok_noop = {
        "goal": "intake confirm",
        "no_op": True,
        "reason": "manifest already complete",
    }
    assert validate_leaf_envelope(ok_noop) == []

    # Missing goal / neither ops nor no_op.
    assert validate_leaf_envelope({"ops": [{"a": 1}]})
    assert validate_leaf_envelope({"goal": "x"})
    assert validate_leaf_envelope({
        "goal": "x", "no_op": True,  # missing reason
    })
    assert validate_leaf_envelope({
        "goal": "x", "no_op": True, "reason": "r", "ops": [{"a": 1}],
    })

    text = '```json\n{"goal":"g","no_op":true,"reason":"already done"}\n```'
    env, errs = parse_leaf_envelope_text(text)
    assert errs == []
    assert env["goal"] == "g"


def test_leaf_oneshot_success_skips_tool_loop(monkeypatch):
    from services.agent import role_models as rm
    from services.dag import node_runner as nr

    rm.reset_leaf_miss_count()
    monkeypatch.setenv("AI_CAD_LEAF_ONESHOT", "1")

    class _Prov:
        name = "ollama"
        model = "llama3.2:latest"

        def complete(self, system, messages, max_tokens=800):
            return (
                '{"goal":"verify leaf","no_op":true,'
                '"reason":"nothing to check"}'
            )

    node = {
        "id": "n_verify_DL1",
        "type": "verify",
        "name": "VERIFY DL1",
        "inputs": {"role": "reviewer", "task_class": "leaf", "depth": 0},
    }
    pack = {
        "node_inputs": node["inputs"],
        "task_class": "leaf",
        "depth": 0,
        "role_model": {
            "provider": "ollama",
            "model": "llama3.2:latest",
            "cost_tier": 0,
        },
        "retry_remaining": 1,
        "node_name": "VERIFY DL1",
    }

    # Inject provider by short-circuiting _resolve_provider.
    monkeypatch.setattr(nr, "_resolve_provider", lambda cp, p=None: _Prov())
    # Ensure agent_turn.run_turn is NOT called.
    called = {"turn": 0}

    def _boom(*a, **k):
        called["turn"] += 1
        raise AssertionError("tool loop should not run on oneshot success")

    monkeypatch.setattr("services.agent.agent_turn.run_turn", _boom)

    verdict, out = nr.run(node, pack, agent_fn=None)
    # make_default path uses _default_agent_fn when agent_fn is None — but
    # run() uses agent_fn or _default_agent_fn. agent_fn=None → _default_agent_fn.
    assert called["turn"] == 0
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert (out["data"].get("agent_result") or {}).get("one_shot") is True
    assert rm.leaf_miss_count() == 0


def test_leaf_oneshot_miss_increments_and_falls_back(monkeypatch):
    from services.agent import role_models as rm
    from services.dag import node_runner as nr

    rm.reset_leaf_miss_count()
    monkeypatch.setenv("AI_CAD_LEAF_ONESHOT", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    class _BadProv:
        name = "ollama"
        model = "llama3.2:latest"

        def complete(self, system, messages, max_tokens=800):
            return "sorry I cannot fill forms, just prose forever"

    class _FallbackResult:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {"handoff": {"status": "done"}, "calls": [], "fallback": True}

    node = {
        "id": "n_intake",
        "type": "plan",
        "name": "INTAKE",
        "inputs": {"role": "intake_analyst", "task_class": "leaf", "depth": 0},
    }
    pack = {
        "node_inputs": node["inputs"],
        "task_class": "leaf",
        "depth": 0,
        "role_model": {
            "provider": "ollama",
            "model": "llama3.2:latest",
            "cost_tier": 0,
        },
        "retry_remaining": 1,
        "node_name": "INTAKE",
    }

    monkeypatch.setattr(nr, "_resolve_provider", lambda cp, p=None: _BadProv())

    # Patch at the module attribute the oneshot uses AND the tool-loop entry.
    import services.agent.agent_turn as at

    def _turn(*args, **kwargs):
        return _FallbackResult()

    monkeypatch.setattr(at, "run_turn", _turn)
    monkeypatch.setattr(nr, "_frontier_fallback_provider", lambda cp, inj=None: _BadProv())

    # Drive the oneshot + fallback path directly for the miss counter, then
    # confirm run() still produces a terminal verdict after miss.
    miss_before = rm.leaf_miss_count()
    oneshot = nr._maybe_run_cheap_leaf_oneshot(
        "confirm intake", pack, _BadProv(), node_name="INTAKE",
    )
    assert oneshot is None
    assert rm.leaf_miss_count() > miss_before
    assert pack.get("leaf_oneshot_miss") is True

    # Full node run after miss: tool loop (stubbed) must confirm.
    # Seed miss flag so frontier fallback path is exercised.
    pack["leaf_oneshot_miss"] = True
    # Force oneshot to return None immediately on second call.
    monkeypatch.setattr(
        nr, "_maybe_run_cheap_leaf_oneshot",
        lambda *a, **k: None,
    )
    verdict, out = nr.run(node, pack, agent_fn=None)
    assert verdict == nr.VERDICT_DONE, (verdict, out)


# ---------------------------------------------------------------------------
# 4. Clean terminal status (soft-join deliver)
# ---------------------------------------------------------------------------


def _drive(manifest, graph, dag_result, completed=None):
    from services.agent import loop

    completed = list(completed or [])
    events = []

    def _emit(phase, text):
        events.append((getattr(phase, "value", phase), text))

    class Phase:
        PLAN = type("P", (), {"value": "plan"})()
        SPECIFY = type("P", (), {"value": "specify"})()
        BUILD = type("P", (), {"value": "build"})()
        VERIFY = type("P", (), {"value": "verify"})()
        DELIVER = type("P", (), {"value": "deliver"})()
        CORRECT = type("P", (), {"value": "correct"})()

        def __init__(self, v):
            self.value = v

        def __eq__(self, other):
            return getattr(other, "value", other) == self.value

    # Re-bind Phase enum used inside loop to simple stand-ins via the real one.
    from services.agent.pipeline.phases import Phase as RealPhase
    from services.agent.pipeline.phases import PhaseMachine

    machine = PhaseMachine()
    reached, status, stop_reason = loop._drive_phases_from_dag(
        machine, manifest, graph, dag_result, completed, _emit, RealPhase,
        ledger=None,
    )
    return reached, status, stop_reason, completed, events


def test_deliver_soft_join_terminal_status_not_running(monkeypatch):
    """Soft-join deliver lands status=done|blocked with stop_reason deliver_partial."""
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.dag.mission_template import build_mission_graph

    manifest = {
        "request_verbatim": "Design a rocket plane.",
        "ambiguities": [],
        "deliverables": [
            {"id": "DL1", "name": "Airframe", "kind": "part",
             "description": "primary", "acceptance_criteria": []},
            {"id": "DL2", "name": "Assembly", "kind": "assembly",
             "description": "mate", "acceptance_criteria": []},
            {"id": "DL3", "name": "Report", "kind": "report",
             "description": "report", "acceptance_criteria": []},
            {"id": "DL4", "name": "Drawing", "kind": "drawing",
             "description": "drawing", "acceptance_criteria": []},
            {"id": "DL5", "name": "Note", "kind": "report",
             "description": "note", "acceptance_criteria": []},
        ],
    }
    graph = build_mission_graph(manifest, graph_id="m8_deliver", org_layer=False)
    node_states = {}
    for n in graph["nodes"]:
        nid = n["id"]
        if nid.startswith("n_deliver_"):
            if nid.endswith("DL2"):
                node_states[nid] = {"status": "done", "output": {"status": "done"}}
            else:
                node_states[nid] = {"status": "failed", "output": {"status": "failed"}}
        else:
            node_states[nid] = {"status": "done", "output": {"status": "done"}}
    dag_result = {"status": "paused", "nodes": node_states}

    reached, status, stop_reason, completed, _ = _drive(manifest, graph, dag_result)
    assert "deliver" in completed, (completed, reached, stop_reason, status)
    assert status in ("done", "blocked"), (status, stop_reason, reached)
    assert status != "running", (status, stop_reason)
    assert stop_reason == "deliver_partial", (status, stop_reason, reached)
    assert stop_reason != ""


def test_deliver_all_done_mission_complete(monkeypatch):
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    from services.dag.mission_template import build_mission_graph

    manifest = {
        "request_verbatim": "Simple bracket.",
        "ambiguities": [],
        "deliverables": [
            {"id": "DL1", "name": "Bracket", "kind": "part",
             "description": "primary", "acceptance_criteria": []},
        ],
    }
    graph = build_mission_graph(manifest, graph_id="m8_all_done", org_layer=False)
    node_states = {
        n["id"]: {"status": "done", "output": {"status": "done"}}
        for n in graph["nodes"]
    }
    dag_result = {"status": "done", "nodes": node_states}
    reached, status, stop_reason, completed, _ = _drive(manifest, graph, dag_result)
    assert "deliver" in completed
    assert status == "done"
    assert stop_reason in ("mission_complete", "deliver_partial", "")
    # Critical: not running with empty.
    assert not (status == "running" and stop_reason == "")
