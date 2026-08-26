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
  (e) streamed content survives an empty final envelope or missing response line
      without being emitted to the display sink twice;
  (f) empty replies resample once and length-with-effort retries without effort;
  (g) a raising on_event callback never breaks the bridge RPC;
  (h) run_turn installs/restores the sink and emits assistant_delta through on_event.
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
from services.agent.providers import token_usage


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
        if method != "complete":
            return {}
        calls.append({"method": method, "params": dict(params or {}), "kwargs": kwargs})
        events, result = steps.pop(0)
        cb = kwargs.get("on_event")
        for kind, data in events:
            if cb is not None:
                cb(kind, data)
        if isinstance(result, BaseException):
            raise result
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


# --- (c) coalescing: thresholds, channel order, and trailing flush --------------

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

    coal.feed("tail", "text")
    coal.feed("think-frag", "thinking")  # channel switch flushes the older tail first
    assert events[1:] == [
        ("assistant_delta", {"text": "tail", "channel": "text"}),
    ]
    coal.flush_all()
    assert events[2:] == [
        ("assistant_delta", {"text": "think-frag", "channel": "thinking"}),
    ]


def test_live_run_19873622099184_seams_stay_in_arrival_order():
    """Faithful reduction of the three corrupt seams in the 227-line live log.

    The old per-channel buffers emitted these as ``batch them.The user``,
    ``saved file.The harness``, and ``guide.Stop``.  The leading fragments are
    exact buffered tails from events.jsonl lines 97, 205, and 224; each arrived
    before the intervening text/context record but was written after it.
    """
    events = []
    coal = agent_turn._AssistantDeltaCoalescer(
        lambda kind, payload: events.append((kind, payload)))

    def external(kind):
        # run_turn's ordered on_event wrapper does this before every non-delta
        # event (agent.context, context injection, tools, terminal records).
        coal.flush_all()
        events.append((kind, {}))

    coal.feed("The user wants a brand new 3D model of", "thinking")
    coal.feed(" from task_list, so batch them.", "thinking")
    coal.feed("I'll build this as a fresh parametric part", "text")
    external("agent.context")

    coal.feed("The user wants an IWB holster for Ruger LCP .380.", "thinking")
    coal.feed(" saved file.", "thinking")
    coal.feed("A holster is a self-contained single-part build", "text")
    external("agent.context")
    external("agent.context_injected")

    coal.feed("The harness wants direct action.", "thinking")
    coal.feed(" independent. Then add_features based on guide.", "thinking")
    coal.feed("Building it directly now.", "text")
    external("agent.context")
    coal.feed("Stop announcing, emit tool calls.", "thinking")
    coal.flush_all()

    observed = []
    for kind, payload in events:
        row = (kind, payload.get("channel"), payload.get("text", ""))
        if (observed and kind == "assistant_delta"
                and observed[-1][0] == kind and observed[-1][1] == row[1]):
            observed[-1] = (kind, row[1], observed[-1][2] + row[2])
        else:
            observed.append(row)
    assert observed == [
        ("assistant_delta", "thinking",
         "The user wants a brand new 3D model of from task_list, so batch them."),
        ("assistant_delta", "text", "I'll build this as a fresh parametric part"),
        ("agent.context", None, ""),
        ("assistant_delta", "thinking",
         "The user wants an IWB holster for Ruger LCP .380. saved file."),
        ("assistant_delta", "text", "A holster is a self-contained single-part build"),
        ("agent.context", None, ""),
        ("agent.context_injected", None, ""),
        ("assistant_delta", "thinking",
         "The harness wants direct action. independent. Then add_features based on guide."),
        ("assistant_delta", "text", "Building it directly now."),
        ("agent.context", None, ""),
        ("assistant_delta", "thinking", "Stop announcing, emit tool calls."),
    ]
    joined_thinking = "".join(
        payload["text"] for kind, payload in events
        if kind == "assistant_delta" and payload["channel"] == "thinking"
    )
    assert joined_thinking == (
        "The user wants a brand new 3D model of from task_list, so batch them."
        "The user wants an IWB holster for Ruger LCP .380. saved file."
        "The harness wants direct action. independent. Then add_features based on guide."
        "Stop announcing, emit tool calls."
    )
    thinking_chunks = [
        payload["text"] for kind, payload in events
        if kind == "assistant_delta" and payload["channel"] == "thinking"
    ]
    assert all("batch them.The user" not in chunk for chunk in thinking_chunks)
    assert all("saved file.The harness" not in chunk for chunk in thinking_chunks)
    assert all("guide.Stop" not in chunk for chunk in thinking_chunks)


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


# --- (e) stream salvage: final-envelope gaps never discard paid content --------


def test_live_220_delta_shape_salvages_plain_text_once(monkeypatch):
    """The exact live count and non-directive text, then empty final content."""
    streamed_reply = "Building it directly now. First, the op vocabulary"
    text_chunks = list(streamed_reply[:17]) + [streamed_reply[17:]]
    thinking_chunks = ["x"] * 202
    streamed = (
        [("delta", {"thinking": chunk}) for chunk in thinking_chunks]
        + [("delta", {"text": chunk}) for chunk in text_chunks]
    )
    calls = _fake_bridge(monkeypatch, [
        (streamed,
         {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)

    prov = OmpBridgeProvider(provider="kimi-code", model="k3-256k")
    reply, thinking = prov.complete_ex(
        "sys", [{"role": "user", "content": "measure it"}])

    assert len(calls) == 1, "stream salvage must precede a billed resample"
    assert reply == streamed_reply
    assert thinking == "x" * 202
    # DISPLAY got all 220 incremental fragments exactly once. The returned copy
    # is PARSE-only: it was not concatenated back into the sink.
    assert len(sink.feeds) == 220
    assert sum(channel == "thinking" for _, channel in sink.feeds) == 202
    assert sum(channel == "text" for _, channel in sink.feeds) == 18
    assert "".join(text for text, channel in sink.feeds if channel == "text") == reply


def test_missing_final_response_line_salvages_streamed_directive(monkeypatch):
    """Transport EOF/timeout after deltas is distinct from an empty final envelope."""
    directive = '```json\n{"done": true, "handoff": {"summary": "ok"}}\n```'
    calls = _fake_bridge(monkeypatch, [
        ([("delta", {"thinking": "finished"}),
          ("delta", {"text": directive})],
         omp_bridge.BridgeUnavailable("bridge process closed stdout")),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)

    prov = OmpBridgeProvider(provider="kimi-code", model="k3-256k")
    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "finish"}])

    assert len(calls) == 1
    assert agent_turn._parse_directive(reply)["done"] is True
    assert sink.feeds.count((directive, "text")) == 1


def test_fenced_directive_in_streamed_thinking_precedes_resample(monkeypatch):
    directive = '```json\n{"tool": "measure_part", "args": {}}\n```'
    calls = _fake_bridge(monkeypatch, [
        ([("delta", {"thinking": directive})],
         {"text": "", "thinking": "final summary omitted the fence",
          "stopReason": "stop", "usage": {}}),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)
    prov = OmpBridgeProvider(provider="kimi-code", model="k3-256k")

    reply, _ = prov.complete_ex("sys", [{"role": "user", "content": "measure"}])

    assert len(calls) == 1
    assert agent_turn._parse_directive(reply)["tool"] == "measure_part"


def test_missing_final_response_names_cause_without_python_resample(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], omp_bridge.BridgeUnavailable("bridge process closed stdout")),
        ([], omp_bridge.BridgeUnavailable("bridge process closed stdout")),
    ])
    prov = OmpBridgeProvider(provider="kimi-code", model="k3-256k")

    with pytest.raises(omp_bridge.BridgeUnavailable) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "finish"}])

    assert len(calls) == 1
    message = str(ei.value)
    assert "stream ended before the final response line" in message
    assert "no usable text or fenced directive" in message

# --- (f) OMP owns retries; Python preserves the sidecar's single result ----------

def test_python_layer_does_not_discard_or_resample_streamed_thinking(monkeypatch):
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
    reply, thinking = prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert len(calls) == 1
    assert sink.discards == 0
    assert sink.feeds == [("half ", "thinking")]
    assert reply == ""
    assert thinking == "just reasoning, no fenced block"


# --- (f2) empty-completion retries belong to vendored OMP ---------------------

def test_empty_anthropic_result_is_not_resampled_in_python(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
        ([], {"text": "the real reply", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-sonnet-5")
    with pytest.raises(omp_bridge.BridgeUnavailable):
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])

    assert len(calls) == 1


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
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    prov = OmpBridgeProvider(provider="anthropic-sub", model="claude-sonnet-5")
    with pytest.raises(
            omp_bridge.BridgeUnavailable,
            match="final response contained no text or thinking"):
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "vendored OMP is the sole empty-completion retry owner"


def test_length_stop_retries_once_without_effort_and_counts_both_usages(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {
            "text": "", "thinking": "", "stopReason": "length",
            "usage": {"input": 10, "output": 16, "totalTokens": 26},
        }),
        ([("delta", {"text": "recovered"})], {
            "text": "recovered", "thinking": "", "stopReason": "stop",
            "usage": {"input": 11, "output": 4, "totalTokens": 15},
        }),
    ])
    sink = _RecordingSink()
    pbase.set_provider_delta_sink(sink)
    token_usage.begin_turn_usage()
    prov = OmpBridgeProvider(
        provider="anthropic-sub", model="claude-fable-5",
        reasoning_effort="high")

    reply, thinking = prov.complete_ex(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=16)
    usage = token_usage.get_turn_usage()

    assert reply == "recovered" and thinking == ""
    assert len(calls) == 2
    assert calls[0]["params"]["reasoningEffort"] == "high"
    assert "reasoningEffort" not in calls[1]["params"]
    assert calls[0]["params"]["maxTokens"] == calls[1]["params"]["maxTokens"] == 16
    assert sink.feeds == [("recovered", "text")]
    assert usage["calls"] == 2
    assert usage["prompt_tokens"] == 21
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 41
    token_usage.begin_turn_usage()


def test_second_empty_length_stop_surfaces_after_exactly_one_retry(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {
            "text": "", "thinking": "", "stopReason": "length",
            "usage": {"input": 3, "output": 8, "totalTokens": 11},
        }),
        ([], {
            "text": "", "thinking": "", "stopReason": "length",
            "usage": {"input": 4, "output": 8, "totalTokens": 12},
        }),
    ])
    token_usage.begin_turn_usage()
    prov = OmpBridgeProvider(
        provider="anthropic-sub", model="claude-fable-5",
        reasoning_effort="high")

    with pytest.raises(omp_bridge.BridgeUnavailable, match="TRUNCATED"):
        prov.complete_ex(
            "sys", [{"role": "user", "content": "hi"}], max_tokens=8)
    usage = token_usage.get_turn_usage()

    assert len(calls) == 2
    assert calls[0]["params"]["reasoningEffort"] == "high"
    assert "reasoningEffort" not in calls[1]["params"]
    assert usage["calls"] == 2
    assert usage["total_tokens"] == 23
    token_usage.begin_turn_usage()


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


# --- (g) raising callback never breaks the bridge RPC --------------------------

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


def test_unrecoverable_empty_completion_reaches_turn_error_detail(monkeypatch):
    calls = _fake_bridge(monkeypatch, [
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
        ([], {"text": "", "thinking": "", "stopReason": "stop", "usage": {}}),
    ])
    provider = OmpBridgeProvider(provider="kimi-code", model="k3-256k")

    result = agent_turn.run_turn(
        "answer the question", provider,
        on_event=lambda _kind, _payload: None,
    )

    assert len(calls) == 1
    assert result.success is False
    assert result.stop_reason == "provider_error"
    detail = result.result["error_detail"]
    assert "final response contained no text or thinking" in detail
    assert "OMP's bounded empty-completion retries" in detail
    assert result.result["retryable"] is True


# --- (h) run_turn installs/restores the sink and emits assistant_delta ---------

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
