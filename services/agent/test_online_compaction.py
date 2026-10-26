"""The user's Compaction choice reaches the online Chudcode session settings.

Chudcode walks ``compaction.methodOrder`` and falls through to the next listed method
when one is unusable or fails; with no CAD pin, a GPT session inherited the user's
global ``[shake, remote, soft]`` and ran shake while Settings said "Summary". These
tests pin what each choice hands the runtime, and that a turn neither lets a request
replace the persisted choice nor resets the per-session Auto-compaction switch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.agent import config as agent_config
from services.agent import worker_rpc
from services.agent.online_turn import resolve_settings
from services.agent.test_worker_rpc_chudcode import _params, online_lane  # noqa: F401


def _cfg(strategy=None, **nested):
    agent: dict = {}
    if strategy is not None:
        agent["compaction_strategy"] = strategy
    if nested:
        agent["compaction"] = nested
    return {"agent": agent}


def _prefs(*, schedule_compaction=False):
    return SimpleNamespace(thinking="medium", schedule_compaction=schedule_compaction)


@pytest.mark.parametrize(
    ("strategy", "methods"),
    [("summary", ["soft"]), ("shake", ["shake"]), ("snapcompact", ["snapcompact"])],
)
def test_explicit_strategy_runs_exactly_that_runtime_method(strategy, methods):
    chosen, settings = agent_config.online_compaction_settings(_cfg(strategy))

    assert chosen == strategy
    assert settings["enabled"] is True
    # One method only: a fallback entry would run a strategy the user did not pick.
    assert settings["methodOrder"] == methods




def test_off_disables_automatic_compaction_without_an_empty_method_order():
    _strategy, settings = agent_config.online_compaction_settings(_cfg("off"))

    assert settings["enabled"] is False
    # An empty order makes a later set_auto_compaction(true) install the runtime's
    # default order as a process-lifetime override that masks later choices.
    assert "methodOrder" not in settings


def test_auto_leaves_enablement_and_order_to_the_agent_service():
    for cfg in (_cfg(), _cfg("auto"), _cfg("")):
        strategy, settings = agent_config.online_compaction_settings(cfg)
        assert strategy == "auto"
        assert "enabled" not in settings
        assert "methodOrder" not in settings




def test_unknown_strategy_is_refused_not_replaced():
    with pytest.raises(ValueError, match="compaction_strategy"):
        agent_config.online_compaction_settings(_cfg("summarise"))
    with pytest.raises(ValueError):
        agent_config.online_compaction_settings(_cfg(3))




def test_a_request_cannot_replace_the_persisted_choice():
    raw = {
        "compaction_strategy": "shake",
        "settings": {
            "compaction_strategy": "shake",
            "compaction_settings": {"enabled": True, "methodOrder": ["shake"]},
        },
    }

    settings = resolve_settings(raw, _prefs(), "openai-codex", "gpt-5.5", _cfg("summary"))

    assert settings["compaction_strategy"] == "summary"
    assert settings["compaction_settings"]["methodOrder"] == ["soft"]




@pytest.mark.parametrize("strategy", ["off", "summary", "shake", "snapcompact"])
def test_explicit_strategy_never_resets_the_session_auto_compaction_switch(strategy):
    settings = resolve_settings(
        {}, _prefs(schedule_compaction=True), "openai-codex", "gpt-5.5", _cfg(strategy)
    )

    # set_auto_compaction is a runtime override above the process configuration;
    # sending it every turn would undo the user's per-session switch.
    assert "auto_compaction" not in settings


def test_auto_keeps_the_turn_policy_compaction_request():
    scheduled = resolve_settings(
        {}, _prefs(schedule_compaction=True), "openai-codex", "gpt-5.5", _cfg("auto")
    )
    unscheduled = resolve_settings({}, _prefs(), "openai-codex", "gpt-5.5", _cfg("auto"))

    assert scheduled["auto_compaction"] is True
    assert "auto_compaction" not in unscheduled


def test_an_explicit_request_switch_is_still_honoured():
    settings = resolve_settings(
        {"auto_compaction": False}, _prefs(), "openai-codex", "gpt-5.5", _cfg("summary")
    )

    assert settings["auto_compaction"] is False
    assert settings["compaction_settings"]["methodOrder"] == ["soft"]


def test_invalid_persisted_choice_refuses_the_turn_assembly():
    with pytest.raises(ValueError):
        resolve_settings({}, _prefs(), "openai-codex", "gpt-5.5", _cfg("everything"))


# ---------------------------------------------------------------------------
# Worker: the persisted choice rides every online turn of a live session
# ---------------------------------------------------------------------------


def _set_strategy(monkeypatch, strategy):
    monkeypatch.setattr(
        agent_config,
        "load_config",
        lambda: {
            "provider": "openai",
            "model": "gpt-test",
            "agent": {"compaction_strategy": strategy},
        },
    )




def test_invalid_choice_refuses_before_the_agent_service_runs(
    online_lane, monkeypatch  # noqa: F811
):
    _set_strategy(monkeypatch, "squash")

    result = worker_rpc._agent_admin_turn(_params(online_lane, run_id="bad"), lambda _e: None)

    assert result["ok"] is False
    assert result["reason"].startswith("invalid_compaction_strategy")
    assert online_lane.host.turns == []


# ---------------------------------------------------------------------------
# Worker: manual /compact is a run on the conversation's session
# ---------------------------------------------------------------------------


def _compact(lane, **compact):
    return worker_rpc._agent_admin_turn(
        _params(lane, run_id="compact-1", request="/compact", compact=compact),
        lambda _e: None,
    )


def test_manual_compact_never_invents_a_method(online_lane, monkeypatch):  # noqa: F811
    _set_strategy(monkeypatch, "auto")

    out = _compact(online_lane)

    assert out["ok"] is True
    assert "method" not in out["turn"]["compaction"]
    assert out["turn"]["compaction"]["tokensBefore"] == 120_000


@pytest.mark.parametrize(
    ("configured", "requested", "reason"),
    [
        ("summary", "shake", "manual_shake_unavailable"),
        ("shake", None, "manual_shake_unavailable"),
        ("off", None, "compaction_off"),
        ("auto", "squash", "invalid_compaction_strategy"),
        ("auto", "auto", "invalid_compaction_strategy"),
    ],
)
def test_manual_compact_refuses_what_the_session_cannot_honour(
    online_lane, monkeypatch, configured, requested, reason  # noqa: F811
):
    _set_strategy(monkeypatch, configured)

    out = _compact(online_lane, **({"strategy": requested} if requested else {}))

    assert out["ok"] is False
    assert out["reason"].startswith(reason)
    assert online_lane.host.compactions == []


@pytest.mark.parametrize(
    ("configured", "requested", "mode"),
    [
        ("auto", "summary", "soft"),
        ("summary", "snapcompact", "snapcompact"),
        ("off", "summary", "soft"),
        ("snapcompact", None, "snapcompact"),
        ("summary", None, "soft"),
        ("auto", None, None),
    ],
)
def test_manual_compact_runs_exactly_the_requested_method(
    online_lane, monkeypatch, configured, requested, mode  # noqa: F811
):
    _set_strategy(monkeypatch, configured)

    out = _compact(online_lane, **({"strategy": requested} if requested else {}))

    assert out["ok"] is True
    assert out["turn"]["compaction"]["strategy"] == (requested or configured)
    [sent] = online_lane.host.compactions
    assert sent["compact"]["mode"] == mode


def test_compaction_starts_the_session_a_turn_would(online_lane, monkeypatch):  # noqa: F811
    # A compaction may be what starts or resumes the conversation's process; the next
    # turn reuses it, so its launch identity must be the turn's.
    _set_strategy(monkeypatch, "summary")
    _compact(online_lane, instructions="keep the bracket dims")
    worker_rpc._agent_admin_turn(_params(online_lane, run_id="turn-1"), lambda _e: None)

    [compaction] = online_lane.host.compactions
    [turn] = online_lane.host.turns
    for key in ("session_id", "workspace_dir", "system_prompt", "settings", "mode"):
        assert compaction[key] == turn[key], key
    assert compaction["compact"]["instructions"] == "keep the bracket dims"

