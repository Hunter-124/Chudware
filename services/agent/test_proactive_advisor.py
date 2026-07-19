"""Unit tests for opt-in proactive advisor gates (no LLM, no engine)."""
from __future__ import annotations

import time

import pytest

from services.agent import proactive_advisor as pa


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("AI_CAD_PROACTIVE_ADVISOR", raising=False)
    monkeypatch.delenv("AI_CAD_ADVISOR", raising=False)


def test_trigger_fail_streak():
    calls = [
        {"tool": "a", "ok": False},
        {"tool": "b", "ok": False},
    ]
    t = pa.evaluate_trigger(
        ok=False, tool="b", calls=calls, tool_calls_made=2, preference="encourage",
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
        ok=False, tool="extrude", calls=calls, tool_calls_made=2, preference="encourage",
    )
    assert t is not None
    assert t["same_tool_n"] >= 2


def test_no_trigger_on_success():
    calls = [{"tool": "a", "ok": True}]
    t = pa.evaluate_trigger(
        ok=True, tool="a", calls=calls, tool_calls_made=1, preference="encourage",
    )
    assert t is None


def test_option_requires_high_severity():
    calls = [{"tool": "x", "ok": False}, {"tool": "y", "ok": False}]
    t = pa.evaluate_trigger(
        ok=False, tool="y", calls=calls, tool_calls_made=2, preference="option",
    )
    # fail_n=2 is med unless same_tool — option suppresses med
    assert t is None


def test_deterministic_note_contains_tool():
    trig = {"severity": "med", "reason": "fail_streak=2", "fail_n": 2, "same_tool_n": 0}
    note = pa.build_deterministic_note(
        tool="doc_apply_ops", ok=False, trigger=trig, preference="encourage",
        result={"error": "unknown op box"},
    )
    assert "doc_apply_ops" in note
    assert "proactive" in note
    assert "unknown op box" in note


def test_maybe_note_off_when_checkbox_false(monkeypatch):
    monkeypatch.setattr(pa, "evaluate_trigger", lambda **k: {
        "severity": "high", "reason": "x", "fail_n": 3, "same_tool_n": 3,
    })
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: False)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "encourage")
    # re-import path uses local import inside maybe_proactive_note
    note, meta = pa.maybe_proactive_note(
        ok=False, tool="t", result="err", calls=[{"tool": "t", "ok": False}] * 3,
        tool_calls_made=3, options={}, allow_llm=False,
    )
    assert note == ""


def test_maybe_note_fires_with_checkbox(monkeypatch):
    import services.agent.config as cfg

    monkeypatch.setattr(cfg, "proactive_advisor_enabled", lambda cfg=None: True)
    monkeypatch.setattr(cfg, "get_advisor_preference", lambda cfg=None: "encourage")
    opts: dict = {}
    calls = [{"tool": "t", "ok": False}, {"tool": "t", "ok": False}]
    note, meta = pa.maybe_proactive_note(
        ok=False, tool="t", result={"error": "boom"}, calls=calls,
        tool_calls_made=2, options=opts, allow_llm=False,
    )
    assert note
    assert "proactive" in note
    assert meta.get("source") == "proactive"
    assert meta.get("body_source") == "deterministic"
    # cooldown: immediate second fire blocked
    note2, meta2 = pa.maybe_proactive_note(
        ok=False, tool="t", result={"error": "boom"}, calls=calls + calls,
        tool_calls_made=3, options=opts, allow_llm=False,
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
