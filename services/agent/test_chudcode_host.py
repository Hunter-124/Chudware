from __future__ import annotations

import sys
import threading
import time
import textwrap

import pytest

from services.agent.chudcode_host import (
    ChudcodeSessionHost,
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
        self.calls.append(("prompt", {"message": message, "images": images}))
        self.prompted.set()
        if not self.block:
            self.release.set()

    def wait_for_idle(self, timeout=None):
        if not self.release.wait(timeout if timeout is not None else 2.0):
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


def test_auth_and_memory_calls_use_only_canonical_command_shapes_and_stop_clients():
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
    assert factory.clients[0].kwargs["no_session"] is True
    assert factory.clients[1].kwargs["no_session"] is True
    assert [client.stop_count for client in factory.clients] == [1, 1]
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
    assert ("cancel_ui_request", {"id": "input"}) in factory.clients[2].calls
    assert [client.stop_count for client in factory.clients] == [1, 1, 1]


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

    assert len(factory.clients) == count
    assert stats["sessionId"] == factory.clients[0].session_id
    assert compact["summary"] == "trimmed"
    assert auto_compact == {"ok": True, "enabled": False}
    assert auto_retry == {"ok": True, "enabled": True}
    assert state["_hostToolNames"] == ["cad_tool"]
    assert "get_session_stats" in call_names(factory.clients[0])
    assert "compact" not in call_names(factory.clients[0])
    assert "compact" in call_names(factory.clients[1])


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

            print(json.dumps({"type": "ready", "protocolVersion": 2}), flush=True)
            for line in sys.stdin:
                command = json.loads(line)
                kind = command["type"]
                request_id = command.get("id")
                if kind == "get_login_providers":
                    respond(request_id, kind, {"providers": [{"id": "openai", "name": "OpenAI", "available": True, "authenticated": False}]})
                elif kind == "get_available_models":
                    respond(request_id, kind, {"models": [model()]})
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
    assert emitted[0]["kind"] == "open_url"
    host.shutdown()
