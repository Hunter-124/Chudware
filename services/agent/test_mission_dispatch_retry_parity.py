"""Focused Chudcode cutover coverage for MissionDispatch.delegate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from services.agent.mission_dispatch import MissionDispatch


@dataclass
class _Node:
    role: str
    tokens_granted: int = 16000
    parent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": "dg_cutover",
            "role": self.role,
            "status": "approved",
            "kind": "worker",
            "depth": 1,
            "parent": self.parent,
            "budget": {
                "tokens_granted": self.tokens_granted,
                "tokens_spent": 0,
                "attempts": 0,
            },
        }


class _Tree:
    root_id = "root"

    def __init__(self, tokens_granted: int = 16000):
        self.tokens_granted = tokens_granted
        self.transitions: list[tuple] = []
        self.requests: list[dict[str, Any]] = []
        self._nodes: dict[str, Any] = {}

    def request_delegation(self, role, brief, budget, **kwargs):
        self.requests.append({"role": role, "brief": brief, "budget": budget, **kwargs})
        return _Node(role, self.tokens_granted, kwargs.get("parent_id"))

    def start(self, node_id):
        self.transitions.append(("start", node_id))

    def accrue(self, node_id, tokens):
        self.transitions.append(("accrue", node_id, tokens))

    def complete(self, node_id, tokens_used=0):
        self.transitions.append(("complete", node_id, tokens_used))

    def fail(self, node_id, reason=""):
        self.transitions.append(("fail", node_id, reason))


class _Ledger:
    def __init__(self):
        self.rows: list[dict[str, Any]] = []

    def append(self, actor, kind, payload):
        self.rows.append({"actor": actor, "kind": kind, "payload": payload})
        return self.rows[-1]


@pytest.fixture
def runtime(monkeypatch):
    from services.agent import config
    from services.agent import role_models

    monkeypatch.setattr(config, "load_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(role_models, "resolve_for_node", lambda *args, **kwargs: None)
    monkeypatch.setattr(role_models, "resolve_role_model", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "get_subagent_tool_call_budget", lambda: 0)


def _dispatch(*, options=None, provider=None, tree=None, events=None):
    return MissionDispatch(
        run_id="run-cutover",
        ledger=_Ledger(),
        delegation_tree=tree or _Tree(),
        provider=provider,
        on_event=(lambda kind, payload: events.append((kind, payload)))
        if events is not None
        else None,
        options={"session_id": "parent-session", **(options or {})},
    )


def _completed_result(**overrides):
    result = {
        "success": True,
        "stop_reason": "completed",
        "result": "built and verified the delegated part",
        "role": "reviewer",
        "allowed_tools": ["measure_part"],
        "run_id": "run-cutover",
        "session_id": "cad-role-stable",
        "parent_id": "dg_cutover",
        "chudcode_session_id": "rpc-child",
        "chudcode_session_file": "/tmp/rpc-child.jsonl",
        "cancelled": False,
        "tokens_used": 3210,
        "provider_usage_recorded": True,
        "offline": False,
    }
    result.update(overrides)
    return result


def test_online_delegate_uses_chudcode_with_scope_lineage_credits_and_events(
    runtime, monkeypatch
):
    from services.agent import chudcode_roles
    from services.agent import config

    calls: list[dict[str, Any]] = []
    events: list[tuple[str, dict]] = []

    def fake_turn(request, role, **kwargs):
        calls.append({"request": request, "role": role, **kwargs})
        kwargs["on_event"]("agent.tool_call", {"tool": "measure_part"})
        return _completed_result()

    provider_constructions: list[tuple] = []
    monkeypatch.setattr(chudcode_roles, "run_role_turn", fake_turn)
    monkeypatch.setattr(
        config,
        "get_provider",
        lambda *args, **kwargs: provider_constructions.append((args, kwargs)),
    )
    tree = _Tree(tokens_granted=12000)
    dispatch = _dispatch(provider=object(), tree=tree, events=events)

    out = dispatch.delegate(
        role="reviewer", brief="inspect and verify", budget_tokens=12000
    )

    assert out["ok"] is True
    assert out["executed"] is True
    assert out["subagent_status"] == "done"
    assert out["tokens_used"] == 3210
    assert out["handoff"]["status"] == "done"
    assert out["session_id"] == "cad-role-stable"
    assert tree.transitions == [
        ("start", "dg_cutover"),
        ("complete", "dg_cutover", 3210),
    ]
    assert provider_constructions == []
    call = calls[0]
    assert call["session_id"] == "parent-session"
    assert call["run_id"] == "run-cutover"
    assert call["parent_id"] == "dg_cutover"
    assert call["max_total_tokens"] == 12000
    assert call["close_session"] is False
    assert call["options"]["mission_dispatch"] is dispatch
    assert call["options"]["subagent_id"] == "dg_cutover"
    assert call["options"]["allowed_tools"]
    assert "coordinate_claim" in call["options"]["allowed_tools"]
    assert "offline_provider" not in call["options"]
    assert events == [("agent.tool_call", {"tool": "measure_part"})]


def test_delegate_session_identity_and_nested_parent_are_stable(runtime, monkeypatch):
    from services.agent import chudcode_roles

    calls: list[dict[str, Any]] = []

    def fake_turn(request, role, **kwargs):
        calls.append({"request": request, "role": role, **kwargs})
        return _completed_result()

    monkeypatch.setattr(chudcode_roles, "run_role_turn", fake_turn)
    tree = _Tree()
    dispatch = _dispatch(tree=tree)
    dispatch._current_node_id = "dg_parent"

    first = dispatch.delegate(role="reviewer", brief="first", budget_tokens=8000)
    second = dispatch._run_subagent_turn(first, "resume")

    assert tree.requests[0]["parent_id"] == "dg_parent"
    assert calls[0]["session_id"] == calls[1]["session_id"] == "parent-session"
    assert calls[0]["parent_id"] == calls[1]["parent_id"] == "dg_cutover"
    assert second["session_id"] == first["session_id"]


def test_explicit_offline_delegate_passes_only_injected_safe_provider(
    runtime, monkeypatch
):
    from services.agent import chudcode_roles

    safe_provider = object()
    calls: list[dict[str, Any]] = []

    def fake_turn(request, role, **kwargs):
        calls.append({"request": request, "role": role, **kwargs})
        return _completed_result(offline=True)

    monkeypatch.setattr(chudcode_roles, "run_role_turn", fake_turn)
    out = _dispatch(options={"offline": True}, provider=safe_provider).delegate(
        role="reviewer", brief="offline audit", budget_tokens=6000
    )

    assert out["ok"] is True
    assert calls[0]["options"]["offline"] is True
    assert calls[0]["options"]["offline_provider"] is safe_provider
    assert calls[0]["max_total_tokens"] == 6000




def test_cancelled_delegate_fails_tree_and_leads_with_cause(runtime, monkeypatch):
    from services.agent import chudcode_roles

    monkeypatch.setattr(
        chudcode_roles,
        "run_role_turn",
        lambda *args, **kwargs: _completed_result(
            success=False,
            stop_reason="cancelled",
            result={"error": "cancelled by user"},
            cancelled=True,
            tokens_used=450,
        ),
    )
    tree = _Tree()
    out = _dispatch(tree=tree).delegate(
        role="reviewer", brief="stop cleanly", budget_tokens=4000
    )

    assert list(out)[:3] == ["ok", "reason", "error"]
    assert out["ok"] is False
    assert out["reason"] == "subagent_cancelled"
    assert "cancelled by user" in out["error"]
    assert out["subagent_status"] == "cancelled"
    assert ("accrue", "dg_cutover", 450) in tree.transitions
    assert any(row[0] == "fail" for row in tree.transitions)


def test_provider_failure_shape_is_failure_first_and_auditable(runtime, monkeypatch):
    from services.agent import chudcode_roles

    monkeypatch.setattr(
        chudcode_roles,
        "run_role_turn",
        lambda *args, **kwargs: _completed_result(
            success=False,
            stop_reason="provider_error",
            result={"error": "Chudcode route unavailable"},
            tokens_used=0,
            provider_usage_recorded=False,
        ),
    )
    out = _dispatch().delegate(role="reviewer", brief="inspect", budget_tokens=5000)

    assert list(out)[:3] == ["ok", "reason", "error"]
    assert out["reason"] == "subagent_provider_error"
    assert "Chudcode route unavailable" in out["error"]
    assert out["executed"] is True
    assert out["provider_usage_recorded"] is False


def test_recursion_policy_refusal_spends_no_credit(runtime, monkeypatch):
    from services.agent import chudcode_roles
    from services.agent import role_models

    called: list[bool] = []
    monkeypatch.setattr(
        role_models,
        "check_spawn_depth_policy",
        lambda *args, **kwargs: {"ok": False, "reason": "max depth reached"},
    )
    monkeypatch.setattr(
        chudcode_roles,
        "run_role_turn",
        lambda *args, **kwargs: called.append(True),
    )
    tree = _Tree()
    out = _dispatch(tree=tree).delegate(
        role="reviewer", brief="too deep", budget_tokens=5000
    )

    assert out["ok"] is False
    assert out["reason"] == "spawn_depth_policy"
    assert called == []
    assert tree.transitions == []
