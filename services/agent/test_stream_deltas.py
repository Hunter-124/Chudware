"""Provider-neutral assistant-delta coalescing and turn-lifetime regressions."""

from __future__ import annotations

import time

import pytest

from services.agent import agent_turn
from services.agent.providers import base as pbase


@pytest.fixture(autouse=True)
def _clean_sink():
    prev = pbase.set_provider_delta_sink(None)
    yield
    pbase.set_provider_delta_sink(prev)


# Coalescing thresholds, channel order, and trailing flush.


def test_coalescer_batches_and_flushes_trailing():
    events = []
    coal = agent_turn._AssistantDeltaCoalescer(
        lambda kind, payload: events.append((kind, payload))
    )

    coal.feed("aaa", "text")
    coal.feed("bbb", "text")
    coal.feed("ccc", "text")
    assert events == []  # 9 chars, no newline, <60 ms → buffered

    coal.feed("x" * 40, "text")  # crosses 48 chars → ONE flush of everything
    assert events == [
        ("assistant_delta", {"text": "aaabbbccc" + "x" * 40, "channel": "text"})
    ]

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
        lambda kind, payload: events.append((kind, payload))
    )

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
        if (
            observed
            and kind == "assistant_delta"
            and observed[-1][0] == kind
            and observed[-1][1] == row[1]
        ):
            observed[-1] = (kind, row[1], observed[-1][2] + row[2])
        else:
            observed.append(row)
    assert observed == [
        (
            "assistant_delta",
            "thinking",
            "The user wants a brand new 3D model of from task_list, so batch them.",
        ),
        ("assistant_delta", "text", "I'll build this as a fresh parametric part"),
        ("agent.context", None, ""),
        (
            "assistant_delta",
            "thinking",
            "The user wants an IWB holster for Ruger LCP .380. saved file.",
        ),
        ("assistant_delta", "text", "A holster is a self-contained single-part build"),
        ("agent.context", None, ""),
        ("agent.context_injected", None, ""),
        (
            "assistant_delta",
            "thinking",
            "The harness wants direct action. independent. Then add_features based on guide.",
        ),
        ("assistant_delta", "text", "Building it directly now."),
        ("agent.context", None, ""),
        ("assistant_delta", "thinking", "Stop announcing, emit tool calls."),
    ]
    joined_thinking = "".join(
        payload["text"]
        for kind, payload in events
        if kind == "assistant_delta" and payload["channel"] == "thinking"
    )
    assert joined_thinking == (
        "The user wants a brand new 3D model of from task_list, so batch them."
        "The user wants an IWB holster for Ruger LCP .380. saved file."
        "The harness wants direct action. independent. Then add_features based on guide."
        "Stop announcing, emit tool calls."
    )
    thinking_chunks = [
        payload["text"]
        for kind, payload in events
        if kind == "assistant_delta" and payload["channel"] == "thinking"
    ]
    assert all("batch them.The user" not in chunk for chunk in thinking_chunks)
    assert all("saved file.The harness" not in chunk for chunk in thinking_chunks)
    assert all("guide.Stop" not in chunk for chunk in thinking_chunks)


def test_coalescer_newline_and_elapsed_flush():
    events = []
    coal = agent_turn._AssistantDeltaCoalescer(
        lambda kind, payload: events.append((kind, payload))
    )

    coal.feed("line\n", "text")  # newline flushes immediately
    assert events == [("assistant_delta", {"text": "line\n", "channel": "text"})]

    coal.feed("tiny", "text")
    assert len(events) == 1
    coal._last["text"] = time.monotonic() - 1.0  # age the channel past 60 ms
    coal.feed("bit", "text")
    assert events[1] == ("assistant_delta", {"text": "tinybit", "channel": "text"})


# Turn lifetime installs/restores the sink and emits assistant_delta.


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
        "go",
        _StreamingProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

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

    res = agent_turn.run_turn(
        "go",
        _NoSinkProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert res.success is True
