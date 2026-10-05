"""Tests for services/agent/role_models.py — rankings merge, auto pick, redaction.

Covers WS2/WS4 acceptance:
  * rankings merge precedence: user > measured > shipped
  * auto pick honours enabled providers + rank threshold
  * redaction role HARD-refuses non-loopback
  * spawn-depth policy (max_spawn_depth + decreasing difficulty)
No network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from services.agent import role_models as rm


@pytest.mark.parametrize("signal,prompt,inputs,expected", [
    ("chat", "What is a fillet?", {}, ("local", "low")),
    ("modelling", "Create a bracket", {}, ("mid", "medium")),
    ("analysis", "Analyse thermal stress", {}, ("strongest", "high")),
    ("mission", "Build two parts", {"feature_count": 5}, ("strong", "high")),
    ("document_size", "Create a bracket", {"feature_count": 41, "context_tokens": 500, "context_window": 1000}, ("strong", "medium")),
    ("availability", "What is a fillet?", {}, ("served", "low")),
    ("local_only", "Analyse thermal stress", {}, None),
])
def test_auto_rules_table(monkeypatch, signal, prompt, inputs, expected):
    from services.agent.preferences import resolve_turn_preferences
    from services.agent.copilot_mode_tools import apply_mode_tools

    monkeypatch.setattr("services.agent.model_manager.classify_bundled_task", lambda *a, **k: None)
    cfg = {"copilot": {"work_style": "do_it"}}
    candidates = [
        {"provider": "llamacpp", "model": "local"},
        {"provider": "vllm", "model": "served"},
        {"provider": "anthropic-sub", "model": "mid"},
        {"provider": "codex-sub", "model": "strong"},
        {"provider": "anthropic", "model": "strongest"},
    ]
    ranks = {model: rm.ModelRank(model_id=model, rank=rank, source="user")
             for model, rank in (("local", 1), ("served", 1), ("mid", 3), ("strong", 4), ("strongest", 5))}
    if signal == "availability":
        candidates[0]["authenticated"] = False
    if signal == "local_only":
        cfg["copilot"]["location"] = "this_computer_only"
        candidates = candidates[2:]
    policy = resolve_turn_preferences(prompt, cfg=cfg, **inputs)
    if expected is None:
        with pytest.raises(rm.RoleModelError, match="local model"):
            rm.resolve_role_model("chat", cfg, request=prompt, turn_preferences=policy,
                                  enabled=candidates, rankings=ranks)
        return
    assignment = rm.resolve_role_model("chat", cfg, request=prompt, turn_preferences=policy,
                                      enabled=candidates, rankings=ranks)
    assert (assignment.model, policy.thinking) == expected
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
            chosen = rm.resolve_role_model("chat", preferred, request=prompt,
                turn_preferences=chosen_policy, enabled=candidates, rankings=ranks)
            assert (chosen.model, chosen_policy.thinking) == (model, thinking)
    if signal == "analysis":
        assert policy.sim_precheck_block
    if signal == "mission":
        assert policy.delegation_depth == 2 and policy.task_list and policy.council
        leaf = rm.resolve_role_model("worker", cfg, request=prompt, turn_preferences=policy,
                                    enabled=candidates, rankings=ranks)
        assert leaf.model == "local"
    if signal == "document_size":
        assert policy.schedule_compaction and policy.trim_tool_schemas


_SHIPPED = Path(__file__).resolve().parent / "model_rankings.yaml"


# ---------------------------------------------------------------------------
# Rankings merge precedence
# ---------------------------------------------------------------------------


class TestRankingsMerge:
    def test_shipped_file_loads(self):
        ranks = rm.load_rankings(include_measured=False)
        assert "grok-4.5" in ranks
        assert ranks["grok-4.5"].rank == 5
        assert ranks["grok-4.5"].source == "shipped"
        assert "qwen3-4b" in ranks
        assert ranks["qwen3-4b"].rank == 1

    def test_unseen_live_family_alias_inherits_rank_but_user_override_wins(self):
        future = "claude-opus-6-2"
        assert rm._rank_for(future, rm.load_rankings(include_measured=False)).rank == 5
        override = rm.load_rankings(
            {"model_rankings": {future: {"rank": 2}}}, include_measured=False
        )
        assert rm._rank_for(future, override).rank == 2
        chosen = rm.auto_pick_model(
            min_rank=5,
            enabled=[{"provider": "anthropic", "model": future},
                     {"provider": "llamacpp", "model": "local"}],
            rankings=rm.load_rankings(include_measured=False),
        )
        assert chosen.model == future

    def test_user_overrides_shipped(self, tmp_path):
        shipped = tmp_path / "rankings.yaml"
        shipped.write_text(yaml.safe_dump({
            "schema": "aicad.model_rankings/1",
            "models": [
                {"id": "alpha", "rank": 5, "source": "shipped", "notes": "s"},
                {"id": "beta", "rank": 2, "source": "shipped", "notes": "s"},
            ],
        }), encoding="utf-8")
        cfg = {"model_rankings": {"alpha": {"rank": 1, "notes": "user demote"}}}
        ranks = rm.load_rankings(
            cfg, rankings_path=shipped, include_measured=False
        )
        assert ranks["alpha"].rank == 1
        assert ranks["alpha"].source == "user"
        assert ranks["beta"].rank == 2
        assert ranks["beta"].source == "shipped"

    def test_measured_overrides_shipped_when_cycles_ge_1(self, tmp_path):
        shipped = tmp_path / "rankings.yaml"
        shipped.write_text(yaml.safe_dump({
            "schema": "aicad.model_rankings/1",
            "models": [
                {"id": "local-a", "rank": 5, "source": "shipped"},
                {"id": "local-b", "rank": 4, "source": "shipped"},
            ],
        }), encoding="utf-8")
        lb = tmp_path / "leaderboard.json"
        lb.write_text(json.dumps({
            "schema": "aicad.leaderboard/1",
            "models": [
                {
                    "id": "local-a",
                    "provider": "llamacpp",
                    "cycles": 3,
                    "primary": {"pass_rate": 0.0},
                },
                {
                    "id": "local-b",
                    "provider": "llamacpp",
                    "cycles": 0,  # ignored — cycles < 1
                    "primary": {"pass_rate": 1.0},
                },
            ],
        }), encoding="utf-8")
        ranks = rm.load_rankings(
            {}, rankings_path=shipped, leaderboard_path=lb, include_measured=True
        )
        # pass_rate 0.0 → measured rank 1, overrides shipped 5
        assert ranks["local-a"].source == "measured"
        assert ranks["local-a"].rank == 1
        # cycles 0 → keep shipped
        assert ranks["local-b"].source == "shipped"
        assert ranks["local-b"].rank == 4

    def test_user_beats_measured(self, tmp_path):
        shipped = tmp_path / "rankings.yaml"
        shipped.write_text(yaml.safe_dump({
            "schema": "aicad.model_rankings/1",
            "models": [{"id": "m1", "rank": 3, "source": "shipped"}],
        }), encoding="utf-8")
        lb = tmp_path / "leaderboard.json"
        lb.write_text(json.dumps({
            "models": [{
                "id": "m1", "cycles": 2,
                "primary": {"pass_rate": 0.9},  # measured → 5
            }],
        }), encoding="utf-8")
        cfg = {"model_rankings": {"m1": 2}}
        ranks = rm.load_rankings(
            cfg, rankings_path=shipped, leaderboard_path=lb
        )
        assert ranks["m1"].source == "user"
        assert ranks["m1"].rank == 2


# ---------------------------------------------------------------------------
# Auto pick + enabled providers + threshold
# ---------------------------------------------------------------------------


class TestAutoPick:
    def _ranks(self) -> dict[str, rm.ModelRank]:
        return {
            "frontier-a": rm.ModelRank("frontier-a", 5, "shipped", provider="xai"),
            "mid-b": rm.ModelRank("mid-b", 3, "shipped", provider="openrouter"),
            "local-c": rm.ModelRank("local-c", 2, "shipped", provider="llamacpp"),
            "tiny-d": rm.ModelRank("tiny-d", 1, "shipped", provider="llamacpp"),
            "org/free-e:free": rm.ModelRank(
                "org/free-e:free", 4, "shipped", provider="openrouter"
            ),
        }

    def test_threshold_filters_low_rank(self):
        enabled = [
            {"provider": "llamacpp", "model": "tiny-d"},
            {"provider": "llamacpp", "model": "local-c"},
            {"provider": "xai", "model": "frontier-a"},
        ]
        pick = rm.auto_pick_model(
            min_rank=3,
            rankings=self._ranks(),
            enabled=enabled,
        )
        # tiny-d (1) and local-c (2) filtered; frontier-a (5) remains
        assert pick.model == "frontier-a"
        assert pick.mode == "auto"

    def test_prefers_cheaper_when_rank_met(self):
        enabled = [
            {"provider": "xai", "model": "frontier-a"},          # cost 5
            {"provider": "openrouter", "model": "org/free-e:free"},  # free cost 3
            {"provider": "llamacpp", "model": "local-c"},         # cost 0 but rank 2
        ]
        pick = rm.auto_pick_model(
            min_rank=3,
            rankings=self._ranks(),
            enabled=enabled,
        )
        # local-c fails threshold; free openrouter cheaper than xai metered
        assert pick.provider == "openrouter"
        assert "free-e" in pick.model

    def test_enabled_providers_honoured(self):
        cfg = {
            "enabled_providers": [
                {"provider": "llamacpp", "model": "tiny-d"},
            ],
        }
        pick = rm.auto_pick_model(
            cfg=cfg,
            min_rank=1,
            rankings=self._ranks(),
        )
        assert pick.provider == "llamacpp"
        assert pick.model == "tiny-d"

    def test_simple_task_allows_local(self):
        enabled = [
            {"provider": "llamacpp", "model": "tiny-d"},
            {"provider": "xai", "model": "frontier-a"},
        ]
        pick = rm.auto_pick_model(
            task_class="simple_part",
            node_depth=0,
            rankings=self._ranks(),
            enabled=enabled,
        )
        # simple_part min_rank=1 → local is cheapest eligible
        assert pick.provider == "llamacpp"

    def test_assembly_needs_higher_rank(self):
        enabled = [
            {"provider": "llamacpp", "model": "tiny-d"},  # rank 1
            {"provider": "llamacpp", "model": "local-c"},  # rank 2
            {"provider": "xai", "model": "frontier-a"},    # rank 5
        ]
        pick = rm.auto_pick_model(
            task_class="assembly",
            node_depth=0,
            rankings=self._ranks(),
            enabled=enabled,
        )
        # assembly min_rank=4 → only frontier-a qualifies
        assert pick.model == "frontier-a"

    def test_depth_demotes_threshold(self):
        # assembly base 4; depth 2 → min_rank 2 → local-c (2) eligible + cheapest
        enabled = [
            {"provider": "llamacpp", "model": "local-c"},
            {"provider": "xai", "model": "frontier-a"},
        ]
        pick = rm.auto_pick_model(
            task_class="assembly",
            node_depth=2,
            rankings=self._ranks(),
            enabled=enabled,
        )
        assert pick.model == "local-c"


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
        with pytest.raises(rm.RoleModelError, match="loopback"):
            rm.resolve_role_model("redaction", cfg)

    def test_auto_picks_local_when_available(self):
        cfg = {
            "ai_roles": {"redaction": "auto"},
            "enabled_providers": [
                {"provider": "xai", "model": "grok-4.5"},
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        a = rm.resolve_role_model("redaction", cfg)
        assert a.provider == "llamacpp"
        assert a.mode == "auto"

    def test_assert_redaction_loopback_direct(self):
        rm.assert_redaction_loopback("llamacpp")
        with pytest.raises(rm.RoleModelError):
            rm.assert_redaction_loopback("openai")
        with pytest.raises(rm.RoleModelError):
            rm.assert_redaction_loopback("xai-sub")


# ---------------------------------------------------------------------------
# resolve_role_model fixed / auto / defaults
# ---------------------------------------------------------------------------


class TestResolveRoleModel:
    def test_fixed_assignment(self):
        cfg = {
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

    def test_auto_default_when_unset(self):
        cfg = {
            "ai_roles": {},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        a = rm.resolve_role_model("default", cfg)
        assert a.mode == "auto"
        assert a.provider == "llamacpp"

    def test_get_ai_roles_defaults(self):
        roles = rm.get_ai_roles({})
        for r in rm.KNOWN_ROLES:
            assert roles[r] == "auto"

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
        assert roles["default"] == "auto"

    def test_resolve_for_node_none_without_ai_roles(self):
        # No ai_roles key → legacy path (None)
        assert rm.resolve_for_node("task", {}, cfg={}) is None

    def test_resolve_for_node_engages_with_ai_roles(self):
        cfg = {
            "ai_roles": {"task": "auto"},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "qwen3-4b"},
            ],
        }
        a = rm.resolve_for_node(
            "task",
            {"task_class": "simple_part", "depth": 0},
            cfg=cfg,
        )
        assert a is not None
        assert a.provider == "llamacpp"


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
        pol = rm.check_spawn_depth_policy(2, letter="A", task_class="assembly")
        assert pol["ok"] is True
        # depth 2 demotes assembly(4) → 2
        assert pol["min_rank"] == 2

    def test_parent_rank_enforces_decreasing(self):
        pol = rm.check_spawn_depth_policy(
            1, letter="C", task_class="assembly", parent_min_rank=3
        )
        assert pol["ok"] is True
        # assembly@depth1 → min(3, 3-1)=2
        assert pol["min_rank"] == 2

    def test_min_rank_for_difficulty_depth(self):
        assert rm.min_rank_for_difficulty(task_class="assembly", node_depth=0) == 4
        assert rm.min_rank_for_difficulty(task_class="assembly", node_depth=3) == 1
        assert rm.min_rank_for_difficulty(
            task_class="simple_part", node_depth=0, risk="high"
        ) == 2


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

    def test_ai_roles_set_refuses_cloud_redaction(self, monkeypatch):
        from services.agent import worker_rpc as rpc

        monkeypatch.setattr("services.agent.config.load_config", lambda: {})
        out = rpc.ROLE_MODELS_METHODS["ai_roles_set"](
            {"roles": {"redaction": {"provider": "openai", "model": "gpt-5"}}},
            lambda *_a, **_k: None,
        )
        assert out["ok"] is False
        assert "loopback" in out["reason"].lower() or "HARD" in out["reason"]
