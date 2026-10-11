from __future__ import annotations

import time
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


def test_cad_failure_inside_the_xd_device_result_does_not_rearm():
    # `write xd://add_features` nests the CAD result under details.xdev.inner;
    # a rolled-back batch there used to count as modeling progress.
    frames = [item for i in range(12) for item in tool("web_search", i)]
    frames += tool("write", 50, args={"path": "xd://add_features"})
    frames[-1][1]["result"] = {"details": {"xdev": {"tool": "add_features", "inner": {
        "ok": False, "rolled_back": True, "error": "batch rejected"}}}}
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1


def test_failed_mutation_does_not_rearm():
    frames = [item for i in range(12) for item in tool("web_search", i)]
    frames += tool("new_part", 50, is_error=True)
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    _, calls, emitted = run(frames, FakeClock())
    assert len(steers(calls)) == len(nudges(emitted)) == 1


def subagent(frames, agent="CubeBuilder"):
    """Chudcode forwards a task sub-agent's own events wrapped in subagent_event."""
    return [(seconds, {"type": "subagent_event", "payload": {"id": agent, "event": frame}})
            for seconds, frame in frames]


def test_subagent_cad_mutation_is_turn_progress_but_its_research_is_not():
    parent_research = [item for i in range(11) for item in tool("web_search", i)]
    later_research = [item for i in range(11, 22) for item in tool("web_search", i)]
    child_research = subagent([item for i in range(100, 120) for item in tool("web_search", i)])

    built = subagent(tool("write", 50, args={"path": "xd://new_part"}))
    _, calls, emitted = run(parent_research + child_research + built + later_research,
                            FakeClock())
    assert steers(calls) == [] and nudges(emitted) == []

    failed = subagent(tool("write", 51, args={"path": "xd://new_part"}, is_error=True))
    _, calls, emitted = run(parent_research + failed + later_research, FakeClock())
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


def ping(seconds, at):
    return (seconds, {"type": "ping", "at": at})


def budget_nudges(emitted):
    return [frame for frame in nudges(emitted) if frame.get("reason") == "turn_budget"]


def emitted_before(emitted, frame, at):
    """True when ``frame`` reached the feed before the ping stamped ``at``."""
    pings = [index for index, item in enumerate(emitted)
             if item.get("type") == "ping" and item.get("at") == at]
    return emitted.index(frame) < pings[0]


def test_budgeted_turn_steers_once_three_minutes_before_expiry():
    frames = [ping(419, 419), ping(1, 420), ping(80, 500), ping(90, 590)]
    result, calls, emitted = run(frames, FakeClock(), timeout=600)
    assert result["ok"]
    assert len(steers(calls)) == len(budget_nudges(emitted)) == 1
    nudge = budget_nudges(emitted)[0]
    # Due at 420 s (budget − 180 s): sent with the 420 s frame, not the 419 s one.
    assert not emitted_before(emitted, nudge, 419) and emitted_before(emitted, nudge, 420)
    assert steers(calls)[0] == (
        "⏱ 3 min left of this turn's budget: save every requested deliverable now "
        "(exact requested paths), then use the remaining time to verify."
    )
    kind, status = map_rpc_frame(nudge)[0]
    assert (kind, status["phase"], status["reason"]) == ("status", "progress_nudge", "turn_budget")
    assert status["text"] == "Turn budget: 3 min left; save requested deliverables now, then verify."
    assert (status["budget_s"], status["remaining_s"]) == (600.0, 180.0)


def test_short_budget_steers_at_eighty_percent():
    frames = [ping(47, 47), ping(1, 48), ping(10, 58)]
    _, calls, emitted = run(frames, FakeClock(), timeout=60)
    assert len(steers(calls)) == len(budget_nudges(emitted)) == 1
    nudge = budget_nudges(emitted)[0]
    assert not emitted_before(emitted, nudge, 47) and emitted_before(emitted, nudge, 48)
    assert steers(calls)[0].startswith("⏱ 12 s left of this turn's budget")


@pytest.mark.parametrize("budget", [{}, {"timeout": None}, {"timeout": 0}, {"timeout": 30}])
def test_unbudgeted_turns_get_no_budget_steer(budget):
    frames = [ping(600, 600), ping(9000, 9600)]
    _, calls, emitted = run(frames, FakeClock(), **budget)
    assert steers(calls) == budget_nudges(emitted) == []


def test_turn_ending_early_never_sends_the_budget_steer():
    frames = [ping(100, 100)]  # agent_end follows at 100 s
    _, calls, emitted = run(frames, FakeClock(), timeout=600)
    assert steers(calls) == budget_nudges(emitted) == []


@pytest.mark.parametrize("mode_opts", [{"mode": "ask"}, {"mode": "plan"}, {"plan_only": True}])
def test_read_only_turns_get_the_budget_steer_without_save(mode_opts):
    frames = [ping(420, 420)]
    _, calls, emitted = run(frames, FakeClock(), timeout=600, **mode_opts)
    assert steers(calls) == [
        "⏱ 3 min left of this turn's budget: give your final answer now, "
        "then use the remaining time to check it."
    ]
    assert "save" not in steers(calls)[0]
    assert "save" not in map_rpc_frame(budget_nudges(emitted)[0])[0][1]["text"]


def test_budget_steer_does_not_spend_the_research_nudge_cap():
    frames = [item for i in range(12) for item in tool("web_search", i)]
    frames += tool("new_part", 50)
    frames += [item for i in range(12, 24) for item in tool("web_search", i)]
    frames += [ping(500, 500)]
    _, calls, emitted = run(frames, FakeClock(), timeout=600)
    assert len(steers(calls)) == 3
    assert len(nudges(emitted)) - len(budget_nudges(emitted)) == 2
    assert len(budget_nudges(emitted)) == 1


def recording_timers():
    timers = []

    class Timer:
        def __init__(self, interval, function):
            self.interval, self.function = interval, function
            self.daemon = False
            self.started = self.cancelled = False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    return timers, Timer


def realtime_host(factory, clock):
    """A host on the real timer path, with a controlled clock and recorded timers."""
    timers, timer = recording_timers()
    host = ChudcodeSessionHost(client_factory=factory, clock=clock)
    host._realtime_watchdog = True
    host._timer_factory = timer
    return host, timers


def test_realtime_budget_timer_is_scheduled_once_and_cancelled_by_the_terminal():
    clock, factory = FakeClock(), FakeFactory()
    host, timers = realtime_host(factory, clock)
    result = host.run_turn({"request": "Do the work", "session_id": "budget",
                            "run_id": "budget-run", "timeout": 600}, lambda _frame: None)
    assert result["ok"]
    assert len(timers) == 1
    assert timers[0].started and timers[0].cancelled
    assert timers[0].interval == 420
    clock.now = 420
    timers[0].function()  # a timer racing past the end of the turn sends nothing
    assert steers(factory.clients[0].calls) == []


def test_realtime_budget_timer_steers_through_the_control_lane():
    clock = FakeClock()

    class FiresMidTurn(FakeClient):
        def wait_for_idle(self, timeout=None):
            clock.now = timers[0].interval
            timers[0].function()
            for _ in range(200):  # the steer runs on its own thread, as in production
                if steers(self.calls):
                    break
                time.sleep(0.01)
            return super().wait_for_idle(timeout)

    class Factory(FakeFactory):
        def __call__(self, **kwargs):
            client = FiresMidTurn(self, **kwargs)
            self.clients.append(client)
            return client

    factory = Factory()
    host, timers = realtime_host(factory, clock)
    emitted = []
    result = host.run_turn({"request": "Do the work", "session_id": "budget",
                            "run_id": "budget-run", "timeout": 600}, emitted.append)
    assert result["ok"]
    assert len(steers(factory.clients[0].calls)) == len(budget_nudges(emitted)) == 1
    assert steers(factory.clients[0].calls)[0].startswith("⏱ 3 min left of this turn's budget")
