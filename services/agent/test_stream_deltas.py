"""test_stream_deltas.py — live assistant_delta streaming for the Python agent lane.

Proves (no network — fake bridge/provider):
  (a) with a provider delta sink installed, complete_ex sends stream:true and
      text/thinking fragments reach the sink on the right channels;
  (b) with no sink, params carry NO `stream` key and no on_event kwarg is
      passed — byte-identical to pre-streaming behaviour;
  (c) the coalescer batches tiny fragments (48-char / 60 ms / newline rules)
      and flushes the trailing partial at the end;
  (d) the returned reply is the provider's full text exactly once (deltas are
      display-only, never concatenated);
  (e) a kimi-code resample discards the first attempt's buffered fragments;
  (f) a raising on_event callback never breaks the bridge RPC;
  (g) run_turn installs/restores the sink around the turn and emits
      assistant_delta through on_event.
"""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

from services.agent import agent_turn, omp_bridge
from services.agent.providers import base as pbase
from services.agent.providers.omp_bridge_provider import OmpBridgeProvider


class _RecordingSink:
    """Provider delta sink double: records (delta, channel) feeds + discards."""

    def __init__(self):
        self.feeds: list[tuple[str, str]] = []
        self.discards = 0

    def __call__(self, delta: str, channel: str) -> None:
        self.feeds.append((delta, channel))

    def discard(self) -> None:
        self.discards += 1
        self.feeds.clear()


@pytest.fixture(autouse=True)
def _clean_sink():
    prev = pbase.set_provider_delta_sink(None)
    yield
    pbase.set_provider_delta_sink(prev)


def _fake_bridge(monkeypatch, script):
    """Install a scripted bridge.call; returns captured calls list.

    script: list of steps, each (events, result). events = list of (kind, data)
    delivered through on_event before the result is returned.
    """
    calls = []
    steps = list(script)

    def fake_call(method, params=None, timeout=120, **kwargs):
        calls.append({"method": method, "params": dict(params or {}), "kwargs": kwargs})
        events, result = steps.pop(0)
        cb = kwargs.get("on_event")
        for kind, data in events:
            if cb is not None:
                cb(kind, data)
        return result

    monkeypatch.setattr(omp_bridge, "call", fake_call)
    return calls


# --- (a) sink installed: stream:true + fragments on the right channels --------

def test_sink_streams_fragments_on_channels(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([("delta", {"text": "Hello "}), ("delta", {"thinking": "hmm "}),
          ("delta", {"text": "world"}), ("other", {"ignored": 1})],
         {"text": "Hello world", "thinking": "hmm ", "stopReason": "stop",
          "usage": {}}),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)

    prov = OmpBridgeProvider(provider="anthropic-sub", model="opus")
    reply, thinking = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert calls[0]["params"]["stream"] is True
    assert sink.feeds == [("Hello ", "text"), ("hmm ", "thinking"), ("world", "text")]
    assert reply == "Hello world"
    assert thinking == "hmm "


# --- (b) no sink: no stream key, no on_event, byte-identical ------------------

def test_no_sink_sends_exactly_todays_params(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "plain", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    assert pbase.provider_delta_sink() is None

    prov = OmpBridgeProvider(provider="anthropic-sub", model="opus",
                             api_key="k", reasoning_effort="high")
    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "hi"}],
                                max_tokens=1234)

    params = calls[0]["params"]
    assert "stream" not in params
    assert "on_event" not in calls[0]["kwargs"]
    assert params["provider"] == "anthropic"  # wire mapping still applied
    assert params["model"] == "opus"
    assert params["apiKey"] == "k"
    assert params["reasoningEffort"] == "high"
    assert params["maxTokens"] == 1234
    assert reply == "plain"


# --- (c) coalescing: thresholds + trailing flush -------------------------------

def test_coalescer_batches_and_flushes_trailing():
    events = []
    coal = agent_turn._AssistantDeltaCoalescer(
        lambda kind, payload: events.append((kind, payload)))

    coal.feed("aaa", "text")
    coal.feed("bbb", "text")
    coal.feed("ccc", "text")
    assert events == []  # 9 chars, no newline, <60 ms → buffered

    coal.feed("x" * 40, "text")  # crosses 48 chars → ONE flush of everything
    assert events == [("assistant_delta",
                       {"text": "aaabbbccc" + "x" * 40, "channel": "text"})]

    coal.feed("tail", "text")  # trailing partial: no flush until flush_all
    coal.feed("think-frag", "thinking")
    assert len(events) == 1
    coal.flush_all()
    assert events[1:] == [
        ("assistant_delta", {"text": "tail", "channel": "text"}),
        ("assistant_delta", {"text": "think-frag", "channel": "thinking"}),
    ]


def test_coalescer_newline_and_elapsed_flush():
    events = []
    coal = agent_turn._AssistantDeltaCoalescer(
        lambda kind, payload: events.append((kind, payload)))

    coal.feed("line\n", "text")  # newline flushes immediately
    assert events == [("assistant_delta", {"text": "line\n", "channel": "text"})]

    coal.feed("tiny", "text")
    assert len(events) == 1
    coal._last["text"] = time.monotonic() - 1.0  # age the channel past 60 ms
    coal.feed("bit", "text")
    assert events[1] == ("assistant_delta", {"text": "tinybit", "channel": "text"})


# --- (d) reply is the provider's full text exactly once ------------------------

def test_reply_not_duplicated_by_deltas(monkeypatch):
    full = "The complete reply."
    _fake_bridge(monkeypatch, [
        ([("delta", {"text": full})],
         {"text": full, "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)

    prov = OmpBridgeProvider(provider="zenmux", model="m")
    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert reply == full  # not full+full, not empty


# --- (e) kimi-code resample discards attempt 1's buffered fragments ------------

def test_resample_discards_first_attempt_buffer(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([("delta", {"thinking": "half "})],
         {"text": "", "thinking": "just reasoning, no fenced block",
          "stopReason": "stop", "usage": {}}),
        ([("delta", {"text": "real answer"})],
         {"text": "real answer", "thinking": "", "stopReason": "stop",
          "usage": {}}),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)

    prov = OmpBridgeProvider(provider="kimi-code", model="k3-256k")
    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert len(calls) == 2  # resample happened
    assert sink.discards == 1  # attempt 1's buffered fragments dropped
    assert sink.feeds == [("real answer", "text")]  # only attempt 2 survived
    assert reply == "real answer"
    # cache-buster nudge appended to wire copy on the retry
    assert len(calls[1]["params"]["messages"]) == 2


# --- (e2) a wholly empty sample is resampled on EVERY provider ----------------
# Live 2026-08-04 basic-modeling sweep: two of eight claude-sonnet-5 legs died
# mid-build on a single completion that carried no text and no thinking. The
# harness has nothing to parse, so the turn ended `provider_error` and every tool
# call it had already made was thrown away — while an immediate replay of the same
# history completed normally. Only kimi-code resampled; now everyone does.

def test_an_empty_anthropic_sample_is_resampled_instead_of_killing_the_turn(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
        ([], {"text": "the real reply", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-sonnet-5")
    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert len(calls) == 2, "the empty sample was not resampled"
    assert reply == "the real reply"
    # The k3 cache-buster is dialect-specific and must NOT ride other providers.
    assert len(calls[1]["params"]["messages"]) == 1


def test_reasoning_without_text_is_not_paid_for_twice(monkeypatch):
    """Thinking alone is a completion outside the k3 text dialect — accepted as-is."""
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "reasoned it out", "stopReason": "stop", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-opus-5")
    reply, thinking = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert len(calls) == 1, "a thinking-only sample must not trigger a billed resample"
    assert reply == "" and thinking == "reasoned it out"


def test_two_empty_samples_still_surface_the_real_error(monkeypatch):
    _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-sonnet-5")
    with pytest.raises(omp_bridge.BridgeUnavailable, match="no completion"):
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])


def test_a_length_stop_is_reported_as_truncation_not_a_missing_reply(monkeypatch):
    """Live 2026-08-04: claude-fable-5 at the harness default reasoningEffort="high"
    burns the whole max_tokens budget reasoning and answers stopReason "length" with no
    text and no thinking. Every leg died in a second as an unexplained provider_error,
    while the same call with no reasoning effort replies normally."""
    _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "length", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-fable-5",
                             reasoning_effort="high")
    with pytest.raises(omp_bridge.BridgeUnavailable) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}], max_tokens=16)
    msg = str(ei.value)
    assert "TRUNCATED" in msg and "16" in msg and "high" in msg, msg


def test_a_length_stop_is_never_resampled(monkeypatch):
    """The same prompt under the same budget truncates identically — a resample is a
    fully billed no-op."""
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "length", "usage": {}}),
        ([], {"text": "would never be reached", "thinking": "", "stopReason": "stop",
              "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-fable-5")
    with pytest.raises(omp_bridge.BridgeUnavailable):
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "a deterministic truncation must not be paid for twice"


# --- (f) raising callback never breaks the bridge RPC --------------------------

def test_raising_callback_does_not_break_call():
    client = omp_bridge.OmpBridgeClient()

    class _FakeProc:
        stdin = io.StringIO()

        def poll(self):
            return None

    client._proc = _FakeProc()
    client._out_q.put(json.dumps(
        {"id": 1, "event": "delta", "data": {"text": "frag"}}) + "\n")
    client._out_q.put(json.dumps(
        {"id": 99, "event": "delta", "data": {"text": "not ours"}}) + "\n")
    client._out_q.put(json.dumps(
        {"id": 1, "ok": True, "result": {"text": "done"}}) + "\n")

    seen = []

    def cb(kind, data):
        seen.append(kind)
        raise RuntimeError("boom")

    result = client.call("complete", {"stream": True}, timeout=5, on_event=cb)
    assert result == {"text": "done"}
    assert seen == ["delta"]  # only OUR id dispatched
    client._proc = None


# --- (g) run_turn installs/restores the sink and emits assistant_delta ---------

def test_run_turn_streams_and_restores_sink():
    class _StreamingProvider:
        name = "streaming-fake"
        replies = [
            '```json\n{"tool": "measure_part", "args": {}}\n```',
            '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
        ]

        def complete(self, system, messages, max_tokens=8000):
            sink = pbase.provider_delta_sink()
            assert sink is not None, "sink must be installed during the turn"
            sink("Hello ", "text")
            sink("world, this is a long enough fragment to flush now", "text")
            return self.replies.pop(0)

    events = []
    res = agent_turn.run_turn(
        "go", _StreamingProvider(), allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        on_event=lambda kind, payload: events.append((kind, payload)))

    assert res.success is True
    deltas = [p for k, p in events if k == "assistant_delta"]
    assert deltas, "expected at least one assistant_delta"
    text_deltas = [d for d in deltas if d["channel"] == "text"]
    joined = "".join(d["text"] for d in text_deltas)
    assert "Hello world" in joined
    assert joined.endswith("flush now")  # trailing partial flushed at turn end
    # sink restored after the turn
    assert pbase.provider_delta_sink() is None


def test_run_turn_without_on_event_installs_nothing():
    class _NoSinkProvider:
        name = "no-sink-fake"
        replies = [
            '```json\n{"tool": "measure_part", "args": {}}\n```',
            '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```',
        ]

        def complete(self, system, messages, max_tokens=8000):
            assert pbase.provider_delta_sink() is None
            return self.replies.pop(0)

    res = agent_turn.run_turn("go", _NoSinkProvider(),
                              allowed_tools=["measure_part"],
                              tool_dispatch=lambda t, a: {"ok": True})
    assert res.success is True
