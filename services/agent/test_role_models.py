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

    def test_rankings_as_dict_shape(self):
        d = rm.rankings_as_dict(include_measured=False)
        assert isinstance(d, dict)
        entry = d["grok-4.5"]
        assert "rank" in entry and "source" in entry and "notes" in entry


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
        from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS

        for name in (
            "ai_roles_get",
            "ai_roles_set",
            "model_rankings_get",
            "model_rankings_set",
        ):
            assert name in WORKER_METHODS, f"missing RPC {name}"
            assert callable(WORKER_METHODS[name])

    def test_ai_roles_get_shape(self, monkeypatch, tmp_path):
        from engine_oracle.parts_hub import role_models_rpc as rpc

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
        from engine_oracle.parts_hub import role_models_rpc as rpc

        monkeypatch.setattr("services.agent.config.load_config", lambda: {})
        out = rpc.ROLE_MODELS_METHODS["ai_roles_set"](
            {"roles": {"redaction": {"provider": "openai", "model": "gpt-5"}}},
            lambda *_a, **_k: None,
        )
        assert out["ok"] is False
        assert "loopback" in out["reason"].lower() or "HARD" in out["reason"]

    def test_model_rankings_get_shape(self, monkeypatch):
        from engine_oracle.parts_hub import role_models_rpc as rpc

        monkeypatch.setattr("services.agent.config.load_config", lambda: {})
        out = rpc.ROLE_MODELS_METHODS["model_rankings_get"](
            {}, lambda *_a, **_k: None
        )
        assert out["ok"] is True
        assert "rankings" in out
        assert "grok-4.5" in out["rankings"]
