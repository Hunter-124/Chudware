"""test_context_event.py — `agent.context` live token-meter events (F1).

The desktop context indicator sat at "0/0" forever: host/src/app.cpp fed it from
`run_started` / `agent_call` / `agent_output`, none of which the live general lane
emits. `_run_turn_impl` now streams one `agent.context` event per provider
completion, carrying the real prompt-token count from
`services.agent.providers.token_usage` and the provider's authoritative
`context_window`.

Frozen contract F1:
    kind    "agent.context"
    payload {"run_id", "used", "window", "model", "provider", "source"}
    `used`   = input tokens of the most recent provider call
    `window` = model context window, 0 == genuinely unknown
    An unmeasured call emits NOTHING (a zero must never clobber a real reading).

Harness idioms (fake scripted provider + captured events) mirror
test_conversation_history.py / test_agent_turn.py.
"""
from __future__ import annotations

from services.agent import agent_turn
from services.agent.providers import token_usage


_TOOL_CALL = '```json\n{"tool": "measure_part", "args": {"doc_id": "d1"}}\n```'
_FINISH = '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'


class _UsageProvider:
    """Scripted provider that records token usage like a real one does."""
    name = "scripted"
    model = "scripted-1"

    def __init__(self, replies=(_FINISH,), prompts=(1234,), *,
                 context_window: int | None = 200000, source="api"):
        self._replies = list(replies)
        self._prompts = list(prompts)
        self._source = source
        self.calls = 0
        if context_window is not None:
            self.context_window = context_window

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        if self._prompts:
            pt = self._prompts.pop(0)
            if pt is not None:
                token_usage.record_usage(
                    provider=self.name, model=self.model,
                    prompt_tokens=pt, completion_tokens=7,
                    source=self._source)
        return self._replies.pop(0) if self._replies else _FINISH


class _SilentProvider:
    """Provider that never records usage — the meter must stay silent."""
    name = "silent"
    model = "silent-1"
    context_window = 65536

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return _FINISH


def _run(provider, **kw):
    """Run one turn with a capturing live sink; return (result, context events)."""
    events: list[dict] = []

    def _sink(kind, payload):
        if kind == "agent.context":
            events.append(dict(payload))

    res = agent_turn.run_turn(
        "measure the part", provider,
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True, "mass_g": 42.0},
        on_event=_sink, run_id="run-ctx-test", **kw)
    return res, events


def test_emits_used_and_window_from_provider():
    provider = _UsageProvider(prompts=(4321,), context_window=131072)
    res, events = _run(provider)
    assert res.success is True
    assert events, "a measured completion must emit agent.context"
    assert events[-1]["used"] == 4321
    assert events[-1]["window"] == 131072


def test_missing_context_window_reports_zero_not_a_guess():
    provider = _UsageProvider(prompts=(999,), context_window=None)
    assert not hasattr(provider, "context_window")
    _res, events = _run(provider)
    assert len(events) >= 1
    assert events[-1]["used"] == 999
    # Unknown window is honestly 0 — never a substring-matched guess.
    assert events[-1]["window"] == 0


def test_unmeasured_call_emits_nothing():
    provider = _SilentProvider()
    res, events = _run(provider)
    assert res.success is True
    assert provider.calls >= 1
    # A zero reading would clobber a real earlier one, so it is never sent.
    assert len(events) == 0


def test_every_completion_emits_not_just_the_first():
    # Tool call then finish == two provider completions, two meter updates.
    provider = _UsageProvider(replies=(_TOOL_CALL, _FINISH),
                              prompts=(1000, 2500))
    res, events = _run(provider)
    assert res.success is True
    assert provider.calls == 2
    assert len(events) > 1, "the meter must track live, not emit once per turn"
    assert [e["used"] for e in events][:2] == [1000, 2500]
    assert events[-1]["used"] == 2500


def test_payload_shape_matches_contract():
    provider = _UsageProvider(prompts=(77,))
    _res, events = _run(provider)
    ev = events[-1]
    assert ev["run_id"] == "run-ctx-test"
    assert ev["model"] == "scripted-1"
    assert ev["provider"] == "scripted"
    assert ev["source"] in ("api", "estimate")
    assert isinstance(ev["used"], int) and isinstance(ev["window"], int)


def test_usage_lookup_failure_never_breaks_the_turn(monkeypatch):
    def _boom():
        raise RuntimeError("usage stash exploded")

    monkeypatch.setattr(token_usage, "get_last_usage", _boom)
    provider = _UsageProvider(prompts=(500,))
    res, events = _run(provider)
    assert isinstance(res, agent_turn.TurnResult)
    assert res.success is True
    assert events == []
