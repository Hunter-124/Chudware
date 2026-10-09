"""agent.message forwarding: Chudcode IRC frames and coordination-board posts
reach the GUI event stream as compact inter-agent message rows."""
from __future__ import annotations

import pytest

from services.agent import task_list
from services.agent.chudcode_events import map_rpc_frame
from services.agent.coordination import CoordinationBoard, clear_boards, get_board

CTX = {"run_id": "run-7", "session_id": "session-2", "node_id": "root-node"}


def _irc(custom_type, details, content=""):
    return {
        "type": "irc_message",
        "message": {
            "role": "custom",
            "customType": custom_type,
            "content": content,
            "display": True,
            "details": details,
            "attribution": "agent",
            "timestamp": 1760000000000,
        },
    }


def test_relay_between_subagents_is_one_agent_message():
    frame = _irc(
        "irc:relay",
        {"from": "SubA", "to": "SubB", "body": "Bore is 22 mm.\nUse H7."},
        content="[IRC `SubA` → `SubB`]\n\nBore is 22 mm.\nUse H7.",
    )
    [(kind, row)] = map_rpc_frame(frame, CTX)
    assert kind == "agent.message"
    assert (row["channel"], row["from"], row["to"]) == ("irc", "SubA", "SubB")
    assert row["text"] == "Bore is 22 mm.\nUse H7."
    assert row["run_id"] == "run-7" and row["id"]


def test_incoming_message_is_addressed_to_main_with_its_body_only():
    frame = _irc(
        "irc:incoming",
        {"id": "m-1", "from": "Planner", "message": "Ready for review", "replyTo": "m-0"},
        content="<irc>rendered prompt wrapper from agent `Planner`</irc>",
    )
    [(kind, row)] = map_rpc_frame(frame, CTX)
    assert kind == "agent.message"
    assert (row["from"], row["to"], row["text"]) == ("Planner", "Main", "Ready for review")
    assert row["message_id"] == "m-1" and row["reply_to"] == "m-0"


def test_workpool_card_uses_pool_channel_and_mode():
    frame = _irc(
        "irc:workpool",
        {"pool": "w", "from": "pool:w", "to": "Worker1", "body": "Mesh the bracket", "mode": "dispatched"},
    )
    [(kind, row)] = map_rpc_frame(frame, CTX)
    assert (kind, row["channel"], row["mode"], row["to"]) == (
        "agent.message", "pool", "dispatched", "Worker1")


def test_legacy_record_without_details_falls_back_to_content():
    frame = _irc("irc:relay", None, content="[IRC `A` → `B`]\n\nhello from agent `A`")
    [(kind, row)] = map_rpc_frame(frame, CTX)
    assert kind == "agent.message"
    assert row["from"] == "A"
    assert row["text"] == "hello from agent `A`"


def test_identical_frames_map_to_the_same_id_so_the_gui_can_dedupe():
    frame = _irc("irc:relay", {"from": "A", "to": "B", "body": "x"})
    [(_, a)] = map_rpc_frame(frame, CTX)
    [(_, b)] = map_rpc_frame(frame, CTX)
    assert a["id"] == b["id"]


def test_empty_message_emits_nothing():
    assert map_rpc_frame(_irc("irc:relay", {"from": "A", "to": "B", "body": ""}), CTX) == []
    assert map_rpc_frame({"type": "irc_message"}, CTX) == []


def test_board_post_forwards_to_bound_sink():
    seen = []
    board = CoordinationBoard("run_fwd", persist=False, on_event=lambda k, p: seen.append((k, p)))
    out = board.post_progress("agent_a", "sketch closed")
    assert out["ok"]
    [(kind, payload)] = seen
    assert kind == "agent.message"
    assert (payload["channel"], payload["from"], payload["to"], payload["text"]) == (
        "board", "agent_a", "board", "sketch closed")
    assert payload["run_id"] == "run_fwd" and payload["id"]


def test_board_post_uses_the_turn_sink_when_unbound():
    seen = []
    token = task_list.set_event_sink(lambda k, p: seen.append((k, p)))
    try:
        CoordinationBoard("run_ambient", persist=False).post_progress("agent_b", "done")
    finally:
        task_list.clear_event_sink(token)
    assert [k for k, _ in seen] == ["agent.message"]


def test_board_post_survives_a_raising_sink_and_no_sink():
    def boom(kind, payload):
        raise RuntimeError("ui gone")

    assert CoordinationBoard("run_boom", persist=False, on_event=boom).post_progress("a", "n")["ok"]
    assert CoordinationBoard("run_none", persist=False).post_progress("a", "n")["ok"]


def test_rejected_post_forwards_nothing():
    seen = []
    board = CoordinationBoard("run_rej", persist=False, on_event=lambda k, p: seen.append(k))
    assert not board.post_progress("agent_a", "   ")["ok"]
    assert seen == []


@pytest.fixture
def _clean_registry():
    clear_boards()
    yield
    clear_boards()


def test_get_board_binds_sink_once(_clean_registry):
    first, second = [], []
    board = get_board("run_reg", persist=False, on_event=lambda k, p: first.append(k))
    assert get_board("run_reg", persist=False, on_event=lambda k, p: second.append(k)) is board
    board.post_progress("x", "note")
    assert first == ["agent.message"] and second == []
