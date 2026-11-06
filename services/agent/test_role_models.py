"""Tests for services/agent/role_models.py — role resolution, redaction, spawn depth.

Covers:
  * fixed / size-tier / session-default resolution
  * redaction role HARD-refuses non-loopback
  * spawn-depth policy (max_spawn_depth)
No network.
"""
from __future__ import annotations

import json

import pytest

from services.agent import role_models as rm


@pytest.mark.parametrize("signal,prompt,inputs,expected", [
    ("chat", "What is a fillet?", {}, ("local", "low")),
    ("modelling", "Create a bracket", {}, ("mid", "medium")),
    ("analysis", "Analyse thermal stress", {}, ("strongest", "high")),
    ("mission", "Build two parts", {}, ("strong", "high")),
    ("document_size", "Create a bracket", {"context_tokens": 500, "context_window": 1000}, ("strong", "medium")),
    ("local_only", "Analyse thermal stress", {}, None),
])
def test_auto_rules_table(monkeypatch, signal, prompt, inputs, expected):
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.copilot_mode_tools import apply_mode_tools

    monkeypatch.setattr("services.agent.model_manager.classify_bundled_task", lambda *a, **k: None)
    cfg = {"assignment_mode": "advanced", "copilot": {"work_style": "do_it"}}
    if signal == "local_only":
        cfg["copilot"]["location"] = "this_computer_only"
    policy = resolve_turn_preferences(prompt, cfg=cfg, **inputs)
    assignment = rm.resolve_role_model("chat", cfg, turn_preferences=policy)
    assert assignment.is_session_default
    if expected is not None:
        assert policy.thinking == expected[1]
    tools = apply_mode_tools(None, ["describe_part", "new_part", "add_features", "delegate",
                                   "mission_start", "advise", "task_list"], preferences=policy)
    if signal == "chat":
        # Classification never downgrades the user's work style (do_it keeps edits).
        assert {"describe_part", "new_part", "add_features"} <= set(tools)
        assert not {"delegate", "mission_start", "advise"} & set(tools)
    if signal == "modelling":
        assert "add_features" in tools and "delegate" not in tools and policy.verify_battery
        for priority, model, thinking in (
            ("best_results", "strongest", "high"),
            ("fastest", "mid", "low"),
            ("lowest_cost", "mid", "low"),
        ):
            preferred = {**cfg, "copilot": {**cfg["copilot"], "priority": priority}}
            chosen_policy = resolve_turn_preferences(prompt, cfg=preferred)
            chosen = rm.resolve_role_model("chat", preferred, turn_preferences=chosen_policy)
            assert chosen.is_session_default and chosen_policy.thinking == thinking
    if signal == "analysis":
        assert policy.sim_precheck_block
    if signal == "mission":
        assert policy.delegation_depth == 2 and policy.task_list
        leaf = rm.resolve_role_model("worker", cfg, turn_preferences=policy)
        assert leaf.is_session_default
    if signal == "document_size":
        assert policy.schedule_compaction


@pytest.mark.parametrize("prompt,expected", [
    ("Create a bracket", 0),
    ("Create a bracket with parallel faces", 0),
    ("Create a bracket; do not use subagents", 0),
    ("Create a bracket and orchestrate the work", 2),
    ("Create a bracket and parallelize the work", 2),
    ("Create a bracket using parallel subagents", 2),
    ("Create a bracket, do not ask questions, and use subagents", 2),
    ("Create a bracket and use sub-agents", 2),
    ("Create a bracket; run tasks in parallel", 2),
])
def test_modelling_delegation_request(prompt, expected):
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.copilot_mode_tools import apply_mode_tools

    policy = resolve_turn_preferences(prompt, cfg={"copilot": {"work_style": "do_it"}},
                                      task_kind="modelling")
    assert policy.delegation_depth == expected
    assert f"Delegation depth limit: {expected};" in policy.prompt_guidance()
    tools = apply_mode_tools(None, ["delegate", "add_features"], preferences=policy)
    assert ("delegate" in tools) == (expected > 0)
    if expected:
        assert "do this work in this turn" not in policy.prompt_guidance()


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_explicit_delegation_depth_overrides_task_default(depth):
    from services.agent.preferences import resolve_turn_preferences

    cfg = {"copilot": {"work_style": "do_it"}, "agent": {"max_subagent_depth": depth}}
    for prompt in ("Create a bracket", "Create a bracket using subagents"):
        policy = resolve_turn_preferences(prompt, cfg=cfg, task_kind="modelling")
        assert policy.delegation_depth == depth
    mission = resolve_turn_preferences("Build two parts with subagents", cfg=cfg, task_kind="mission")
    assert mission.delegation_depth == depth


def test_answer_only_disables_requested_delegation():
    from services.agent.preferences import resolve_turn_preferences

    cfg = {"copilot": {"work_style": "answer_only"}, "agent": {"max_subagent_depth": 2}}
    policy = resolve_turn_preferences("Use subagents", cfg=cfg, task_kind="modelling")
    assert policy.delegation_depth == 0


# ---------------------------------------------------------------------------
# Redaction loopback HARD floor
# ---------------------------------------------------------------------------


class TestRedactionLoopback:
    def test_fixed_cloud_raises(self):
        cfg = {
            "ai_roles": {
                "redaction": {"provider": "anthropic", "model": "claude-4"},
            },
        }
        with pytest.raises(rm.RoleModelError, match="HARD-refuses non-loopback"):
            rm.resolve_role_model("redaction", cfg)

    def test_fixed_local_ok(self):
        cfg = {
            "ai_roles": {
                "redaction": {"provider": "llamacpp", "model": "qwen3-4b"},
            },
        }
        a = rm.resolve_role_model("redaction", cfg)
        assert a.provider == "llamacpp"
        assert a.mode == "fixed"

    def test_auto_with_only_cloud_raises(self):
        cfg = {
            "ai_roles": {"redaction": "auto"},
            "enabled_providers": [
                {"provider": "xai", "model": "grok-4.5"},
                {"provider": "openrouter", "model": "mistral-small"},
            ],
        }
        with pytest.raises(rm.RoleModelError, match="ranked Auto"):
            rm.resolve_role_model("redaction", cfg)

    def test_auto_does_not_guess_local_when_available(self):
        cfg = {
            "ai_roles": {"redaction": "auto"},
            "enabled_providers": [
                {"provider": "xai", "model": "grok-4.5"},
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        with pytest.raises(rm.RoleModelError, match="ranked Auto"):
            rm.resolve_role_model("redaction", cfg)

    def test_assert_redaction_loopback_direct(self):
        rm.assert_redaction_loopback("llamacpp")
        with pytest.raises(rm.RoleModelError):
            rm.assert_redaction_loopback("openai")
        with pytest.raises(rm.RoleModelError):
            rm.assert_redaction_loopback("xai-sub")

    def test_loopback_validation_rejects_unspecified_and_remote_endpoints(self):
        assert not rm.host_is_loopback("http://0.0.0.0:11434")
        assert not rm.provider_is_loopback("ollama", cfg={"ollama": {"base_url": "https://remote.example"}})
        assert not rm.provider_is_loopback("xai-oauth", base_url="http://localhost:8080")


# ---------------------------------------------------------------------------
# resolve_role_model fixed / auto / defaults
# ---------------------------------------------------------------------------


class TestResolveRoleModel:
    def test_fixed_assignment(self):
        cfg = {
            "advanced_overrides": True,
            "ai_roles": {
                "task": {"provider": "xai", "model": "grok-4.5"},
            },
            "enabled_providers": [
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        a = rm.resolve_role_model("task", cfg)
        assert a.mode == "fixed"
        assert a.provider == "xai"
        assert a.model == "grok-4.5"

    def test_default_when_unset(self):
        cfg = {
            "assignment_mode": "advanced",
            "ai_roles": {},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        a = rm.resolve_role_model("default", cfg)
        assert a.is_session_default

    def test_get_ai_roles_defaults(self):
        roles = rm.get_ai_roles({})
        for r in rm.KNOWN_ROLES:
            assert roles[r] == "default"

    def test_known_roles_includes_sketch2d_model3d(self):
        assert "sketch2d" in rm.KNOWN_ROLES
        assert "model3d" in rm.KNOWN_ROLES

    def test_set_ai_roles_no_persist(self):
        roles = rm.set_ai_roles(
            {"task": {"provider": "xai", "model": "grok-4.5"}},
            cfg={},
            persist=False,
        )
        assert roles["task"] == {"provider": "xai", "model": "grok-4.5"}
        assert roles["default"] == "default"

    def test_resolve_follows_chat_without_config(self):
        assert rm.resolve_role_model("task", {}).is_session_default

    def test_legacy_auto_override_requires_reassignment(self):
        cfg = {
            "assignment_mode": "advanced",
            "advanced_overrides": True,
            "ai_roles": {"task": "auto"},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        with pytest.raises(rm.RoleModelError, match="ranked Auto"):
            rm.resolve_role_model("task", cfg)


# ---------------------------------------------------------------------------
# Spawn-depth policy
# ---------------------------------------------------------------------------


class TestSpawnDepthPolicy:
    def test_defaults_by_letter(self):
        assert rm.max_spawn_depth_for_capability("A") == 2
        assert rm.max_spawn_depth_for_capability("B") == 4
        assert rm.max_spawn_depth_for_capability("C") == 8
        assert rm.max_spawn_depth_for_capability(None) == 8

    def test_config_override(self):
        cfg = {"ai_roles_policy": {"max_spawn_depth": 3}}
        assert rm.max_spawn_depth_for_capability("C", cfg=cfg) == 3

    def test_exceeds_max_refused(self):
        pol = rm.check_spawn_depth_policy(3, letter="A")
        assert pol["ok"] is False
        assert pol["max_spawn_depth"] == 2
        assert "exceeds" in pol["reason"]

    def test_within_max_ok(self):
        pol = rm.check_spawn_depth_policy(2, letter="A")
        assert pol == {
            "ok": True, "max_spawn_depth": 2, "depth": 2, "reason": "within policy",
        }


# ---------------------------------------------------------------------------
# RPC registration (import-level, no worker process)
# ---------------------------------------------------------------------------


class TestRpcRegistration:
    def test_methods_registered_on_worker(self):
        from services.agent.worker_rpc import WORKER_METHODS

        for name in (
            "ai_roles_get",
            "ai_roles_set",
        ):
            assert name in WORKER_METHODS, f"missing RPC {name}"
            assert callable(WORKER_METHODS[name])

    def test_ai_roles_get_shape(self, monkeypatch, tmp_path):
        from services.agent import worker_rpc as rpc

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "services.agent.config.load_config", lambda: {}
        )
        out = rpc.ROLE_MODELS_METHODS["ai_roles_get"]({}, lambda *_a, **_k: None)
        assert out["ok"] is True
        assert "roles" in out
        for r in rm.KNOWN_ROLES:
            assert r in out["roles"]

    def test_ai_roles_set_refuses_cloud_redaction(self, monkeypatch, tmp_path):
        from services.agent import worker_rpc as rpc

        monkeypatch.setattr("services.paths.CONFIG", tmp_path / "config.json")
        out = rpc.ROLE_MODELS_METHODS["ai_roles_set"](
            {"roles": {"redaction": {"provider": "openai", "model": "gpt-5"}}},
            lambda *_a, **_k: None,
        )
        assert out["ok"] is False
        assert "loopback" in out["reason"].lower() or "HARD" in out["reason"]


def test_simple_size_routing_and_aliases():
    cfg = {
        "assignment_mode": "simple",
        "model_sizes": {
            size: {"mode": "fixed", "provider": "codex-sub", "model": size + "-model"}
            for size in rm.MODEL_SIZES
        },
        "ai_roles": {"task": {"provider": "anthropic", "model": "old", "thinking": "high"}},
    }
    for role, size in rm.ROLE_SIZES.items():
        resolved = rm.resolve_role_model(role, cfg)
        assert (resolved.provider, resolved.model) == ("codex-sub", size + "-model")
        assert rm.harness_role_for_mission_role(role) == rm.ROLE_MODEL_KEY_ALIASES.get(role, role)
    assert set(rm.KNOWN_ROLES) - {"redaction", "private"} <= set(rm.ROLE_SIZES)
    assert rm.get_role_thinking("task", cfg) == "auto"
    assert rm.resolve_role_model("drawing_detailer", cfg).model == "medium-model"


def test_simple_empty_and_legacy_defaults():
    view = rm.role_settings_view({})
    assert view["assignment_mode"] == "simple"
    assert view["advanced_overrides"] is False
    assert all(value == {"mode": "default", "provider": "", "model": "", "thinking": "auto"}
               for value in view["model_sizes"].values())
    assert rm.resolve_role_model("memory", {}).is_session_default
    assert rm.get_assignment_mode({"ai_roles": {}}) == "simple"
    assert rm.get_assignment_mode({"ai_roles": {"task": {"thinking": "high"}}}) == "advanced"
    assert rm.get_assignment_mode({"ai_roles": {"task": "default"}}) == "advanced"


@pytest.mark.parametrize("role", ["chat", "worker", "task"])
def test_legacy_explicit_auto_stays_unresolved(role):
    cfg = {"advanced_overrides": True, "ai_roles": {role: "auto"}}
    assert rm.get_assignment_mode(cfg) == "advanced"
    with pytest.raises(rm.RoleModelError, match="ranked Auto"):
        rm.resolve_role_model(role, cfg)
    assert rm.get_ai_roles(cfg)[role] == "auto"

def test_legacy_auto_survives_unrelated_assignment_save(monkeypatch, tmp_path):
    from services.agent import config

    path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", path)
    path.write_text(json.dumps({"assignment_mode": "simple",
                                "model_sizes": {"large": "auto"}}))
    rm.update_role_settings({"model_sizes": {"small": "default"}})
    cfg = config.read_config_uncached()
    assert rm.get_chat_assignment(cfg)["mode"] == "auto"
    assert rm.get_model_sizes(cfg)["large"]["mode"] == "auto"
    with pytest.raises(rm.RoleModelError, match="ranked Auto"):
        rm.resolve_role_model("chat", cfg)


def test_settings_switch_partial_updates_and_atomic_rejection(monkeypatch, tmp_path):
    from services.agent import worker_rpc as rpc

    config_path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", config_path)
    original = {"advanced_overrides": True,
                "ai_roles": {"task": {"provider": "anthropic", "model": "old", "thinking": "high"}},
                "unrelated": {"keep": 1}}
    config_path.write_text(json.dumps(original), encoding="utf-8")
    call = lambda params: rpc._ai_roles_set(params, lambda *_args: None)
    assert rpc._ai_roles_get({}, lambda *_args: None)["assignment_mode"] == "advanced"
    simple = call({"assignment_mode": "simple", "model_sizes": {
        "medium": {"mode": "fixed", "provider": "codex-sub", "model": "mid"}}})
    assert simple["ok"] and simple["roles"]["task"] == original["ai_roles"]["task"]
    assert simple["advanced_overrides"] is True and simple["active_role_overrides"] == ["task"]
    assert simple["model_sizes"]["small"]["mode"] == "default"
    assert rm.resolve_role_model("task", json.loads(config_path.read_text())).model == "old"
    changed = call({"model_sizes": {"medium": {"model": "new-mid"}}})
    assert changed["model_sizes"]["medium"]["provider"] == "codex-sub"
    assert changed["model_sizes"]["medium"]["model"] == "new-mid"
    before = config_path.read_text()
    bad = call({"assignment_mode": "advanced", "model_sizes": {"large": {"mode": "fixed"}}})
    assert not bad["ok"] and config_path.read_text() == before
    bad = call({"roles": {"task": {"thinking": "impossible"}},
                "model_sizes": {"small": "default"}})
    assert not bad["ok"] and config_path.read_text() == before
    advanced = call({"assignment_mode": "advanced"})
    assert advanced["ok"] and advanced["roles"]["task"] == original["ai_roles"]["task"]
    cfg = json.loads(config_path.read_text())
    assert cfg["unrelated"] == original["unrelated"]
    assert rm.resolve_role_model("task", cfg).model == "old"
    assert rm.get_role_thinking("task", cfg) == "high"
    assert call({"roles": {"task": {"mode": "tier"}}})["model_sizes"]["medium"]["model"] == "new-mid"
    assert rm.resolve_role_model("task", json.loads(config_path.read_text())).model == "new-mid"

def test_independent_chat_pin_and_dormant_override_migration(monkeypatch, tmp_path):
    from services.agent import config

    path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", path)
    path.write_text(json.dumps({
        "assignment_mode": "simple",
        "model_sizes": {"large": {"provider": "codex-sub", "model": "former-chat"}},
        "ai_roles": {"task": {"provider": "ollama", "model": "dormant"}},
    }))
    rm.update_role_settings({"model_sizes": {"large": {"provider": "anthropic", "model": "tier"}}})
    cfg = config.read_config_uncached()
    assert rm.resolve_role_model("chat", cfg).model == "former-chat"
    assert rm.resolve_role_model("task", cfg).is_session_default
    assert cfg["ai_roles"]["task"]["model"] == "dormant"
    rm.update_role_settings({"roles": {"task": {"provider": "ollama", "model": "chosen"}},
                             "advanced_overrides": True})
    cfg = config.read_config_uncached()
    assert rm.resolve_role_model("task", cfg).model == "chosen"
    rm.update_role_settings({"chat_assignment": {"provider": "xai", "model": "pinned"}})
    cfg = config.read_config_uncached()
    assert rm.resolve_role_model("chat", cfg).model == "pinned"
    assert rm.resolve_role_model("task", cfg).model == "chosen"

def test_legacy_chat_default_preserves_configured_pair_on_settings_save(monkeypatch, tmp_path):
    from services.agent import config
    from services.agent.online_turn import resolve_model_and_effort

    path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", path)
    path.write_text(json.dumps({"provider": "anthropic", "model": "legacy"}))
    rm.update_role_settings({"model_sizes": {"small": "default"}})
    cfg = config.read_config_uncached()
    assert rm.get_chat_assignment(cfg) == {"mode": "fixed", "provider": "anthropic", "model": "legacy"}
    rm.update_role_settings({"chat_assignment": "default"})
    cfg = config.read_config_uncached()
    _, provider, model, assignment = resolve_model_and_effort("hello", {}, cfg, None)
    assert assignment.is_session_default and provider == model == ""



def test_simple_private_and_redaction_remain_local(monkeypatch, tmp_path):
    from services.agent import worker_rpc as rpc

    monkeypatch.setattr("services.paths.CONFIG", tmp_path / "config.json")
    cfg = {"assignment_mode": "simple",
           "model_sizes": {"large": {"provider": "anthropic", "model": "cloud"}},
           "ai_roles": {"redaction": {"provider": "ollama", "model": "local"},
                        "private": {"provider": "ollama", "model": "local"}}}
    assert rm.resolve_role_model("redaction", cfg).provider == "ollama"
    assert rm.resolve_role_model("private", cfg).provider == "ollama"
    cfg["ai_roles"]["redaction"] = {"provider": "anthropic", "model": "cloud"}
    with pytest.raises(rm.RoleModelError):
        rm.resolve_role_model("redaction", cfg)
    cfg["ai_roles"]["private"] = {"provider": "anthropic", "model": "cloud"}
    with pytest.raises(rm.RoleModelError):
        rm.resolve_role_model("private", cfg)
    for role in ("redaction", "private"):
        out = rpc._ai_roles_set({"roles": {role: "default"}}, lambda *_args: None)
        assert not out["ok"]
        assert role not in rm.ROLE_SIZES
    from services.agent.omp_roles import _settings_for_role
    with pytest.raises(rm.RoleModelError):
        _settings_for_role("private", {"config": cfg, "provider": "anthropic", "model": "cloud"})


def test_simple_online_and_nested_role_settings(monkeypatch):
    from services.agent.omp_roles import _settings_for_role
    from services.agent.online_turn import resolve_model_and_effort

    cfg = {"assignment_mode": "simple", "provider": "anthropic", "model": "chat",
           "model_sizes": {
               "large": {"provider": "codex-sub", "model": "large-model"},
               "medium": {"provider": "codex-sub", "model": "medium-model"},
           }}
    _, provider, model, _ = resolve_model_and_effort("hello", {}, cfg, None)
    assert (provider, model) == ("codex-sub", "large-model")
    nested = _settings_for_role("drawing_detailer", {"config": cfg, "settings": {
        "provider": "anthropic", "model": "chat"}, "_explicit_role_settings": ()})
    assert (nested["provider"], nested["model"]) == ("codex-sub", "medium-model")
    _, provider, model, _ = resolve_model_and_effort(
        "hello", {}, {"provider": "anthropic", "model": "chat"}, None)
    assert (provider, model) == ("anthropic", "chat")


def test_advanced_explicit_specialist_model_and_thinking_win_over_alias():
    cfg = {"assignment_mode": "advanced", "advanced_overrides": True, "ai_roles": {
        "general_agent": {"provider": "ollama", "model": "old-worker", "thinking": "low"},
        "task": {"provider": "anthropic", "model": "task", "thinking": "high"},
        "geometry_designer": {"provider": "ollama", "model": "old-design", "thinking": "medium"},
        "model3d": {"provider": "anthropic", "model": "new-design", "thinking": "xhigh"},
    }}
    for role, model, effort in (("general_agent", "old-worker", "low"),
                                ("geometry_designer", "old-design", "medium")):
        assert rm.mission_role_key(role, cfg) == role
        assert rm.resolve_role_model(role, cfg).model == model
        assert rm.resolve_role_thinking(role, cfg) == effort
    assert rm.resolve_role_model("feature_modeler", cfg).model == "new-design"


def test_local_only_rejects_fixed_size_without_turn_preferences(monkeypatch):
    from services.agent.omp_roles import _settings_for_role
    from services.dag import node_runner

    cfg = {"assignment_mode": "simple", "copilot": {"location": "this_computer_only"},
           "model_sizes": {"medium": {"provider": "anthropic", "model": "cloud"},
                           "large": {"provider": "ollama", "model": "local"}}}
    with pytest.raises(rm.LocalOnlyRoleModelError):
        rm.resolve_role_model("task", cfg)
    with pytest.raises(rm.LocalOnlyRoleModelError):
        _settings_for_role("general_agent", {"config": cfg})
    with pytest.raises(rm.LocalOnlyRoleModelError):
        _settings_for_role("general_agent", {"config": cfg, "settings": {
            "provider": "anthropic", "model": "cloud"}})
    monkeypatch.setattr("services.agent.config.load_config", lambda: cfg)
    with pytest.raises(rm.LocalOnlyRoleModelError):
        node_runner._resolve_role_settings({"role": "general_agent"})


def test_dag_default_does_not_substitute_persisted_chat_model(monkeypatch):
    from services.dag import node_runner

    cfg = {"assignment_mode": "simple", "provider": "anthropic", "model": "stored-chat",
           "model_sizes": {"large": {"provider": "codex-sub", "model": "live-chat"}}}
    monkeypatch.setattr("services.agent.config.load_config", lambda: cfg)
    monkeypatch.setattr("services.agent.agent_events.emit_event", lambda *_a, **_kw: None)
    context = {"role": "general_agent"}
    settings = node_runner._resolve_role_settings(context)
    assert "provider" not in settings and "model" not in settings
    assert context["role_model"]["mode"] == "default"


def test_online_follow_advanced_chat_fixed_default_and_manual_override():
    from services.agent.online_turn import resolve_model_and_effort

    cfg = {"assignment_mode": "advanced", "provider": "anthropic", "model": "chat",
           "ai_roles": {"chat": {"provider": "codex-sub", "model": "pinned", "thinking": "high"}}}
    pref, provider, model, assignment = resolve_model_and_effort("hello", {}, cfg, None)
    assert (provider, model, assignment.mode, pref.thinking) == ("codex-sub", "pinned", "fixed", "high")
    _, provider, model, assignment = resolve_model_and_effort(
        "hello", {"provider": "ollama", "model": "manual"}, cfg, None)
    assert (provider, model, assignment) == ("ollama", "manual", None)

    cfg["ai_roles"]["chat"] = {"mode": "default", "thinking": "high"}
    pref, provider, model, assignment = resolve_model_and_effort("hello", {}, cfg, None)
    assert (provider, model, assignment.mode, pref.thinking) == ("anthropic", "chat", "default", "high")


def test_online_follow_simple_auto_requires_reassignment():
    from services.agent.online_turn import resolve_model_and_effort

    cfg = {"assignment_mode": "simple", "model_sizes": {"large": {"mode": "auto"}}}
    with pytest.raises(rm.RoleModelError, match="ranked Auto"):
        resolve_model_and_effort("hello", {}, cfg, None)


def test_advanced_root_chat_follows_default_unless_legacy_chat_is_explicit():
    from services.agent.online_turn import resolve_model_and_effort

    cfg = {"assignment_mode": "advanced", "ai_roles": {
        "default": {"provider": "codex-sub", "model": "default-model", "thinking": "high"}}}
    view = rm.role_settings_view(cfg)
    assert view["chat_assignment"] == {"mode": "fixed", "provider": "codex-sub", "model": "default-model"}
    pref, provider, model, assignment = resolve_model_and_effort("hello", {}, cfg, None)
    assert (provider, model, assignment.mode, pref.thinking) == ("codex-sub", "default-model", "fixed", "high")

    cfg["ai_roles"]["chat"] = {"provider": "ollama", "model": "legacy-chat", "thinking": "low"}
    view = rm.role_settings_view(cfg)
    assert view["chat_assignment"] == {"mode": "fixed", "provider": "ollama", "model": "legacy-chat"}
    pref, provider, model, assignment = resolve_model_and_effort("hello", {}, cfg, None)
    assert (provider, model, assignment.mode, pref.thinking) == ("ollama", "legacy-chat", "fixed", "low")
    assert rm.role_settings_view({})["chat_assignment"]["mode"] == "default"


# ---------------------------------------------------------------------------
# advanced_overrides switch, follow-chat as tier, size-reference overrides, tier thinking
# ---------------------------------------------------------------------------


def _tier_cfg(**extra):
    cfg = {"assignment_mode": "advanced", "model_sizes": {
        size: {"mode": "fixed", "provider": "codex-sub", "model": size + "-model"}
        for size in rm.MODEL_SIZES}}
    cfg.update(extra)
    return cfg


def _config_file(monkeypatch, tmp_path, content):
    path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", path)
    path.write_text(json.dumps(content))
    return path


def test_switch_is_strictly_boolean_and_missing_means_off():
    assert rm.advanced_overrides_enabled({}) is False
    assert rm.advanced_overrides_enabled(None) is False
    assert rm.advanced_overrides_enabled({"advanced_overrides": "true"}) is False
    assert rm.advanced_overrides_enabled({"advanced_overrides": 1}) is False
    assert rm.advanced_overrides_enabled({"advanced_overrides": True}) is True


def test_retained_override_is_ignored_while_switch_is_off_and_applies_when_on():
    override = {"provider": "ollama", "model": "pin", "thinking": "high"}
    cfg = _tier_cfg(ai_roles={"task": override})
    assert rm.resolve_role_model("task", cfg).model == "medium-model"
    assert rm.resolve_role_model("general_agent", cfg).model == "medium-model"
    assert rm.mission_role_key("general_agent", cfg) == "task"
    assert rm.get_role_thinking("task", cfg) == "auto"
    view = rm.role_settings_view(cfg)
    assert view["advanced_overrides"] is False
    assert view["active_role_overrides"] == ["task"]  # retained, whether or not applied
    assert view["roles"]["task"] == override

    cfg["advanced_overrides"] = True
    pinned = rm.resolve_role_model("task", cfg)
    assert (pinned.provider, pinned.model, pinned.source) == ("ollama", "pin", "ai_roles")
    assert rm.get_role_thinking("task", cfg) == "high"
    assert rm.role_settings_view(cfg)["advanced_overrides"] is True


def test_save_with_switch_off_keeps_retained_override_and_switch_round_trips(monkeypatch, tmp_path):
    from services.agent import config

    _config_file(monkeypatch, tmp_path, {
        "assignment_mode": "advanced",
        "ai_roles": {"task": {"provider": "ollama", "model": "pin", "thinking": "high"}},
        "model_sizes": {"medium": {"provider": "codex-sub", "model": "mid"}}})
    view = rm.update_role_settings({"model_sizes": {"small": {"provider": "codex-sub", "model": "tiny"}}})
    assert view["advanced_overrides"] is False and view["active_role_overrides"] == ["task"]
    cfg = config.read_config_uncached()
    assert cfg["advanced_overrides"] is False and cfg["active_role_overrides"] == ["task"]
    assert cfg["ai_roles"]["task"] == {"provider": "ollama", "model": "pin", "thinking": "high"}
    assert rm.resolve_role_model("task", cfg).model == "mid"
    assert rm.get_role_thinking("task", cfg) == "auto"

    assert rm.update_role_settings({"advanced_overrides": True})["advanced_overrides"] is True
    cfg = config.read_config_uncached()
    assert rm.resolve_role_model("task", cfg).model == "pin"
    assert rm.get_role_thinking("task", cfg) == "high"

    rm.update_role_settings({"advanced_overrides": False})
    cfg = config.read_config_uncached()
    assert cfg["ai_roles"]["task"]["model"] == "pin" and cfg["active_role_overrides"] == ["task"]
    assert rm.resolve_role_model("task", cfg).model == "mid"


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [True]])
def test_advanced_overrides_must_be_a_boolean(monkeypatch, tmp_path, value):
    from services.agent import worker_rpc as rpc

    path = _config_file(monkeypatch, tmp_path, {})
    out = rpc._ai_roles_set({"advanced_overrides": value}, lambda *_args: None)
    assert not out["ok"] and "advanced_overrides" in out["reason"]
    assert json.loads(path.read_text()) == {}


def test_advanced_overrides_alone_is_a_valid_save(monkeypatch, tmp_path):
    from services.agent import config, worker_rpc as rpc

    _config_file(monkeypatch, tmp_path, {})
    assert rpc._ai_roles_get({}, lambda *_args: None)["advanced_overrides"] is False
    out = rpc._ai_roles_set({"advanced_overrides": True}, lambda *_args: None)
    assert out["ok"] and out["advanced_overrides"] is True
    assert config.read_config_uncached()["advanced_overrides"] is True
    assert rpc._ai_roles_set({"advanced_overrides": False}, lambda *_args: None)["advanced_overrides"] is False


@pytest.mark.parametrize("entry", ["default", {"mode": "default"}, {"mode": "default", "thinking": "high"}])
@pytest.mark.parametrize("switch", [False, True])
def test_follow_chat_override_resolves_as_no_override(entry, switch):
    cfg = _tier_cfg(advanced_overrides=switch, ai_roles={"task": entry, "general_agent": entry})
    cfg["model_sizes"]["medium"]["thinking"] = "low"
    for role in ("task", "general_agent"):
        assignment = rm.resolve_role_model(role, cfg)
        assert (assignment.model, assignment.source) == ("medium-model", "model_sizes")
        assert rm.get_role_thinking(role, cfg) == "low"  # the tier's, never the entry's
    assert rm.mission_role_key("general_agent", cfg) == "task"
    assert rm.stored_role_overrides(cfg) == set() == rm.active_role_overrides(cfg)
    assert rm.role_settings_view(cfg)["active_role_overrides"] == []


def test_ai_roles_set_default_is_tier_and_clears_the_override(monkeypatch, tmp_path):
    from services.agent import config, worker_rpc as rpc

    _config_file(monkeypatch, tmp_path, {
        "assignment_mode": "advanced", "advanced_overrides": True,
        "ai_roles": {"task": {"provider": "ollama", "model": "pin"},
                     "planning": {"provider": "ollama", "model": "plan"}},
        "model_sizes": {"medium": {"provider": "codex-sub", "model": "mid"},
                        "large": {"provider": "codex-sub", "model": "big"}}})
    call = lambda params: rpc._ai_roles_set(params, lambda *_args: None)
    out = call({"roles": {"task": {"mode": "default"}, "planning": "default"}})
    assert out["ok"] and out["active_role_overrides"] == []
    cfg = config.read_config_uncached()
    assert cfg["active_role_overrides"] == []
    assert rm.resolve_role_model("task", cfg).model == "mid"
    assert rm.resolve_role_model("planning", cfg).model == "big"

    # Root chat keeps its own Default semantics: not a tier role, so the entry survives.
    out = call({"roles": {"chat": {"mode": "default", "thinking": "high"}}})
    assert out["ok"] and out["active_role_overrides"] == ["chat"]
    assert rm.get_role_thinking("chat", config.read_config_uncached()) == "high"


def test_size_reference_override_tracks_the_current_size_assignment(monkeypatch, tmp_path):
    from services.agent import config

    _config_file(monkeypatch, tmp_path, {
        "assignment_mode": "advanced", "advanced_overrides": True,
        "model_sizes": {size: {"provider": "codex-sub", "model": size + "-v1"}
                        for size in rm.MODEL_SIZES}})
    stored = {"mode": "size", "size": "small", "thinking": "high"}
    view = rm.update_role_settings({"roles": {"cam": {**stored, "provider": "stale", "model": "stale"}}})
    assert view["roles"]["cam"] == stored and view["active_role_overrides"] == ["cam"]
    cfg = config.read_config_uncached()
    assert cfg["ai_roles"]["cam"] == stored  # exact normalized shape; provider/model dropped
    pinned = rm.resolve_role_model("cam", cfg)  # cam is a large role; the override points at small
    assert (pinned.model, pinned.source, pinned.mode) == ("small-v1", "model_sizes", "fixed")
    assert rm.resolve_role_model("cam_programmer", cfg).model == "small-v1"

    rm.update_role_settings({"model_sizes": {"small": {"model": "small-v2"}}})
    cfg = config.read_config_uncached()
    assert rm.resolve_role_model("cam", cfg).model == "small-v2"
    assert cfg["ai_roles"]["cam"] == stored

    rm.update_role_settings({"roles": {"cam": {"thinking": "xhigh"}}})  # partial: keeps the size
    cfg = config.read_config_uncached()
    assert cfg["ai_roles"]["cam"] == {"mode": "size", "size": "small", "thinking": "xhigh"}
    rm.update_role_settings({"roles": {"cam": {"mode": "size", "size": "large", "thinking": "auto"}}})
    cfg = config.read_config_uncached()
    assert cfg["ai_roles"]["cam"] == {"mode": "size", "size": "large"}
    assert rm.resolve_role_model("cam", cfg).model == "large-v1"

    # Flag off: the retained reference is ignored and the role uses its own tier (large).
    rm.update_role_settings({"advanced_overrides": False})
    cfg = config.read_config_uncached()
    assert cfg["ai_roles"]["cam"] == {"mode": "size", "size": "large"}
    assert rm.resolve_role_model("cam", cfg).model == "large-v1"
    rm.update_role_settings({"roles": {"cam": {"mode": "size", "size": "medium"}},
                             "advanced_overrides": False})
    assert rm.resolve_role_model("cam", config.read_config_uncached()).model == "large-v1"
    assert rm.resolve_role_model("cam", {**config.read_config_uncached(),
                                         "advanced_overrides": True}).model == "medium-v1"


def test_size_reference_honours_the_referenced_tier_mode():
    cfg = _tier_cfg(advanced_overrides=True, ai_roles={"cam": {"mode": "size", "size": "small"}})
    cfg["model_sizes"]["small"] = {"mode": "default"}
    assert rm.resolve_role_model("cam", cfg).is_session_default
    cfg["model_sizes"]["small"] = {"mode": "auto"}
    with pytest.raises(rm.RoleModelError, match="ranked Auto"):
        rm.resolve_role_model("cam", cfg)
    cfg["model_sizes"]["small"] = {"provider": "anthropic", "model": "cloud"}
    cfg["copilot"] = {"location": "this_computer_only"}
    with pytest.raises(rm.LocalOnlyRoleModelError):
        rm.resolve_role_model("cam", cfg)


@pytest.mark.parametrize("role", ["redaction", "private", "chat"])
def test_size_reference_is_rejected_for_restricted_and_chat_seats(monkeypatch, tmp_path, role):
    from services.agent import worker_rpc as rpc

    path = _config_file(monkeypatch, tmp_path, {})
    out = rpc._ai_roles_set({"roles": {role: {"mode": "size", "size": "small"}}}, lambda *_args: None)
    assert not out["ok"] and "size" in out["reason"]
    assert json.loads(path.read_text()) == {}
    stored = _tier_cfg(ai_roles={"redaction": {"mode": "size", "size": "small"},
                                 "private": {"mode": "size", "size": "small"}})
    for restricted in ("redaction", "private"):
        with pytest.raises(rm.RoleModelError, match="loopback"):
            rm.resolve_role_model(restricted, stored)


@pytest.mark.parametrize("value", [
    {"mode": "size"},
    {"mode": "size", "size": "huge"},
    {"mode": "size", "size": ""},
    {"size": "small"},
    {"mode": "fixed", "size": "small", "provider": "p", "model": "m"},
])
def test_size_reference_validation(value):
    with pytest.raises(ValueError):
        rm._validate_assignment(value, allow_thinking=True, allow_size=True)
    with pytest.raises(ValueError):
        rm.update_role_settings({"roles": {"task": value}})


@pytest.mark.parametrize("params", [
    {"model_sizes": {"small": {"mode": "size", "size": "large"}}},
    {"chat_assignment": {"mode": "size", "size": "large"}},
])
def test_size_reference_is_only_a_role_override_form(params):
    with pytest.raises(ValueError):
        rm.update_role_settings(params)


def test_tier_thinking_round_trips_and_merges_partial_updates(monkeypatch, tmp_path):
    from services.agent import config

    _config_file(monkeypatch, tmp_path, {})
    assert all(value["thinking"] == "auto" for value in rm.get_model_sizes({}).values())
    view = rm.update_role_settings({"model_sizes": {"medium": {"thinking": "high"}}})
    assert view["model_sizes"]["medium"] == {
        "mode": "default", "provider": "", "model": "", "thinking": "high"}
    view = rm.update_role_settings({"model_sizes": {"medium": {"provider": "codex-sub", "model": "mid"}}})
    assert view["model_sizes"]["medium"] == {
        "mode": "fixed", "provider": "codex-sub", "model": "mid", "thinking": "high"}  # model change keeps it
    view = rm.update_role_settings({"model_sizes": {"medium": {"thinking": "low"}}})
    assert view["model_sizes"]["medium"] == {
        "mode": "fixed", "provider": "codex-sub", "model": "mid", "thinking": "low"}  # thinking keeps model
    view = rm.update_role_settings({"model_sizes": {"medium": "default"}})
    assert view["model_sizes"]["medium"] == {
        "mode": "default", "provider": "", "model": "", "thinking": "low"}  # mode switch keeps it
    view = rm.update_role_settings({"model_sizes": {"medium": {"thinking": "auto"}}})
    assert view["model_sizes"]["medium"]["thinking"] == "auto"
    assert rm.get_model_sizes(config.read_config_uncached()) == view["model_sizes"]
    assert view["chat_assignment"] == {"mode": "default", "provider": "", "model": ""}  # no thinking key
    with pytest.raises(ValueError, match="thinking"):
        rm.update_role_settings({"model_sizes": {"small": {"thinking": "impossible"}}})


def test_tier_thinking_applies_to_every_role_dispatched_through_the_tier():
    cfg = _tier_cfg()
    cfg["model_sizes"]["medium"]["thinking"] = "low"
    cfg["model_sizes"]["large"]["thinking"] = "xhigh"
    assert rm.get_model_sizes(cfg)["medium"] == {
        "mode": "fixed", "provider": "codex-sub", "model": "medium-model", "thinking": "low"}
    assert rm.get_model_sizes(cfg)["small"]["thinking"] == "auto"
    for role, size in rm.ROLE_SIZES.items():
        if role == "chat":
            continue
        expected = {"medium": "low", "large": "xhigh"}.get(size, "auto")
        assert rm.get_role_thinking(role, cfg) == expected, role
        assert rm.resolve_role_thinking(role, cfg) == (None if expected == "auto" else expected), role
    # Root chat and the restricted seats are never tier-dispatched.
    for role in ("chat", "private", "redaction"):
        assert rm.get_role_thinking(role, cfg) == "auto", role


def test_auto_tier_defers_to_the_dispatcher_and_passes_nothing_without_one():
    cfg = _tier_cfg()
    assert rm.resolve_role_thinking("task", cfg) is None
    assert rm.resolve_role_thinking("task", cfg, dispatch_effort="med") == "medium"
    cfg["model_sizes"]["medium"]["thinking"] = "high"
    assert rm.resolve_role_thinking("task", cfg, dispatch_effort="low") == "high"
    assert rm.clamp_subagent_effort(rm.resolve_role_thinking("task", cfg)) == "high"


def test_override_thinking_wins_over_tier_thinking_and_size_reference_inherits_it():
    cfg = _tier_cfg(advanced_overrides=True, ai_roles={
        "task": {"provider": "ollama", "model": "pin", "thinking": "minimal"},
        "planning": {"provider": "ollama", "model": "plan"},
        "vision": {"mode": "size", "size": "small"},
        "sim": {"mode": "size", "size": "small", "thinking": "max"}})
    for size, level in {"small": "low", "medium": "medium", "large": "high"}.items():
        cfg["model_sizes"][size]["thinking"] = level
    assert rm.get_role_thinking("task", cfg) == "minimal"  # own thinking beats the tier's
    assert rm.get_role_thinking("planning", cfg) == "auto"  # fixed override: dispatcher decides
    assert rm.get_role_thinking("vision", cfg) == "low"  # size reference without its own thinking
    assert rm.get_role_thinking("sim", cfg) == "max"  # size reference with its own thinking
    assert rm.get_role_thinking("advisor", cfg) == "high"  # no override: its own tier (large)
    assert rm.resolve_role_thinking("planning", cfg, dispatch_effort="medium") == "medium"
    cfg["advanced_overrides"] = False  # retained overrides are ignored for model AND thinking
    assert [rm.get_role_thinking(role, cfg) for role in ("task", "planning", "vision", "sim")] == [
        "medium", "high", "high", "high"]
