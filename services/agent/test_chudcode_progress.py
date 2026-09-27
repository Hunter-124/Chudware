from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from services.agent.chudcode_events import map_rpc_frame
from services.agent.chudcode_host import ChudcodeSessionHost
from services.agent.test_chudcode_host import FakeClient, FakeFactory


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptedClient(FakeClient):
    def wait_for_idle(self, timeout=None):
        for seconds, frame in self.factory.frames:
            self.factory.clock.now += seconds
            for listener in tuple(self.listeners):
                listener(frame)
        for listener in tuple(self.listeners):
            listener({"type": "agent_end", "messages": [], "isTerminal": True})
        return ()


class ScriptedFactory(FakeFactory):
    def __init__(self, clock, frames):
        super().__init__()
        self.clock = clock
        self.frames = frames

    def __call__(self, **kwargs):
        client = ScriptedClient(self, **kwargs)
        self.clients.append(client)
        return client


def tool(name, index, *, seconds=0, args=None, is_error=False):
    call_id = f"tool-{index}"
    return [
        (0, {"type": "tool_execution_start", "toolCallId": call_id,
             "toolName": name, "args": args or {}}),
        (seconds, {"type": "tool_execution_end", "toolCallId": call_id,
                   "toolName": name, "isError": is_error}),
    ]


def run(frames, clock, **params):
    factory = ScriptedFactory(clock, frames)
    host = ChudcodeSessionHost(client_factory=factory, clock=clock)
    emitted = []
    result = host.run_turn({"request": "Build a robot arm", "session_id": "arm",
                            "run_id": "arm-run", "host_tools": [SimpleNamespace(name="new_part"),
                                                            SimpleNamespace(name="add_features")],
                            **params}, emitted.append)
    return result, factory.clients[0].calls, emitted


def steers(calls):
    return [payload["message"] for name, payload in calls if name == "steer"]


def nudges(emitted):
    return [frame for frame in emitted if frame.get("type") == "agent.progress_nudge"]


def test_twelve_web_search_completions_steer_once_and_show_status():
    frames = [item for i in range(18) for item in tool("web_search", i)]
    result, calls, emitted = run(frames, FakeClock())
    assert result["ok"]
    assert len(steers(calls)) == len(nudges(emitted)) == 1
    assert "12 research calls" in steers(calls)[0]
    assert "clearance/tolerance" in steers(calls)[0]
    assert map_rpc_frame(nudges(emitted)[0])[0][0] == "status"
    assert map_rpc_frame(nudges(emitted)[0])[0][1]["phase"] == "progress_nudge"
    assert "start modeling" in map_rpc_frame(nudges(emitted)[0])[0][1]["text"]

def test_recorded_custom_starts_and_tool_result_messages_count_once():
    frames = []
    for i in range(12):
        ident = f"call-{i}"
        frames.append((0, {"type": "custom", "customType": "tool_execution_start",
                           "data": {"toolCallId": ident, "toolName": "web_search"}}))
        frames.append((0, {"type": "message", "message": {
            "role": "toolResult", "toolCallId": ident, "toolName": "web_search",
            "content": [{"type": "text", "text": "Reference"}],
        }}))
        frames.append((0, {"type": "tool_execution_end", "toolCallId": ident,
                           "toolName": "web_search"}))
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1
    assert "12 research calls" in steers(calls)[0]


def test_successful_cad_mutation_rearms_with_two_nudges_max():
    frames = [item for i in range(12) for item in tool("web_search", i)]
    frames += tool("new_part", 50)
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    frames += tool("add_features", 51)
    frames += [item for i in range(24, 36) for item in tool("web_search", i)]
    result, calls, emitted = run(frames, FakeClock())
    assert result["ok"]
    assert len(steers(calls)) == len(nudges(emitted)) == 2


def test_chudcode_write_to_cad_tool_resets_watchdog_but_reading_guide_does_not():
    # Online CAD tools are dispatched through Chudcode's `write xd://...`
    # builtin, not always via a frame named after the host tool.
    frames = [item for i in range(11) for item in tool("web_search", i)]
    frames += tool("write", 49, args={"path": "xd://cad_guide"})
    frames += tool("web_search", 48)
    frames += tool("write", 50, args={"path": "xd://new_part"})
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 2


def test_chudcode_parts_search_counts_and_refused_cad_mutation_does_not_rearm():
    frames = [item for i in range(11) for item in tool("web_search", i)]
    frames += tool("write", 49, args={"path": "xd://part_search"})
    frames += tool("write", 50, args={"path": "xd://new_part"})
    # Chudcode's host-tool result envelope reports policy refusals in details.
    frames[-1][1]["result"] = {"details": {"ok": False, "reason": "blocked_by_mode"}}
    frames += [item for i in range(11, 23) for item in tool("web_search", i)]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1


def test_failed_mutation_does_not_rearm():
    frames = [item for i in range(12) for item in tool("web_search", i)]
    frames += tool("new_part", 50, is_error=True)
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1


def test_visible_text_resets_stall_timer_but_thinking_does_not(monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROGRESS_RESEARCH_CALLS", "0")
    monkeypatch.setenv("CHUDWARE_PROGRESS_STALL_S", "480")
    frames = tool("read", 1, args={"path": "https://example.com/servo.pdf"})
    frames += [(7 * 60, {"type": "message_update", "assistantMessageEvent": {
                   "type": "text_delta", "delta": "Measured mount span."}}),
               (7 * 60, {"type": "message_update", "assistantMessageEvent": {
                   "type": "thinking_delta", "delta": "Still researching."}}),
               (60, {"type": "message_update", "assistantMessageEvent": {
                   "type": "thinking_delta", "delta": "Considering dimensions."}})]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1
    assert "8.0 min" in steers(calls)[0]

def test_live_six_axis_timeline_schema_reads_do_not_count_as_mutations(monkeypatch):
    # Replays the recorded 2026-09-27 session's custom starts, terminal tool
    # results and long thinking-only gaps. Keep its original 8-minute limit
    # here: reads of xd://asm_add_component and xd://asm_mate at 02:12:54
    # previously reset it before the expected 02:13:44 nudge.
    monkeypatch.setenv("CHUDWARE_PROGRESS_RESEARCH_CALLS", "0")
    monkeypatch.setenv("CHUDWARE_PROGRESS_STALL_S", "480")
    frames = []
    previous = datetime.fromisoformat("2026-09-27T02:05:44.699+00:00")

    def at(timestamp, frame):
        nonlocal previous
        when = datetime.fromisoformat(f"2026-09-27T{timestamp}+00:00")
        frames.append(((when - previous).total_seconds(), frame))
        previous = when

    def start(timestamp, call_id, name, path=None):
        at(timestamp, {"type": "custom", "customType": "tool_execution_start",
                       "data": {"toolCallId": call_id, "toolName": name,
                                "args": {"path": path} if path else {}}})

    def done(timestamp, call_id, name, *, result=None):
        at(timestamp, {"type": "tool_execution_end", "toolCallId": call_id,
                       "toolName": name, "result": result, "isError": False})

    start("02:05:44.699", "servo", "write", "xd://make_standard_part")
    done("02:05:44.761", "servo", "write", result={"details": {"ok": True, "doc_id": "servo"}})
    start("02:06:17.304", "horn", "web_search")
    done("02:06:22.306", "horn", "web_search")
    at("02:12:54.628", {"type": "message_update", "assistantMessageEvent": {
        "type": "thinking_delta", "delta": "A horn-agnostic interface seems best."}})
    start("02:12:54.628", "schema-component", "read", "xd://asm_add_component")
    start("02:12:54.629", "schema-mate", "read", "xd://asm_mate")
    done("02:12:54.630", "schema-component", "read")
    done("02:12:54.767", "schema-mate", "read")
    at("02:13:44.762", {"type": "message_update", "assistantMessageEvent": {
        "type": "thinking_delta", "delta": "Still planning the whole assembly."}})
    result, calls, emitted = run(frames, FakeClock())
    assert result["ok"]
    assert len(steers(calls)) == len(nudges(emitted)) == 1
    assert "8.0 min" in steers(calls)[0]
    assert "Build and save one part at a time" in steers(calls)[0]


def test_default_nudges_six_minutes_after_mutation(monkeypatch):
    monkeypatch.delenv("CHUDWARE_PROGRESS_STALL_S", raising=False)
    monkeypatch.setenv("CHUDWARE_PROGRESS_RESEARCH_CALLS", "0")
    frames = tool("write", 1, args={"path": "xd://make_standard_part"})
    frames += [(33, {"type": "tool_execution_start", "toolCallId": "search",
                    "toolName": "web_search"}),
               (6 * 60 - 33, {"type": "message_update", "assistantMessageEvent": {
                   "type": "thinking_delta", "delta": "Thinking about the entire mechanism."}})]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1
    assert "6.0 min" in steers(calls)[0]


def test_both_limits_disabled(monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROGRESS_RESEARCH_CALLS", "0")
    monkeypatch.setenv("CHUDWARE_PROGRESS_STALL_S", "0")
    frames = [item for i in range(15) for item in tool("web_search", i, seconds=60)]
    _, calls, emitted = run(frames, FakeClock())
    assert steers(calls) == nudges(emitted) == []


@pytest.mark.parametrize("mode_opts", [{"mode": "ask"}, {"mode": "plan"},
                                       {"plan_only": True},
                                       {"turn_preferences": SimpleNamespace(mode="ask")}])
def test_user_read_only_turns_are_never_nudged(mode_opts):
    frames = [item for i in range(15) for item in tool("web_search", i, seconds=60)]
    _, calls, emitted = run(frames, FakeClock(), **mode_opts)
    assert steers(calls) == nudges(emitted) == []
