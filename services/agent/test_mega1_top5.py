"""mega-1 TOP-5 harness fixes — unit coverage.

1. LangGraph Annotated reducer fan-in (two sibling writers, no exception)
2. on_event dual arity at run_mission boundary
3. Auth fail-closed: xai:* with no token/key → MissionAuthError pre-DAG
4. Non-retryable provider error taxonomy → stop_reason=provider_auth
5. (tools_slot) spec_writer concrete_tools expands ecad/fw/em families
"""
from __future__ import annotations

import os
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 1. LangGraph concurrent fan-in with Annotated reducers
# ---------------------------------------------------------------------------

def test_dag_state_reducer_fan_in_two_writers():
    """Two parallel nodes writing node_states must not raise INVALID_CONCURRENT."""
    from langgraph.graph import END, StateGraph

    from services.dag.graph_loader import DagState

    def n1(state: dict) -> dict:
        return {"node_states": {"n_spec_DL1": {"status": "done", "verdict": "done"}}}

    def n2(state: dict) -> dict:
        return {
            "node_states": {"n_spec_DL2": {"status": "failed", "verdict": "escalate"}},
            "open_questions": [{"node": "n_spec_DL2", "question": "auth"}],
        }

    def join(state: dict) -> dict:
        return {"run_status": "done"}

    sg: StateGraph = StateGraph(DagState)
    sg.add_node("start", lambda s: {"run_status": "running"})
    sg.add_node("n_spec_DL1", n1)
    sg.add_node("n_spec_DL2", n2)
    sg.add_node("n_spec_done", join)
    sg.set_entry_point("start")
    sg.add_edge("start", "n_spec_DL1")
    sg.add_edge("start", "n_spec_DL2")
    sg.add_edge("n_spec_DL1", "n_spec_done")
    sg.add_edge("n_spec_DL2", "n_spec_done")
    sg.add_edge("n_spec_done", END)

    final = sg.compile().invoke({
        "run_id": "mega1-fanin",
        "run_status": "running",
        "node_states": {},
        "open_questions": [],
    })
    assert final["node_states"]["n_spec_DL1"]["status"] == "done"
    assert final["node_states"]["n_spec_DL2"]["status"] == "failed"
    assert final["run_status"] == "done"
    assert any(q.get("node") == "n_spec_DL2" for q in final.get("open_questions") or [])


# ---------------------------------------------------------------------------
# 2. on_event dual arity
# ---------------------------------------------------------------------------

def test_normalize_on_event_dict_arity():
    from services.agent.loop import normalize_on_event

    seen: list[Any] = []

    def sink_dict(ev: dict) -> None:
        seen.append(("dict", ev))

    adapted = normalize_on_event(sink_dict)
    assert adapted is not None
    adapted({"kind": "status", "text": "hi"})
    adapted("dag.node.started", {"node_id": "n1"})
    assert seen[0][0] == "dict" and seen[0][1]["kind"] == "status"
    # two-arg emit adapted into single dict with kind
    assert seen[1][0] == "dict"
    assert seen[1][1].get("kind") == "dag.node.started"
    assert seen[1][1].get("node_id") == "n1"


def test_normalize_on_event_str_dict_arity():
    from services.agent.loop import normalize_on_event

    seen: list[Any] = []

    def sink_pair(kind: str, payload: dict) -> None:
        seen.append((kind, payload))

    adapted = normalize_on_event(sink_pair)
    assert adapted is not None
    adapted("dag.run.started", {"run_id": "r1"})
    adapted({"kind": "mission_phase", "phase": "intake"})
    assert seen[0] == ("dag.run.started", {"run_id": "r1"})
    # dict form adapted into (kind, dict)
    assert seen[1][0] == "mission_phase"
    assert seen[1][1]["phase"] == "intake"


def test_normalize_on_event_never_raises_on_bad_sink():
    from services.agent.loop import normalize_on_event

    def boom(*_a, **_k):
        raise RuntimeError("sink exploded")

    adapted = normalize_on_event(boom)
    adapted("x", {"y": 1})  # must not raise
    adapted({"kind": "z"})


# ---------------------------------------------------------------------------
# 3. Auth fail-closed pre-DAG
# ---------------------------------------------------------------------------

def test_provider_for_tier_xai_no_creds_raises_mission_auth_error(monkeypatch):
    from services.agent.tiers import MissionAuthError, provider_for_tier
    from services.agent.providers import sub_auth

    monkeypatch.delenv("AI_CAD_PROVIDER", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(sub_auth, "load_credential", lambda *_a, **_k: None)

    with pytest.raises(MissionAuthError) as ei:
        provider_for_tier("xai:grok-4-5", cfg={"offline_mode": False})
    err = ei.value
    assert err.code == "auth_missing"
    assert "xai-sub" in err.tried and "xai" in err.tried
    d = err.to_dict()
    assert d["ok"] is False and d["code"] == "auth_missing"


def test_provider_for_tier_xai_with_key_constructs(monkeypatch):
    from services.agent import config as agent_config
    from services.agent.tiers import provider_for_tier
    from services.agent.providers import sub_auth

    monkeypatch.delenv("AI_CAD_PROVIDER", raising=False)
    monkeypatch.setattr(sub_auth, "load_credential", lambda *_a, **_k: None)
    monkeypatch.setenv("XAI_API_KEY", "test-key-not-real")
    calls = []

    def tracked(name=None, cfg=None):
        calls.append((name, (cfg or {}).get("api_key")))
        return type("P", (), {"name": name})()

    monkeypatch.setattr(agent_config, "get_provider", tracked)
    prov = provider_for_tier("xai:grok-4.5", cfg={})
    assert calls[-1][0] == "xai"
    assert calls[-1][1] == "test-key-not-real"
    assert prov.name == "xai"


def test_run_mission_auth_fail_closed_pre_dag(monkeypatch, tmp_path):
    """xai:* with no creds → stop_reason=provider_auth BEFORE DAG launches."""
    monkeypatch.setenv("AI_CAD_TIER", "xai:grok-4-5")
    monkeypatch.setenv("AI_CAD_ADVISOR", "0")
    monkeypatch.delenv("AI_CAD_PROVIDER", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    from services.agent import loop
    from services.agent.tiers import MissionAuthError
    from services.agent.providers import sub_auth
    from services.agent.providers.base import Provider

    monkeypatch.setattr(sub_auth, "load_credential", lambda *_a, **_k: None)

    class _MockProv(Provider):
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            return '{"done": true}'

    manifest = {
        "schema": "aicad.deliverables/1",
        "request_verbatim": "a part",
        "deliverables": [{
            "id": "DL1", "kind": "part", "name": "widget",
            "description": "x",
            "acceptance_criteria": [{
                "id": "AC1", "text": "exists",
                "check": {"tool": "measure_part", "pass_rule": "ok"},
            }],
            "depends_on": [], "status": "pending",
        }],
        "ambiguities": [], "assumptions": [],
    }

    dag_called = {"n": 0}

    def _fake_dag_run(*_a, **_k):
        dag_called["n"] += 1
        return {"status": "done", "nodes": {}}

    monkeypatch.setattr("services.dag.executor.run", _fake_dag_run)

    # Force provider_for_tier to raise even if env leaks a key.
    import services.agent.tiers as tiers_mod

    def _raise_auth(tier, cfg=None):
        raise MissionAuthError(
            "no creds", code="auth_missing", tried=["xai-sub", "xai"], tier=tier)

    monkeypatch.setattr(tiers_mod, "provider_for_tier", _raise_auth)
    monkeypatch.setattr(tiers_mod, "detect_tier_from_env", lambda: "xai:grok-4-5")
    monkeypatch.setattr(tiers_mod, "parse_xai_tier",
                        lambda t: "grok-4.5" if t and str(t).startswith("xai:") else None)

    result = loop.run_mission(
        "a part", _MockProv(), name="mega1-auth-test",
        intake_fn=lambda r: manifest, execute_dag=True, autonomy_default="auto")
    assert result.stop_reason == "provider_auth", result.stop_reason
    assert result.status == "failed"
    assert dag_called["n"] == 0, "DAG must not launch after auth fail-closed"


# ---------------------------------------------------------------------------
# 4. Non-retryable provider error taxonomy
# ---------------------------------------------------------------------------

def test_classify_provider_error_auth_non_retryable():
    from services.agent.agent_turn import classify_provider_error, _PROVIDER_AUTH

    stop, retryable = classify_provider_error(
        Exception("Error code: 400 - Incorrect API key provided. console.x.ai"))
    assert stop == _PROVIDER_AUTH
    assert retryable is False

    class _E(Exception):
        status_code = 401

    stop, retryable = classify_provider_error(_E("unauthorized"))
    assert stop == _PROVIDER_AUTH and retryable is False

    class _E403(Exception):
        status_code = 403

    stop, retryable = classify_provider_error(_E403("forbidden"))
    assert stop == _PROVIDER_AUTH and retryable is False


def test_classify_provider_error_429_and_5xx_retryable():
    from services.agent.agent_turn import classify_provider_error, _FAILED

    class _R(Exception):
        status_code = 429

    stop, retryable = classify_provider_error(_R("rate limit"))
    assert stop == _FAILED and retryable is True

    class _S(Exception):
        status_code = 503

    stop, retryable = classify_provider_error(_S("unavailable"))
    assert stop == _FAILED and retryable is True


def test_agent_turn_provider_auth_abort():
    """complete() raising invalid-key → TurnResult stop_reason=provider_auth."""
    from services.agent import agent_turn

    class _BadKeyProv:
        name = "xai"
        model = "grok-4.5"

        def complete(self, system, messages, max_tokens=8000):
            raise RuntimeError(
                "Error code: 400 - {'code': 'invalid-argument', "
                "'error': 'Incorrect API key provided. You can obtain an API key "
                "from https://console.x.ai.'}")

    res = agent_turn.run_turn(
        "spec the part", _BadKeyProv(), name="SPECIFY DL1",
        allowed_tools=["ledger_get"], max_tool_calls=2)
    assert res.success is False
    assert res.stop_reason == "provider_auth"
    assert res.result.get("retryable") is False


def test_node_runner_maps_provider_auth_to_escalate():
    from services.dag import node_runner as nr
    assert "provider_auth" in nr._ESCALATE_REASONS
    assert nr._map_verdict("provider_auth", False) == nr.VERDICT_ESCALATE


# ---------------------------------------------------------------------------
# 5. tools_slot: spec_writer expands ECAD/fw/em families
# ---------------------------------------------------------------------------

def test_spec_writer_tools_slot_includes_domain_families():
    from services.agent.roles import load_role

    card = load_role("spec_writer")
    tools = card.concrete_tools
    assert any(t.startswith("ecad_") for t in tools), tools
    assert any(t.startswith("fw_") for t in tools), tools
    assert any(t.startswith("em_") for t in tools), tools
    assert "board_from_schematic" in tools
    # Policy tokens still carry family: entries for registry enforcement.
    tokens = card.allow_tokens
    assert "family:ecad" in tokens
    assert "family:fw" in tokens
    assert "family:em" in tokens
