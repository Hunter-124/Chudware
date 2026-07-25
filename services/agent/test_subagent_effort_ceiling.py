"""Sub-agent reasoning-effort ceiling + model preference (Hunter directive 2026-07-25).

Two standing rules this module locks in:

1. NO sub-agent ever dispatches above ``high`` effort. Providers accept richer
   ladders (``anthropic_provider._EFFORTS`` carries ``max``/``xhigh``; gemini and
   moonshot expose ``max``), and a sub-agent inherits the user's global config via
   ``dict(cfg)`` — so a Settings-level ``max`` rode straight into every spawned
   child whenever the per-role resolver returned None.
2. Prefer a STRONGER model at LOW effort over a weaker model at HIGH effort —
   concretely, Opus must win over Sonnet when their capability ranks tie.
"""
from __future__ import annotations

import pytest

from services.agent import role_models as rm


# ---------------------------------------------------------------------------
# 1. The clamp itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["max", "MAX", " Max ", "xhigh", "x-high",
                                 "extra_high", "ultra", "highest", "maximum"])
def test_above_high_levels_collapse_to_high(raw):
    assert rm.clamp_subagent_effort(raw) == "high"


@pytest.mark.parametrize("raw", ["low", "medium", "high"])
def test_allowed_levels_pass_through(raw):
    assert rm.clamp_subagent_effort(raw) == raw


def test_blank_and_none_leave_provider_default():
    assert rm.clamp_subagent_effort(None) is None
    assert rm.clamp_subagent_effort("") is None
    assert rm.clamp_subagent_effort("   ") is None


def test_med_alias_normalizes():
    assert rm.clamp_subagent_effort("med") == "medium"


def test_ceiling_constant_is_high_and_role_levels_never_exceed_it():
    """The role-level ladder and the ceiling must not drift apart."""
    assert rm.SUBAGENT_MAX_THINKING == "high"
    assert "max" not in rm.CONCRETE_THINKING
    assert "max" not in rm.THINKING_LEVELS
    for level in rm.CONCRETE_THINKING:
        assert rm.clamp_subagent_effort(level) == level


# ---------------------------------------------------------------------------
# 2. The clamp is WIRED into sub-agent dispatch
# ---------------------------------------------------------------------------

def test_dag_node_never_inherits_global_max_effort(monkeypatch):
    """cfg_pick = dict(cfg) inherits the user's global reasoning_effort, and the
    resolver only OVERWRITES it on success. With resolution returning None a
    Settings-level 'max' used to reach the child provider untouched."""
    from services.dag import node_runner as nr
    from services.agent.providers.openai_provider import OpenAIProvider

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "reasoning_effort": "max",  # the user's global Settings pin
        "enabled_providers": [
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
        ],
    }
    monkeypatch.setattr("services.agent.config.load_config", lambda: dict(cfg))
    monkeypatch.setattr(
        "services.agent.config.get_provider",
        lambda name=None, cfg=None, role=None: OpenAIProvider(
            api_key="ollama",
            model=(cfg or {}).get("model") or "llama3.2:latest",
            base_url="http://127.0.0.1:11434/v1"),
    )
    # Force the "resolver leaves it unset" branch — the exact leak path.
    monkeypatch.setattr(
        "services.agent.role_models.resolve_role_thinking",
        lambda *a, **k: None,
    )

    context = {
        "role_id": "sketcher",
        "task_class": "modification",
        "depth": 1,
        "node_inputs": {"role": "sketcher", "task_class": "modification",
                        "depth": 1},
        "retry_remaining": 2,
    }
    nr._resolve_provider(context, provider=None)
    row = context.get("role_model") or {}
    assert row.get("reasoning_effort") == "high", row


# ---------------------------------------------------------------------------
# 3. Stronger-model-at-low-effort beats weaker-model-at-high-effort
# ---------------------------------------------------------------------------

def test_auto_pick_prefers_opus_over_sonnet_at_equal_rank():
    """Directive: prefer Opus at low effort over Sonnet at high effort.

    Both ship at rank 5 and share a cost class, so the pick falls to the
    tie-break. Opus currently wins only because "claude-opus-4-8" sorts before
    "claude-sonnet-5" alphabetically — a rename would silently flip it, so pin
    the outcome here.
    """
    enabled = [
        {"provider": "anthropic", "model": "claude-sonnet-5"},
        {"provider": "anthropic", "model": "claude-opus-4-8"},
    ]
    ranks = rm.load_rankings({})
    assert ranks["claude-opus-4-8"].rank == ranks["claude-sonnet-5"].rank, (
        "test premise: the two must tie on rank for the tie-break to decide")

    for order in (enabled, list(reversed(enabled))):
        pick = rm.auto_pick_model(cfg={}, enabled=order, min_rank=5)
        assert pick.model == "claude-opus-4-8", (order, pick)


def test_opus_at_low_effort_is_reachable_for_subagents():
    """The ceiling must not block the preferred combination: Opus + low."""
    assert rm.clamp_subagent_effort("low") == "low"
    from services.agent.providers.anthropic_provider import _normalize_effort
    assert _normalize_effort("low") == "low"
