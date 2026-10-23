"""Mega-8 RoleSurface regressions (FitGecko).

Covers mega-7 TOP-5 items owned by RoleSurface:
  1. Capability separation — PLAN cards lose leaf exec tools (add_features).
  2. Typed-refusal soft gate — free-form ledger_note does NOT satisfy;
     valid {reason, domains_considered, why_flat} does; delegate call does.
  3. Cheap-leaf one-shot envelope — validates, falls back, increments leaf_miss.

Item 4 of that list, "clean terminal status — soft-join deliver → done|blocked +
stop_reason deliver_partial", is NOT covered here and no longer can be: the refactor deleted
``services/agent/loop.py::_drive_phases_from_dag`` and nothing re-implemented it, so
``deliver_partial`` has no producer anywhere in the tree (grep services/ core/: comments,
docstrings and tests only). A ``_drive`` helper here monkeypatched that deleted function
and was itself never called — removed 2026-08-05 so this file's coverage claim matches
what it actually asserts.
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
    assert validate_typed_refusal(
        {
            "reason": "x",
            "domains_considered": [],
            "why_flat": "y",
        }
    )
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


def test_multi_domain_request_includes_typed_refusal_scaffold():
    """F9: layout/plan request must teach typed_refusal escape hatch."""
    from services.dag import node_runner as nr

    req = nr._build_request(
        "design",
        {
            "role": "layout_engineer",
            "task_class": "assembly",
            "request": "Build the layout skeleton for the rocket plane.",
        },
        {
            "role_id": "layout_engineer",
            "task_class": "assembly",
            "domain_tags": ["plan", "layout", "assembly"],
            "allowed_tools": ["ledger_get", "ledger_note", "delegate"],
        },
    )
    assert "typed_refusal" in req
    assert "domains_considered" in req
    assert "delegate" in req.lower()


def test_typed_refusal_gate_skips_specify_workers():
    """Live mega-8: n_spec_* are type=design + multi_feature/assembly — must NOT gate."""
    from services.dag import node_runner as nr
    from services.agent.role_models import is_multi_domain_plan_or_build

    assert not is_multi_domain_plan_or_build(
        role_id="spec_writer",
        node_type="design",
        domain_tags=["spec", "geometry"],
        task_class="multi_feature_part",
    )
    assert not is_multi_domain_plan_or_build(
        role_id="spec_writer",
        node_type="design",
        domain_tags=["spec", "assembly", "geometry"],
        task_class="assembly",
    )
    # PLAN/layout still gated.
    assert is_multi_domain_plan_or_build(
        role_id="layout_engineer",
        node_type="design",
        domain_tags=["plan", "layout", "assembly"],
        task_class="assembly",
    )
    assert is_multi_domain_plan_or_build(
        role_id="feature_modeler",
        node_type="design",
        domain_tags=["geometry", "assembly"],
        task_class="assembly",
    )

    node = {
        "id": "n_spec_DL1",
        "type": "design",
        "name": "SPECIFY DL1",
        "inputs": {
            "role": "spec_writer",
            "task_class": "multi_feature_part",
            "depth": 0,
        },
    }
    pack = {
        "node_inputs": node["inputs"],
        "role_id": "spec_writer",
        "task_class": "multi_feature_part",
        "depth": 0,
        "capability_letter": "C",
        "retry_remaining": 2,
    }

    class _R:
        success = True
        stop_reason = "confirmed"
        final_code = None
        result = {
            "calls": [{"tool": "ledger_note", "ok": True}],
            "handoff": {
                "status": "done",
                "summary": "spec written flat",
                "outputs": {"spec": "ok"},
            },
        }

    verdict, out = nr.run(node, pack, agent_fn=lambda r, p: _R())
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert out["data"].get("stop_reason") != "delegate_or_typed_refusal_required"


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
    assert validate_leaf_envelope(
        {
            "goal": "x",
            "no_op": True,  # missing reason
        }
    )
    # mega-24: no_op=true WITH ops is salvaged to ops-only (prefer ops).
    assert (
        validate_leaf_envelope(
            {
                "goal": "x",
                "no_op": True,
                "reason": "r",
                "ops": [{"a": 1}],
            }
        )
        == []
    )

    text = '```json\n{"goal":"g","no_op":true,"reason":"already done"}\n```'
    env, errs = parse_leaf_envelope_text(text)
    assert errs == []
    assert env["goal"] == "g"


def test_leaf_envelope_salvages_truncated_ops_json():
    """Live mega-8: ollama DL5 returned truncated ops[] with trailing commas."""
    from services.agent.role_models import parse_leaf_envelope_text

    blob = """{
  "goal": "Design a model rocket plane with flight computer",
  "ops": [
    {
      "type": "fit_calc",
    
"""
    env, errs = parse_leaf_envelope_text(blob)
    assert env is not None, errs
    assert env["goal"].startswith("Design a model rocket")
    assert env.get("no_op") is True
    assert env.get("reason")

    env2, errs2 = parse_leaf_envelope_text(
        '{"goal":"g","no_op":true,"reason":"nothing to do",}'
    )
    assert env2 is not None, errs2
    assert env2["no_op"] is True


def test_reviewer_leaf_runs_through_chudcode_with_verification_tools(monkeypatch):
    from services.agent import chudcode_roles
    from services.dag import node_runner as nr

    calls = []

    def role_turn(request, role, **kwargs):
        calls.append((request, role, kwargs))
        assert "verify_battery" in kwargs["options"]["allowed_tools"]
        return {
            "success": True,
            "stop_reason": "completed",
            "result": "verified with tools",
            "calls": [{"tool": "verify_battery", "ok": True, "result": {"ok": True}}],
            "tool_calls": 1,
        }

    monkeypatch.setattr(chudcode_roles, "run_role_turn", role_turn)
    monkeypatch.setattr(nr, "_ADVISOR_ENABLED", False)
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
    verdict, out = nr.run(node, pack)

    assert len(calls) == 1
    assert calls[0][1] == "reviewer"
    assert verdict == nr.VERDICT_DONE, (verdict, out)
    assert not (out["data"].get("agent_result") or {}).get("one_shot")


def test_leaf_chudcode_failure_uses_shared_dag_retry_contract(monkeypatch):
    from services.agent import chudcode_roles
    from services.agent import role_models as rm
    from services.dag import node_runner as nr

    rm.reset_leaf_miss_count()
    calls = []

    def role_turn(request, role, **kwargs):
        calls.append((request, role, kwargs))
        return {
            "success": False,
            "stop_reason": "provider_error",
            "result": {"retryable": True, "error": "transient local model failure"},
        }

    monkeypatch.setattr(chudcode_roles, "run_role_turn", role_turn)
    monkeypatch.setattr(nr, "_ADVISOR_ENABLED", False)
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
        "retry_remaining": 2,
        "node_name": "INTAKE",
    }
    miss_before = rm.leaf_miss_count()
    verdict, out = nr.run(node, pack)

    assert verdict == nr.VERDICT_RETRY, (verdict, out)
    assert out["retry_remaining"] == 1
    assert len(calls) == 1
    assert rm.leaf_miss_count() == miss_before


def test_openai_provider_accepts_string_content():
    """Live mega-8: leaf oneshot used plain-string content; OpenAI/ollama crashed."""
    pytest.importorskip("openai")
    from services.agent.providers.openai_provider import OpenAIProvider

    p = OpenAIProvider(api_key="k", base_url="http://127.0.0.1:11434/v1")
    # Plain string (historical oneshot / overseer form).
    m = p._message("user", "fill the leaf envelope")
    assert m == {"role": "user", "content": "fill the leaf envelope"}
    # List-of-parts still collapses to string for text-only.
    m2 = p._message(
        "user", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )
    assert m2 == {"role": "user", "content": "a\n\nb"}


def test_leaf_request_is_forwarded_to_chudcode_role_seam(monkeypatch):
    from services.agent import chudcode_roles
    from services.dag import node_runner as nr

    seen = {}

    def role_turn(request, role, **kwargs):
        seen.update({"request": request, "role": role, "kwargs": kwargs})
        return {
            "success": True,
            "stop_reason": "completed",
            "result": "leaf complete",
        }

    monkeypatch.setattr(chudcode_roles, "run_role_turn", role_turn)
    result = nr._default_agent_fn(
        "do leaf",
        {
            "node_id": "n",
            "node_inputs": {"role": "intake_analyst", "task_class": "leaf"},
            "task_class": "leaf",
            "role_model": {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "cost_tier": 0,
            },
            "allowed_tools": [],
        },
    )
    assert result.success is True
    assert seen["request"] == "do leaf"
    assert seen["role"] == "intake_analyst"
    assert seen["kwargs"]["options"]["settings"]["provider"] == "ollama"


def test_context_pack_and_graph_seed_run_id():
    """run_id must reach agent_turn for once-per-mission op-vocab keying."""
    from services.dag.context_pack import assemble

    node = {
        "id": "n_build_DL1",
        "type": "design",
        "name": "BUILD",
        "inputs": {
            "role": "feature_modeler",
            "task_class": "multi_feature_part",
            "run_id": "from-inputs",
        },
    }
    pack = assemble(node, {"retry_remaining": 1})
    assert pack.get("run_id") == "from-inputs"

    node2 = {
        "id": "n_plan",
        "type": "plan",
        "name": "PLAN",
        "inputs": {"role": "layout_engineer", "task_class": "assembly"},
    }
    pack2 = assemble(node2, {"retry_remaining": 1, "run_id": "from-state"})
    assert pack2.get("run_id") == "from-state"
