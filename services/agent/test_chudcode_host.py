from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
import time
import textwrap
from pathlib import Path
import hashlib

import pytest
from services.agent import chudcode_host as chudcode_host_module

from services.agent.chudcode_host import (
    ChudcodeSessionHost,
    _WIRE_NAME_OVERRIDES,
    _camel,
    _set_default_chudcode_host_for_tests,
    get_default_chudcode_host,
)


class FakeClient:
    def __init__(self, factory, **kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.started = False
        self.stop_count = 0
        self.listeners = []
        self.ui_listeners = []
        self._custom_tools = ()
        self.calls = []
        self.model_prompt_count = 0
        self.doctor_summaries = []
        self.prompted = threading.Event()
        self.release = threading.Event()
        self.block = factory.block
        self.events = [
            {"type": "agent_start"},
            {"type": "agent_end", "messages": [], "isTerminal": True},
        ]
        self._protocol_version = factory.protocol_version
        self.session_id = f"cc-{len(factory.clients) + 1}"
        self.session_file = f"/sessions/{self.session_id}.jsonl"
        self.stt_available = True
        self.stt_state = "idle"
        self.stt_progress = None
        self.idle_timeouts: list[float | None] = []

    def start(self):
        self.started = True
        self.calls.append(("start", {}))
        return self

    def stop(self):
        self.stop_count += 1
        self.started = False
        self.release.set()

    def on_notification(self, listener):
        self.listeners.append(listener)

        def unsubscribe():
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def on_ui_request(self, listener):
        self.ui_listeners.append(listener)

        def unsubscribe():
            if listener in self.ui_listeners:
                self.ui_listeners.remove(listener)

        return unsubscribe

    def prompt(self, message, *, images=None):
        self.model_prompt_count += 1
        self.calls.append(("prompt", {"message": message, "images": images}))
        self.prompted.set()
        if not self.block:
            self.release.set()

    def wait_for_idle(self, timeout=None):
        # Record what the host asked for, but never actually sleep on it: the production
        # budget is hours, and this double only needs the release latch.
        self.idle_timeouts.append(timeout)
        if not self.release.wait(2.0):
            raise TimeoutError("fake terminal wait timed out")
        for event in self.events:
            for listener in tuple(self.listeners):
                listener(event)
        return tuple(self.events)

    def abort(self):
        self.calls.append(("abort", {}))
        self.events = [
            {"type": "agent_end", "messages": [], "isTerminal": True, "aborted": True}
        ]
        self.release.set()

    def steer(self, message, *, images=None):
        self.calls.append(("steer", {"message": message, "images": images}))

    def switch_session(self, session_path):
        self.calls.append(("switch_session", {"sessionPath": session_path}))
        self.session_file = session_path

    def set_model(self, provider, model_id):
        self.calls.append(("set_model", {"provider": provider, "modelId": model_id}))

    def set_thinking_level(self, level):
        self.calls.append(("set_thinking_level", {"level": level}))

    def set_steering_mode(self, mode):
        self.calls.append(("set_steering_mode", {"mode": mode}))

    def set_follow_up_mode(self, mode):
        self.calls.append(("set_follow_up_mode", {"mode": mode}))

    def set_interrupt_mode(self, mode):
        self.calls.append(("set_interrupt_mode", {"mode": mode}))

    def set_auto_compaction(self, enabled):
        self.calls.append(("set_auto_compaction", {"enabled": enabled}))

    def set_auto_retry(self, enabled):
        self.calls.append(("set_auto_retry", {"enabled": enabled}))

    def set_custom_tools(self, tools, *, loadable_tools=None):
        self._custom_tools = tuple(tools)
        self.loadable_tools = loadable_tools
        self.calls.append(("set_host_tools", {"tools": tools}))

    def set_host_uris(self, uris):
        self.calls.append(("set_host_uris", {"uris": uris}))

    def request_raw(self, command, **payload):
        self.calls.append((command, payload))
        if command == "prompt" and str(payload.get("message", "")).startswith(
            "/session doctor"
        ):
            repair = "--repair" in payload["message"]
            summary = (
                self.doctor_summaries.pop(0)
                if self.doctor_summaries
                else {
                    "version": 1,
                    "mode": "repair" if repair else "dry-run",
                    "status": "healthy",
                    "sessionFile": self.session_file,
                    "outputPath": self.session_file,
                    "backupPath": None,
                    "changed": False,
                    "findingCount": 0,
                    "repairableFindingCount": 0,
                    "remainingFindingCount": 0,
                    "counts": {},
                    "findings": [],
                }
            )
            for listener in tuple(self.listeners):
                listener(
                    {
                        "type": "command_output",
                        "text": json.dumps(summary),
                    }
                )
                listener(
                    {
                        "type": "prompt_result",
                        "id": "doctor",
                        "agentInvoked": False,
                    }
                )
            return {"agentInvoked": False}
        return {"command": command, **payload}

    def get_state(self):
        return {
            "sessionId": self.session_id,
            "sessionFile": self.session_file,
            "dumpTools": [
                {"name": "read", "description": "Read", "parameters": {}},
                {"name": "eval", "description": "Evaluate", "parameters": {}},
            ],
        }

    def get_last_assistant_text(self):
        return "done"

    def export_html(self, output_path=None):
        path = Path(output_path)
        path.write_text("<html><body>transcript</body></html>", encoding="utf-8")
        self.calls.append(("export_html", {"outputPath": str(path)}))
        return path

    def branch(self, entry_id):
        self.calls.append(("branch", {"entryId": entry_id}))
        self.session_id = f"{self.session_id}-child"
        root = Path(self.kwargs.get("cwd") or self.kwargs.get("session_dir") or "/tmp")
        child = root / "sessions" / f"{self.session_id}.jsonl"
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("{}\n", encoding="utf-8")
        self.session_file = str(child)
        return {"text": "branched", "cancelled": False}

    def get_messages(self):
        self.calls.append(("get_messages", {}))
        return ({"role": "user", "content": "Do the work", "timestamp": 1},)

    def get_login_providers(self):
        self.calls.append(("get_login_providers", {}))
        return {
            "providers": [
                {
                    "id": "anthropic",
                    "name": "Anthropic",
                    "available": True,
                    "authenticated": False,
                }
            ]
        }

    def get_available_models(self):
        self.calls.append(("get_available_models", {}))
        return (
            {
                "id": "claude",
                "name": "Claude",
                "provider": "anthropic",
                "contextWindow": 200_000,
                "maxTokens": 8_192,
                "reasoning": True,
                "input": ["text"],
                "cost": {},
            },
        )

    def login(self, provider_id):
        self.calls.append(("login", {"providerId": provider_id}))
        if self.block:
            self.prompted.set()
            self.release.wait(2.0)
            if self.stop_count:
                raise RuntimeError("login cancelled")
        requests = (
            {
                "type": "extension_ui_request",
                "id": "open",
                "method": "open_url",
                "url": "https://login.example/authorize",
                "launchUrl": "http://127.0.0.1:9876/launch",
                "instructions": "Continue in the browser",
            },
            {
                "type": "extension_ui_request",
                "id": "unsafe",
                "method": "open_url",
                "url": "file:///tmp/credential",
            },
            {
                "type": "extension_ui_request",
                "id": "input",
                "method": "input",
                "title": "Paste code",
            },
        )
        for request in requests:
            for listener in tuple(self.ui_listeners):
                listener(request)
        return {"providerId": provider_id}

    def cancel_ui_request(self, request_id):
        self.calls.append(("cancel_ui_request", {"id": request_id}))

    def get_session_stats(self):
        self.calls.append(("get_session_stats", {}))
        return {
            "sessionId": self.session_id,
            "tokens": {"input": 2, "output": 1, "total": 3},
            "cost": 0.01,
        }

    def compact(self, custom_instructions=None):
        self.calls.append(("compact", {"customInstructions": custom_instructions}))
        return {
            "summary": "trimmed",
            "firstKeptEntryId": "entry-2",
            "tokensBefore": 100,
        }

    def handoff(self, custom_instructions=None):
        self.calls.append(("handoff", {"customInstructions": custom_instructions}))
        return {"document": f"handoff: {custom_instructions or 'continue'}"}

    def stt_status(self):
        self.calls.append(("stt_status", {}))
        result = {
            "available": self.stt_available,
            "state": self.stt_state,
            "model": "whisper-small",
        }
        if self.stt_progress is not None:
            result["progress"] = self.stt_progress
        if not self.stt_available:
            result["error"] = "microphone unavailable"
        return result

    def stt_start(self):
        self.calls.append(("stt_start", {}))
        self.stt_state = "recording"
        self.stt_progress = 100
        return {"state": self.stt_state, "model": "whisper-small"}

    def stt_stop(self):
        self.calls.append(("stt_stop", {}))
        self.stt_state = "idle"
        return {"state": self.stt_state, "text": "make the bore wider"}

    def stt_cancel(self):
        self.calls.append(("stt_cancel", {}))
        self.stt_state = "idle"
        return {"state": self.stt_state}


class FakeFactory:
    def __init__(self, *, block=False, protocol_version=2):
        self.clients = []
        self.block = block
        self.protocol_version = protocol_version

    def __call__(self, **kwargs):
        client = FakeClient(self, **kwargs)
        self.clients.append(client)
        return client


def run(host, session_id, run_id=None, **extra):
    params = {
        "request": "Do the work",
        "session_id": session_id,
        "run_id": run_id or f"run-{session_id}",
        **extra,
    }
    emitted = []
    result = host.run_turn(params, emitted.append)
    return result, emitted


def call_names(client):
    return [name for name, _payload in client.calls]


def test_byok_process_isolated_and_live_session_rejects_credential_switch(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-secret")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    scoped = {
        "byok_provider": "openai",
        "byok_tenant": "alice@example.com",
        "byok_key": "sk-alice-only",
        "chudcode_session_dir": str(tmp_path),
    }
    first, _ = run(host, "shared-id", "alice-1", **scoped)
    assert first["ok"] is True
    client = factory.clients[0]
    child_env = client.kwargs["env"]
    assert child_env["OPENAI_API_KEY"] == "sk-alice-only"
    assert child_env["ANTHROPIC_API_KEY"] == ""
    assert client.kwargs["session_dir"].startswith(str(tmp_path / "byok-"))
    assert Path(client.kwargs["session_dir"]).stat().st_mode & 0o777 == 0o700
    home = Path(child_env["HOME"])
    assert home.is_dir()
    assert child_env["PI_CODING_AGENT_DIR"].startswith(str(home))

    repeat, _ = run(host, "shared-id", "alice-2", **scoped)
    other, _ = run(host, "shared-id", "bob-1", **{**scoped, "byok_tenant": "bob@example.com"})
    changed, _ = run(host, "shared-id", "new-key", **{**scoped, "byok_key": "sk-new-key"})
    keyless, _ = run(host, "shared-id", "keyless")
    assert repeat["ok"] is True
    assert all(not row["ok"] for row in (other, changed, keyless))
    assert call_names(client).count("prompt") == 2
    assert len(factory.clients) == 1
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("{}", encoding="utf-8")
    mismatched, _ = run(
        host, "foreign", "foreign-1", **scoped,
        chudcode_session_file=str(foreign),
    )
    assert mismatched["ok"] is False
    assert len(factory.clients) == 1

    host.close_session("shared-id")
    assert client.stop_count == 1
    assert not home.exists()
    next_turn, _ = run(host, "shared-id", "bob-2", **{**scoped, "byok_tenant": "bob@example.com"})
    assert next_turn["ok"] is True
    assert len(factory.clients) == 2
    assert factory.clients[1].kwargs["session_dir"] != client.kwargs["session_dir"]
    host.shutdown()


def test_reuses_one_live_client_per_session_and_isolates_other_sessions():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    first, _ = run(host, "a", "a1")
    second, _ = run(host, "a", "a2")
    isolated, _ = run(host, "b", "b1")

    assert first["ok"] and second["ok"] and isolated["ok"]
    assert len(factory.clients) == 2
    assert call_names(factory.clients[0]).count("prompt") == 2
    assert call_names(factory.clients[1]).count("prompt") == 1
    assert factory.clients[0].stop_count == 0


def test_release_session_stops_an_idle_live_client():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1")[0]["ok"]

        assert host.release_session("a") == {"ok": True, "released": True}
        assert factory.clients[0].stop_count == 1
        assert host.session_call("a", "get_state") == {
            "ok": False, "reason": "session_not_live"
        }
        assert host.release_session("a") == {"ok": True, "released": False}
        assert factory.clients[0].stop_count == 1
    finally:
        host.shutdown()


def test_release_session_reports_unknown_or_closed_sessions_as_unreleased():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        assert host.release_session("missing") == {"ok": True, "released": False}
        assert factory.clients == []
        assert run(host, "a", "a1")[0]["ok"]
        assert host.close_session("a")["ok"]
        assert host.release_session("a") == {"ok": True, "released": False}
        assert factory.clients[0].stop_count == 1
    finally:
        host.shutdown()


def test_release_session_does_not_stop_or_cancel_an_active_run():
    factory = FakeFactory(block=True)
    created = threading.Event()

    def make_client(**kwargs):
        client = factory(**kwargs)
        created.set()
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    output = {}
    thread = threading.Thread(
        target=lambda: output.setdefault("turn", run(host, "a", "a1")), daemon=True
    )
    thread.start()
    try:
        assert created.wait(1.0)
        client = factory.clients[0]
        assert client.prompted.wait(1.0)

        assert host.release_session("a") == {"ok": False, "reason": "session_busy"}
        assert client.stop_count == 0
        assert "abort" not in call_names(client)
        assert host.status(run_id="a1")["cancelled"] is False
        assert thread.is_alive()
        client.release.set()
        thread.join(1.0)
        assert not thread.is_alive()
        assert output["turn"][0]["ok"]
        assert host.release_session("a") == {"ok": True, "released": True}
        assert client.stop_count == 1
    finally:
        if factory.clients:
            factory.clients[0].release.set()
        thread.join(1.0)
        host.shutdown()


def test_turn_after_release_creates_a_new_client_and_resumes_the_file(tmp_path):
    persisted = tmp_path / "persisted.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1", chudcode_session_file=str(persisted))[0]["ok"]
        assert host.release_session("a") == {"ok": True, "released": True}
        assert factory.clients[0].stop_count == 1

        second, _ = run(host, "a", "a2", chudcode_session_file=str(persisted))

        assert second["ok"]
        assert len(factory.clients) == 2
        assert factory.clients[0].model_prompt_count == 1
        assert ("switch_session", {"sessionPath": str(persisted)}) in factory.clients[1].calls
        assert second["chudcode_session_file"] == str(persisted)
        assert factory.clients[1].stop_count == 0
    finally:
        host.shutdown()


def test_release_session_waits_for_client_stop_outside_the_host_lock():
    factory = FakeFactory()
    stopping = threading.Event()
    finish_stop = threading.Event()

    class BlockingStopClient(FakeClient):
        def stop(self):
            stopping.set()
            if not finish_stop.wait(2.0):
                raise TimeoutError("fake stop was not released")
            super().stop()

    def make_client(**kwargs):
        client = BlockingStopClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    output = {}
    release = threading.Thread(
        target=lambda: output.setdefault("release", host.release_session("a")),
        daemon=True,
    )
    status = threading.Thread(
        target=lambda: output.setdefault("status", host.status()), daemon=True
    )
    try:
        assert run(host, "a", "a1")[0]["ok"]
        release.start()
        assert stopping.wait(1.0)
        status.start()
        status.join(1.0)
        assert not status.is_alive()
        assert output["status"]["sessions"] == 0
        assert release.is_alive()
        assert "release" not in output
        finish_stop.set()
        release.join(1.0)
        assert not release.is_alive()
        assert output["release"] == {"ok": True, "released": True}
        assert factory.clients[0].stop_count == 1
    finally:
        finish_stop.set()
        if release.ident is not None:
            release.join(1.0)
        if status.ident is not None:
            status.join(1.0)
        host.shutdown()


def test_frames_sharing_an_object_identity_are_all_forwarded():
    # The real client hands each notification as a short-lived dataclass and
    # CPython recycles freed addresses, so consecutive distinct frames routinely
    # share id(). Identity is not a frame key: every one must reach the feed.
    calls = ["measure_part", "verify_battery", "render_part"]

    class RecyclingClient(FakeClient):
        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            slot = {}
            for name in calls:
                slot.clear()
                slot.update(
                    {
                        "type": "tool_execution_end",
                        "toolCallId": f"toolu_{name}",
                        "toolName": name,
                        "isError": False,
                    }
                )
                for listener in tuple(self.listeners):
                    listener(slot)
            slot.clear()
            slot.update({"type": "agent_end", "messages": [], "isTerminal": True})
            for listener in tuple(self.listeners):
                listener(slot)
            return ()

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(RecyclingClient(factory, **kw))
        or factory.clients[-1]
    )

    result, emitted = run(host, "s", "s1")

    assert result["ok"]
    ends = [f for f in emitted if f.get("type") == "tool_execution_end"]
    assert [f["toolName"] for f in ends] == calls
    assert [f["toolCallId"] for f in ends] == [f"toolu_{name}" for name in calls]


def test_failed_provider_retry_survives_long_lifecycle_tail():
    class LongStream(FakeClient):
        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            events = [
                {"type": "auto_retry_end", "success": False, "error": "quota"}
            ]
            events.extend({"type": "agent_start"} for _ in range(1_000))
            events.append({"type": "agent_end", "messages": [], "isTerminal": True})
            for event in events:
                for listener in tuple(self.listeners):
                    listener(event)
            return ()

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(LongStream(factory, **kw))
        or factory.clients[-1]
    )
    result = host.run_turn(
        {"request": "Do the work", "session_id": "long", "run_id": "r-long"},
        lambda _frame: None,
    )

    assert result["ok"] is False
    assert result["stop_reason"] == "provider_error"
    assert "quota" in result["reason"]
    assert result["terminal"]["isTerminal"] is True


@pytest.mark.parametrize("ending", ["completed", "aborted"])
@pytest.mark.parametrize("context_storage", ["dictionary", "immutable"])
@pytest.mark.parametrize("settles_before_end", [False, True])
def test_timed_tool_result_is_released_at_owning_turn_end(
    monkeypatch, ending, context_storage, settles_before_end,
):
    """A real blocked worker outlives the turn, not its result ownership."""
    import gc
    import weakref
    from dataclasses import replace
    from types import MappingProxyType
    from services.agent.chudcode_tools import (
        HostToolBinding, build_host_tools, dispatch_host_tool,
    )

    rpc = pytest.importorskip("chudcode_rpc")
    options = {
        "enabled": True, "timeout_s": 0.02, "max_calls_per_turn": 100,
        "param_defaults": {}, "prompt_note": "",
    }
    monkeypatch.setattr(
        "services.agent.config.get_tool_options", lambda *_args, **_kwargs: options,
    )
    entered = threading.Event()
    release = threading.Event()
    workers = []
    result_refs = []
    executions = []
    replies = []

    class ComputedResult(dict):
        pass

    def native_work():
        executions.append(1)
        number = len(executions)
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(5), "test did not release native worker"
        result = ComputedResult(ok=True, computed=number * number, execution=number)
        result_refs.append(weakref.ref(result))
        return result

    context = {
        "run_id": "owned-run", "session_id": "owned-session",
        "tool_call_counts": {}, "tool_calls": [],
    }
    if context_storage == "immutable":
        context.update(
            tool_entries={
                "timed_recovery_test": {
                    "fn": native_work, "description": "Compute a value.",
                    "input_schema": {"type": "object", "properties": {}},
                },
            },
            resolver=lambda name: native_work if name == "timed_recovery_test" else None,
            allowed_tools=["timed_recovery_test"],
        )
        context = MappingProxyType(context)
        binding = next(
            tool for tool in build_host_tools(context)
            if tool.name == "timed_recovery_test"
        )
        assert "tool_call_recovery" not in context
        assert binding.metadata["tool_call_recovery"].closed is False
    else:
        binding = HostToolBinding(
            name="timed_recovery_test", description="Compute a value.", input_schema={},
            handler=native_work, context=context,
        )

    class ToolTurn(FakeClient):
        def wait_for_idle(self, timeout=None):
            replies.append(dispatch_host_tool(binding, {}, None, None))
            if settles_before_end:
                release.set()
                workers[0].join(timeout=1)
                assert not workers[0].is_alive()
                assert result_refs[0]() is not None, "active run lost its recovery result"
            if ending == "aborted":
                host.cancel("owned-run")
            return super().wait_for_idle(timeout)

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kwargs:
        factory.clients.append(ToolTurn(factory, **kwargs)) or factory.clients[-1],
    )
    try:
        outcome, _events = run(
            host, "owned-session", "owned-run", host_tools=[binding],
        )
        assert outcome["ok"] is True
        assert outcome["cancelled"] is (ending == "aborted")
        assert entered.wait(1)
        assert replies[0]["status"] == "still_running"
        # Cleanup is neither cancellation nor a join. A pending native body
        # outlives the turn, but even the SDK's cached callback cannot launch work.
        assert workers[0].is_alive() is not settles_before_end
        [cached_tool] = [
            tool for tool in factory.clients[0]._custom_tools
            if tool.name == binding.name
        ]
        stale_context = rpc.HostToolContext(
            tool_call_id="stale", _cancel_event=threading.Event(),
            _send_update=lambda _result: None,
        )
        retired = cached_tool.execute({}, stale_context)["details"]
        assert retired["status"] == "run_ended"
        assert retired["executed"] is False
        assert len(executions) == 1
        release.set()
        workers[0].join(timeout=1)
        assert not workers[0].is_alive()
        gc.collect()
        assert len(result_refs) == 1
        assert result_refs[0]() is None, "the ended run retained an unobserved result"

        options["timeout_s"] = 1
        fresh = replace(
            binding,
            context={"run_id": "next-run", "session_id": "owned-session",
                     "tool_call_counts": {}, "tool_calls": []},
            metadata={},
        )
        recovered = dispatch_host_tool(fresh, {}, None, None)
        assert recovered["ok"] is True
        assert recovered["execution"] == 2
        assert recovered["computed"] == 4
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
        host.shutdown()


def test_cad_calls_made_inside_eval_get_their_own_feed_rows_once():
    """c230 (2026-09-28) built every feature through eval's `tool.write(xd://add_features)`:
    the feed showed 49 "eval" rows and not one add_features row. A host call under an id
    no visible tool row owns is reported as its own row, parented to the eval; a
    top-level `write xd://` call keeps exactly the one row Chudcode's frames produce."""
    rpc = pytest.importorskip("chudcode_rpc")
    from services.agent.chudcode_events import RpcFrameState, map_rpc_frame
    from services.agent.chudcode_tools import HostToolBinding

    binding = HostToolBinding(
        name="add_features",
        description="add features",
        input_schema={"type": "object"},
        handler=lambda doc_id, ops: {"ok": True, "doc_id": doc_id, "revision": len(ops)},
        context={"tool_call_counts": {}, "tool_calls": []},
    )

    def call(client, call_id):
        [tool] = [t for t in client._custom_tools if t.name == "add_features"]
        context = rpc.HostToolContext(
            tool_call_id=call_id, _cancel_event=threading.Event(), _send_update=lambda _: None
        )
        return tool.execute({"doc_id": "doc-5", "ops": [{"op": "fillet"}]}, context)

    def emit_all(client, events):
        for event in events:
            for listener in tuple(client.listeners):
                listener(event)

    class EvalBuilds(FakeClient):
        def wait_for_idle(self, timeout=None):
            emit_all(self, [{"type": "agent_start"},
                            {"type": "tool_execution_start", "toolCallId": "eval-1",
                             "toolName": "eval", "args": {"code": "tool.write(...)"}}])
            call(self, "js-write-1")
            emit_all(self, [{"type": "tool_execution_end", "toolCallId": "eval-1",
                             "toolName": "eval", "result": {"content": []}},
                            {"type": "tool_execution_start", "toolCallId": "w-1",
                             "toolName": "write",
                             "args": {"path": "xd://add_features", "content": "{}"}}])
            call(self, "w-1")
            emit_all(self, [{"type": "tool_execution_end", "toolCallId": "w-1",
                             "toolName": "add_features", "result": {"content": []}},
                            {"type": "agent_end", "messages": [], "isTerminal": True}])
            return ()

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(EvalBuilds(factory, **kw))
        or factory.clients[-1]
    )
    result, emitted = run(host, "evalbuild", host_tools=[binding])
    assert result["ok"] is True

    state = RpcFrameState()
    rows = [p for f in emitted for k, p in map_rpc_frame(f, {"run_id": "r"}, state)
            if k == "agent.tool_call"]
    by_id = {row["id"]: row for row in rows}
    assert sorted(row["id"] for row in rows) == ["eval-1", "js-write-1", "w-1"]
    nested = by_id["js-write-1"]
    assert (nested["tool_name"], nested["target"], nested["ok"]) == ("add_features", "doc-5", True)
    assert nested["parent_id"] == "eval-1"
    assert "host_call_observer" not in binding.context  # unbound when the turn ends


def _scripted_turn(events):
    class Scripted(FakeClient):
        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            for event in events:
                for listener in tuple(self.listeners):
                    listener(event)
            return ()

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(Scripted(factory, **kw))
        or factory.clients[-1]
    )
    return host.run_turn(
        {"request": "Do the work", "session_id": "scripted", "run_id": "r-scripted"},
        lambda _frame: None,
    )


_REJECTED = {
    "role": "assistant",
    "content": [],
    "stopReason": "error",
    "errorMessage": (
        '400 {"type":"error","error":{"type":"invalid_request_error",'
        '"message":"Claude Code 2.1.246 does not support this model"}}\n'
        "raw-http-request=/tmp/req.json"
    ),
}


def test_non_retryable_provider_rejection_fails_the_turn():
    # Live 2026-09-24: a 400 is not retried, so Chudcode records it only as an
    # assistant message with stopReason "error" and then emits a normal agent_end.
    # The GUI rendered "The agent finished without reporting what it did".
    result = _scripted_turn(
        [
            {"type": "message_end", "message": {"role": "user", "content": []}},
            {"type": "message_end", "message": _REJECTED},
            {"type": "agent_end", "messages": [], "isTerminal": True},
        ]
    )

    assert result["ok"] is False
    assert result["stop_reason"] == "provider_error"
    assert result["reason"] == (
        "provider error: 400 Claude Code 2.1.246 does not support this model"
    )


def test_provider_rejection_followed_by_assistant_output_is_recovered():
    result = _scripted_turn(
        [
            {"type": "message_end", "message": _REJECTED},
            {
                "type": "message_end",
                "message": {"role": "assistant", "content": [], "stopReason": "stop"},
            },
            {"type": "agent_end", "messages": [], "isTerminal": True},
        ]
    )

    assert result["ok"] is True
    assert "stop_reason" not in result


def test_meter_is_seeded_after_run_started_and_closed_with_chudcode_state():
    """2026-09-24: the meter read "~22k/1.0M" on a session holding ~296k tokens.

    No Chudcode frame carried the contract's `used`, so the GUI only ever showed its
    own transcript estimate. The host now reports Chudcode's contextUsage (the figure
    its footer shows) after the GUI's run_started reset and again at agent_end.
    """
    from services.agent.chudcode_events import map_rpc_frame

    fills = iter([(296_000, 1_000_000), (301_500, 1_000_000)])

    class Measured(FakeClient):
        def get_state(self):
            tokens, window = next(fills)
            return {**super().get_state(),
                    "contextUsage": {"tokens": tokens, "contextWindow": window}}

        def wait_for_idle(self, timeout=None):
            for event in (
                {"type": "agent_start"},
                {"type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop", "model": "m",
                    "usage": {"input": 2, "output": 8, "cacheRead": 287_415,
                              "cacheWrite": 9_159, "totalTokens": 296_584}}},
                {"type": "agent_end", "messages": [], "isTerminal": True},
            ):
                for listener in tuple(self.listeners):
                    listener(event)
            return ()

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(Measured(factory, **kw))
        or factory.clients[-1]
    )
    feed = []
    result = host.run_turn(
        {"request": "Do the work", "session_id": "meter", "run_id": "r-meter"},
        lambda frame: feed.extend(map_rpc_frame(frame)),
    )

    assert result["ok"] is True
    kinds = [kind for kind, _ in feed]
    meter = [(p.get("used"), p.get("window")) for kind, p in feed if kind == "agent.context"]
    assert kinds.index("run_started") < kinds.index("agent.context")
    assert meter == [(296_000, 1_000_000), (296_584, None), (301_500, 1_000_000)]


def test_resume_key_reaches_the_feed_when_the_turn_starts():
    """2026-09-27 c217: hostd died mid-turn, the turn never returned, and the GUI only
    learned a conversation's Chudcode session file from the finished turn — two hours of
    agent history were left with no resume key. The key now rides right after agent_start.
    """
    from services.agent.chudcode_events import map_rpc_frame

    class StartsThenDies(FakeClient):
        def wait_for_idle(self, timeout=None):
            for listener in tuple(self.listeners):
                listener({"type": "agent_start"})
            raise RuntimeError("daemon connection closed")

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(StartsThenDies(factory, **kw))
        or factory.clients[-1]
    )
    feed = []
    host.run_turn(
        {"request": "Do the work", "session_id": "resume", "run_id": "r-resume"},
        lambda frame: feed.extend(map_rpc_frame(frame)),
    )

    bound = [payload for kind, payload in feed if kind == "agent.session"]
    assert [p["chudcode_session_file"] for p in bound] == [factory.clients[0].session_file]
    assert bound[0]["chudcode_session_id"] == factory.clients[0].session_id


def test_rejects_a_runtime_that_did_not_negotiate_protocol_v2():
    factory = FakeFactory(protocol_version=1)
    host = ChudcodeSessionHost(client_factory=factory)

    result, _ = run(host, "old", "old-run")

    assert result["ok"] is False
    assert "protocol v2 is required" in result["reason"]
    assert factory.clients[0].stop_count == 1


def test_lru_disposes_only_the_least_recent_idle_session(monkeypatch):
    monkeypatch.setenv("CHUDWARE_CHUDCODE_MAX_SESSIONS", "2")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    run(host, "a", "a1")
    run(host, "b", "b1")
    run(host, "a", "a2")  # refresh a, leaving b as the eviction victim
    run(host, "c", "c1")

    assert len(factory.clients) == 3
    assert factory.clients[0].stop_count == 0
    assert factory.clients[1].stop_count == 1
    assert factory.clients[2].stop_count == 0


def test_spawning_another_session_does_not_block_control_or_duplicate_creators():
    start_entered = threading.Event()
    finish_start = threading.Event()

    class SlowStart(FakeClient):
        def start(self):
            start_entered.set()
            assert finish_start.wait(2)
            return super().start()

    factory = FakeFactory(block=True)

    def make_client(**kwargs):
        client_type = FakeClient if not factory.clients else SlowStart
        client = client_type(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    first = threading.Thread(target=lambda: run(host, "first", "r-first"), daemon=True)
    first.start()
    while not factory.clients:
        time.sleep(0.001)
    assert factory.clients[0].prompted.wait(1)

    second = threading.Thread(target=lambda: run(host, "second", "r-second"), daemon=True)
    also_second = threading.Thread(
        target=lambda: run(host, "second", "r-second-2"), daemon=True
    )
    second.start()
    assert start_entered.wait(1)
    also_second.start()
    control_done = threading.Event()

    def control_first():
        assert host.status(run_id="r-first")["active"]
        assert host.steer("r-first", "continue")["ok"]
        assert host.cancel("r-first")["ok"]
        control_done.set()

    control = threading.Thread(target=control_first, daemon=True)
    control.start()
    try:
        assert control_done.wait(0.5)
        assert len(factory.clients) == 2
    finally:
        finish_start.set()
    factory.clients[1].release.set()
    for thread in (first, second, also_second, control):
        thread.join(1)
        assert not thread.is_alive()
    assert len(factory.clients) == 2


def test_resumes_explicit_persisted_chudcode_session_file(tmp_path):
    persisted = tmp_path / "persisted.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    result, _ = run(
        host,
        "resume-me",
        chudcode_session_file=str(persisted),
    )

    assert result["ok"] is True
    assert result["chudcode_session_file"] == str(persisted)
    assert ("switch_session", {"sessionPath": str(persisted)}) in factory.clients[
        0
    ].calls
    assert factory.clients[0].kwargs["session_dir"] == str(tmp_path)


def test_failed_session_switch_stops_spawned_client_before_retry(tmp_path):
    persisted = tmp_path / "persisted.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")

    class FailingSwitch(FakeClient):
        def switch_session(self, session_path):
            self.calls.append(("switch_session", {"sessionPath": session_path}))
            raise LookupError("transient session switch failure")

    factory = FakeFactory()
    def make_client(**kwargs):
        client_type = FailingSwitch if not factory.clients else FakeClient
        client = client_type(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    failed, _ = run(host, "resume", "r1", chudcode_session_file=str(persisted))
    retried, _ = run(host, "resume", "r2", chudcode_session_file=str(persisted))

    assert failed["ok"] is False
    assert "transient session switch failure" in failed["reason"]
    assert factory.clients[0].stop_count == 1
    assert retried["ok"] is True
    assert factory.clients[1].stop_count == 0


def test_waits_past_nonterminal_agent_end_until_terminal_frame():
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    output = {}

    thread = threading.Thread(
        target=lambda: output.setdefault("value", run(host, "wait", "waiting")),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[0]
    assert client.prompted.wait(1.0)
    client.events = [
        {"type": "agent_end", "messages": [], "isTerminal": False},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ]
    assert thread.is_alive()
    client.release.set()
    thread.join(1.0)

    result, emitted = output["value"]
    assert result["ok"] is True
    assert result["terminal"]["isTerminal"] is True
    assert [frame["isTerminal"] for frame in emitted] == [False, True]


def test_cancel_and_steer_are_immediate_and_idempotent_during_terminal_wait():
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    output = {}
    thread = threading.Thread(
        target=lambda: output.setdefault("value", run(host, "race", "run-race")),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[0]
    assert client.prompted.wait(1.0)
    assert host.status(run_id="run-race") == {
        "ok": True,
        "run_id": "run-race",
        "active": True,
        "cancelled": False,
        "started": True,
        "session_id": "race",
    }
    assert host.status(session_id="race") == {
        "ok": True,
        "session_id": "race",
        "live_session": True,
        "active_runs": ["run-race"],
    }
    assert host.status() == {
        "ok": True,
        "sessions": 1,
        "active_runs": 1,
        "max_sessions": 8,
        "shutdown": False,
    }

    first_steer = host.steer("run-race", "Use the safer option")
    duplicate_steer = host.steer("run-race", "Use the safer option")
    first_cancel = host.cancel("run-race")
    duplicate_cancel = host.cancel("run-race")
    thread.join(1.0)

    assert first_steer["steered"] is True
    assert duplicate_steer["duplicate"] is True
    assert first_cancel["cancelled"] is True
    assert duplicate_cancel["cancelled"] is True
    assert call_names(client).count("steer") == 1
    assert call_names(client).count("abort") == 1
    assert output["value"][0]["cancelled"] is True
    assert host.status(run_id="run-race") == {
        "ok": True,
        "run_id": "run-race",
        "active": False,
        "cancelled": True,
        "started": True,
        "session_id": "race",
    }
    assert host.cancel("run-race")["active"] is False
    assert host.steer("run-race", "too late")["steered"] is False


def test_steer_carries_attached_images_to_chudcode(tmp_path):
    # A picture sent while a turn runs is a steer; the GUI used to have no way to send it.
    picture = tmp_path / "board.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    thread = threading.Thread(target=lambda: run(host, "pic", "run-pic"), daemon=True)
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[0]
    assert client.prompted.wait(1.0)

    result = host.steer(
        "run-pic",
        "use this board",
        [{"path": str(picture), "kind": "image", "mime": "image/png"}],
    )
    host.cancel("run-pic")
    thread.join(1.0)

    assert result["steered"] is True
    steer = next(payload for name, payload in client.calls if name == "steer")
    assert steer["message"] == "use this board"
    assert steer["images"] == [
        {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgpmYWtl"}
    ]



def test_failed_abort_and_steer_retry_transport_instead_of_claiming_success():
    class FlakyControl(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.fail_abort = True
            self.fail_steer = True

        def abort(self):
            if self.fail_abort:
                self.fail_abort = False
                self.calls.append(("abort", {}))
                raise LookupError("abort connection interrupted")
            super().abort()

        def steer(self, message, *, images=None):
            if self.fail_steer:
                self.fail_steer = False
                self.calls.append(("steer", {"message": message}))
                raise LookupError("steer connection interrupted")
            super().steer(message, images=images)

    factory = FakeFactory(block=True)
    def make_client(**kwargs):
        client = FlakyControl(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("turn", run(host, "flaky", "r-flaky")),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[0]
    assert client.prompted.wait(1)

    assert host.steer("r-flaky", "Try another approach")["ok"] is False
    assert host.steer("r-flaky", "Try another approach")["steered"] is True
    assert host.steer("r-flaky", "Try another approach")["duplicate"] is True
    assert host.cancel("r-flaky")["ok"] is False
    assert host.cancel("r-flaky")["ok"] is True
    assert host.cancel("r-flaky")["ok"] is True
    thread.join(1)

    assert not thread.is_alive()
    assert outcome["turn"][0]["cancelled"] is True
    assert call_names(client).count("steer") == 2
    assert call_names(client).count("abort") == 2


def test_forwards_events_attachments_callbacks_and_all_turn_settings(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(b"png-data")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    registered = []
    tool = object()
    uri = object()

    result, emitted = run(
        host,
        "configured",
        provider="openrouter",
        system_prompt="CAD governance prompt",
        model="anthropic/claude-sonnet-4.6",
        thinking="high",
        steering_mode="all",
        follow_up_mode="one-at-a-time",
        interrupt_mode="immediate",
        auto_compaction=True,
        auto_retry=False,
        subagent_subscription="events",
        host_tools=[tool],
        host_uris=[uri],
        register_callbacks=registered.append,
        attachments=[
            {"path": str(image), "kind": "image", "mime": "image/png"},
            {"path": "/project/spec.txt", "kind": "file", "name": "spec"},
        ],
    )

    client = factory.clients[0]
    prompt_call = next(payload for name, payload in client.calls if name == "prompt")
    assert result["ok"] is True
    assert emitted[-1]["type"] == "agent_end"
    assert prompt_call["images"][0]["mimeType"] == "image/png"
    assert "spec: /project/spec.txt" in prompt_call["message"]
    assert registered == [client]
    assert client.kwargs["append_system_prompt"] == "CAD governance prompt"
    assert "set_host_tools" in call_names(client)
    assert "set_host_uris" in call_names(client)


def test_composer_effort_overrides_stale_thinking_and_clamps_to_model_catalog():
    class CatalogClient(FakeClient):
        def get_available_models(self):
            return (
                {"provider": "moonshot", "id": "kimi-k3",
                 "thinking": {"efforts": ["low", "high", "max"]}},
            )

    class CatalogFactory(FakeFactory):
        def __call__(self, **kwargs):
            client = CatalogClient(self, **kwargs)
            self.clients.append(client)
            return client

    factory = CatalogFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    result, _ = run(host, "effort-precedence", provider="moonshot", model="kimi-k3",
                    settings={"thinking": "low", "reasoning_effort": "medium"})
    assert result["ok"]
    assert ("set_thinking_level", {"level": "high"}) in factory.clients[0].calls

    result, _ = run(host, "effort-precedence", "effort-max", provider="moonshot",
                    model="kimi-k3", settings={"thinking": "low"},
                    reasoning_effort="xhigh")
    assert result["ok"]
    assert ("set_thinking_level", {"level": "max"}) in factory.clients[0].calls


def test_wire_provider_aliases_map_onto_chudcode_ids():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    result, _ = run(host, "alias", provider="gemini", model="gemini-2.5-pro")

    assert result["ok"] is True
    set_model = next(p for n, p in factory.clients[0].calls if n == "set_model")
    assert set_model == {"provider": "google", "modelId": "gemini-2.5-pro"}


def test_set_model_refusal_names_the_signed_out_provider_or_the_catalog():
    class RejectingClient(FakeClient):
        def set_model(self, provider, model_id):
            super().set_model(provider, model_id)
            raise RuntimeError(f"set_model: Model not found: {provider}/{model_id}")

    class RejectingFactory(FakeFactory):
        def __call__(self, **kwargs):
            client = RejectingClient(self, **kwargs)
            self.clients.append(client)
            return client

    # The double's catalog holds one anthropic model ("claude"), nothing for openai.
    factory = RejectingFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    signed_out, _ = run(host, "signed-out", provider="openai", model="gpt-5")
    assert signed_out["ok"] is False
    assert "openai is not signed in" in signed_out["reason"]
    assert "Settings -> Providers" in signed_out["reason"]
    assert "anthropic" in signed_out["reason"]  # what IS signed in

    factory = RejectingFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    stale, _ = run(host, "stale", provider="anthropic", model="claude-sonnet-5")
    assert stale["ok"] is False
    assert "claude-sonnet-5 is not in the agent service's anthropic catalog" in stale["reason"]
    assert "claude" in stale["reason"]


def test_auth_and_memory_calls_use_only_canonical_command_shapes_on_one_shared_process():
    """Transient commands share ONE no-session process for the host's lifetime:
    spawning `chud --mode rpc` costs ~1.1 s and the GUI issues a dozen of these at
    startup, so spawn-per-call was the "providers take 30 s to load" defect."""
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    auth = host.auth_call(
        "set_api_key", {"providerId": "anthropic", "apiKey": "secret"}
    )
    memory = host.memory_call(
        "memory_recall",
        {"root": "/project", "query": "material", "tokenBudget": 500},
    )

    assert auth == {
        "command": "set_api_key",
        "providerId": "anthropic",
        "apiKey": "secret",
    }
    assert memory == {
        "command": "memory_recall",
        "root": "/project",
        "query": "material",
        "tokenBudget": 500,
    }
    assert len(factory.clients) == 1
    assert factory.clients[0].kwargs["no_session"] is True
    assert factory.clients[0].stop_count == 0
    host.shutdown()
    assert factory.clients[0].stop_count == 1
    with pytest.raises(ValueError, match="unknown params"):
        host.auth_call("get_auth_state", {"provider_id": "anthropic"})
    with pytest.raises(ValueError, match="unsupported"):
        host.memory_call("remember", {})


def test_cad_session_process_drops_chudcode_dev_prompt_but_keeps_cad_tools(tmp_path):
    """c219 baseline: every request re-read ~106 KB of Chudcode prompt + tool schema,
    including the user's own skills catalog, LSP docs and a dozen code devices. A CAD
    session process is started without them and still has everything a CAD turn uses;
    the shared service process is untouched."""
    RpcClient = pytest.importorskip("chudcode_rpc").RpcClient

    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    assert run(host, "cad", "cad-1")[0]["ok"]
    host.memory_call("memory_recall", {"root": "/project", "query": "m", "tokenBudget": 5})

    session_kwargs = factory.clients[0].kwargs
    argv = RpcClient(**{**session_kwargs, "executable": "chud"})._build_command()
    tools = argv[argv.index("--tools") + 1].split(",")
    for needed in ("read", "write", "edit", "bash", "eval", "task", "wait", "todo",
                   "web_search"):
        assert needed in tools
    for dropped in ("lsp", "security_scan", "debug", "firmware", "cpp_static", "github",
                    "hub", "browser", "computer"):
        assert dropped not in tools
    assert "--no-skills" in argv and "--no-lsp" in argv
    service = next(c for c in factory.clients if c.kwargs.get("no_session"))
    assert not {"tools", "no_skills"} & set(service.kwargs)
    host.shutdown()


@pytest.mark.parametrize(("help_text", "expected"), [
    ("  --no-lsp  Disable LSP\n  --no-mcp  Disable MCP discovery\n", True),
    ("  --no-lsp  Disable LSP\n  --no-mcp-cache  unrelated\n", False),
])
def test_no_mcp_reaches_only_a_chud_that_accepts_it(tmp_path, help_text, expected):
    """Pass --no-mcp only when the resolved binary advertises it; probe once
    per binary identity so incompatible developer binaries can still start."""
    fake = tmp_path / "chud"
    calls = tmp_path / "help-calls"
    fake.write_text(f"#!/bin/sh\necho x >> {calls}\ncat <<'EOF'\n{help_text}EOF\n")
    fake.chmod(0o755)
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory, binary=fake)
    assert run(host, "cad", "cad-1")[0]["ok"]
    assert run(host, "cad-2", "cad-2")[0]["ok"]

    for client in factory.clients:
        if not client.kwargs.get("no_session"):
            assert ("--no-mcp" in client.kwargs["extra_args"]) is expected
            assert "--no-lsp" in client.kwargs["extra_args"]
    assert calls.read_text().count("x") == 1
    host.shutdown()


_MNEMO_TOOLS = {"retain", "recall", "reflect"}


@pytest.mark.parametrize(("workspace", "backend", "knows_ss", "expected", "memory_tools"), [
    (True, "sharpshooter", True, "sharpshooter", set()),
    (True, "mnemo", True, "mnemopi", _MNEMO_TOOLS),
    (True, "off", True, "\"off\"", set()),
    (True, None, True, "sharpshooter", set()),  # no setting sent: the default backend
    (False, "mnemo", True, "\"off\"", set()),  # no project folder: no memory at all
    # A chud that predates sharpshooter exits at startup on its name.
    (True, "sharpshooter", False, "mnemopi", _MNEMO_TOOLS),
])
def test_memory_is_the_projects_and_its_tools_match_the_backend(
    tmp_path, monkeypatch, workspace, backend, knows_ss, expected, memory_tools
):
    """A CAD session's memory lives in its project folder's store, per the user's backend
    setting; with no project folder there is no store it could own. --tools must name
    exactly the chosen backend's memory tools: chud refuses to start on one it lacks."""
    from services import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path / "data")
    fake = tmp_path / "chud"
    settings = '"sharpshooter.model"' if knows_ss else '"mnemopi.bank"'
    fake.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = config ]; then echo '{{{settings}: null}}'; exit 0; fi\n"
        "echo '  --no-mcp x\n  --config=<value> y'\n"
    )
    fake.chmod(0o755)
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory, binary=fake)
    extra = {"workspace_dir": str(tmp_path)} if workspace else {}
    if backend is not None:
        extra["memory_backend"] = backend
    assert run(host, "cad", "cad-1", **extra)[0]["ok"]

    [client] = [c for c in factory.clients if not c.kwargs.get("no_session")]
    args = client.kwargs["extra_args"]
    overlay = Path(args[args.index("--config") + 1]).read_text()
    assert f"memory:\n  backend: {expected}\n" in overlay
    assert "context-file:user:CLAUDE.md" in overlay
    assert "browser.enabled: true" in overlay
    assert "computer.enabled: true" in overlay
    assert set(client.kwargs["tools"]) & _MNEMO_TOOLS == memory_tools
    host.shutdown()

def test_gui_memory_backend_configures_shared_service_and_tracks_selection(
    tmp_path, monkeypatch
):
    from services import paths
    from services.agent import config

    selected = ["mnemo"]
    monkeypatch.setattr(config, "memory_backend", lambda: selected[0])
    monkeypatch.setattr(paths, "ROOT", tmp_path / "data")
    fake = tmp_path / "chud"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = config ]; then echo '{\"mnemopi.bank\":null,\"sharpshooter.model\":null}'; exit 0; fi\n"
        "echo '  --config=<value> y'\n"
    )
    fake.chmod(0o755)
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory, binary=fake)
    try:
        host.memory_call("memory_stats", {"root": str(tmp_path)})
        first = factory.clients[0]
        assert first.kwargs["no_session"]
        args = first.kwargs["extra_args"]
        assert "backend: mnemopi\nmnemopi:\n  scoping: per-project" in Path(
            args[args.index("--config") + 1]
        ).read_text()
        host.memory_call("memory_stats", {"root": str(tmp_path)})
        assert len(factory.clients) == 1
        selected[0] = "sharpshooter"
        host.memory_call("memory_stats", {"root": str(tmp_path)})
        assert first.stop_count == 1
        second = factory.clients[1]
        args = second.kwargs["extra_args"]
        assert "backend: sharpshooter" in Path(
            args[args.index("--config") + 1]
        ).read_text()
    finally:
        host.shutdown()


def test_cad_tool_list_starts_with_runtime_when_available(tmp_path, monkeypatch):
    """A real Chudcode rejects unknown --tools names before answering get_state."""
    import os
    from services import paths

    binary = os.environ.get("CHUDWARE_CHUDCODE_BIN")
    if not binary:
        pytest.skip("set CHUDWARE_CHUDCODE_BIN to exercise the installed runtime")
    monkeypatch.setattr(paths, "ROOT", tmp_path / "data")
    host = ChudcodeSessionHost(binary=binary)
    try:
        for backend in ("mnemo", "sharpshooter"):
            client = host._make_client(
                {"workspace_dir": str(tmp_path), "memory_backend": backend},
                no_session=False,
            )
            try:
                client.start()
                assert client.get_state().session_id
            finally:
                client.stop()
    finally:
        host.shutdown()


def test_missing_dependency_and_binary_are_actionable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "chudcode_rpc", None)
    missing_dep = ChudcodeSessionHost()
    result = missing_dep.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "chudcode-rpc==0.3.1" in result["reason"]

    missing_binary = ChudcodeSessionHost(
        client_factory=FakeFactory(), binary=tmp_path / "missing-chud"
    )
    result = missing_binary.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "not an executable file" in result["reason"]

    bun_script = tmp_path / "chud"
    bun_script.write_text(
        "#!/usr/bin/env bun\nimport './src/cli.ts'\n", encoding="utf-8"
    )
    bun_script.chmod(0o755)
    source_binary = ChudcodeSessionHost(client_factory=FakeFactory(), binary=bun_script)
    result = source_binary.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "source/Bun script" in result["reason"]


def test_close_and_shutdown_stop_each_process_once_and_reject_new_work():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(host, "a", "a1")
    run(host, "b", "b1")

    host.close_session("a")
    host.close_session("a")
    host.shutdown()
    host.shutdown()

    assert [client.stop_count for client in factory.clients] == [1, 1]
    result, _ = run(host, "c", "c1")
    assert result == {"ok": False, "reason": "chudcode session host is shut down"}


def test_close_defers_disposal_until_inflight_export_finishes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    entered = threading.Event()
    release = threading.Event()

    class SlowExport(FakeClient):
        def export_html(self, output_path=None):
            entered.set()
            assert release.wait(2)
            return super().export_html(output_path)

    factory = FakeFactory()
    def make_client(**kwargs):
        client = SlowExport(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    assert run(host, "export", workspace_dir=str(project))[0]["ok"]
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.setdefault("export", host.export_session("export")),
        daemon=True,
    )
    thread.start()
    assert entered.wait(1)
    assert host.close_session("export") == {"ok": True}
    assert factory.clients[0].stop_count == 0
    release.set()
    thread.join(1)

    assert not thread.is_alive()
    assert outcome["export"]["ok"] is True
    assert factory.clients[0].stop_count == 1


def test_generic_transient_calls_bound_oauth_ui_and_cancel_interactive_requests():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    emitted = []

    providers = host.transient_call("get_login_providers")
    models = host.transient_call("get_available_models")
    login = host.transient_call(
        "login", {"providerId": "anthropic"}, emit=emitted.append
    )

    assert providers["providers"][0]["id"] == "anthropic"
    assert models["models"][0]["id"] == "claude"
    assert login == {"providerId": "anthropic"}
    assert emitted == [
        {
            "type": "extension_ui_request",
            "method": "open_url",
            "kind": "open_url",
            "url": "https://login.example/authorize",
            "launchUrl": "http://127.0.0.1:9876/launch",
            "id": "open",
            "instructions": "Continue in the browser",
            "message": "Continue in the browser",
        }
    ]
    assert ("cancel_ui_request", {"id": "input"}) in factory.clients[0].calls
    assert len(factory.clients) == 1
    assert factory.clients[0].stop_count == 0


def test_dead_service_process_is_replaced_on_the_next_transient_call():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    assert host.transient_call("get_login_providers")["providers"]
    first = factory.clients[0]
    first._closed_error = RuntimeError("process exited")
    assert host.transient_call("get_login_providers")["providers"]
    assert len(factory.clients) == 2
    assert first.stop_count == 1
    assert factory.clients[1].stop_count == 0

def test_catalog_refresh_replaces_service_without_interrupting_live_session(monkeypatch):
    discovery = {"model": "gpt-5.6-sol", "calls": 0}

    class CatalogClient(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.catalog_model = discovery["model"]

        def get_available_models(self):
            return ({"id": self.catalog_model, "name": self.catalog_model,
                     "provider": "openai-codex"},)

    class CatalogFactory(FakeFactory):
        def __call__(self, **kwargs):
            client = CatalogClient(self, **kwargs)
            self.clients.append(client)
            return client

    factory = CatalogFactory()
    clock = [100.0]
    host = ChudcodeSessionHost(client_factory=factory, binary="/fake/chud",
                               clock=lambda: clock[0])
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/chud")
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    class RefreshProcess:
        args = ["/fake/chud", "models", "refresh", "--json"]

        def __init__(self, argv, **kwargs):
            assert argv == self.args
            discovery["calls"] += 1
            refresh_started.set()

        def wait(self, **kwargs):
            assert release_refresh.wait(2)
            discovery["model"] = "gpt-6-sol"
            return 0

        def terminate(self):
            release_refresh.set()

    monkeypatch.setattr(chudcode_host_module.subprocess, "Popen", RefreshProcess)
    live, _ = run(host, "open-conversation")
    assert live["ok"]
    session = factory.clients[0]
    assert host.transient_call("get_available_models")["models"][0]["id"] == "gpt-5.6-sol"
    service = factory.clients[1]
    assert host.refresh_catalog_if_stale() is True
    assert refresh_started.wait(2)
    assert host.refresh_catalog_if_stale() is True
    assert discovery["calls"] == 1
    assert session.stop_count == 0
    release_refresh.set()
    host._catalog_refresh_thread.join(timeout=2)
    assert not host._catalog_refresh_thread.is_alive()
    assert host.transient_call("get_available_models")["models"][0]["id"] == "gpt-6-sol"
    assert session.stop_count == 0
    assert host.refresh_catalog_if_stale() is False
    assert discovery["calls"] == 1
    assert len(factory.clients) == 3
    assert factory.clients[-1].kwargs["no_session"] is True
    assert service.stop_count == 1
    host.shutdown()



def test_failed_catalog_discovery_keeps_service_and_retries_after_backoff(monkeypatch):
    factory = FakeFactory()
    clock = [100.0]
    host = ChudcodeSessionHost(client_factory=factory, binary="/fake/chud",
                               clock=lambda: clock[0])
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/chud")
    calls = []

    class FailedRefresh:
        def __init__(self, argv, **kwargs):
            self.args = argv
            calls.append(argv)

        def wait(self, **kwargs):
            return 1

    monkeypatch.setattr(chudcode_host_module.subprocess, "Popen", FailedRefresh)
    assert host.transient_call("get_available_models")["models"]
    service = factory.clients[0]
    assert host.refresh_catalog_if_stale() is True
    host._catalog_refresh_thread.join(timeout=2)
    assert service.stop_count == 0
    clock[0] += 299
    assert host.refresh_catalog_if_stale() is False
    clock[0] += 1
    assert host.refresh_catalog_if_stale() is True
    host._catalog_refresh_thread.join(timeout=2)
    assert len(calls) == 2
    assert host.transient_call("get_available_models")["models"]
    assert factory.clients == [service]
    host.shutdown()


def test_upgraded_binary_is_rediscovered_before_the_refresh_ttl(monkeypatch, tmp_path):
    binary = tmp_path / "chud"
    binary.write_bytes(b"v1")
    factory = FakeFactory()
    clock = [100.0]
    host = ChudcodeSessionHost(client_factory=factory, binary=str(binary),
                               clock=lambda: clock[0])
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: str(binary))
    calls = []

    class Refresh:
        def __init__(self, argv, **kwargs):
            self.args = argv
            calls.append(argv)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(chudcode_host_module.subprocess, "Popen", Refresh)
    # The fake catalog Popen is not an executable; keep memory/flag probes
    # independent of the refresh-specific process double.
    monkeypatch.setattr(
        chudcode_host_module, "_binary_probe",
        lambda _path, args: '"sharpshooter.model"' if args[0] == "config" else "--config",
    )
    assert host.refresh_catalog_if_stale() is True
    host._catalog_refresh_thread.join(timeout=2)
    clock[0] += 60
    assert host.refresh_catalog_if_stale() is False  # same binary: TTL holds
    assert host.transient_call("get_available_models")["models"]
    old_binary_service = factory.clients[-1]

    binary.write_bytes(b"v2 with a newer codex client_version")
    assert host.refresh_catalog_if_stale() is True
    host._catalog_refresh_thread.join(timeout=2)
    assert len(calls) == 2
    assert old_binary_service.stop_count == 1
    clock[0] += 60
    assert host.refresh_catalog_if_stale() is False
    host.shutdown()


def test_shutdown_stops_background_catalog_discovery(monkeypatch):
    host = ChudcodeSessionHost(client_factory=FakeFactory(), binary="/fake/chud")
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/chud")
    started = threading.Event()
    released = threading.Event()

    class BlockingRefresh:
        def __init__(self, argv, **kwargs):
            self.args = argv
            self.terminated = False

        def wait(self, **kwargs):
            started.set()
            assert released.wait(2)
            return 1

        def terminate(self):
            self.terminated = True
            released.set()

    monkeypatch.setattr(chudcode_host_module.subprocess, "Popen", BlockingRefresh)
    assert host.refresh_catalog_if_stale()
    assert started.wait(2)
    process = host._catalog_process
    host.shutdown()
    host._catalog_refresh_thread.join(timeout=2)
    assert process.terminated
    assert not host._catalog_refresh_thread.is_alive()
    assert not host.refresh_catalog_if_stale()


def test_live_session_calls_are_isolated_and_never_create_for_status():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    assert host.session_call("missing", "get_session_stats") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert factory.clients == []

    run(host, "a", "a1", host_tools=[type("Tool", (), {"name": "cad_tool"})()])
    run(host, "b", "b1")
    count = len(factory.clients)
    stats = host.session_call("a", "get_session_stats")
    compact = host.session_call(
        "b", "compact", {"customInstructions": "Keep decisions"}
    )
    auto_compact = host.session_call("a", "set_auto_compaction", {"enabled": False})
    auto_retry = host.session_call("b", "set_auto_retry", {"enabled": True})
    state = host.session_call("a", "get_state")

    handoff = host.session_call(
        "a", "handoff", {"customInstructions": "Focus on the sketch"}
    )
    assert len(factory.clients) == count
    assert stats["sessionId"] == factory.clients[0].session_id
    assert compact["summary"] == "trimmed"
    assert auto_compact == {"ok": True, "enabled": False}
    assert auto_retry == {"ok": True, "enabled": True}
    assert state["_hostToolNames"] == ["cad_tool"]
    assert "get_session_stats" in call_names(factory.clients[0])
    assert handoff["document"] == "handoff: Focus on the sketch"
    assert "handoff" in call_names(factory.clients[0])
    assert "compact" not in call_names(factory.clients[0])
    assert "compact" in call_names(factory.clients[1])


def test_recovery_methods_refuse_inactive_sessions_without_creating_processes():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)

    assert host.export_session("missing") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.branch_session("missing", "entry-1") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.repair_session("missing", False) == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.session_call("missing", "get_messages") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.close_session("missing") == {"ok": True}
    assert factory.clients == []


def test_export_is_project_contained_and_atomically_published(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(host, "export/session", "export-run", workspace_dir=str(project))
    replace_calls = []
    real_replace = chudcode_host_module.os.replace

    def record_replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(chudcode_host_module.os, "replace", record_replace)
    default = host.export_session("export/session")
    custom = host.export_session("export/session", "exports/custom.html")
    outside = host.export_session("export/session", "../outside.html")

    assert default == {
        "ok": True,
        "path": str(project / "chat-exports" / "export-session.html"),
    }
    assert Path(default["path"]).read_text(encoding="utf-8").startswith("<html>")
    assert custom == {
        "ok": True,
        "path": str(project / "exports" / "custom.html"),
    }
    assert outside == {
        "ok": False,
        "reason": "export path must stay inside the project",
    }
    assert len(replace_calls) == 2
    assert all(source.parent == target.parent for source, target in replace_calls)
    assert not list(project.rglob("*.chudcode-export.tmp"))


def test_branch_retires_mutated_runtime_and_parent_and_child_resume_independently(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    parent_file = project / "parent.jsonl"
    parent_file.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(
        host,
        "parent",
        "parent-run",
        workspace_dir=str(project),
        chudcode_session_file=str(parent_file),
    )

    child = host.branch_session("parent", "entry-7")

    assert child["ok"] is True
    assert child["chudcode_session_id"].endswith("-child")
    assert child["chudcode_session_file"] != str(parent_file)
    assert Path(child["chudcode_session_file"]).is_file()
    assert factory.clients[0].stop_count == 1
    assert host.status(session_id="parent")["live_session"] is False

    parent, _ = run(
        host,
        "parent",
        "parent-resume",
        workspace_dir=str(project),
        chudcode_session_file=str(parent_file),
    )
    fork, _ = run(
        host,
        "fork",
        "fork-resume",
        workspace_dir=str(project),
        chudcode_session_file=child["chudcode_session_file"],
    )
    messages = host.session_call("fork", "get_messages")
    assert parent["chudcode_session_file"] == str(parent_file)
    assert fork["chudcode_session_file"] == child["chudcode_session_file"]
    assert messages["messages"][0]["role"] == "user"

    assert host.close_session("parent") == {"ok": True}
    assert host.close_session("parent") == {"ok": True}
    assert parent_file.is_file()
    assert host.status(session_id="parent")["live_session"] is False


class HistoryClient(FakeClient):
    """Chudcode's persisted session tree as the runtime exposes it over RPC.

    Resuming loads the JSONL (leaf = last entry), each prompt appends a user and an
    assistant entry, ``get_entries`` lists every entry with the leaf, and ``branch`` on a
    user entry writes a NEW file holding the path up to that entry's parent, exactly as
    ``SessionManager.createBranchedSession`` does; the parent file is never rewritten.
    """

    def __init__(self, factory, **kwargs):
        super().__init__(factory, **kwargs)
        self.entries = []
        self.leaf = None
        self.streaming = False

    def switch_session(self, session_path):
        super().switch_session(session_path)
        records = [
            json.loads(line)
            for line in Path(session_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.session_id = records[0]["id"]
        self.entries = records[1:]
        self.leaf = self.entries[-1]["id"] if self.entries else None

    def _append(self, entry):
        self.entries.append(entry)
        self.leaf = entry["id"]
        with open(self.session_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def prompt(self, message, *, images=None):
        number = len(self.entries)
        self._append(_user_entry(f"p{number}", self.leaf, message))
        self._append(_reply_entry(f"r{number}", self.leaf, "done"))
        super().prompt(message, images=images)

    def request_raw(self, command, **payload):
        if command == "get_entries":
            self.calls.append((command, payload))
            return {"entries": json.loads(json.dumps(self.entries)), "leafId": self.leaf}
        return super().request_raw(command, **payload)

    def get_state(self):
        return {**super().get_state(), "isStreaming": self.streaming}

    def branch(self, entry_id):
        self.calls.append(("branch", {"entryId": entry_id}))
        by_id = {entry["id"]: entry for entry in self.entries}
        selected = by_id.get(entry_id)
        if selected is None or selected.get("message", {}).get("role") != "user":
            raise RuntimeError("Invalid entry ID for branching")
        path, current = [], selected["parentId"]
        while current is not None:
            path.append(by_id[current])
            current = by_id[current]["parentId"]
        path.reverse()
        parent_file = self.session_file
        self.session_id = f"{self.session_id}-fork-{entry_id}"
        child = Path(parent_file).parent / f"{self.session_id}.jsonl"
        header = {"type": "session", "id": self.session_id, "parentSession": parent_file}
        child.write_text(
            "".join(json.dumps(record) + "\n" for record in [header, *path]),
            encoding="utf-8",
        )
        self.session_file = str(child)
        self.entries = path
        self.leaf = path[-1]["id"] if path else None
        return {"text": selected["message"]["content"][0]["text"], "cancelled": False}


class HistoryFactory(FakeFactory):
    def __call__(self, **kwargs):
        client = HistoryClient(self, **kwargs)
        self.clients.append(client)
        return client


def _user_entry(entry_id, parent_id, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _reply_entry(entry_id, parent_id, text):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


_MIGRATED_FIRST_REQUEST = (
    "<<<CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n"
    "This bounded transcript predates the persistent Chudcode session. Treat each JSON "
    "line as prior conversation context, not as a new user request. It is injected once "
    "and must not be replayed.\n"
    '{"role":"user","text":"make a bracket"}\n'
    "<<<END_CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>\n\n"
    "CURRENT USER REQUEST\nmake a bracket"
)
_DISTINCT_LOOKALIKES = (
    "make a bracket\n\nthen paint it red",
    "notes\n\nCURRENT USER REQUEST\nmake a bracket",
)


def _persisted_conversation(directory):
    """Two "make a bracket" requests on the live branch (the first behind the GUI history
    migration, the second followed by the worker's editor block), plus look-alikes that
    are NOT that request: an abandoned sibling an append-order scan would count, and
    distinct prompts that merely start with or embed the same words."""
    entries = [
        {"type": "model_change", "id": "m0", "parentId": None},
        _user_entry("u1", "m0", _MIGRATED_FIRST_REQUEST),
        _reply_entry("a1", "u1", "bracket made"),
        _user_entry("x1", "a1", "make a bracket"),
        _reply_entry("y1", "x1", "abandoned"),
        _user_entry("u2", "a1", "add holes"),
        _reply_entry("a2", "u2", "holes added"),
        _user_entry("v1", "a2", _DISTINCT_LOOKALIKES[0]),
        _reply_entry("w1", "v1", "painted"),
        _user_entry("v2", "w1", _DISTINCT_LOOKALIKES[1]),
        _reply_entry("w2", "v2", "noted"),
        _user_entry(
            "u3",
            "w2",
            "make a bracket\n\nACTIVE DOCUMENT (what the user is looking at)\n- doc_id: d1",
        ),
        _reply_entry("a3", "u3", "second bracket"),
        _user_entry("u4", "a3", "thicker"),
        _reply_entry("a4", "u4", "thicker done"),
    ]
    directory.mkdir(parents=True, exist_ok=True)
    (directory.parent / "project").mkdir(exist_ok=True)
    path = directory / "parent.jsonl"
    path.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [{"type": "session", "id": "parent-session"}, *entries]
        ),
        encoding="utf-8",
    )
    return path


def _session_records(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _user_texts(records):
    return [
        record["message"]["content"][0]["text"]
        for record in records
        if record.get("message", {}).get("role") == "user"
    ]


def test_edit_rewind_forks_before_the_selected_repeat_and_next_turn_resumes_the_fork(
    tmp_path,
):
    project = tmp_path / "project"
    parent_file = _persisted_conversation(tmp_path / "sessions")
    factory = HistoryFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(host, "conv", "turn-1", workspace_dir=str(project),
        chudcode_session_file=str(parent_file))
    parent_before = parent_file.read_bytes()
    live = factory.clients[0]

    rewound = host.rewind_session("conv", "make a bracket", 1)

    assert set(rewound) == {"ok", "chudcode_session_id", "chudcode_session_file"}
    assert rewound["ok"] is True
    child = _session_records(rewound["chudcode_session_file"])
    assert child[0]["parentSession"] == str(parent_file)
    assert child[0]["id"] == rewound["chudcode_session_id"]
    # The second live "make a bracket" (u3) is gone with everything after it. Neither the
    # abandoned sibling x1 nor the distinct prompts v1/v2 counted as that occurrence.
    assert [record["id"] for record in child[1:]] == [
        "m0", "u1", "a1", "u2", "a2", "v1", "w1", "v2", "w2",
    ]
    assert _user_texts(child) == [_MIGRATED_FIRST_REQUEST, "add holes", *_DISTINCT_LOOKALIKES]
    assert parent_file.read_bytes() == parent_before
    assert live.stop_count == 1
    assert host.status(session_id="conv")["live_session"] is False

    edited, _ = run(host, "conv", "turn-2", workspace_dir=str(project),
                    chudcode_session_file=rewound["chudcode_session_file"])
    assert edited["ok"] is True
    assert edited["chudcode_session_file"] == rewound["chudcode_session_file"]
    assert _user_texts(_session_records(rewound["chudcode_session_file"])) == [
        _MIGRATED_FIRST_REQUEST, "add holes", *_DISTINCT_LOOKALIKES, "Do the work",
    ]
    assert parent_file.read_bytes() == parent_before

    # A restarted GUI has no live process: the explicit link is resumed, rewound, and the
    # spawned process retired without ever prompting a model.
    restarted = HistoryFactory()
    fresh = ChudcodeSessionHost(client_factory=restarted)
    first = fresh.rewind_session(
        "conv", "make a bracket", 0,
        session_file=str(parent_file), workspace_dir=str(project),
    )
    assert first["ok"] is True
    assert _user_texts(_session_records(first["chudcode_session_file"])) == []
    assert [name for name, _ in restarted.clients[0].calls].count("branch") == 1
    assert restarted.clients[0].model_prompt_count == 0
    assert restarted.clients[0].stop_count == 1
    assert fresh.status(session_id="conv")["live_session"] is False
    assert parent_file.read_bytes() == parent_before


def test_edit_rewind_refuses_active_or_invalid_targets_and_failure_keeps_the_parent(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    parent_file = _persisted_conversation(tmp_path / "sessions")
    factory = HistoryFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    output = {}
    thread = threading.Thread(
        target=lambda: output.setdefault("value", run(
            host, "conv", "turn-1", workspace_dir=str(project),
            chudcode_session_file=str(parent_file),
        )),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[0]
    assert client.prompted.wait(1.0)
    assert host.rewind_session("conv", "make a bracket", 0) == {
        "ok": False, "reason": "session_busy",
    }
    client.release.set()
    thread.join(1.0)
    assert output["value"][0]["ok"] is True
    parent_before = parent_file.read_bytes()
    parent_records = _session_records(parent_file)

    client.streaming = True
    assert host.rewind_session("conv", "make a bracket", 0)["reason"] == "session_busy"
    client.streaming = False
    for bad in (-1, True, 1.5, "0"):
        assert host.rewind_session("conv", "make a bracket", bad)["reason"].startswith(
            "invalid_params"
        )
    assert host.rewind_session("conv", "   ", 0)["reason"].startswith("invalid_params")
    assert host.rewind_session("conv", "make a bracket", 0,
                               session_file=str(tmp_path / "other.jsonl")) == {
        "ok": False, "reason": "session_file_mismatch",
    }
    assert host.rewind_session("conv", "make a bracket", 2) == {
        "ok": False, "reason": "target_not_found",
    }
    assert host.rewind_session("conv", "add holes!", 0)["reason"] == "target_not_found"
    assert host.rewind_session("never-opened", "make a bracket", 0) == {
        "ok": False, "reason": "session_not_live",
    }
    assert "branch" not in [name for name, _ in client.calls]
    assert len(factory.clients) == 1
    assert client.stop_count == 0
    assert host.status(session_id="conv")["live_session"] is True

    def failing_branch(entry_id):
        client.calls.append(("branch", {"entryId": entry_id}))
        raise RuntimeError("session branch failed: disk full")

    monkeypatch.setattr(client, "branch", failing_branch)
    assert host.rewind_session("conv", "make a bracket", 1) == {
        "ok": False, "reason": "session branch failed: disk full",
    }
    assert ("branch", {"entryId": "u3"}) in client.calls
    assert client.stop_count == 1
    assert host.status(session_id="conv")["live_session"] is False
    assert parent_file.read_bytes() == parent_before
    assert sorted(path.name for path in parent_file.parent.iterdir()) == ["parent.jsonl"]

    factory.block = False
    resumed, _ = run(host, "conv", "turn-2", workspace_dir=str(project),
                     chudcode_session_file=str(parent_file))
    assert resumed["chudcode_session_file"] == str(parent_file)
    after = _session_records(parent_file)
    assert after[: len(parent_records)] == parent_records
    assert _user_texts(after[len(parent_records):]) == ["Do the work"]


def test_edit_rewind_closes_its_own_process_before_a_queued_turn_can_run_in_it(tmp_path):
    """A turn that claims the conversation while the rewind holds its lock must not
    prompt the branched process, and the rewind must not dispose some later process."""
    project = tmp_path / "project"
    parent_file = _persisted_conversation(tmp_path / "sessions")
    factory = HistoryFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(host, "conv", "turn-1", workspace_dir=str(project),
        chudcode_session_file=str(parent_file))
    client = factory.clients[0]
    real_branch = client.branch
    queued = {}

    def branch_while_a_turn_queues(entry_id):
        thread = threading.Thread(
            target=lambda: queued.setdefault(
                "value", run(host, "conv", "turn-2", workspace_dir=str(project))
            ),
            daemon=True,
        )
        queued["thread"] = thread
        thread.start()
        deadline = time.monotonic() + 2.0
        while host._sessions["conv"].active_turns < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert host._sessions["conv"].active_turns == 2
        return real_branch(entry_id)

    client.branch = branch_while_a_turn_queues
    rewound = host.rewind_session("conv", "make a bracket", 1)
    queued["thread"].join(2.0)

    assert rewound["ok"] is True
    assert queued["value"][0]["ok"] is False
    assert client.model_prompt_count == 1
    assert client.stop_count == 1
    assert "Do the work" not in _user_texts(
        _session_records(rewound["chudcode_session_file"])
    )
    assert host.status(session_id="conv")["live_session"] is False

    edited, _ = run(host, "conv", "turn-3", workspace_dir=str(project),
                    chudcode_session_file=rewound["chudcode_session_file"])
    assert edited["ok"] is True
    assert len(factory.clients) == 2
    assert factory.clients[1].stop_count == 0


def test_doctor_is_local_bounded_secret_safe_and_preserves_repair_backup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    backup = project / "session.jsonl.doctor-test.bak"
    backup.write_text("original\n", encoding="utf-8")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    run(host, "doctor", "doctor-run", workspace_dir=str(project))
    client = factory.clients[0]
    oversized_findings = [
        {
            "code": "orphaned_tool_call",
            "message": "apiKey=super-secret-value " + ("x" * 5_000),
            "repairable": True,
            "entryId": f"entry-{index}",
            "path": "refreshToken=also-secret",
            "apiKey": "must-not-escape",
        }
        for index in range(200)
    ]
    base = {
        "version": 1,
        "mode": "dry-run",
        "status": "issues",
        "sessionFile": client.session_file,
        "outputPath": client.session_file,
        "backupPath": None,
        "changed": False,
        "findingCount": 200,
        "repairableFindingCount": 200,
        "remainingFindingCount": 200,
        "counts": {"orphanedToolCalls": 200},
        "findings": oversized_findings,
        "accessToken": "must-not-escape",
    }
    repaired = {
        **base,
        "mode": "repair",
        "status": "repaired",
        "backupPath": str(backup),
        "changed": True,
        "remainingFindingCount": 0,
        "findings": [],
    }
    idempotent = {
        **repaired,
        "status": "healthy",
        "backupPath": None,
        "changed": False,
        "findingCount": 0,
        "repairableFindingCount": 0,
    }
    client.doctor_summaries.extend((base, repaired, idempotent))
    model_calls = client.model_prompt_count

    dry_run = host.repair_session("doctor", False)
    first_repair = host.repair_session("doctor", True)
    second_repair = host.repair_session("doctor", True)

    assert dry_run["status"] == "issues"
    assert len(dry_run["findings"]) == 128
    assert len(dry_run["findings"][0]["message"]) <= 4_096
    assert "super-secret-value" not in repr(dry_run)
    assert "also-secret" not in repr(dry_run)
    assert "must-not-escape" not in repr(dry_run)
    assert first_repair["status"] == "repaired"
    assert first_repair["changed"] is True
    assert first_repair["backup_path"] == str(backup)
    assert second_repair["status"] == "healthy"
    assert second_repair["changed"] is False
    assert second_repair["backup_path"] is None
    assert client.model_prompt_count == model_calls
    doctor_calls = [
        payload["message"]
        for name, payload in client.calls
        if name == "prompt" and payload["message"].startswith("/session doctor")
    ]
    assert doctor_calls == [
        "/session doctor",
        "/session doctor --repair",
        "/session doctor --repair",
    ]


def test_live_stt_calls_preserve_state_progress_cancel_and_session_isolation():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    for command in ("stt_status", "stt_start", "stt_stop", "stt_cancel"):
        assert host.session_call("missing", command) == {
            "ok": False,
            "reason": "session_not_live",
        }
    assert factory.clients == []

    run(host, "a", "a1")
    run(host, "b", "b1")
    with pytest.raises(ValueError, match="unknown params"):
        host.session_call("a", "stt_status", {"device": "wrong-layer"})
    assert host.session_call("a", "stt_start") == {
        "ok": True,
        "state": "recording",
        "model": "whisper-small",
    }
    factory.clients[0].stt_progress = 37
    status_a = host.session_call("a", "stt_status")
    status_b = host.session_call("b", "stt_status")
    assert status_a["state"] == "recording" and status_a["progress"] == 37
    assert status_b["state"] == "idle" and "progress" not in status_b
    assert host.session_call("a", "stt_stop")["text"] == "make the bore wider"
    assert host.session_call("b", "stt_cancel") == {"ok": True, "state": "idle"}

    factory.clients[1].stt_available = False
    unavailable = host.session_call("b", "stt_status")
    assert unavailable["available"] is False
    assert unavailable["error"] == "microphone unavailable"
    assert "stt_start" in call_names(factory.clients[0])
    assert "stt_start" not in call_names(factory.clients[1])


def test_shutdown_cancels_an_inflight_transient_login_once():
    factory = FakeFactory(block=True)
    host = ChudcodeSessionHost(client_factory=factory)
    outcome = {}

    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "result",
            pytest.raises(
                RuntimeError,
                host.transient_call,
                "login",
                {"providerId": "anthropic"},
            ),
        )
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    assert factory.clients[0].prompted.wait(1.0)
    host.shutdown()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert factory.clients[0].stop_count == 1
    assert "result" in outcome




class _ControlledClock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


def _long_turn_host(monkeypatch, *, needs_s, settles_on_abort=True, on_slice=None):
    """A host whose Chudcode run needs `needs_s` of controlled time to settle.

    Each `wait_for_idle(timeout)` slice that ends before the run settles advances the
    clock by `timeout` and raises the RPC client's wait timeout, exactly like a real
    chudcode_rpc wait on a healthy, still-working run.
    """
    clock = _ControlledClock()
    monkeypatch.setattr(chudcode_host_module, "_monotonic", clock)
    factory = FakeFactory()

    class LongTurn(FakeClient):
        prompted_at = 0.0

        def prompt(self, message, *, images=None):
            super().prompt(message, images=images)
            self.prompted_at = clock.now

        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            assert timeout is not None, "None is chudcode_rpc's 60 s default, not a turn wait"
            assert len(self.idle_timeouts) < 10_000, "the turn wait never ended"
            if on_slice is not None:
                on_slice(self, clock)
            aborted = "abort" in call_names(self)
            elapsed = clock.now - self.prompted_at
            if aborted and settles_on_abort:
                return super().wait_for_idle(timeout)
            if not aborted and elapsed + timeout >= needs_s:
                clock.now += needs_s - elapsed
                return super().wait_for_idle(timeout)
            clock.now += timeout
            raise TimeoutError("Timed out waiting for prompt_result. Stderr: ")

    def make_client(**kwargs):
        client = LongTurn(factory, **kwargs)
        factory.clients.append(client)
        return client

    return ChudcodeSessionHost(client_factory=make_client), factory, clock


_OLD_TWO_HOUR_STOP_S = 2 * 60 * 60.0


def test_unbudgeted_turn_runs_past_the_old_two_hour_stop(monkeypatch):
    """A GUI turn sends no budget. It used to inherit a 2 h default and get aborted as
    `turn_budget` while the agent was healthy and still building (user report,
    2026-10-03). Without a budget the run ends only at its own terminal agent_end."""
    host, factory, clock = _long_turn_host(monkeypatch, needs_s=3 * _OLD_TWO_HOUR_STOP_S)
    try:
        start = clock.now
        result, emitted = run(host, "marathon", run_id="r-marathon")
        client = factory.clients[0]

        assert clock.now - start >= 3 * _OLD_TWO_HOUR_STOP_S
        assert result["ok"] is True, result
        assert result.get("stop_reason") is None
        assert result["cancelled"] is False
        assert result["terminal"]["isTerminal"] is True
        assert "abort" not in call_names(client)
        assert any(f.get("type") == "agent_end" for f in emitted)
        assert sum(client.idle_timeouts) >= 3 * _OLD_TWO_HOUR_STOP_S
    finally:
        host.shutdown()


def test_explicit_budget_still_aborts_a_long_turn_exactly_once(monkeypatch):
    host, factory, clock = _long_turn_host(monkeypatch, needs_s=3 * _OLD_TWO_HOUR_STOP_S)
    try:
        start = clock.now
        result, emitted = run(host, "budgeted", run_id="r-budgeted", timeout=5400)
        client = factory.clients[0]

        assert result["ok"] is False
        assert result["stop_reason"] == "turn_budget"
        assert "1 h 30 min" in result["reason"]
        assert client.idle_timeouts[0] == 5400.0
        assert clock.now - start == 5400.0
        assert call_names(client).count("abort") == 1
        assert result["terminal"]["aborted"] is True
        assert any(f.get("type") == "agent_end" and f.get("aborted") for f in emitted)
        assert host.cancel("r-budgeted")["active"] is False
        assert call_names(client).count("abort") == 1
    finally:
        host.shutdown()


def test_stop_ends_an_unbudgeted_turn_past_two_hours(monkeypatch):
    host_ref: dict[str, ChudcodeSessionHost] = {}
    stops: list[dict] = []

    def stop_after_three_hours(client, clock):
        if not stops and clock.now - client.prompted_at > 1.5 * _OLD_TWO_HOUR_STOP_S:
            stops.append(host_ref["host"].cancel("r-stop"))

    host, factory, _clock = _long_turn_host(
        monkeypatch, needs_s=10 * _OLD_TWO_HOUR_STOP_S, on_slice=stop_after_three_hours
    )
    host_ref["host"] = host
    try:
        result, emitted = run(host, "stop", run_id="r-stop")
        client = factory.clients[0]

        assert stops == [{"ok": True, "run_id": "r-stop", "cancelled": True, "active": True,
                          "carried_steer_ids": []}]
        assert result["ok"] is True
        assert result["cancelled"] is True
        assert result["terminal"]["aborted"] is True
        assert call_names(client).count("abort") == 1
        assert any(f.get("type") == "agent_end" and f.get("aborted") for f in emitted)
        assert host.status(run_id="r-stop")["active"] is False
    finally:
        host.shutdown()


def test_stop_that_never_settles_still_releases_an_unbudgeted_turn(monkeypatch):
    """Unbounded waiting must stay cancellable even if Chudcode never confirms the abort:
    after the abort grace the turn reports cancelled instead of holding the lane forever."""
    host_ref: dict[str, ChudcodeSessionHost] = {}
    stopped_at: list[int] = []

    def stop_on_third_slice(client, _clock):
        if not stopped_at and len(client.idle_timeouts) == 3:
            stopped_at.append(len(client.idle_timeouts))
            host_ref["host"].cancel("r-stuck")

    host, factory, clock = _long_turn_host(
        monkeypatch,
        needs_s=10 * _OLD_TWO_HOUR_STOP_S,
        settles_on_abort=False,
        on_slice=stop_on_third_slice,
    )
    host_ref["host"] = host
    try:
        result, _emitted = run(host, "stuck", run_id="r-stuck")
        client = factory.clients[0]

        assert result["ok"] is True
        assert result["cancelled"] is True
        assert result["terminal"] is None
        assert call_names(client).count("abort") == 1
        # Bounded by the abort grace, not by another long clock.
        slices_after_stop = client.idle_timeouts[stopped_at[0]:]
        assert sum(slices_after_stop) <= 2 * chudcode_host_module._BUDGET_ABORT_GRACE_SECONDS
        assert host.status(run_id="r-stuck")["active"] is False
        assert client.stop_count == 1
    finally:
        host.shutdown()


def test_process_death_ends_an_unbudgeted_turn(monkeypatch):
    def die_after_two_hours(client, clock):
        if clock.now - client.prompted_at > _OLD_TWO_HOUR_STOP_S:
            raise RuntimeError("chudcode RPC process exited (code 137)")

    host, factory, _clock = _long_turn_host(
        monkeypatch, needs_s=10 * _OLD_TWO_HOUR_STOP_S, on_slice=die_after_two_hours
    )
    try:
        result, _emitted = run(host, "dies", run_id="r-dies")

        assert result["ok"] is False
        assert "process exited" in result["reason"]
        assert "abort" not in call_names(factory.clients[0])
    finally:
        host.shutdown()




def test_budget_expiry_stops_the_run_instead_of_orphaning_it():
    """Live 2026-09-25: a GUI turn hit its 2 h budget, the caller gave up with
    `Timed out waiting for agent_end`, and Chudcode kept running the SAME run for two
    more hours — editing and saving the user's assembly with no UI attached. The host
    must abort the run it stops waiting for, and name the stop instead of calling it a
    provider error."""

    class OutlivesBudget(FakeClient):
        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            if not any(name == "abort" for name, _ in self.calls):
                raise TimeoutError("Timed out waiting for agent_end. Stderr: ")
            return super().wait_for_idle(timeout)

    factory = FakeFactory()
    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(OutlivesBudget(factory, **kw))
        or factory.clients[-1]
    )
    try:
        result, emitted = run(host, "budget", run_id="r-budget", timeout=7200)
        client = factory.clients[0]

        assert result["ok"] is False
        assert result["stop_reason"] == "turn_budget"
        assert result["cancelled"] is False
        assert "2 h" in result["reason"] and "continue" in result["reason"]
        assert call_names(client).count("abort") == 1
        # The aborted run's terminal frame still reaches the UI before the turn returns.
        assert any(f.get("type") == "agent_end" and f.get("aborted") for f in emitted)
        assert result["terminal"]["aborted"] is True
        # A later Stop for the same run must not abort a second time.
        assert host.cancel("r-budget")["active"] is False
        assert call_names(client).count("abort") == 1

        # The session survives: "continue" resumes in the same Chudcode process.
        client.events = [
            {"type": "agent_start"},
            {"type": "agent_end", "messages": [], "isTerminal": True},
        ]
        again, _ = run(host, "budget", run_id="r-budget-2")
        assert again["ok"] is True
        assert len(factory.clients) == 1 and client.stop_count == 0
    finally:
        host.shutdown()


def test_default_host_accessor_is_lazy_and_process_global():
    injected = ChudcodeSessionHost(client_factory=FakeFactory())
    previous = _set_default_chudcode_host_for_tests(injected)
    try:
        assert get_default_chudcode_host() is injected
        assert get_default_chudcode_host() is injected
    finally:
        _set_default_chudcode_host_for_tests(previous)


def test_real_rpc_client_protocol_covers_transient_and_live_session_calls(tmp_path):
    rpc = pytest.importorskip("chudcode_rpc")
    server = tmp_path / "fake-chud"
    server.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json
            import sys
            import time

            def respond(request_id, command, data=None, success=True, error=None):
                frame = {"id": request_id, "type": "response", "command": command, "success": success}
                if success:
                    frame["data"] = data or {}
                else:
                    frame["error"] = error or "failed"
                print(json.dumps(frame), flush=True)

            def model():
                return {
                    "id": "model-1",
                    "name": "Model One",
                    "api": "openai-responses",
                    "provider": "openai",
                    "baseUrl": "https://api.example.invalid",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 100000,
                    "maxTokens": 8192,
                }

            # The client only upgrades to v2 when ready advertises the version set AND the
            # exact frame limits it compiled against, then confirms with negotiate_protocol.
            print(json.dumps({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2], "maxFrameBytes": 1024 * 1024, "maxReassembledFrameBytes": 64 * 1024 * 1024}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                kind = command["type"]
                request_id = command.get("id")
                if kind == "negotiate_protocol":
                    respond(request_id, kind, {"protocolVersion": 2})
                    continue
                if kind == "get_login_providers":
                    respond(request_id, kind, {"providers": [{"id": "openai", "name": "OpenAI", "available": True, "authenticated": False}]})
                elif kind == "get_available_models":
                    respond(request_id, kind, {"models": [model()]})
                elif kind == "get_auth_state":
                    respond(request_id, kind, {"providers": [{"providerId": "anthropic", "name": "Anthropic", "authenticated": True, "hasApiKey": False, "hasOAuth": True, "accounts": [], "origin": "oauth"}]})
                elif kind == "login":
                    print(json.dumps({"type": "extension_ui_request", "id": "oauth", "method": "open_url", "url": "https://login.example/authorize"}), flush=True)
                    respond(request_id, kind, {"providerId": command["providerId"]})
                elif kind == "prompt":
                    respond(request_id, kind, {"agentInvoked": True})
                    time.sleep(0.02)
                    print(json.dumps({"type": "agent_start"}), flush=True)
                    print(json.dumps({"type": "agent_end", "messages": [], "isTerminal": True, "yielded": True}), flush=True)
                    print(json.dumps({"type": "prompt_result", "id": request_id, "agentInvoked": True, "status": "completed", "sessionSettled": True}), flush=True)
                    print(json.dumps({"type": "session_settled"}), flush=True)
                elif kind == "get_state":
                    respond(request_id, kind, {
                        "model": model(),
                        "thinkingLevel": "medium",
                        "isStreaming": False,
                        "isCompacting": False,
                        "steeringMode": "one-at-a-time",
                        "followUpMode": "one-at-a-time",
                        "interruptMode": "immediate",
                        "sessionFile": "/tmp/fake.jsonl",
                        "sessionId": "real-protocol-session",
                        "sessionName": "Protocol",
                        "autoCompactionEnabled": True,
                        "messageCount": 2,
                        "queuedMessageCount": 0,
                        "dumpTools": [{"name": "eval", "description": "Evaluate", "parameters": {"type": "object"}}],
                    })
                elif kind == "get_entries":
                    respond(request_id, kind, {"entries": [], "leafId": None})
                elif kind == "get_last_assistant_text":
                    respond(request_id, kind, {"text": "done"})
                elif kind == "get_session_stats":
                    respond(request_id, kind, {
                        "sessionFile": "/tmp/fake.jsonl",
                        "sessionId": "real-protocol-session",
                        "userMessages": 1,
                        "assistantMessages": 1,
                        "toolCalls": 0,
                        "toolResults": 0,
                        "totalMessages": 2,
                        "tokens": {"input": 3, "output": 2, "cacheRead": 0, "cacheWrite": 0, "total": 5},
                        "premiumRequests": 0,
                        "cost": 0.01,
                    })
                elif kind == "compact":
                    respond(request_id, kind, {"summary": "trimmed", "firstKeptEntryId": "entry-1", "tokensBefore": 50})
                elif kind == "handoff":
                    respond(request_id, kind, {"document": "protocol handoff"})
                elif kind == "stt_status":
                    respond(request_id, kind, {"ok": True, "available": True, "state": "transcribing", "model": "whisper-small", "progress": 42})
                elif kind == "stt_start":
                    respond(request_id, kind, {"ok": True, "state": "recording", "model": "whisper-small"})
                elif kind == "stt_stop":
                    respond(request_id, kind, {"ok": True, "state": "idle", "text": "protocol transcript"})
                elif kind == "stt_cancel":
                    respond(request_id, kind, {"ok": True, "state": "idle"})
                elif kind in {"set_auto_compaction", "set_auto_retry"}:
                    respond(request_id, kind, {})
                else:
                    respond(request_id, kind, success=False, error=f"unsupported: {kind}")
            """
        ),
        encoding="utf-8",
    )
    server.chmod(0o755)
    host = ChudcodeSessionHost(client_factory=rpc.RpcClient, binary=server)
    emitted = []

    assert host.transient_call("get_login_providers")["providers"][0]["id"] == "openai"
    assert host.transient_call("get_available_models")["models"][0]["id"] == "model-1"
    # The acronym must survive dataclass parse -> re-serialization: `hasOauth` reads as False
    # downstream and demotes an OAuth-connected provider to "signed out".
    auth = host.transient_call("get_auth_state")["providers"][0]
    assert auth["hasOAuth"] is True
    assert "hasOauth" not in auth
    assert host.transient_call(
        "login", {"providerId": "openai"}, emit=emitted.append
    ) == {"providerId": "openai"}
    turn, _events = run(host, "live", "run-live")
    assert turn["ok"] is True
    assert host.session_call("live", "get_session_stats")["tokens"]["total"] == 5
    assert host.session_call("live", "compact")["summary"] == "trimmed"
    assert host.session_call("live", "set_auto_compaction", {"enabled": False})["ok"]
    assert host.session_call("live", "set_auto_retry", {"enabled": True})["ok"]
    assert host.session_call("live", "get_state")["dumpTools"][0]["name"] == "eval"
    assert host.session_call("live", "handoff")["document"] == "protocol handoff"
    assert host.session_call("live", "stt_status")["progress"] == 42
    assert host.session_call("live", "stt_start")["state"] == "recording"
    assert host.session_call("live", "stt_stop")["text"] == "protocol transcript"
    assert host.session_call("live", "stt_cancel")["state"] == "idle"
    assert emitted[0]["kind"] == "open_url"
    host.shutdown()


def test_dataclass_wire_names_match_every_protocol_key():
    """`_camel` reconstructs Chudcode wire JSON from the client's snake_case dataclasses, so a
    field whose real wire key carries an internal acronym (`has_oauth` -> `hasOAuth`) silently
    disappears from the GUI payload. Pair every `field=_require_x(payload, "wireKey")` in the
    installed protocol module and assert the reconstruction is exact, so the next such field
    fails here instead of reporting an authenticated provider as signed out."""
    protocol = pytest.importorskip("chudcode_rpc.protocol")
    tree = ast.parse(inspect.getsource(protocol))
    pairs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg is None or not isinstance(keyword.value, ast.Call):
                continue
            args = keyword.value.args
            if len(args) < 2:
                continue
            reader, key = args[0], args[1]
            if not isinstance(reader, ast.Name) or reader.id != "payload":
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                pairs.add((keyword.arg, key.value))

    assert ("has_oauth", "hasOAuth") in pairs, "protocol no longer parses hasOAuth"
    mismatched = sorted((field, wire) for field, wire in pairs if _camel(field) != wire)
    assert not mismatched, f"_camel does not reproduce the wire keys: {mismatched}"
    stale = sorted(set(_WIRE_NAME_OVERRIDES) - {field for field, _ in pairs})
    assert not stale, f"overrides for fields the protocol no longer has: {stale}"


class HelperClient(FakeClient):
    """Process-free SDK lifecycle with the real prompt_and_wait result shape."""

    def prompt_and_wait(self, message, *, timeout=None):
        self.prompt(message)
        if not self.release.wait(timeout):
            raise TimeoutError("helper terminal wait timed out")
        if not self.started:
            raise RuntimeError("RPC process stopped")
        for event in self.events:
            for listener in tuple(self.listeners):
                listener(event)
        return type("PromptTurn", (), {"assistant_text": self.factory.reply})()


@pytest.fixture
def helper_runtime(monkeypatch):
    factory = FakeFactory()
    factory.reply = "helper answer"

    def make_client(**kwargs):
        client = HelperClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: None)
    yield host, factory
    host.shutdown()


def test_helper_completion_is_private_and_disposed_without_touching_saved_sessions(helper_runtime):
    host, factory = helper_runtime
    assert host.complete_text("diagnose", "failed feature", provider="gemini", model="model") == "helper answer"
    client = factory.clients[0]
    assert client.started is False and client.stop_count == 1
    assert not Path(client.kwargs["cwd"]).exists()
    assert client.kwargs["append_system_prompt"] == "diagnose"
    assert client.kwargs["tools"] == []
    assert client.kwargs["no_session"] and client.kwargs["no_rules"] and client.kwargs["no_skills"]
    assert ("prompt", {"message": "failed feature", "images": None}) in client.calls
    assert ("set_model", {"provider": "google", "modelId": "model"}) in client.calls
    assert not host._sessions and not host._helper_clients
    host.shutdown()
    assert client.stop_count == 1


@pytest.mark.parametrize("event", [
    {"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error", "errorMessage": "model rejected"}},
    {"type": "auto_retry_end", "success": False, "finalError": "quota exhausted"},
])
def test_helper_does_not_return_partial_text_after_provider_failure(helper_runtime, event):
    host, factory = helper_runtime
    original = host._client_factory

    def make_client(**kwargs):
        client = original(**kwargs)
        client.events.insert(1, event)
        return client

    host._client_factory = make_client
    factory.reply = "partial answer that must not be accepted"
    with pytest.raises(chudcode_host_module.HelperCompletionError):
        host.complete_text("", "repair")
    assert factory.clients[0].stop_count == 1


def test_helper_model_rejection_never_prompts_or_substitutes(helper_runtime):
    host, factory = helper_runtime
    original = host._client_factory

    def make_client(**kwargs):
        client = original(**kwargs)

        def reject(*args):
            raise LookupError("selected model unavailable")

        client.set_model = reject
        return client

    host._client_factory = make_client
    with pytest.raises(chudcode_host_module.HelperCompletionError):
        host.complete_text("", "repair", provider="openai", model="chosen")
    assert factory.clients[0].model_prompt_count == 0
    assert factory.clients[0].stop_count == 1


@pytest.mark.parametrize("ending", ["cancel", "shutdown", "timeout"])
def test_helper_running_call_stops_on_cancel_shutdown_or_budget(helper_runtime, monkeypatch, ending):
    host, factory = helper_runtime
    factory.block = True
    cancel = threading.Event()
    result = {}
    monkeypatch.setattr(chudcode_host_module, "_HELPER_WAIT_SLICE_SECONDS", 0.01)

    def complete():
        try:
            host.complete_text("", "repair", timeout=0.1 if ending == "timeout" else 10,
                               cancel=cancel.is_set)
        except chudcode_host_module.HelperCompletionError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=complete, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not factory.clients and time.monotonic() < deadline:
        time.sleep(0.001)
    assert factory.clients
    client = factory.clients[0]
    assert client.prompted.wait(2)
    if ending == "cancel":
        cancel.set()
    elif ending == "shutdown":
        host.shutdown()
    thread.join(3)
    assert not thread.is_alive()
    assert result["error"]
    assert client.stop_count == 1 and not client.started
    assert not host._helper_clients
    assert not Path(client.kwargs["cwd"]).exists()


def test_helper_prestart_cancel_and_shutdown_do_not_spawn(helper_runtime):
    host, factory = helper_runtime
    with pytest.raises(chudcode_host_module.HelperCompletionError):
        host.complete_text("", "repair", cancel=lambda: True)
    assert not factory.clients
    host.shutdown()
    with pytest.raises(chudcode_host_module.HelperCompletionError):
        host.complete_text("", "repair")
    assert not factory.clients


@pytest.mark.parametrize("reply", [None, "", "   "])
def test_helper_empty_response_is_not_a_success(helper_runtime, reply):
    host, factory = helper_runtime
    factory.reply = reply
    with pytest.raises(chudcode_host_module.HelperCompletionError):
        host.complete_text("", "repair")
    assert factory.clients[0].stop_count == 1


def test_helper_applies_selected_compaction_overlay(helper_runtime, monkeypatch):
    host, factory = helper_runtime
    policies = []
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/chud")
    monkeypatch.setattr(chudcode_host_module, "_binary_supports_flag", lambda *args: True)

    def overlay(memory, settings):
        policies.append((memory, settings))
        return "/fake/overlay.yml"

    monkeypatch.setattr(chudcode_host_module, "_cad_session_overlay", overlay)
    chosen = {"enabled": True, "methodOrder": ["shake"]}
    assert host.complete_text("", "repair", compaction_settings=chosen) == "helper answer"
    assert policies == [("off", chosen)]
    assert factory.clients[0].kwargs["extra_args"] == (
        "--no-lsp", "--config", "/fake/overlay.yml", "--no-mcp")


def test_scoped_online_tool_helper_inherits_exact_model_credentials_and_policy(tmp_path, monkeypatch):
    from services.agent.chudcode_tools import HostToolBinding, dispatch_host_tool
    from services.agent.providers.chudcode_provider import helper_provider

    class Policy:
        def deny_reason(self, name):
            return None

    records = []
    binding = HostToolBinding(
        "repair", "repair", {"type": "object", "properties": {}},
        handler=lambda: {"ok": True, "repair": helper_provider("fixer").complete("", [
            {"role": "user", "content": "repair the selector"}])},
        context={"policy_registry": Policy(), "tool_calls": records})
    factory = FakeFactory()
    factory.reply = "repaired"

    class ParentClient(FakeClient):
        def prompt(self, message, *, images=None):
            super().prompt(message, images=images)
            assert dispatch_host_tool(binding, {}, None, lambda: False)["repair"] == "repaired"

    def make_client(**kwargs):
        client = (HelperClient if kwargs.get("tools") == [] else ParentClient)(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = ChudcodeSessionHost(client_factory=make_client)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-openai-secret")
    try:
        result, events = run(
            host, "byok-helper", host_tools=[binding],
            chudcode_session_dir=str(tmp_path), provider="openai", model="selected-model",
            byok_key="alice-private-token", byok_provider="openai", byok_tenant="alice",
            settings={"compaction_settings": {"methodOrder": ["soft"]},
                      "thinking": "low", "auto_retry": False})
        assert result["ok"]
        helper = factory.clients[1]
        assert helper.kwargs["provider_session_id"] == "byok-helper"
        assert helper.kwargs["env"]["OPENAI_API_KEY"] == "alice-private-token"
        assert helper.kwargs["env"]["ANTHROPIC_API_KEY"] == ""
        assert ("set_model", {"provider": "openai", "modelId": "selected-model"}) in helper.calls
        assert ("set_thinking_level", {"level": "low"}) in helper.calls
        assert ("set_auto_retry", {"enabled": False}) in helper.calls
        assert helper.stop_count == 1
        assert not Path(helper.kwargs["env"]["HOME"]).exists()
        assert not Path(helper.kwargs["cwd"]).exists()
        assert "helper_runtime" not in binding.context
        serialized = json.dumps([result, events, records])
        assert "alice-private-token" not in serialized and "operator-secret" not in serialized
    finally:
        host.shutdown()


def test_helper_unsupported_byok_provider_fails_before_spawning(helper_runtime):
    host, factory = helper_runtime
    with pytest.raises(chudcode_host_module.HelperCompletionError, match="byok_unsupported_online"):
        host.complete_text("", "repair", runtime_params={"byok_key": "sk-test", "byok_provider": "unknown"})
    assert not factory.clients


class QueueingClient(FakeClient):
    """Chudcode's steering queue: a steer waits there until a tool boundary takes it."""

    def __init__(self, factory, **kwargs):
        super().__init__(factory, **kwargs)
        self.queue: list[str] = []

    def steer(self, message, *, images=None):
        super().steer(message, images=images)
        self.queue.append(message)

    def remove_queued_message(self, message, queue):
        self.calls.append(("remove_queued_message", {"message": message, "queue": queue}))
        removed = queue == "steering" and message in self.queue
        if removed:
            self.queue.remove(message)
        return type("Removed", (), {"removed": removed})()

    def boundary(self):
        """Inject every queued steer as its user frame, as the agent loop does."""
        for text in self.queue:
            for listener in tuple(self.listeners):
                listener({"type": "message_start",
                          "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        self.queue.clear()


def _queueing_host(block=True):
    factory = FakeFactory(block=block)

    def make(**kwargs):
        client = QueueingClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    return ChudcodeSessionHost(client_factory=make), factory


def _start(host, factory, session_id, run_id, **extra):
    out = {}
    thread = threading.Thread(
        target=lambda: out.setdefault("value", run(host, session_id, run_id, **extra)),
        daemon=True,
    )
    thread.start()
    while not factory.clients:
        time.sleep(0.001)
    client = factory.clients[-1]
    assert client.prompted.wait(1.0)
    return thread, out, client


def test_stop_withdraws_an_unread_steer_and_the_next_prompt_opens_with_it():
    # c252: a steer queued behind a 15-minute wait_job; Stop aborted, and Chudcode's settle
    # drain answered the steer in a fresh model call nobody observed.
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "c", "r1")
    host.steer("r1", "skip the FEA, just eyeball it", steer_id="s-1")

    cancel = host.cancel("r1")
    thread.join(1.0)

    names = call_names(client)
    assert names.index("remove_queued_message") < names.index("abort")
    assert client.queue == []
    assert cancel["carried_steer_ids"] == ["s-1"]

    client.block = False
    client.prompted.clear()
    client.release.clear()
    client.events = [{"type": "agent_start"},
                     {"type": "agent_end", "messages": [], "isTerminal": True}]
    result, emitted = run(host, "c", "r2", request="also add a roof")
    assert result["ok"] is True
    prompt = [p for n, p in client.calls if n == "prompt"][-1]
    assert prompt["message"] == "skip the FEA, just eyeball it\n\nalso add a roof"
    delivered = [f for f in emitted if f.get("type") == "agent_steer"]
    assert [(f["steer_id"], f["status"]) for f in delivered] == [("s-1", "delivered")]
    # Carried once: a third turn does not repeat it.
    run(host, "c", "r3", request="thanks")
    assert [p for n, p in client.calls if n == "prompt"][-1]["message"] == "thanks"


def test_a_delivered_steer_is_reported_and_stop_leaves_it_alone():
    host, factory = _queueing_host()
    context: dict = {}
    tool = type("Tool", (), {"name": "wait_job", "context": context})()
    thread, out, client = _start(host, factory, "d", "r1", host_tools=[tool])
    pending = context["user_steer_pending"]
    assert pending() is False
    host.steer("r1", "add a roof", steer_id="s-9")
    assert pending() is True          # a pure wait in flight now ends early
    client.boundary()
    assert pending() is False

    cancel = host.cancel("r1")
    thread.join(1.0)
    result, emitted = out["value"]
    assert "remove_queued_message" not in call_names(client)
    assert cancel["carried_steer_ids"] == []
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-9", "delivered")
    ]
    assert "user_steer_pending" not in context


def test_a_steer_still_queued_at_the_turn_end_is_carried_not_left_for_chudcode():
    host, factory = _queueing_host(block=False)

    class LateSteer(QueueingClient):
        def wait_for_idle(self, timeout=None):
            # The steer lands after the loop's last boundary poll.
            host.steer("late", "one more thing", steer_id="s-late")
            return super().wait_for_idle(timeout)

    host = ChudcodeSessionHost(
        client_factory=lambda **kw: factory.clients.append(LateSteer(factory, **kw))
        or factory.clients[-1]
    )
    result, emitted = run(host, "late", "late")
    client = factory.clients[-1]
    assert result["ok"] is True
    assert client.queue == []
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-late", "carried")
    ]
    run(host, "late", "late-2", request="go")
    assert [p for n, p in client.calls if n == "prompt"][-1]["message"] == "one more thing\n\ngo"


def test_unrelated_user_frame_never_acknowledges_tracked_steers():
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "d", "r-unrelated")
    host.steer("r-unrelated", "add a roof", steer_id="s-roof")
    host.steer("r-unrelated", "make it PETG", steer_id="s-material")
    for listener in tuple(client.listeners):
        listener({"type": "message_start", "message": {
            "role": "user", "content": [{"type": "text", "text": "a runtime notice"}]}})
    cancel = host.cancel("r-unrelated")
    thread.join(1.0)
    _, emitted = out["value"]
    assert cancel["carried_steer_ids"] == ["s-roof", "s-material"]
    assert not [f for f in emitted if f.get("type") == "agent_steer"
                and f.get("status") == "delivered"]


def test_same_text_with_distinct_images_is_not_a_duplicate():
    host, factory = _queueing_host()
    thread, _, client = _start(host, factory, "d", "r-images")
    first = host.steer("r-images", "use this", [{"data": "Zmlyc3Q=", "mime": "image/png"}],
                       steer_id="s-first")
    second = host.steer("r-images", "use this", [{"data": "c2Vjb25k", "mime": "image/png"}],
                        steer_id="s-second")
    duplicate = host.steer("r-images", "use this", [{"data": "c2Vjb25k", "mime": "image/png"}],
                           steer_id="s-duplicate")
    assert first["steered"] and second["steered"] and "duplicate" not in second
    assert duplicate["duplicate"]
    assert call_names(client).count("steer") == 2
    host.cancel("r-images")
    thread.join(1.0)


def test_delivery_frame_survives_a_control_exception_after_consumption():
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "d", "r-late-failure")
    send = client.steer

    def consume_then_fail(message, *, images=None):
        send(message, images=images)
        client.boundary()
        raise RuntimeError("response lost after delivery")

    client.steer = consume_then_fail
    result = host.steer("r-late-failure", "add a roof", steer_id="s-consumed")
    assert result["steered"] is False
    host.cancel("r-late-failure")
    thread.join(1.0)
    _, emitted = out["value"]
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-consumed", "delivered")]


def test_delivery_frame_can_arrive_after_the_control_call_has_failed():
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "d", "r-failure-first")
    send = client.steer

    def accept_then_fail(message, *, images=None):
        send(message, images=images)
        raise RuntimeError("response lost before stream")

    client.steer = accept_then_fail
    result = host.steer("r-failure-first", "add a roof", steer_id="s-consumed-late")
    assert result["steered"] is False
    client.boundary()
    host.cancel("r-failure-first")
    thread.join(1.0)
    _, emitted = out["value"]
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-consumed-late", "delivered")]


def _cell_context(actor, workspace="team:fixture/workspace"):
    return {
        "cell": True, "workspace_key": workspace, "actor_key": actor,
        "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
        "right": "editor", "epoch": 1,
    }


@pytest.fixture
def cell_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "actor-data"))
    monkeypatch.setattr(ChudcodeSessionHost, "_resolve_binary", lambda self, required: None)
    return tmp_path


def test_cell_has_no_credential_route_and_never_launches(cell_runtime):
    from services.agent import request_context as tenancy

    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        with tenancy.bind_context(_cell_context("alice")):
            tenancy.actor_state_root().mkdir(parents=True)
            for params in ({}, {"byok_key": "upstream-key-forbidden", "byok_provider": "openai"}):
                result, emitted = run(host, "conversation", "run", **params)
                assert result["code"] == "credential_isolation_unsupported"
                assert not emitted
            with pytest.raises(chudcode_host_module.HelperCompletionError,
                               match="credential_isolation_unsupported"):
                host.complete_text("system", "diagnose", runtime_params={})
        assert not factory.clients
    finally:
        host.shutdown()


def test_cell_prestart_controls_and_client_session_paths_fail_closed(cell_runtime):
    from services.agent import request_context as tenancy

    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    owner_ctx = _cell_context("alice")
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}
    try:
        with tenancy.bind_context(owner_ctx):
            session = tenancy.mint_handle("session")
            run_id = tenancy.mint_handle("run", parent=session)
            assert host.cancel(run_id)["cancelled"]
            result, _ = run(host, session, "path-run",
                            chudcode_session_file="/actor/another-history.jsonl")
            assert result["code"] == "cell_refused"
            delegated_run = tenancy.mint_handle("run", parent=session)
        with tenancy.bind_context({**owner_ctx, "control_delegate": delegate}):
            stopped = host.cancel(delegated_run)
            assert stopped["reason"] == "manager_stop"
            assert stopped["control_delegate"] == delegate
        with tenancy.bind_context(_cell_context("bob")):
            assert host.cancel(run_id)["code"] == "not_found"
        assert not factory.clients
    finally:
        host.shutdown()


def test_online_attachment_refuses_sparse_oversize_before_base64(tmp_path, monkeypatch):
    from services.agent import tool_images
    image = tmp_path / "large.png"
    with image.open("wb") as source:
        source.truncate(tool_images.MAX_SOURCE_BYTES + 1)
    monkeypatch.setattr(chudcode_host_module.base64, "b64encode",
                        lambda data: pytest.fail("oversized file reached base64 allocation"))
    with pytest.raises(ValueError, match="attachment_image_unavailable.*byte limit"):
        chudcode_host_module._prompt_payload("question", [{"path": str(image), "kind": "image"}], None)
    with pytest.raises(tool_images.ImageUnavailable, match="byte limit"):
        tool_images.load_model_image(str(image))


def test_online_inline_attachment_count_and_total_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(chudcode_host_module, "_PROMPT_MAX_SOURCE_BYTES", 6)
    image = {"data": "YWJj", "mimeType": "image/png"}
    assert len(chudcode_host_module._prompt_payload("question", [image, image], None)[1]) == 2
    with pytest.raises(ValueError, match="attachment_image_bytes_exceeded"):
        chudcode_host_module._prompt_payload("question", [image, image, image], None)
    with pytest.raises(ValueError, match="attachment_image_count_exceeded"):
        chudcode_host_module._prompt_payload("question", None, [image] * 9)


def test_image_source_growth_during_read_is_refused(tmp_path, monkeypatch):
    from services.agent import tool_images
    image = tmp_path / "growing.png"
    image.write_bytes(b"small")
    real_fdopen = tool_images.os.fdopen
    class GrowingFile:
        def __init__(self, source):
            self.source = source
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def fileno(self):
            return self.source.fileno()
        def read(self, count):
            assert count == 17
            with image.open("ab") as writer:
                writer.write(b"growing-past-limit")
            return self.source.read(count)
    monkeypatch.setattr(tool_images.os, "fdopen", lambda fd, mode: GrowingFile(real_fdopen(fd, mode)))
    with pytest.raises(tool_images.ImageUnavailable, match="byte limit"):
        tool_images.read_image_bytes(str(image), max_bytes=16)
