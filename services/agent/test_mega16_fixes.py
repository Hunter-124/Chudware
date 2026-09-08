"""Mega-16 routing fixes — paid openrouter frontier + xai spending-limit fallthrough.

Axes:
  Y — auto_pick routes frontier nodes to openrouter paid tier when xai is blocked
  (no refusal loops, no ollama-all collapse). Rank floor >=5 respected.
"""

from __future__ import annotations

import pytest


PAID_DEEPSEEK = "deepseek/deepseek-chat-v3.1"
TIER = f"openrouter:{PAID_DEEPSEEK}"


@pytest.fixture(autouse=True)
def _clear_unavailable():
    from services.agent.role_models import clear_unavailable_providers

    clear_unavailable_providers()
    yield
    clear_unavailable_providers()


# ---------------------------------------------------------------------------
# 1. paid DeepSeek tier parsing + rank floor
# ---------------------------------------------------------------------------


def test_parse_openrouter_deepseek_tier():
    from services.agent.tiers import parse_openrouter_tier, profile_tier_for

    assert parse_openrouter_tier(TIER) == PAID_DEEPSEEK
    assert profile_tier_for(TIER) == "frontier"


def test_shipped_deepseek_rank_meets_frontier_floor():
    from services.agent.role_models import (
        load_rankings,
        _FRONTIER_MEASURED_FLOOR,
    )

    ranks = load_rankings({}, include_measured=False)
    assert PAID_DEEPSEEK in ranks, sorted(ranks)
    assert ranks[PAID_DEEPSEEK].rank >= _FRONTIER_MEASURED_FLOOR
    assert ranks[PAID_DEEPSEEK].provider == "openrouter"
    # stem alias also shipped
    assert ranks["deepseek-chat-v3.1"].rank >= _FRONTIER_MEASURED_FLOOR


def test_measured_demotion_floors_deepseek():
    """Zero-pass gym row must not demote paid deepseek below floor 5."""
    import json
    import tempfile
    from pathlib import Path

    from services.agent.role_models import load_rankings, _FRONTIER_MEASURED_FLOOR

    with tempfile.TemporaryDirectory() as td:
        lb = Path(td) / "leaderboard.json"
        lb.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "id": PAID_DEEPSEEK,
                            "provider": "openrouter",
                            "cycles": 4,
                            "primary": {"pass_rate": 0.0},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded = load_rankings(
            {},
            leaderboard_path=lb,
            include_measured=True,
        )
        assert loaded[PAID_DEEPSEEK].rank >= _FRONTIER_MEASURED_FLOOR


# ---------------------------------------------------------------------------
# 2. auto_pick: depth-0 hard → openrouter paid when xai blocked; leaf → ollama
# ---------------------------------------------------------------------------


def test_auto_pick_depth0_hard_xai_blocked_picks_openrouter_paid():
    """Axis Y: xai spending-limit marked unavailable → openrouter paid frontier."""
    from services.agent.role_models import (
        ModelRank,
        auto_pick_model,
        mark_provider_unavailable,
        _FRONTIER_MEASURED_FLOOR,
    )

    mark_provider_unavailable(
        "xai-sub",
        reason="personal-team-blocked:spending-limit",
    )
    ranks = {
        PAID_DEEPSEEK: ModelRank(
            model_id=PAID_DEEPSEEK,
            rank=5,
            source="shipped",
            notes="mega-16",
            provider="openrouter",
        ),
        "grok-4.5": ModelRank(
            model_id="grok-4.5",
            rank=5,
            source="shipped",
            notes="frontier",
            provider="xai",
        ),
        "llama3.2:latest": ModelRank(
            model_id="llama3.2:latest",
            rank=1,
            source="shipped",
            notes="local",
            provider="ollama",
        ),
    }
    enabled = [
        {"provider": "xai-sub", "model": "grok-4.5"},
        {"provider": "openrouter", "model": PAID_DEEPSEEK},
        {"provider": "ollama", "model": "llama3.2:latest"},
    ]
    a = auto_pick_model(
        task_class="analysis",
        node_depth=0,
        min_rank=4,
        rankings=ranks,
        enabled=enabled,
    )
    assert a.provider == "openrouter", a
    assert a.model == PAID_DEEPSEEK, a
    assert (a.rank or 0) >= _FRONTIER_MEASURED_FLOOR, a
    assert a.provider != "xai-sub"
    assert a.provider != "ollama"


def test_auto_pick_leaf_still_ollama_when_xai_blocked():
    """Leaf low-difficulty still prefers ollama (cheapest eligible)."""
    from services.agent.role_models import (
        ModelRank,
        auto_pick_model,
        mark_provider_unavailable,
    )

    mark_provider_unavailable("xai-sub", reason="spending-limit")
    ranks = {
        PAID_DEEPSEEK: ModelRank(
            model_id=PAID_DEEPSEEK,
            rank=5,
            source="shipped",
            notes="mega-16",
            provider="openrouter",
        ),
        "llama3.2:latest": ModelRank(
            model_id="llama3.2:latest",
            rank=1,
            source="shipped",
            notes="local",
            provider="ollama",
        ),
    }
    enabled = [
        {"provider": "xai-sub", "model": "grok-4.5"},
        {"provider": "openrouter", "model": PAID_DEEPSEEK},
        {"provider": "ollama", "model": "llama3.2:latest"},
    ]
    a = auto_pick_model(
        task_class="leaf",
        node_depth=2,
        min_rank=1,
        rankings=ranks,
        enabled=enabled,
    )
    assert a.provider == "ollama", a
    assert "llama" in a.model


def test_rank_floor_respected_hard_node_not_ollama():
    """analysis min_rank=4 must not collapse to ollama rank-1."""
    from services.agent.role_models import ModelRank, auto_pick_model

    ranks = {
        PAID_DEEPSEEK: ModelRank(
            model_id=PAID_DEEPSEEK,
            rank=5,
            source="shipped",
            notes="mega-16",
            provider="openrouter",
        ),
        "llama3.2:latest": ModelRank(
            model_id="llama3.2:latest",
            rank=1,
            source="shipped",
            notes="local",
            provider="ollama",
        ),
    }
    enabled = [
        {"provider": "openrouter", "model": PAID_DEEPSEEK},
        {"provider": "ollama", "model": "llama3.2:latest"},
    ]
    a = auto_pick_model(
        task_class="analysis",
        min_rank=4,
        rankings=ranks,
        enabled=enabled,
    )
    assert a.provider == "openrouter", a
    assert (a.rank or 0) >= 5, a


def test_keys_openrouter_enables_paid_deepseek_in_candidates(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from services.agent.role_models import _enabled_providers

    en = _enabled_providers(
        {
            "keys": {"openrouter": "sk-or-test"},
            "subscription_auth": {"xai-sub": {"access_token": "t"}},
        }
    )
    models = {(e["provider"], e["model"]) for e in en}
    assert ("openrouter", PAID_DEEPSEEK) in models, en
    assert any(e["provider"] == "ollama" for e in en), en


# ---------------------------------------------------------------------------
# 3. spending-limit → provider_unavailable (no xai retry loop)
# ---------------------------------------------------------------------------


def test_is_provider_unavailable_error_spending_limit():
    from services.agent.role_models import is_provider_unavailable_error

    msg = (
        "xAI subscription tier not API-enabled (Heavy-only gating as of 2026-05): "
        '{"code":"personal-team-blocked:spending-limit","error":'
        '"You have run out of credits or need a Grok subscription."}'
    )
    assert is_provider_unavailable_error(RuntimeError(msg)) is True
    assert is_provider_unavailable_error("spending-limit") is True
    assert is_provider_unavailable_error(ValueError("incorrect api key")) is False


def test_classify_provider_error_spending_limit_not_auth():
    from services.agent.agent_turn import classify_provider_error

    class Fake403(RuntimeError):
        status_code = 403

    exc = Fake403(
        "XaiTierDeniedError: personal-team-blocked:spending-limit "
        "You have run out of credits"
    )
    stop, retryable = classify_provider_error(exc)
    assert stop == "provider_unavailable", (stop, retryable)
    assert retryable is False


def test_mark_provider_unavailable_skips_xai_in_auto_pick():
    from services.agent.role_models import (
        ModelRank,
        auto_pick_model,
        is_provider_unavailable,
        mark_provider_unavailable,
    )

    mark_provider_unavailable("xai-sub", reason="spending-limit")
    assert is_provider_unavailable("xai-sub")
    assert is_provider_unavailable("xai")  # both paths blocked together

    ranks = {
        "grok-4.5": ModelRank(
            model_id="grok-4.5",
            rank=5,
            source="shipped",
            provider="xai",
        ),
        PAID_DEEPSEEK: ModelRank(
            model_id=PAID_DEEPSEEK,
            rank=5,
            source="shipped",
            provider="openrouter",
        ),
    }
    enabled = [
        {"provider": "xai-sub", "model": "grok-4.5"},
        {"provider": "openrouter", "model": PAID_DEEPSEEK},
    ]
    a = auto_pick_model(min_rank=4, rankings=ranks, enabled=enabled)
    assert a.provider == "openrouter"
    assert a.model == PAID_DEEPSEEK


def test_enabled_providers_drops_unavailable_xai():
    from services.agent.role_models import (
        _enabled_providers,
        mark_provider_unavailable,
    )

    mark_provider_unavailable("xai-sub", reason="spending-limit")
    en = _enabled_providers(
        {
            "enabled_providers": [
                {"provider": "xai-sub", "model": "grok-4.5"},
                {"provider": "openrouter", "model": PAID_DEEPSEEK},
                {"provider": "ollama", "model": "llama3.2:latest"},
            ],
            "keys": {"openrouter": "sk-or-test"},
        }
    )
    providers = {e["provider"] for e in en}
    assert "xai-sub" not in providers
    assert "openrouter" in providers
    assert "ollama" in providers
