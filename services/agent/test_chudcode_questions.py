"""Interactive ask_user: the desktop chat's question form blocks the tool until answered."""
from __future__ import annotations

import threading
import time

from services.agent.chudcode_events import map_rpc_frame
from services.agent.chudcode_host import ChudcodeSessionHost
from services.agent.mission_dispatch import MissionDispatch
from services.agent.test_chudcode_host import FakeFactory


class _Binding:
    """Host-tool binding stand-in: run_turn binds ask_human into its shared context."""

    def __init__(self) -> None:
        self.context: dict = {}


def _start_turn(host, factory, run_id="run-q", **extra):
    binding = _Binding()
    emitted: list = []
    output: dict = {}
    params = {
        "request": "Design the bracket",
        "session_id": "conv-q",
        "run_id": run_id,
        "host_tools": [binding],
        **extra,
    }
    thread = threading.Thread(
        target=lambda: output.setdefault("result", host.run_turn(params, emitted.append)),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    assert factory.clients[0].prompted.wait(1.0)
    return binding, emitted, output, thread


def _ask_async(dispatch, binding, questions):
    out: dict = {}

    def call():
        ask_human = binding.context["ask_human"]
        out["result"] = dispatch.ask_user(
            questions=questions,
            ask_human=lambda batch: ask_human(batch, agent_id=None, cancel=None),
        )

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return out, thread


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _frames(emitted, kind):
    return [f for f in emitted if f.get("type") == kind]


def test_form_answers_unblock_the_tool_with_typed_user_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, emitted, output, turn = _start_turn(host, factory, interactive_questions=True)
    dispatch = MissionDispatch(run_id="run-q")
    asked, thread = _ask_async(dispatch, binding, [
        {"id": "material", "kind": "choice", "prompt": "Material?",
         "options": ["PLA", "PETG"], "suggested": "PLA"},
        {"id": "bore", "kind": "dimension", "family": "length", "prompt": "Bore?",
         "suggested": {"value": 8, "unit": "mm"}, "required": True},
    ])
    assert _wait_for(lambda: _frames(emitted, "agent_question"))
    question = _frames(emitted, "agent_question")[0]
    assert thread.is_alive(), "ask_user must block until the user answers"

    [(kind, payload)] = map_rpc_frame(question, {"run_id": "run-q"})
    assert kind == "agent.question"
    assert [o["value"] for o in payload["questions"][0]["options"]] == ["PLA", "PETG"]
    assert payload["questions"][1]["suggested"] == {"unit": "mm", "value": 8}

    batch_id = question["batch_id"]
    assert host.answer("run-q", "b_unknown", {"material": "PLA"})["ok"] is False
    result = host.answer("run-q", batch_id, {"material": "PETG",
                                             "bore": {"value": 0.5, "unit": "in"}})
    assert result == {"ok": True, "batch_id": batch_id, "answered": True, "run_id": "run-q"}
    thread.join(2.0)
    answers = asked["result"]["answers"]
    assert asked["result"]["answered_by"] == "user"
    assert answers["material"]["value"] == "PETG" and answers["material"]["source"] == "user"
    assert answers["bore"]["canonical"] == {"value": 12.7, "unit": "mm"}
    assert [f["source"] for f in _frames(emitted, "agent_answer")] == ["user"]
    assert host.answer("run-q", batch_id, {"material": "PLA"})["ok"] is False

    factory.clients[0].release.set()
    turn.join(2.0)
    assert output["result"]["ok"] is True
    assert "ask_human" not in binding.context


def test_a_chat_reply_answers_the_pending_form_instead_of_steering(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, emitted, output, turn = _start_turn(host, factory, interactive_questions=True)
    dispatch = MissionDispatch(run_id="run-q")
    asked, thread = _ask_async(dispatch, binding, "Which motor will you use?")
    assert _wait_for(lambda: _frames(emitted, "agent_question"))

    steered = host.steer("run-q", "An Estes E12-4", steer_id="s-reply")
    assert steered["answered_question"] == _frames(emitted, "agent_question")[0]["batch_id"]
    thread.join(2.0)
    assert asked["result"]["user_reply"] == "An Estes E12-4"
    assert asked["result"]["answers"] == {}
    assert not [c for c in factory.clients[0].calls if c[0] == "steer"]
    answer = _frames(emitted, "agent_answer")[0]
    assert answer["steer_id"] == "s-reply"
    mapped = map_rpc_frame(answer, {"run_id": "run-q"})
    assert [kind for kind, _ in mapped] == ["agent.answer", "agent.steer"]
    assert mapped[1][1]["steer_id"] == "s-reply"
    assert mapped[1][1]["status"] == "delivered"

    # With nothing pending, the same path steers again.
    assert host.steer("run-q", "Also keep it light")["steered"] is True
    assert ("steer", {"message": "Also keep it light", "images": None}) in factory.clients[0].calls
    factory.clients[0].release.set()
    turn.join(2.0)


def test_stop_releases_a_waiting_question_as_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, emitted, output, turn = _start_turn(host, factory, interactive_questions=True)
    asked, thread = _ask_async(MissionDispatch(run_id="run-q"), binding, "Which motor?")
    assert _wait_for(lambda: _frames(emitted, "agent_question"))

    host.cancel("run-q")
    thread.join(2.0)
    assert asked["result"]["cancelled"] is True and asked["result"]["ok"] is False
    assert [f["source"] for f in _frames(emitted, "agent_answer")] == ["cancelled"]
    turn.join(2.0)


def test_headless_turns_get_no_question_transport():
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, _emitted, _output, turn = _start_turn(host, factory)
    assert "ask_human" not in binding.context
    factory.clients[0].release.set()
    turn.join(2.0)


def test_a_later_batch_reusing_an_id_is_asked_not_replayed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    seen: list = []

    def ask_human(batch):
        from services.agent.chudcode_questions import Resolution

        seen.append(batch)
        return Resolution(source="user", answers={"q1": f"answer {len(seen)}"})

    dispatch = MissionDispatch(run_id="run-replay")
    first = dispatch.ask_user(questions="First question?", ask_human=ask_human)
    second = dispatch.ask_user(questions="A different question?", ask_human=ask_human)
    assert len(seen) == 2
    assert first["answers"]["q1"]["value"] == "answer 1"
    assert second["answers"]["q1"]["value"] == "answer 2"


def test_reply_images_keep_exact_payload_and_wait_for_streamed_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, emitted, _, turn = _start_turn(host, factory, interactive_questions=True)
    asked, question_thread = _ask_async(MissionDispatch(run_id="run-q"), binding, "Which mount?")
    assert _wait_for(lambda: _frames(emitted, "agent_question"))
    attachments = [{"data": "aW1hZ2U=", "mime": "image/png"}]
    result = host.steer("run-q", "Use the four holes [Image #1]", attachments, steer_id="s-image")
    question_thread.join(2.0)
    assert result["steered"] and "answered_question" not in result
    assert asked["result"]["user_reply"] == "Use the four holes [Image #1]"
    answer = _frames(emitted, "agent_answer")[0]
    assert "steer_id" not in answer
    assert [kind for kind, _ in map_rpc_frame(answer)] == ["agent.answer"]
    client = factory.clients[0]
    sent = next(payload for name, payload in client.calls if name == "steer")
    assert sent["message"] == "Use the four holes [Image #1]"
    assert not _frames(emitted, "agent_steer")
    for listener in tuple(client.listeners):
        listener({"type": "message_start", "message": {"role": "user", "content": [
            {"type": "text", "text": sent["message"]},
        ]}})
    assert not _frames(emitted, "agent_steer"), "text alone does not acknowledge the image"
    for listener in tuple(client.listeners):
        listener({"type": "message_start", "message": {"role": "user", "content": [
            {"type": "text", "text": sent["message"]}, *sent["images"],
        ]}})
    assert [(f["steer_id"], f["status"]) for f in _frames(emitted, "agent_steer")] == [
        ("s-image", "delivered")]
    client.release.set()
    turn.join(2.0)


def test_failed_reply_image_send_does_not_claim_attachment_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    binding, emitted, _, turn = _start_turn(host, factory, interactive_questions=True)
    asked, question_thread = _ask_async(MissionDispatch(run_id="run-q"), binding, "Which mount?")
    assert _wait_for(lambda: _frames(emitted, "agent_question"))

    def fail(*args, **kwargs):
        raise RuntimeError("lost control response")

    client = factory.clients[0]
    client.steer = fail
    result = host.steer("run-q", "Use this mount", [{"data": "aW1hZ2U=", "mime": "image/png"}],
                        steer_id="s-failed-image")
    question_thread.join(2.0)
    assert result["steered"] is False
    assert asked["result"]["user_reply"] == "Use this mount"
    assert all("steer_id" not in f for f in _frames(emitted, "agent_answer"))
    assert not _frames(emitted, "agent_steer")
    client.release.set()
    turn.join(2.0)
