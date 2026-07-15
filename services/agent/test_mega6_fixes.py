"""Mega-6 live harness-fix regressions (Fixer6 = StaticOwl).

Watch-list from mega-5 findings + mega-6 attempt-1 openrouter auth wipe:
  1. keyless openrouter never auto_picked / never constructed
  2. child join handoff not null when child had tools / success
  3. provider token usage recorded into turn result + ledger
  4. BUILD geometry roles keep new_part/add_features + tool floor ≥8
  5. ollama local leaf construction failure degrades (does not block pick)
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1. Keyless openrouter must not be offered or constructed
# ---------------------------------------------------------------------------


def test_keyless_openrouter_excluded_from_enabled_providers(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import _enabled_providers

    en = _enabled_providers({"provider": "xai-sub", "model": "grok-4.5"})
    assert not any(e.get("provider") == "openrouter" for e in en), en
    assert any(e.get("provider") == "xai-sub" for e in en), en
    assert any(e.get("provider") == "ollama" for e in en), en


def test_keyless_openrouter_excluded_from_explicit_list(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import _enabled_providers

    en = _enabled_providers({
        "enabled_providers": [
            {"provider": "openrouter", "model": "nvidia/nemotron-ultra:free"},
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    })
    assert not any(e.get("provider") == "openrouter" for e in en), en
    assert any(e.get("provider") == "xai-sub" for e in en), en


def test_auto_pick_depth0_without_openrouter_key_picks_xai(monkeypatch):
    """mega-6 attempt-1: depth0 min_rank=3 picked nemotron free → 401 wipe."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import auto_pick_model

    a = auto_pick_model(
        cfg={"provider": "xai-sub", "model": "grok-4.5"},
        task_class="unknown",
        node_depth=0,
    )
    assert a.provider != "openrouter", a
    assert a.provider == "xai-sub", a


def test_openrouter_construction_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent import config as agent_config

    with pytest.raises(ValueError, match="API key"):
        agent_config.get_provider(name="openrouter", cfg={"model": "x"})


def test_openrouter_enabled_when_key_present(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import _enabled_providers

    en = _enabled_providers({
        "provider": "xai-sub",
        "model": "grok-4.5",
        "openrouter": {"api_key": "sk-test"},
    })
    assert any(e.get("provider") == "openrouter" for e in en), en


# ---------------------------------------------------------------------------
# 2. Join handoff plumbing
# ---------------------------------------------------------------------------


def test_join_synthesises_handoff_when_child_omits_envelope(tmp_path, monkeypatch):
    """mega-5: join executed=true, handoff=null, spent=0."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega6_join", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        last_tokens = 0
        _nodes = {"root": type("N", (), {"depth": 0, "role": "orchestrator"})()}

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            self.last_tokens = tokens_used
            return None

        def fail(self, node_id, reason=""):
            return None

    class _Provider:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            return "```json\n{\"status\":\"done\",\"summary\":\"ok\"}\n```"

    tree = _Tree()
    md = MissionDispatch(
        run_id="mega6_join",
        ledger=ledger,
        delegation_tree=tree,
        provider=_Provider(),
        task_class="simple_part",
    )

    import services.agent.delegation as dlg
    import services.agent.agent_turn as at
    from services.agent import config as agent_config

    real_delegate = dlg.delegate
    real_run = at.run_turn
    real_load = agent_config.load_config
    real_get = agent_config.get_provider

    class _Turn:
        success = True
        stop_reason = "confirmed"
        # Child returned tools but NO handoff key — the mega-5 join fingerprint.
        result = {
            "tool_calls": 2,
            "calls": [{"tool": "cad_guide", "ok": True},
                      {"tool": "list_tools", "ok": False}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "calls": 1,
            },
            "provider_usage_recorded": True,
            "tokens_used": 140,
        }

    dlg.delegate = lambda *a, **k: {
        "id": "dg_join",
        "status": "approved",
        "role": "feature_modeler",
        "depth": 1,
        "kind": "worker",
        "parent": "root",
        "budget": {"tokens_granted": 8000},
    }
    at.run_turn = lambda *a, **k: _Turn()
    agent_config.load_config = lambda: {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    agent_config.get_provider = lambda name=None, cfg=None: _Provider()
    try:
        out = md.delegate(
            role="feature_modeler",
            brief="Make the airframe skin panel.",
            budget_tokens=8000,
        )
    finally:
        dlg.delegate = real_delegate
        at.run_turn = real_run
        agent_config.load_config = real_load
        agent_config.get_provider = real_get

    assert out.get("subagent_status") == "done" or out.get("ok") is True, out
    assert isinstance(out.get("handoff"), dict), out
    assert out["handoff"].get("status") in ("done", "blocked"), out
    assert out.get("tokens_used") == 140, out
    assert out.get("tokens_used_estimate") is False, out
    assert out.get("provider_usage_recorded") is True, out
    assert tree.last_tokens == 140, tree.last_tokens


# ---------------------------------------------------------------------------
# 3. Provider usage stash + turn attach
# ---------------------------------------------------------------------------


def test_token_usage_stash_and_with_usage():
    from services.agent.providers import token_usage as tu
    from services.agent.agent_turn import _with_usage

    tu.begin_turn_usage()
    tu.record_usage(
        provider="xai-sub", model="grok-4.5",
        prompt_tokens=12, completion_tokens=8,
    )
    row = tu.get_last_usage()
    assert row["total_tokens"] == 20
    turn = tu.get_turn_usage()
    assert turn["calls"] == 1
    assert turn["total_tokens"] == 20

    result = _with_usage({"handoff": {"status": "done"}}, tu)
    assert result["provider_usage_recorded"] is True
    assert result["tokens_used"] == 20
    assert result["usage"]["prompt_tokens"] == 12


def test_extract_openai_usage_dict():
    from services.agent.providers.token_usage import extract_openai_usage

    u = extract_openai_usage({
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 7,
            "total_tokens": 10,
        }
    })
    assert u["prompt_tokens"] == 3
    assert u["completion_tokens"] == 7
    assert u["total_tokens"] == 10


def test_agent_turn_records_usage_in_result(tmp_path, monkeypatch):
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at
    from services.agent.providers import token_usage as tu

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega6_usage", path=tmp_path / "ledger.json")

    class _Prov:
        name = "mock"
        model = "mock-model"

        def complete(self, system, messages, max_tokens=8000):
            tu.record_usage(
                provider=self.name, model=self.model,
                prompt_tokens=50, completion_tokens=25, total_tokens=75,
            )
            return '```json\n{"status":"done","summary":"ok","outputs":{}}\n```'

    res = at.run_turn(
        "finish",
        _Prov(),
        name="n_test",
        ledger=ledger,
        max_tool_calls=2,
    )
    assert res.success
    assert res.result.get("provider_usage_recorded") is True
    assert res.result.get("tokens_used") == 75
    events = [e for e in ledger.events
              if (e.get("payload") or {}).get("event") == "provider_usage"]
    assert events, ledger.events[-5:]


# ---------------------------------------------------------------------------
# 4. BUILD enablement floor
# ---------------------------------------------------------------------------


def test_feature_modeler_allowlist_includes_new_part():
    from services.agent.roles import load_role

    card = load_role("feature_modeler")
    tools = card.concrete_tools
    assert "new_part" in tools
    assert "add_features" in tools


def test_build_role_tool_floor_at_least_eight(tmp_path, monkeypatch):
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega6_build", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        _nodes = {"root": type("N", (), {"depth": 0, "role": "orchestrator"})()}

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            return None

        def fail(self, node_id, reason=""):
            return None

    class _Provider:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            return '```json\n{"status":"done","summary":"ok"}\n```'

    md = MissionDispatch(
        run_id="mega6_build",
        ledger=ledger,
        delegation_tree=_Tree(),
        provider=_Provider(),
        task_class="simple_part",
        phase="build",
    )

    import services.agent.delegation as dlg
    import services.agent.agent_turn as at
    from services.agent import config as agent_config

    captured = {}

    def _spy_run(*a, **k):
        captured["max_tool_calls"] = k.get("max_tool_calls")
        captured["allowed_tools"] = k.get("allowed_tools")

        class _T:
            success = True
            stop_reason = "confirmed"
            result = {"handoff": {"status": "done"}, "tool_calls": 0, "calls": []}

        return _T()

    real_delegate = dlg.delegate
    real_run = at.run_turn
    real_load = agent_config.load_config
    real_get = agent_config.get_provider
    dlg.delegate = lambda *a, **k: {
        "id": "dg_build",
        "status": "approved",
        "role": "feature_modeler",
        "depth": 1,
        "kind": "worker",
        "parent": "root",
        "budget": {"tokens_granted": 2000},
        "phase": "build",
    }
    at.run_turn = _spy_run
    agent_config.load_config = lambda: {
        "provider": "xai-sub", "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    agent_config.get_provider = lambda name=None, cfg=None: _Provider()
    try:
        md.delegate(
            role="feature_modeler",
            brief="Build the plate.",
            budget_tokens=2000,
        )
    finally:
        dlg.delegate = real_delegate
        at.run_turn = real_run
        agent_config.load_config = real_load
        agent_config.get_provider = real_get

    assert captured.get("max_tool_calls") is not None
    assert int(captured["max_tool_calls"]) >= 8, captured
    tools = list(captured.get("allowed_tools") or [])
    assert "new_part" in tools, tools
    assert "add_features" in tools, tools


# ---------------------------------------------------------------------------
# 5. Local leaf construction failure degrades (construction path)
# ---------------------------------------------------------------------------


def test_resolve_provider_degrades_ollama_construct_fail_to_injected(monkeypatch):
    """When ollama construction fails and no openrouter key, fall to injected."""
    from services.dag import node_runner as nr
    from services.agent import config as agent_config
    from services.agent import role_models as rm

    class _Injected:
        name = "xai-sub"
        model = "grok-4.5"

    def _fake_pick(*a, **k):
        return rm.RoleModelAssignment(
            role="task",
            provider="ollama",
            model="llama3.2:latest",
            mode="auto",
            rank=1,
            source="test",
            reason="test",
        )

    def _fake_get(name=None, cfg=None, role=None):
        if name in ("openai", "ollama"):
            raise RuntimeError("connection refused 11434")
        if name == "openrouter":
            raise ValueError("openrouter provider requires an API key")
        return _Injected()

    monkeypatch.setattr(rm, "resolve_role_model", _fake_pick)
    monkeypatch.setattr(rm, "resolve_for_node", lambda *a, **k: None)
    monkeypatch.setattr(agent_config, "get_provider", _fake_get)
    monkeypatch.setattr(agent_config, "load_config", lambda: {
        "provider": "xai-sub", "model": "grok-4.5",
    })
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    pack = {
        "role_id": "feature_modeler",
        "depth": 2,
        "task_class": "simple_part",
        "node_inputs": {"role": "feature_modeler", "depth": 2},
    }
    prov = nr._resolve_provider(pack, provider=_Injected())
    assert getattr(prov, "name", None) == "xai-sub"


def test_resolve_provider_emits_model_assignment_event(monkeypatch):
    """mega-6 live: 0 model_assignment rows on DAG nodes despite auto_pick."""
    from services.dag import node_runner as nr
    from services.agent import config as agent_config
    from services.agent import role_models as rm

    events = []

    class _Prov:
        name = "xai-sub"
        model = "grok-4.5"

    def _fake_pick(*a, **k):
        return rm.RoleModelAssignment(
            role="task",
            provider="xai-sub",
            model="grok-4.5",
            mode="auto",
            rank=5,
            source="test",
            reason="test pick",
        )

    monkeypatch.setattr(rm, "resolve_role_model", _fake_pick)
    monkeypatch.setattr(rm, "resolve_for_node", lambda *a, **k: None)
    monkeypatch.setattr(
        agent_config, "get_provider",
        lambda name=None, cfg=None, role=None: _Prov())
    monkeypatch.setattr(agent_config, "load_config", lambda: {
        "provider": "xai-sub", "model": "grok-4.5",
    })

    class _Led:
        notes = []

        def append(self, actor, kind, payload):
            self.notes.append((actor, kind, payload))

    led = _Led()
    pack = {
        "role_id": "spec_writer",
        "node_id": "n_spec_DL1",
        "node_name": "SPECIFY DL1",
        "depth": 0,
        "task_class": "unknown",
        "node_inputs": {"role": "spec_writer", "depth": 0},
        "ledger": led,
        "_event_sink": lambda *a, **k: events.append((a, k)),
    }
    prov = nr._resolve_provider(pack, provider=None)
    assert getattr(prov, "name", None) == "xai-sub"
    assert pack.get("role_model", {}).get("provider") == "xai-sub"
    assert any(
        (n[2] or {}).get("event") == "model_assignment" for n in led.notes
    ), led.notes


def test_new_part_without_add_features_not_auto_done(tmp_path, monkeypatch):
    """mega-6 live BUILD: new_part + preflight thrash auto-completed empty solid."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega6_build_mut", path=tmp_path / "ledger.json")

    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            # 1: new_part, 2-4: preflight thrash, then stop
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"new_part","args":{"name":"p"}}\n```'
                )
            if seq["n"] <= 4:
                return (
                    '```json\n{"tool":"preflight_ops","args":{"ops":[]}}\n```'
                )
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
    # Must NOT auto-complete as confirmed product success.
    assert res.stop_reason in ("max_iters", "phase_step_cap", "no_code"), res.stop_reason
    # Either failed or blocked — not a silent confirmed empty solid.
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
