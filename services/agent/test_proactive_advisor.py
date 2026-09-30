"""Focused tests for proactive advisor gating and one-shot role turns."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from services.agent import proactive_advisor as pa


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CHUDWARE_PROACTIVE_ADVISOR", raising=False)
    monkeypatch.delenv("CHUDWARE_ADVISOR", raising=False)


def test_trigger_fail_streak():
    calls = [
        {"tool": "a", "ok": False},
        {"tool": "b", "ok": False},
    ]
    t = pa.evaluate_trigger(
        ok=False,
        tool="b",
        calls=calls,
        tool_calls_made=2,
        preference="encourage",
    )
    assert t is not None
    assert t["fail_n"] >= 2
    assert t["severity"] in ("med", "high")


def test_trigger_same_tool_twice():
    calls = [
        {"tool": "extrude", "ok": False},
        {"tool": "extrude", "ok": False},
    ]
    t = pa.evaluate_trigger(
        ok=False,
        tool="extrude",
        calls=calls,
        tool_calls_made=2,
        preference="encourage",
    )
    assert t is not None
    assert t["same_tool_n"] >= 2


def test_policy_refusals_do_not_feed_failure_or_thrash_streaks():
    calls = [
        {
            "tool": "extrude",
            "ok": False,
            "policy_refusal": True,
            "executed": False,
        },
        {
            "tool": "extrude",
            "ok": False,
            "policy_refusal": True,
            "executed": False,
        },
    ]

    trigger = pa.evaluate_trigger(
        ok=False,
        tool="extrude",
        calls=calls,
        tool_calls_made=2,
        preference="encourage",
    )

    assert trigger is not None
    assert trigger["reason"] == "policy_refusal"
    assert trigger["fail_n"] == 0
    assert trigger["same_tool_n"] == 0
    assert trigger["policy_refusal"] is True


def test_explicitly_executed_failures_still_feed_streaks():
    calls = [
        {"tool": "extrude", "ok": False, "executed": True},
        {"tool": "extrude", "ok": False, "executed": True},
    ]

    trigger = pa.evaluate_trigger(
        ok=False,
        tool="extrude",
        calls=calls,
        tool_calls_made=2,
        preference="encourage",
    )

    assert trigger is not None
    assert trigger["fail_n"] == 2
    assert trigger["same_tool_n"] == 2
    assert "same_tool_x2" in trigger["reason"]


def test_no_trigger_on_success():
    calls = [{"tool": "a", "ok": True}]
    t = pa.evaluate_trigger(
        ok=True,
        tool="a",
        calls=calls,
        tool_calls_made=1,
        preference="encourage",
    )
    assert t is None


def test_option_requires_high_severity():
    calls = [{"tool": "x", "ok": False}, {"tool": "y", "ok": False}]
    t = pa.evaluate_trigger(
        ok=False,
        tool="y",
        calls=calls,
        tool_calls_made=2,
        preference="option",
    )
    # fail_n=2 is med unless same_tool — option suppresses med
    assert t is None


def test_policy_note_is_deterministic_and_never_spends_llm_budget(monkeypatch):
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: True)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "encourage")
    monkeypatch.setattr(
        pa,
        "_try_llm_advice",
        lambda **kwargs: pytest.fail("policy refusal must not call advisor provider"),
    )
    calls = [
        {
            "tool": "extrude",
            "ok": False,
            "policy_refusal": True,
            "executed": False,
        },
        {
            "tool": "extrude",
            "ok": False,
            "policy_refusal": True,
            "executed": False,
        },
    ]
    options = {}

    note, meta = pa.maybe_proactive_note(
        ok=False,
        tool="extrude",
        result={"status": "offline_policy"},
        calls=calls,
        tool_calls_made=2,
        options=options,
        allow_llm=True,
    )

    assert "policy" in note
    assert "not executed" in note
    assert meta["llm"] is False
    assert options[pa._STATE_KEY]["llm_fires"] == 0


def test_deterministic_note_contains_tool():
    trig = {"severity": "med", "reason": "fail_streak=2", "fail_n": 2, "same_tool_n": 0}
    note = pa.build_deterministic_note(
        tool="doc_apply_ops",
        ok=False,
        trigger=trig,
        preference="encourage",
        result={"error": "unknown op box"},
    )
    assert "doc_apply_ops" in note
    assert "proactive" in note
    assert "unknown op box" in note


def test_maybe_note_off_when_checkbox_false(monkeypatch):
    monkeypatch.setattr(
        pa,
        "evaluate_trigger",
        lambda **k: {
            "severity": "high",
            "reason": "x",
            "fail_n": 3,
            "same_tool_n": 3,
        },
    )
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: False)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "encourage")
    # re-import path uses local import inside maybe_proactive_note
    note, meta = pa.maybe_proactive_note(
        ok=False,
        tool="t",
        result="err",
        calls=[{"tool": "t", "ok": False}] * 3,
        tool_calls_made=3,
        options={},
        allow_llm=False,
    )
    assert note == ""


def test_maybe_note_fires_with_checkbox(monkeypatch):
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: True)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "encourage")
    opts: dict = {}
    calls = [{"tool": "t", "ok": False}, {"tool": "t", "ok": False}]
    note, meta = pa.maybe_proactive_note(
        ok=False,
        tool="t",
        result={"error": "boom"},
        calls=calls,
        tool_calls_made=2,
        options=opts,
        allow_llm=False,
    )
    assert note
    assert "proactive" in note
    assert meta.get("source") == "proactive"
    assert meta.get("body_source") == "deterministic"
    # cooldown: immediate second fire blocked
    note2, meta2 = pa.maybe_proactive_note(
        ok=False,
        tool="t",
        result={"error": "boom"},
        calls=calls + calls,
        tool_calls_made=3,
        options=opts,
        allow_llm=False,
    )
    assert note2 == ""
    assert meta2.get("skipped") == "cooldown"


def test_inject_if_needed_appends(monkeypatch):
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: True)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "require")
    msgs: list = []
    events: list = []
    calls = [{"tool": "z", "ok": False}, {"tool": "z", "ok": False}]
    ok = pa.inject_if_needed(
        msgs,
        ok=False,
        tool="z",
        result="nope",
        calls=calls,
        tool_calls_made=5,
        options={},
        on_event=lambda k, p: events.append((k, p)),
        parts_fn=lambda s: s,
        allow_llm=False,
    )
    assert ok is True
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert events and events[0][0] == "agent.advisor"


def test_proactive_config_helper_default_false(monkeypatch):
    from services.agent import config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda: {})
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda c=None: "encourage")
    assert cfg.proactive_advisor_enabled({}) is False
    assert cfg.proactive_advisor_enabled({"proactive_advisor": True}) is True
    assert cfg.proactive_advisor_enabled({"agent": {"proactive_advisor": True}}) is True


def test_proactive_off_when_advisor_off(monkeypatch):
    from services.agent import config as cfg

    monkeypatch.setattr(cfg, "get_advisor_preference", lambda c=None: "off")
    assert cfg.proactive_advisor_enabled({"proactive_advisor": True}) is False


def test_proactive_uses_bounded_one_shot_role_turn(monkeypatch):
    import services.agent.config as cfg
    import services.agent.role_models as role_models

    captured = {}

    def fake_role_turn(request, role, **kwargs):
        captured.update({"request": request, "role": role, **kwargs})
        return {
            "success": True,
            "cancelled": False,
            "result": (
                '{"verdict":"steer","notes":["inspect the executed failure"],'
                '"focus":"ids"}'
            ),
        }

    monkeypatch.setattr(cfg, "load_config", lambda: {"reasoning_effort": "max"})
    monkeypatch.setattr(
        cfg,
        "get_provider",
        lambda **kwargs: pytest.fail("online advisor must not resolve a provider"),
    )
    monkeypatch.setattr(
        role_models,
        "resolve_role_model",
        lambda role, config: SimpleNamespace(
            provider="mock",
            model="mock-model",
            rank=2,
        ),
    )
    monkeypatch.setattr(
        role_models,
        "resolve_role_thinking",
        lambda role, config, **kwargs: kwargs["dispatch_effort"],
    )
    monkeypatch.setattr(pa, "run_role_turn", fake_role_turn)

    note = pa._try_llm_advice(
        tool="extrude",
        ok=False,
        trigger={
            "severity": "high",
            "reason": "fail_streak=3",
            "fail_n": 3,
            "same_tool_n": 3,
        },
        result={"error": "bad id"},
        recent_calls=[],
        options={
            "session_id": "chat-7",
            "run_id": "run-4",
            "node_id": "node-2",
        },
    )

    assert "inspect the executed failure" in note
    assert captured["role"] == "advisor"
    assert captured["session_id"] == "chat-7"
    assert captured["run_id"] == "run-4"
    assert captured["parent_id"] == "node-2"
    assert captured["close_session"] is True
    assert captured["max_tokens"] == pa._LLM_MAX_TOKENS
    assert captured["max_total_tokens"] == pa._LLM_MAX_TOKENS
    assert captured["options"]["allowed_tools"] == []
    assert captured["options"]["settings"] == {
        "provider": "mock",
        "model": "mock-model",
        "reasoning_effort": "max",
    }


@pytest.mark.parametrize(
    "turn",
    [
        {"success": False, "cancelled": False, "result": {"error": "down"}},
        {"success": False, "cancelled": True, "result": {"error": "stopped"}},
        {"success": True, "cancelled": False, "result": ""},
    ],
)
def test_proactive_role_failure_degrades_to_deterministic_note(monkeypatch, turn):
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "load_config", lambda: {})
    monkeypatch.setattr(pa, "run_role_turn", lambda *args, **kwargs: turn)
    assert (
        pa._try_llm_advice(
            tool="extrude",
            ok=False,
            trigger={"severity": "high", "reason": "fail_streak=3"},
            result={"error": "bad id"},
            recent_calls=[],
        )
        == ""
    )


def test_proactive_explicit_offline_mode_reaches_role_turn(monkeypatch):
    import services.agent.config as cfg

    captured = {}
    offline_provider = object()

    def fake_role_turn(*args, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "cancelled": False,
            "result": '{"verdict":"steer","notes":["local"],"focus":"ids"}',
        }

    monkeypatch.setattr(cfg, "load_config", lambda: {"offline_mode": True})
    monkeypatch.setattr(pa, "run_role_turn", fake_role_turn)
    note = pa._try_llm_advice(
        tool="extrude",
        ok=False,
        trigger={"severity": "high", "reason": "fail_streak=3"},
        result={"error": "bad id"},
        recent_calls=[],
        options={"offline_provider": offline_provider},
    )
    assert note
    assert captured["options"]["offline"] is True
    assert captured["options"]["offline_provider"] is offline_provider
