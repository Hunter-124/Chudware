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
from types import SimpleNamespace

from services.agent import role_models as rm


# ---------------------------------------------------------------------------
# 1. The clamp itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "max",
        "MAX",
        " Max ",
        "xhigh",
        "x-high",
        "extra_high",
        "ultra",
        "highest",
        "maximum",
    ],
)
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


def test_provider_clamp_rebinds_child_without_mutating_parent():
    parent = SimpleNamespace(reasoning_effort="max", model="ambient")

    child = rm.clamp_provider_effort(parent)

    assert child is not parent
    assert child.reasoning_effort == "high"
    assert parent.reasoning_effort == "max"
    assert rm.clamp_provider_effort(child) is child


# ---------------------------------------------------------------------------
# 2. The clamp is WIRED into sub-agent dispatch
# ---------------------------------------------------------------------------


def test_dag_node_never_inherits_global_max_effort(monkeypatch):
    """cfg_pick = dict(cfg) inherits the user's global reasoning_effort, and the
    resolver only OVERWRITES it on success. With resolution returning None a
    Settings-level 'max' used to reach the child provider untouched."""
    from services.dag import node_runner as nr

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "reasoning_effort": "max",  # the user's global Settings pin
        "enabled_providers": [
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        ],
    }
    monkeypatch.setattr("services.agent.config.load_config", lambda: dict(cfg))
    # Force the "resolver leaves it unset" branch — the exact leak path.
    monkeypatch.setattr(
        "services.agent.role_models.resolve_role_thinking",
        lambda *a, **k: None,
    )

    context = {
        "role_id": "sketcher",
        "task_class": "modification",
        "depth": 1,
        "node_inputs": {"role": "sketcher", "task_class": "modification", "depth": 1},
        "retry_remaining": 2,
    }
    settings = nr._resolve_role_settings(context)
    row = context.get("role_model") or {}
    assert row.get("reasoning_effort") == "high", row
    assert settings["reasoning_effort"] == "high"


def test_dag_session_default_clamps_final_ambient_provider(monkeypatch):
    """Path B's session-default branch reuses ambient state unless clamped last."""
    from services.dag import node_runner as nr

    ambient = SimpleNamespace(name="ambient", model="frontier", reasoning_effort="max")
    assignment = rm.RoleModelAssignment(
        role="task", provider="", model="", mode=rm.SESSION_DEFAULT
    )
    monkeypatch.setattr(
        "services.agent.config.load_config",
        lambda: {"reasoning_effort": "max"},
    )
    monkeypatch.setattr(rm, "resolve_for_node", lambda *a, **k: assignment)
    monkeypatch.setattr(rm, "resolve_role_thinking", lambda *a, **k: None)

    settings = nr._resolve_role_settings(
        {
            "role_id": "sketcher",
            "task_class": "modification",
            "depth": 1,
            "node_inputs": {
                "role": "sketcher",
                "task_class": "modification",
                "depth": 1,
            },
        },
        injected=ambient,
    )

    assert settings == {
        "provider": "ambient",
        "model": "frontier",
        "reasoning_effort": "high",
    }
    assert ambient.reasoning_effort == "max"


@pytest.mark.parametrize(
    "role_resolution_fails",
    [False, True],
    ids=["session-default", "resolution-exception-fallback"],
)
def test_delegate_session_default_clamps_final_ambient_provider(
    monkeypatch,
    role_resolution_fails,
):
    """Path A passes only a capped effort setting into online Chudcode."""
    from services.agent import chudcode_roles
    from services.agent.mission_dispatch import MissionDispatch

    ambient = SimpleNamespace(name="ambient", model="frontier", reasoning_effort="max")
    assignment = rm.RoleModelAssignment(
        role="worker", provider="", model="", mode=rm.SESSION_DEFAULT
    )
    captured = {}

    monkeypatch.setattr(
        "services.agent.roles.load_role",
        lambda role: SimpleNamespace(system_prompt="", concrete_tools=[]),
    )
    monkeypatch.setattr(
        "services.agent.config.load_config",
        lambda: {
            "reasoning_effort": "max",
            "copilot": {"priority": "best_results"},
        },
    )
    if role_resolution_fails:

        def resolve_role_model(*_args, **_kwargs):
            raise RuntimeError("role resolution unavailable")
    else:

        def resolve_role_model(*_args, **_kwargs):
            return assignment

    # Unpinned delegated leaves resolve through the worker seat, not ai_roles.
    monkeypatch.setattr(rm, "resolve_role_model", resolve_role_model)

    def fake_run_turn(_request, _role, **kwargs):
        captured["options"] = kwargs.get("options") or {}
        return {
            "success": True,
            "stop_reason": "completed",
            "result": "done",
            "session_id": "cad-role-effort",
            "tokens_used": 100,
            "provider_usage_recorded": True,
            "cancelled": False,
        }

    monkeypatch.setattr(chudcode_roles, "run_role_turn", fake_run_turn)
    dispatch = MissionDispatch(run_id="", provider=ambient)
    result = dispatch._run_subagent_turn(
        {
            "id": "dg_effort",
            "role": "feature_modeler",
            "depth": 1,
            "budget": {"tokens_granted": 60_000},
        },
        "Build the assigned feature.",
    )

    options = captured["options"]
    assert result["ok"] is True
    assert options["settings"]["reasoning_effort"] == "high"
    assert "provider" not in options["settings"]
    assert "model" not in options["settings"]
    assert "offline_provider" not in options
    assert ambient.reasoning_effort == "max"


def _feature_modeler_budget_requirements():
    from services.agent.delegation import delegation_budget_requirements

    brief = "Build the ratified feature chain and verify the finished part."
    req = delegation_budget_requirements("feature_modeler", brief, tier="local-24k")
    return brief, req


def _feature_modeler_tree(run_id):
    from services.agent.delegation import DelegationTree

    return DelegationTree(
        run_id=run_id,
        orchestrator_budget=2_000_000,
        tier="local-24k",
    )


def test_absurd_positive_grant_spawns_with_budget_advisory():
    from services.agent.delegation import delegate

    brief, req = _feature_modeler_budget_requirements()
    tree = _feature_modeler_tree("budget-advisory")
    result = delegate("feature_modeler", brief, 50, tree=tree)

    assert result["ok"] is True
    assert result["status"] == "approved"
    assert len(tree.all_nodes()) == 2
    assert result["available_tokens"] == 50
    assert result["recommended_tokens"] == req["recommended_tokens"]
    assert result["mandated_calls"] == 18
    assert "proceeding" in result["remedy"].lower()

    warnings = [
        event
        for event in tree._pending_events
        if event.get("kind") == "delegation_budget_warning"
    ]
    assert len(warnings) == 1
    payload = warnings[0]["payload"]
    assert payload["available_tokens"] == 50
    assert payload["recommended_tokens"] == req["recommended_tokens"]
    assert payload["mandated_calls"] == 18
    assert "proceeding" in payload["message"].lower()


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
        "test premise: the two must tie on rank for the tie-break to decide"
    )

    for order in (enabled, list(reversed(enabled))):
        pick = rm.auto_pick_model(cfg={}, enabled=order, min_rank=5)
        assert pick.model == "claude-opus-4-8", (order, pick)


def test_opus_at_low_effort_is_reachable_for_subagents():
    """The ceiling must not block the preferred combination: Opus + low."""
    assert rm.clamp_subagent_effort("low") == "low"
    from services.agent.providers.anthropic_provider import _normalize_effort

    assert _normalize_effort("low") == "low"
