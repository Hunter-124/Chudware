"""test_agent_turn.py — T-W30 bounded tool-executing agent turn.

Proves the turn parses model tool calls, dispatches them, feeds results back, is
bounded by the tool budget, and never fabricates success on an empty turn.
"""
from __future__ import annotations

from services.agent import agent_turn


class _ScriptedProvider:
    """Returns a fixed sequence of replies; ignores the prompt."""
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


def test_turn_dispatches_tool_then_finishes():
    provider = _ScriptedProvider([
        '```json\n{"tool": "measure_part", "args": {"doc_id": "d1"}}\n```',
        '```json\n{"done": true, "handoff": {"summary": "measured"}}\n```',
    ])
    seen = {}

    def _dispatch(tool, args):
        seen["tool"] = tool
        seen["args"] = args
        return {"mass_g": 42.0}

    res = agent_turn.run_turn("measure the part", provider,
                              allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    assert res.success is True
    assert res.stop_reason == "confirmed"
    assert seen["tool"] == "measure_part"
    assert res.result["handoff"] == {"summary": "measured"}
    assert res.result["calls"][0]["ok"] is True


def test_turn_denies_out_of_allowlist_tool():
    provider = _ScriptedProvider([
        '```json\n{"tool": "export_part", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {}}\n```',
    ])
    called = []

    def _dispatch(tool, args):
        called.append(tool)
        return {}

    res = agent_turn.run_turn("do it", provider,
                              allowed_tools=["measure_part"],
                              tool_dispatch=_dispatch)
    # export_part was never dispatched (not in allowlist).
    assert "export_part" not in called
    assert any(c.get("error") for c in res.result["calls"])


def test_turn_bounded_by_budget():
    # Always asks for another tool; must stop at the budget with a retry verdict.
    provider = _ScriptedProvider(
        ['```json\n{"tool": "measure_part", "args": {}}\n```'] * 20)
    res = agent_turn.run_turn("loop", provider, allowed_tools=["measure_part"],
                              max_tool_calls=3, tool_dispatch=lambda t, a: {"ok": True})
    assert res.result["tool_calls"] == 3
    assert res.stop_reason == "max_iters"


def test_empty_turn_is_no_code_not_fabricated_success():
    provider = _ScriptedProvider(["I am not sure what to do."])
    res = agent_turn.run_turn("go", provider, tool_dispatch=lambda t, a: {})
    assert res.success is False
    assert res.stop_reason == "no_code"


def test_dispatch_error_fed_back_then_recovered():
    provider = _ScriptedProvider([
        '```json\n{"tool": "fit_report", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {"ok": true}}\n```',
    ])

    def _dispatch(tool, args):
        raise RuntimeError("engine offline")

    res = agent_turn.run_turn("check", provider, allowed_tools=["fit_report"],
                              tool_dispatch=_dispatch)
    # the failed call is recorded, and the model got a chance to finish.
    assert res.result["calls"][0]["ok"] is False
    assert res.success is True  # finished with a handoff after the error feedback


def test_turn_honours_max_tokens_cap():
    """T-W30 finisher: the per-completion cap is passed through to the provider
    (tier generation_headroom when the caller knows the tier; 2048 default)."""
    seen = {}

    class _CapProvider:
        name = "cap"

        def complete(self, system, messages, max_tokens=None):
            seen["max_tokens"] = max_tokens
            return '```json\n{"done": true, "handoff": {}}\n```'

    agent_turn.run_turn("go", _CapProvider(), tool_dispatch=lambda t, a: {})
    assert seen["max_tokens"] == 2048  # shipped default (no tier known)

    agent_turn.run_turn("go", _CapProvider(), max_tokens=8000,
                        tool_dispatch=lambda t, a: {})
    assert seen["max_tokens"] == 8000  # caller-supplied tier budget wins


def test_turn_logs_every_tool_call_to_ledger():
    """T-W30 DoD: ledger events for every turn — each dispatched tool call is
    appended as an agent_turn event when a ledger is supplied."""
    provider = _ScriptedProvider([
        '```json\n{"tool": "measure_part", "args": {"doc_id": "d1"}}\n```',
        '```json\n{"tool": "fit_report", "args": {}}\n```',
        '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
    ])

    class _ListLedger:
        def __init__(self):
            self.events = []

        def append(self, actor, kind, payload):
            self.events.append({"actor": actor, "kind": kind, "payload": payload})

    ledger = _ListLedger()
    res = agent_turn.run_turn("check the part", provider,
                              allowed_tools=["measure_part", "fit_report"],
                              ledger=ledger, actor="feature_modeler",
                              tool_dispatch=lambda t, a: {"ok": True})
    assert res.success is True
    kinds = [e["payload"].get("event") for e in ledger.events
             if e["kind"] == "agent_turn"]
    assert kinds.count("tool_call") == 2, ledger.events
    assert all(e["actor"] == "feature_modeler" for e in ledger.events)


# ---------------------------------------------------------------------------
# T-W32 — directive parsing: nested args, unterminated fences, trailing prose
# ---------------------------------------------------------------------------

def test_parse_directive_nested_args():
    """The r4 PLAN-node regression: a fenced tool call with NESTED object args
    must parse (the old brace-pair regex truncated at the first '}')."""
    from services.agent.agent_turn import _parse_directive
    reply = ('```json\n{"tool": "buildgraph_plan", "args": {"plan_json": '
             '{"deliverables": [{"name": "bushing", "ops": {"kind": "revolve"}}]}}}\n```')
    obj = _parse_directive(reply)
    assert obj is not None and obj["tool"] == "buildgraph_plan"
    assert obj["args"]["plan_json"]["deliverables"][0]["ops"]["kind"] == "revolve"


def test_parse_directive_unterminated_fence():
    """A local model hitting its completion cap mid-fence: complete JSON object,
    missing closing fence — still parsed."""
    from services.agent.agent_turn import _parse_directive
    reply = 'Let me plan.\n```json\n{"tool": "fit_calc", "args": {"nominal": 20}}'
    obj = _parse_directive(reply)
    assert obj is not None and obj["tool"] == "fit_calc"


def test_parse_directive_trailing_prose_after_object():
    from services.agent.agent_turn import _parse_directive
    reply = '{"done": true, "handoff": {"summary": "ok"}}\nThat completes the node.'
    obj = _parse_directive(reply)
    assert obj is not None and obj.get("done") is True


def test_parse_directive_prose_only_is_none():
    from services.agent.agent_turn import _parse_directive
    assert _parse_directive("Okay, let me think about the bushing design.") is None
    assert _parse_directive("") is None
