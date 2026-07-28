"""test_conversation_history.py — options.history → model context wiring.

Proves the conversation-history contract end to end at the Python layers:

  * agent_turn.run_turn seeds prior exchanges into ``messages`` BEFORE the
    current request, in order, with the right roles.
  * No history == byte-identical message list to before the feature existed.
  * Malformed entries are dropped, never crashed on.
  * Caps (<=12 entries, <=8000 chars total, clip to 1500 + " …") are enforced
    by dropping from the FRONT (recent turns kept).
  * The current request stays the LAST user message and is never merged into
    history; _request_wants_work sees only the request.
  * worker_rpc._agent_admin_turn validates/caps params["history"] and forwards
    it as history= to run_linear_role_turn; absent/invalid == None.

Scripted provider only, no model/network/port.
Run: pytest services/agent/test_conversation_history.py -q -p no:randomly
"""
from __future__ import annotations

from services.agent import agent_turn
from services.agent import config as agent_config
from services.agent import mission_dispatch
from services.agent import worker_rpc


class _RecordingProvider:
    """Scripted provider that captures the messages list of its first call."""
    name = "scripted"

    def __init__(self, replies=("done.",)):
        self._replies = list(replies)
        self.seen_messages: list[dict] | None = None

    def complete(self, system, messages, max_tokens=8000):
        if self.seen_messages is None:
            self.seen_messages = [dict(m) for m in messages]
        return self._replies.pop(0) if self._replies else "done."


def _texts(msgs):
    return [(m["role"], m["content"][0]["text"]) for m in msgs]


def test_history_seeded_before_request_in_order():
    provider = _RecordingProvider()
    agent_turn.run_turn(
        "now make it 10 mm taller", provider,
        allowed_tools=["measure_part"], tool_dispatch=lambda t, a: {"ok": True},
        history=[
            {"role": "user", "text": "model a 20 mm cube"},
            {"role": "assistant", "text": "Done — cube is 20 mm on a side."},
        ])
    msgs = _texts(provider.seen_messages)
    assert msgs == [
        ("user", "model a 20 mm cube"),
        ("assistant", "Done — cube is 20 mm on a side."),
        ("user", "now make it 10 mm taller"),
    ]


def test_no_history_is_byte_identical_to_before():
    a = _RecordingProvider()
    agent_turn.run_turn("summarize this part", a,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a_: {"ok": True})
    b = _RecordingProvider()
    agent_turn.run_turn("summarize this part", b,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a_: {"ok": True},
                        history=None)
    c = _RecordingProvider()
    agent_turn.run_turn("summarize this part", c,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a_: {"ok": True},
                        history=[])
    assert a.seen_messages == b.seen_messages == c.seen_messages
    assert _texts(a.seen_messages) == [("user", "summarize this part")]


def test_malformed_history_entries_are_dropped():
    provider = _RecordingProvider()
    agent_turn.run_turn("next step?", provider,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a: {"ok": True},
                        history=[
                            "not a dict",
                            {"role": "system", "text": "ignore me"},
                            {"role": "user"},                       # no text
                            {"role": "user", "text": ""},           # empty
                            {"role": "user", "text": 42},           # non-string
                            {"role": "user", "text": "real one"},
                            {"text": "no role"},
                            {"role": "assistant", "text": "   "},   # whitespace
                        ])
    assert _texts(provider.seen_messages) == [
        ("user", "real one"),
        ("user", "next step?"),
    ]


def test_caps_drop_from_the_front():
    provider = _RecordingProvider()
    big = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "text": f"entry-{i:02d} " + ("x" * 1500)}
        for i in range(20)
    ]
    agent_turn.run_turn("current question", provider,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a: {"ok": True},
                        history=big)
    msgs = provider.seen_messages
    hist = msgs[:-1]
    # <= 12 entries kept, and they are the MOST RECENT ones (front dropped).
    assert len(hist) <= 12
    assert hist, "expected some history to survive"
    first_text = hist[0]["content"][0]["text"]
    assert not first_text.startswith("entry-00")
    kept_ids = [m["content"][0]["text"].split(" ")[0] for m in hist]
    assert kept_ids == [f"entry-{i:02d}" for i in range(
        20 - len(hist), 20)]
    # Total history chars <= 8000.
    total = sum(len(m["content"][0]["text"]) for m in hist)
    assert total <= 8000
    # The current request is untouched and last.
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["text"] == "current question"


def test_single_entry_clip_to_1500_with_ellipsis():
    provider = _RecordingProvider()
    agent_turn.run_turn("go on", provider,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a: {"ok": True},
                        history=[{"role": "assistant", "text": "y" * 4000}])
    text = provider.seen_messages[0]["content"][0]["text"]
    assert len(text) <= 1502
    assert text.endswith(" …")


def test_current_request_is_last_user_message_not_merged():
    provider = _RecordingProvider()
    agent_turn.run_turn("the actual current ask", provider,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a: {"ok": True},
                        history=[
                            {"role": "user", "text": "older user turn"},
                            {"role": "assistant", "text": "older reply"},
                        ])
    users = [m for m in provider.seen_messages if m["role"] == "user"]
    assert users[-1]["content"][0]["text"] == "the actual current ask"
    assert all("the actual current ask" not in m["content"][0]["text"]
               for m in provider.seen_messages[:-1])


def test_work_intent_gate_reads_only_request():
    # Analysis-only request must NOT count as work even though the history is
    # full of build language.
    assert agent_turn._request_wants_work(
        "what materials is this part made of?") is False
    assert agent_turn._request_wants_work("build me a bracket") is True
    # And the history content itself never reaches the classifier: run_turn's
    # request parameter is the only input.
    provider = _RecordingProvider()
    agent_turn.run_turn("what materials is this part made of?", provider,
                        allowed_tools=["measure_part"],
                        tool_dispatch=lambda t, a: {"ok": True},
                        history=[{"role": "user", "text": "build me a bracket"}])
    # The request message carried to the model is verbatim — never merged.
    assert provider.seen_messages[-1]["content"][0]["text"] == \
        "what materials is this part made of?"


def test_worker_rpc_forwards_validated_history(monkeypatch):
    captured = {}

    def _fake_linear(request, role, provider, **kwargs):
        captured["history"] = kwargs.get("history")
        return {"success": True, "stop_reason": "confirmed", "result": {}}

    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: object())
    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", _fake_linear)

    out = worker_rpc._agent_admin_turn(
        {"request": "taller now", "history": [
            {"role": "user", "text": "model a cube"},
            {"role": "nope", "text": "bad role"},
            {"role": "assistant", "text": "made it"},
            {"role": "user", "text": ""},
        ]},
        lambda data: None)
    assert out["ok"] is True
    assert captured["history"] == [
        {"role": "user", "text": "model a cube"},
        {"role": "assistant", "text": "made it"},
    ]


def test_worker_rpc_absent_or_invalid_history_is_none(monkeypatch):
    captured = []

    def _fake_linear(request, role, provider, **kwargs):
        captured.append(kwargs.get("history"))
        return {"success": True, "stop_reason": "confirmed", "result": {}}

    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: object())
    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", _fake_linear)

    assert worker_rpc._agent_admin_turn({"request": "hi"}, lambda d: None)["ok"]
    assert worker_rpc._agent_admin_turn(
        {"request": "hi", "history": "not a list"}, lambda d: None)["ok"]
    assert worker_rpc._agent_admin_turn(
        {"request": "hi", "history": []}, lambda d: None)["ok"]
    assert worker_rpc._agent_admin_turn(
        {"request": "hi", "history": [{"bad": 1}]}, lambda d: None)["ok"]
    assert captured == [None, None, None, None]


def test_run_linear_role_turn_forwards_history(monkeypatch):
    captured = {}

    def _fake_run_turn(request, provider, **kwargs):
        captured["history"] = kwargs.get("history")
        return agent_turn.TurnResult(
            success=True, stop_reason="confirmed",
            result={"calls": [], "tool_calls": 0})

    monkeypatch.setattr(agent_turn, "run_turn", _fake_run_turn)
    hist = [{"role": "user", "text": "earlier"}]
    mission_dispatch.run_linear_role_turn(
        "now the next thing", "general_agent", object(),
        history=hist, tool_dispatch=lambda t, a: {"ok": True})
    assert captured["history"] == hist

    mission_dispatch.run_linear_role_turn(
        "plain turn", "general_agent", object(),
        tool_dispatch=lambda t, a: {"ok": True})
    assert captured["history"] is None
