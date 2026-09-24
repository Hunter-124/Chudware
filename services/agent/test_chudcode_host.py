from __future__ import annotations

import ast
import inspect
import json
import sys
import threading
import time
import textwrap
from pathlib import Path

import pytest
from services.agent import chudcode_host as chudcode_host_module

from services.agent.chudcode_host import (
    ChudcodeSessionHost,
    _TURN_ABSOLUTE_CAP_SECONDS,
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

    def steer(self, message):
        self.calls.append(("steer", {"message": message}))

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

    def set_custom_tools(self, tools):
        self._custom_tools = tuple(tools)
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
                {"name": "browser", "description": "Browse", "parameters": {}},
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
    assert child_env["CHUDCODE_CODING_AGENT_DIR"].startswith(str(home))

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

def test_web_subscription_pool_and_desktop_process_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "operator-oauth")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-openai")
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    shared = {"chudcode_session_dir": str(tmp_path), "web_actor": True,
              "web_tenant": "tenant@example.test"}
    subscription = {**shared, "web_key_source": "subscription",
                    "web_provider": "anthropic-sub", "web_credential": "tenant-oauth"}
    assert run(host, "sub", "sub-1", **subscription)[0]["ok"]
    sub_env = factory.clients[0].kwargs["env"]
    assert sub_env["ANTHROPIC_OAUTH_TOKEN"] == "tenant-oauth"
    assert sub_env["ANTHROPIC_API_KEY"] == ""
    assert sub_env["OPENAI_API_KEY"] == ""
    assert factory.clients[0].kwargs["session_dir"].startswith(str(tmp_path / "subscription-"))
    assert not run(host, "sub", "sub-switch", **{**subscription, "web_credential": "different"})[0]["ok"]
    assert not run(host, "codex", "codex-1", **{**subscription, "web_provider": "openai-codex"})[0]["ok"]
    assert not run(host, "invalid", "invalid-1", web_actor=True)[0]["ok"]
    pool = {**shared, "web_key_source": "pool", "web_provider": "openrouter",
            "web_credential": "operator-pool"}
    assert run(host, "pool", "pool-1", **pool)[0]["ok"]
    pool_env = factory.clients[1].kwargs["env"]
    assert pool_env["OPENROUTER_API_KEY"] == "operator-pool"
    assert pool_env["ANTHROPIC_API_KEY"] == ""
    assert pool_env["ANTHROPIC_OAUTH_TOKEN"] == ""
    assert factory.clients[1].kwargs["session_dir"].startswith(str(tmp_path / "pool-"))
    assert run(host, "desktop", "desktop-1")[0]["ok"]
    assert "env" not in factory.clients[2].kwargs
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

        def steer(self, message):
            if self.fail_steer:
                self.fail_steer = False
                self.calls.append(("steer", {"message": message}))
                raise LookupError("steer connection interrupted")
            super().steer(message)

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
    assert call_names(client)[1:9] == [
        "set_model",
        "set_thinking_level",
        "set_steering_mode",
        "set_follow_up_mode",
        "set_interrupt_mode",
        "set_auto_compaction",
        "set_auto_retry",
        "set_subagent_subscription",
    ]
    assert "set_host_tools" in call_names(client)
    assert "set_host_uris" in call_names(client)


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
    assert "claude-sonnet-5 is not in Chudcode's anthropic catalog" in stale["reason"]
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


def test_missing_dependency_and_binary_are_actionable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "chudcode_rpc", None)
    missing_dep = ChudcodeSessionHost()
    result = missing_dep.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "chudcode-rpc==0.2.0" in result["reason"]

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


def test_turn_budget_is_explicit_and_not_the_rpc_client_short_wait_default():
    """Passing no timeout inherits chudcode-rpc's 60s `_wait_for_agent_end` default, which
    killed every real modeling turn mid-build with `Timed out waiting for agent_end` and an
    empty stderr. The host must name its own budget; a caller-supplied timeout still wins."""
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        result, _events = run(host, "budget")
        assert result["ok"] is True
        assert factory.clients[0].idle_timeouts == [_TURN_ABSOLUTE_CAP_SECONDS]
        assert _TURN_ABSOLUTE_CAP_SECONDS > 60.0

        run(host, "budget", run_id="run-budget-2", timeout=12)
        assert factory.clients[0].idle_timeouts[-1] == 12.0
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
                    respond(request_id, kind, {})
                    time.sleep(0.02)
                    print(json.dumps({"type": "agent_start"}), flush=True)
                    print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
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
                        "dumpTools": [{"name": "browser", "description": "Browse", "parameters": {"type": "object"}}],
                    })
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
    assert host.session_call("live", "get_state")["dumpTools"][0]["name"] == "browser"
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
