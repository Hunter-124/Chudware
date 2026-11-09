from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
import hashlib

import pytest
from services.agent import omp_host as omp_host_module
from services.agent.omp_bridge import ExtensionBridge as RealExtensionBridge

from services.agent.omp_host import (
    OmpSessionHost,
    _set_default_omp_host_for_tests,
    get_default_omp_host,
)

#: A decodable 1x1 PNG: composer images are decoded and checked before they are sent.
_PNG_1PX_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmQsAAAAASUVORK5CYII="
_PNG_1PX = base64.b64decode(_PNG_1PX_B64)


class FakeBridge:
    """Exercise host integration without starting an extension process."""
    def __init__(self, runtime_dir, dispatch):
        self.dispatch = dispatch
        self.definitions = []
        self.policy = {}
        self.operations = []
        self.keyless_providers = []
    def start(self):
        return self
    def info(self):
        return {"transport": "unix", "endpoint": "/private/test.sock", "token": "fixture"}
    def publish_tools(self, definitions):
        self.definitions = list(definitions)
    def publish_keyless_providers(self, providers):
        self.keyless_providers = list(providers)
    def wait_tools(self, *, timeout):
        return None
    def call(self, operation, arguments, *, timeout, cancel_event=None):
        self.operations.append((operation, dict(arguments)))
        if operation == "settings_set":
            self.policy = dict(arguments["compaction"])
            return {"compaction": dict(self.policy)}
        if operation == "settings_get":
            return {"compaction": dict(self.policy)}
        if operation == "tool_state":
            return {"tools": [{"name": item["name"], "available": True, "builtin": False,
                               "description": item["description"]} for item in self.definitions]}
        if operation == "compact":
            for listener in tuple(self.client.listeners):
                listener({"type": "manual_compaction_start", "mode": arguments.get("mode")})
            result = self.client.compact(arguments.get("instructions"))
            for listener in tuple(self.client.listeners):
                listener({"type": "manual_compaction_end", "aborted": False, "result": result})
            return {"completed": True, "result": result, "method": arguments.get("mode", "soft"),
                    "contextBefore": {"tokens": 100, "estimated": False},
                    "contextAfter": {"tokens": 25, "estimated": True}}
        if operation == "handoff":
            return {"document": "fixture handoff"}
        if operation == "setup_providers":
            return {"providers": [{"id": "openai", "name": "OpenAI", "authKinds": ["api_key"]}]}
        if operation == "auth_state":
            return {"provider": arguments["provider"], "configured": True, "oauth": False}
        if operation == "auth_states":
            return {"providers": [{"provider": p, "configured": True, "oauth": False}
                                  for p in arguments["providers"]]}
        if operation in {"set_api_key", "remove_api_key"}:
            return {"provider": arguments["provider"], "configured": operation == "set_api_key", "oauth": False}
        if operation == "memory_search":
            return {"results": []}
        if operation == "memory_status":
            return {"backend": "off"}
        if operation == "memory_save":
            return {"saved": True}
        raise ValueError(operation)
    def stop(self):
        pass


@pytest.fixture(autouse=True)
def fake_omp_bridge(monkeypatch):
    monkeypatch.setattr("services.agent.omp_bridge.ExtensionBridge", FakeBridge)

class StubBinding:
    def __init__(self, name, context=None):
        self.name = name
        self.context = context if context is not None else {}

    def as_dict(self):
        return {"name": self.name, "description": "", "input_schema": {},
                "load_mode": "essential"}

    def to_host_tool(self, factory):
        return factory(name=self.name, execute=lambda args, ctx: {"content": []})


class FakeClient:
    def __init__(self, factory, **kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.started = False
        self.stop_count = 0
        self.listeners = []
        self.ui_listeners = []
        self.calls = []
        self.model_prompt_count = 0
        self.prompted = threading.Event()
        self.release = threading.Event()
        self.compact_started = threading.Event()
        self.block = factory.block
        self.events = [
            {"type": "agent_start"},
            {"type": "agent_end", "messages": [], "isTerminal": True},
        ]
        self._protocol_version = factory.protocol_version
        self.session_id = f"cc-{len(factory.clients) + 1}"
        self.session_file = f"/sessions/{self.session_id}.jsonl"
        self.model = {"provider": "fixture", "id": "fixture-model"}
        self.idle_timeouts: list[float | None] = []

    def start(self):
        self.started = True
        self.calls.append(("start", {}))
        self._omp_bridge.client = self
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
        return "fake-prompt"

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

    def wait_for_settled(self, timeout=None):
        self.wait_for_idle(timeout=timeout)
        status = "aborted" if any(e.get("aborted") for e in self.events) else "completed"
        event = {"type": "prompt_result", "id": "fake-prompt", "status": status,
                 "sessionSettled": True}
        for listener in tuple(self.listeners):
            listener(event)

    def abort_and_restore_queue(self):
        self.abort()
        return {"steering": [], "followUp": []}

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
        self.model = {"provider": provider, "id": model_id}

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


    def set_host_uris(self, uris):
        self.calls.append(("set_host_uris", {"uris": uris}))

    def request_raw(self, command, **payload):
        if command == "get_available_models":
            return {"models": list(self.get_available_models())}
        self.calls.append((command, payload))
        return {"command": command, **payload}

    def get_state(self):
        return {
            "sessionId": self.session_id,
            "sessionFile": self.session_file,
            "model": dict(self.model),
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
        self.compact_started.set()
        if self.block:
            # A long summary: only abort() ends it, as OMP cancels the compaction.
            self.release.wait(2.0)
            from omp_rpc import RpcCommandError

            raise RpcCommandError(command="compact", error="Compaction cancelled", code=None)
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


def test_terminal_reports_touched_rolled_back_parts(monkeypatch):
    from services.mcp import cad_common

    warning = {"doc_id": "part1", "after": "f1", "hidden_features": ["f7", "f8"],
               "warning": "WARNING: part1 is rolled back after f1; unbuilt features: f7, f8"}
    seen = []
    monkeypatch.setattr(cad_common, "touched_rollback_warnings",
                        lambda run_id: seen.append(run_id) or [warning])
    host = OmpSessionHost(client_factory=FakeFactory())
    try:
        result, emitted = run(host, "rollback", "rollback-run")
        assert result["ok"] and seen == ["rollback-run"]
        assert result["rollback_warnings"] == [warning]
        assert warning["warning"] in result["result"]
        notice = next(i for i, frame in enumerate(emitted)
                      if frame.get("type") == "assistant_delta"
                      and "Chudware safety notice" in frame.get("text", ""))
        settled = next(i for i, frame in enumerate(emitted)
                       if frame.get("type") in ("prompt_result", "session_settled")
                       and (frame.get("sessionSettled") or frame.get("type") == "session_settled"))
        assert notice < settled
        assert "f7" in emitted[notice]["text"] and "f8" in emitted[notice]["text"]
    finally:
        host.shutdown()


def call_names(client):
    return [name for name, _payload in client.calls]


def test_byok_process_isolated_and_live_session_rejects_credential_switch(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-secret")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    scoped = {
        "byok_provider": "openai",
        "byok_tenant": "alice@example.com",
        "byok_key": "sk-alice-only",
        "omp_session_dir": str(tmp_path),
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
        omp_session_file=str(foreign),
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
    host = OmpSessionHost(client_factory=factory)

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
    host = OmpSessionHost(client_factory=factory)
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
    host = OmpSessionHost(client_factory=factory)
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

    host = OmpSessionHost(client_factory=make_client)
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
    host = OmpSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1", omp_session_file=str(persisted))[0]["ok"]
        assert host.release_session("a") == {"ok": True, "released": True}
        assert factory.clients[0].stop_count == 1

        second, _ = run(host, "a", "a2", omp_session_file=str(persisted))

        assert second["ok"]
        assert len(factory.clients) == 2
        assert factory.clients[0].model_prompt_count == 1
        assert ("switch_session", {"sessionPath": str(persisted)}) in factory.clients[1].calls
        assert second["omp_session_file"] == str(persisted)
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

    host = OmpSessionHost(client_factory=make_client)
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


def test_release_does_not_acknowledge_or_resume_while_prior_release_is_stopping():
    """A timed-out release control may still be flushing the previous journal."""
    factory = FakeFactory()
    stopping = threading.Event()
    finish_stop = threading.Event()

    class BlockingStopClient(FakeClient):
        def stop(self):
            stopping.set()
            if not finish_stop.wait(3.0):
                raise TimeoutError("fake stop was not released")
            super().stop()

    def make_client(**kwargs):
        client = BlockingStopClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = OmpSessionHost(client_factory=make_client)
    output = {}
    release = threading.Thread(
        target=lambda: output.setdefault("release", host.release_session("a")),
        daemon=True,
    )
    try:
        assert run(host, "a", "a1")[0]["ok"]
        release.start()
        assert stopping.wait(1.0)
        assert host.release_session("a") == {"ok": False, "reason": "session_busy"}
        blocked, _ = run(host, "a", "a2")
        assert not blocked["ok"] and "closing" in blocked["reason"]
        assert len(factory.clients) == 1
        assert run(host, "b", "b1")[0]["ok"]
        finish_stop.set()
        release.join(1.0)
        assert output["release"] == {"ok": True, "released": True}
        resumed, _ = run(host, "a", "a3")
        assert resumed["ok"] and len(factory.clients) == 3
    finally:
        finish_stop.set()
        if release.ident is not None:
            release.join(1.0)
        host.shutdown()


def test_failed_release_keeps_runtime_unavailable_for_transfer():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1")[0]["ok"]
        client = factory.clients[0]
        stop = client.stop
        client.stop = lambda: (_ for _ in ()).throw(RuntimeError("stop failed"))
        with pytest.raises(RuntimeError, match="stop failed"):
            host.release_session("a")
        assert host.release_session("a") == {"ok": False, "reason": "session_busy"}
        blocked, _ = run(host, "a", "a2")
        assert not blocked["ok"] and "closing" in blocked["reason"]
        assert len(factory.clients) == 1
        client.stop = stop
    finally:
        host.shutdown()


def test_evicted_session_cannot_transfer_until_its_runtime_has_stopped(monkeypatch):
    monkeypatch.setenv("CHUDWARE_OMP_MAX_SESSIONS", "1")
    factory = FakeFactory()
    stopping = threading.Event()
    finish_stop = threading.Event()

    class BlockingStopClient(FakeClient):
        def stop(self):
            stopping.set()
            if not finish_stop.wait(3.0):
                raise TimeoutError("fake stop was not released")
            super().stop()

    def make_client(**kwargs):
        client = BlockingStopClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = OmpSessionHost(client_factory=make_client)
    output = {}
    other = threading.Thread(target=lambda: output.setdefault("b", run(host, "b", "b1")),
                             daemon=True)
    try:
        assert run(host, "a", "a1")[0]["ok"]
        other.start()
        assert stopping.wait(1.0)
        assert host.release_session("a") == {"ok": False, "reason": "session_busy"}
        blocked, _ = run(host, "a", "a2")
        assert not blocked["ok"] and "closing" in blocked["reason"]
        assert len(factory.clients) == 1
        finish_stop.set()
        other.join(1.0)
        assert not other.is_alive() and output["b"][0]["ok"]
        assert host.release_session("a") == {"ok": True, "released": False}
    finally:
        finish_stop.set()
        if other.ident is not None:
            other.join(1.0)
        host.shutdown()


@pytest.mark.parametrize("compaction", [False, True])
def test_private_control_stop_before_runtime_registration_prevents_start(monkeypatch, compaction):
    from services.agent import worker_rpc
    from services.worker_main import _invoke_omp_control

    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    monkeypatch.setattr(worker_rpc, "get_default_omp_host", lambda: host)
    try:
        assert _invoke_omp_control("cancel", {"run_id": "early"})["cancelled"] is True
        params = {"session_id": "a", "run_id": "early", "request": "work"}
        if compaction:
            result = host.run_compaction({**params, "compact": {}}, lambda _: None)
            assert result["outcome"] == "cancelled"
        else:
            result = host.run_turn(params, lambda _: None)
        assert result["cancelled"] is True
        assert not factory.clients
    finally:
        host.shutdown()


def test_prestart_stop_keeps_the_supplied_credential_scope(tmp_path):
    host = OmpSessionHost(client_factory=FakeFactory())
    scoped = {"byok_provider": "openai", "byok_tenant": "alice",
              "byok_key": "alice-key", "omp_session_dir": str(tmp_path)}
    try:
        assert host.cancel("wrong-key", credential_params={**scoped, "byok_key": "other-key"},
                           allow_prestart=True)["cancelled"]
        accepted, _ = run(host, "a", "wrong-key", **scoped)
        assert accepted["ok"] and not accepted.get("cancelled", False)
        assert host.cancel("right-key", credential_params=scoped, allow_prestart=True)["cancelled"]
        stopped, _ = run(host, "a", "right-key", **scoped)
        assert stopped["cancelled"]
        assert host.cancel("untrusted-unknown")["cancelled"] is False
    finally:
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
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(RecyclingClient(factory, **kw))
        or factory.clients[-1]
    )

    result, emitted = run(host, "s", "s1")

    assert result["ok"]
    ends = [f for f in emitted if f.get("type") == "tool_execution_end"]
    assert [f["toolName"] for f in ends] == calls
    assert [f["toolCallId"] for f in ends] == [f"toolu_{name}" for name in calls]


@pytest.mark.parametrize("queue_limit", [2, 256])
def test_slow_event_emit_does_not_block_reader_control_and_turn_flushes(monkeypatch, queue_limit):
    """Exercise control replies on the very thread that dispatches notifications."""
    monkeypatch.setattr(omp_host_module, "_EVENT_QUEUE_LIMIT", queue_limit)
    entered = threading.Event()
    release_emit = threading.Event()
    control_done = threading.Event()
    turn_done = threading.Event()
    emitted = []
    output = {}
    forwarding_threads = []

    class ReaderClient(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.inbox = queue.Queue()
            self.settled = threading.Event()
            self.queued = threading.Event()
            self.reader = None

        def prompt(self, message, *, images=None):
            answer = super().prompt(message, images=images)
            self.reader = threading.Thread(target=self.read_frames, name="fake-rpc-reader")
            self.reader.start()
            for frame in [
                {"type": "agent_start"},
                {"type": "message_update", "messageId": "m", "message": {},
                 "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                           "delta": "think"}},
                {"type": "message_update", "messageId": "m", "message": {},
                 "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                           "delta": "ing"}},
                {"type": "message_update", "messageId": "m", "message": {},
                 "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                           "delta": " more"}},
                {"type": "message_update", "messageId": "m", "message": {},
                 "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0,
                                           "delta": "!"}},
                {"type": "message_update", "messageId": "m", "message": {},
                 "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1,
                                           "delta": "answer"}},
            ]:
                self.inbox.put(("event", frame))
            self.queued.set()
            return answer

        def read_frames(self):
            while True:
                kind, value = self.inbox.get()
                if kind == "stop":
                    return
                if kind == "state":
                    value.set()
                    continue
                frames = (
                    [
                        {"type": "agent_end", "messages": [], "isTerminal": True},
                        {"type": "prompt_result", "id": "fake-prompt", "status": "completed",
                         "sessionSettled": True},
                    ]
                    if kind == "settle" else [value]
                )
                for frame in frames:
                    for listener in tuple(self.listeners):
                        listener(frame)
                if kind == "settle":
                    self.settled.set()

        def get_state(self):
            if self.prompted.is_set():
                done = threading.Event()
                self.inbox.put(("state", done))
                if not done.wait(2):
                    raise TimeoutError("fake reader did not process get_state")
            return super().get_state()

        def wait_for_settled(self, timeout=None):
            if not self.settled.wait(3):
                raise TimeoutError("fake reader did not settle")

        def stop(self):
            super().stop()
            if self.reader is not None:
                self.inbox.put(("stop", None))
                self.reader.join(2)

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(ReaderClient(factory, **kw))
        or factory.clients[-1]
    )

    def emit(frame):
        forwarding_threads.append(threading.current_thread())
        if frame["type"] == "message_update" and not entered.is_set():
            entered.set()
            assert release_emit.wait(3)
            time.sleep(0.02)
        emitted.append(frame)

    def turn():
        output["turn"] = host.run_turn(
            {"request": "Work", "session_id": "reader", "run_id": "reader-run"}, emit
        )
        turn_done.set()

    turn_thread = threading.Thread(target=turn)
    control_thread = None
    try:
        turn_thread.start()
        assert entered.wait(2)
        client = factory.clients[0]
        assert client.queued.wait(1)

        def get_state():
            output["state"] = host.session_call("reader", "get_state")
            control_done.set()

        control_thread = threading.Thread(target=get_state)
        control_thread.start()
        assert control_done.wait(1), "GUI emit blocked the RPC response reader"
        assert output["state"]["sessionId"] == client.session_id
        if queue_limit == 256:
            client.inbox.put(("settle", None))
            assert client.settled.wait(1)
        assert not turn_done.is_set(), "turn returned before its queued events were emitted"
        release_emit.set()
        if queue_limit == 2:
            client.inbox.put(("settle", None))
            assert client.settled.wait(1)
        turn_thread.join(3)
        assert not turn_thread.is_alive()
        assert output["turn"]["ok"]
        assert output["turn"]["terminal"]["type"] == "prompt_result"
        types = [frame["type"] for frame in emitted if frame["type"] != "session_bound"]
        assert types[0] == "agent_start"
        assert types[-2:] == ["agent_end", "prompt_result"]
        assert all(kind == "message_update" for kind in types[1:-2])
        deltas = [frame["assistantMessageEvent"] for frame in emitted
                  if frame["type"] == "message_update"]
        kinds = [delta["type"] for delta in deltas]
        assert kinds == sorted(kinds, reverse=True)  # thinking precedes text
        assert "".join(delta["delta"] for delta in deltas if delta["type"] == "thinking_delta") == "thinking more!"
        assert "".join(delta["delta"] for delta in deltas if delta["type"] == "text_delta") == "answer"
        event_threads = {thread for thread in forwarding_threads if thread.name == "omp-event-forwarder"}
        assert len(event_threads) == 1
        assert client.reader not in event_threads
        assert host.close_session("reader") == {"ok": True}
        assert all(not thread.is_alive() for thread in event_threads)
    finally:
        release_emit.set()
        if turn_thread.ident is not None:
            turn_thread.join(4)
        if control_thread is not None and control_thread.ident is not None:
            control_thread.join(4)
        host.shutdown()


def test_slow_consumer_does_not_delay_another_sessions_forwarder():
    entered, release = threading.Event(), threading.Event()
    output = {}

    class QuickClient(FakeClient):
        def wait_for_settled(self, timeout=None):
            for frame in [
                {"type": "agent_start"},
                {"type": "prompt_result", "id": "fake-prompt", "status": "completed",
                 "sessionSettled": True},
            ]:
                for listener in tuple(self.listeners):
                    listener(frame)

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(QuickClient(factory, **kw))
        or factory.clients[-1]
    )

    def slow_emit(frame):
        if frame["type"] == "agent_start":
            entered.set()
            assert release.wait(3)

    turn = threading.Thread(target=lambda: output.setdefault(
        "slow", host.run_turn({"request": "Work", "session_id": "slow", "run_id": "slow"},
                              slow_emit)
    ))
    try:
        turn.start()
        assert entered.wait(2)
        result, events = run(host, "fast")
        assert result["ok"]
        assert any(frame["type"] == "prompt_result" for frame in events)
        assert turn.is_alive()
        release.set()
        turn.join(3)
        assert not turn.is_alive() and output["slow"]["ok"]
    finally:
        release.set()
        if turn.ident is not None:
            turn.join(4)
        host.shutdown()


@pytest.mark.parametrize("kind", ["text_delta", "thinking_delta"])
@pytest.mark.parametrize("shape", ["mapping", "sdk", "subagent"])
def test_bounded_event_queue_coalesces_only_consecutive_same_block(monkeypatch, kind, shape):
    from omp_rpc._wire import MessageUpdateEvent, SubagentEvent, SubagentEventPayload

    monkeypatch.setattr(omp_host_module, "_EVENT_QUEUE_LIMIT", 2)
    entered = threading.Event()
    release = threading.Event()
    emitted = []
    errors = []

    def emit(frame, _received_at):
        if frame == {"type": "gate"}:
            entered.set()
            assert release.wait(3)
        emitted.append(omp_host_module._json_value(frame))

    def delta(text, message_id="m", index=0):
        update = {"type": kind, "contentIndex": index, "delta": text,
                  "partial": {"latest": text}}
        if shape == "mapping":
            return {"type": "message_update", "messageId": message_id,
                    "message": {"latest": text}, "assistantMessageEvent": update}
        event = MessageUpdateEvent(message={"latest": text}, message_id=message_id,
                                   assistant_message_event=update)
        if shape == "subagent":
            return SubagentEvent(payload=SubagentEventPayload(id="child", event=event))
        return event

    forwarder = omp_host_module._EventForwarder(emit, errors)
    blocked = None
    try:
        assert forwarder.put({"type": "gate"})
        assert entered.wait(1)
        assert forwarder.put(delta("a"))
        assert forwarder.put(delta("b"))
        assert forwarder.put(delta("c"))
        # Compact the already queued consecutive pair to make room for a boundary.
        assert forwarder.put({"type": "boundary"})
        accepted = threading.Event()

        def put_other_block():
            assert forwarder.put(delta("d", message_id="other", index=1))
            accepted.set()

        blocked = threading.Thread(target=put_other_block)
        blocked.start()
        assert not accepted.wait(0.05)  # no loss or cross-boundary merge
        release.set()
        assert accepted.wait(2)
        forwarder.close()
        assert not forwarder.thread.is_alive()
        assert not errors
        assert emitted[0] == {"type": "gate"}
        assert emitted[2] == {"type": "boundary"}
        updates = [emitted[1], emitted[3]]
        if shape == "subagent":
            updates = [frame["payload"]["event"] for frame in updates]
        assert [frame["assistantMessageEvent"]["delta"] for frame in updates] == ["abc", "d"]
        assert updates[0]["assistantMessageEvent"]["partial"] == {"latest": "c"}
        assert updates[0]["messageId"] == "m"
        assert updates[1]["messageId"] == "other"
    finally:
        release.set()
        if blocked is not None and blocked.ident is not None:
            blocked.join(3)
        forwarder.close()


@pytest.mark.parametrize("different", ["message", "block", "kind", "owner", "metadata"])
def test_full_event_queue_never_merges_unrelated_deltas(monkeypatch, different):
    monkeypatch.setattr(omp_host_module, "_EVENT_QUEUE_LIMIT", 1)
    entered, release, accepted = threading.Event(), threading.Event(), threading.Event()
    emitted, errors = [], []

    def delta(text, *, message="m", block=0, kind="thinking_delta", owner="child", extra=None):
        child = {"type": kind, "contentIndex": block, "delta": text}
        if extra:
            child.update(extra)
        return {"type": "subagent_event", "payload": {"id": owner, "event": {
            "type": "message_update", "messageId": message, "message": {},
            "assistantMessageEvent": child,
        }}}

    def emit(frame, _received_at):
        if frame == {"type": "gate"}:
            entered.set()
            assert release.wait(3)
        emitted.append(frame)

    changed = {
        "message": {"message": "other"},
        "block": {"block": 1},
        "kind": {"kind": "text_delta"},
        "owner": {"owner": "other"},
        "metadata": {"extra": {"opaque": "preserve"}},
    }[different]
    first, second = delta("a"), delta("b", **changed)
    forwarder = omp_host_module._EventForwarder(emit, errors)
    writer = None
    try:
        forwarder.put({"type": "gate"})
        assert entered.wait(1)
        forwarder.put(first)

        def put_second():
            assert forwarder.put(second)
            accepted.set()

        writer = threading.Thread(target=put_second)
        writer.start()
        assert not accepted.wait(0.05)
        release.set()
        assert accepted.wait(2)
        forwarder.close()
        assert emitted == [{"type": "gate"}, first, second]
        assert not errors
    finally:
        release.set()
        if writer is not None and writer.ident is not None:
            writer.join(3)
        forwarder.close()


def test_closing_active_session_stops_event_forwarder():
    entered = threading.Event()
    release_emit = threading.Event()
    worker_threads = []
    output = {}

    class ClosingClient(FakeClient):
        def wait_for_settled(self, timeout=None):
            for listener in tuple(self.listeners):
                listener({"type": "agent_start"})
            assert self.release.wait(3)
            for listener in tuple(self.listeners):
                listener({"type": "prompt_result", "id": "fake-prompt", "status": "aborted",
                          "sessionSettled": True})

    factory = FakeFactory(block=True)
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(ClosingClient(factory, **kw))
        or factory.clients[-1]
    )

    def emit(frame):
        if frame["type"] == "agent_start":
            worker_threads.append(threading.current_thread())
            entered.set()
            assert release_emit.wait(3)

    turn = threading.Thread(target=lambda: output.setdefault(
        "result", host.run_turn({"request": "Work", "session_id": "closing",
                                 "run_id": "closing-run"}, emit)
    ))
    try:
        turn.start()
        assert entered.wait(2)
        assert host.close_session("closing") == {"ok": True}
        assert turn.is_alive()
        release_emit.set()
        turn.join(3)
        assert not turn.is_alive()
        assert worker_threads and all(not thread.is_alive() for thread in worker_threads)
        assert len(factory.clients[0].listeners) == 1
        assert isinstance(factory.clients[0].listeners[0].__self__, omp_host_module._DictationActivity)
    finally:
        release_emit.set()
        if turn.ident is not None:
            turn.join(4)
        host.shutdown()


def test_lifecycle_before_prompt_response_keeps_event_order():
    class EarlyLifecycle(FakeClient):
        def prompt(self, message, *, images=None):
            for frame in [
                {"type": "agent_start"},
                {"type": "prompt_result", "id": "fake-prompt", "status": "completed"},
                {"type": "session_settled"},
                {"type": "after_settlement"},
            ]:
                for listener in tuple(self.listeners):
                    listener(frame)
            return super().prompt(message, images=images)

        def wait_for_settled(self, timeout=None):
            pass

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(EarlyLifecycle(factory, **kw))
        or factory.clients[-1]
    )
    try:
        result, emitted = run(host, "early")
        assert result["ok"]
        assert result["terminal"]["type"] == "session_settled"
        assert [frame["type"] for frame in emitted if frame["type"] != "session_bound"] == [
            "agent_start", "prompt_result", "session_settled", "after_settlement",
        ]
    finally:
        host.shutdown()


def test_event_emit_failure_is_reported_after_remaining_events_flush():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    emitted = []

    def emit(frame):
        if frame["type"] == "agent_end":
            raise ValueError("GUI transport failed")
        emitted.append(frame)

    try:
        result = host.run_turn({"request": "Work", "session_id": "emit-error",
                                "run_id": "emit-error-run"}, emit)
        assert not result["ok"]
        assert result["stop_reason"] == "runtime_error"
        assert "GUI transport failed" in result["reason"]
        assert any(frame["type"] == "prompt_result" for frame in emitted)
    finally:
        host.shutdown()


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
    host = OmpSessionHost(
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
    assert result["terminal"]["status"] == "completed"

def test_control_timeout_is_runtime_error(monkeypatch):
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)

    def timeout(*_args):
        raise omp_host_module._BoundedCallTimeout("OMP control request timed out")

    monkeypatch.setattr(host, "_configure_turn", timeout)
    try:
        result, _emitted = run(host, "timeout", "r-timeout")
        assert result["ok"] is False
        assert result["stop_reason"] == "runtime_error"
        assert result["reason"] == "OMP control request timed out"
    finally:
        host.shutdown()



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
    from services.agent.omp_tools import (
        HostToolBinding, build_host_tools, dispatch_host_tool,
    )

    rpc = pytest.importorskip("omp_rpc")
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
    host = OmpSessionHost(
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
        cached_tool = binding.to_host_tool(rpc.host_tool)
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


def _nested_feed_run(script, handler=None):
    from services.agent.omp_bridge import _ToolContext
    from services.agent.omp_events import RpcFrameState, map_rpc_frame
    from services.agent.omp_tools import HostToolBinding

    binding = HostToolBinding(
        name="add_features",
        description="add features",
        input_schema={"type": "object"},
        handler=handler or (lambda doc_id, ops: {
            "ok": True, "doc_id": doc_id, "revision": len(ops),
        }),
        context={"tool_call_counts": {}, "tool_calls": []},
    )

    def call(client, call_id, *, agent_id="Main", parent_agent_id=None, doc_id="doc-5"):
        context = _ToolContext({
            "tool_call_id": call_id, "agent_id": agent_id,
            "parent_agent_id": parent_agent_id, "session_id": client.session_id,
        })
        return client._omp_bridge.dispatch(
            "add_features", {"doc_id": doc_id, "ops": [{"op": "fillet"}]}, context
        )

    def emit_all(client, *events):
        for event in events:
            for listener in tuple(client.listeners):
                listener(event)

    class EvalBuilds(FakeClient):
        def wait_for_idle(self, timeout=None):
            emit_all(self, {"type": "agent_start"})
            script(self, emit_all, call)
            emit_all(self, {"type": "agent_end", "messages": [], "isTerminal": True})
            return ()

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(EvalBuilds(factory, **kw))
        or factory.clients[-1]
    )
    try:
        result, emitted = run(host, "evalbuild", host_tools=[binding])
        assert result["ok"] is True
    finally:
        host.shutdown()
    assert "host_call_observer" not in binding.context
    state = RpcFrameState()
    mapped = [item for frame in emitted for item in map_rpc_frame(frame, {"run_id": "r"}, state)]
    return emitted, mapped


def _nested_child(agent_id, event):
    return {"type": "subagent_event", "payload": {"id": agent_id, "event": event}}


def test_cad_calls_made_inside_eval_get_their_own_feed_rows_once():
    def script(client, emit, call):
        emit(client, {"type": "tool_execution_start", "toolCallId": "eval-1",
                      "toolName": "eval", "args": {"code": "tool.write(...)"}})
        assert call(client, "js-write-1")["details"]["ok"] is True
        assert call(client, "js-write-2")["details"]["ok"] is True
        emit(client, {"type": "tool_execution_end", "toolCallId": "eval-1",
                      "toolName": "eval", "result": {"content": []}},
             {"type": "tool_execution_start", "toolCallId": "w-1",
              "toolName": "write", "args": {"path": "xd://add_features", "content": "{}"}})
        call(client, "w-1")
        emit(client, {"type": "tool_execution_end", "toolCallId": "w-1",
                      "toolName": "add_features", "result": {"content": []}},
             {"type": "tool_execution_start", "toolCallId": "js-write-direct",
              "toolName": "write", "args": {"path": "xd://add_features", "content": "{}"}})
        call(client, "js-write-direct")
        emit(client, {"type": "tool_execution_end", "toolCallId": "js-write-direct",
                      "toolName": "add_features", "result": {"content": []}})

    emitted, mapped = _nested_feed_run(script)
    starts = [frame for frame in emitted if frame["type"] == "host_tool_start"]
    ends = [frame for frame in emitted if frame["type"] == "host_tool_end"]
    assert [frame["toolCallId"] for frame in starts] == ["js-write-1", "js-write-2"]
    assert [frame["toolCallId"] for frame in ends] == ["js-write-1", "js-write-2"]
    assert all(frame["parentToolCallId"] == "eval-1" for frame in starts + ends)
    rows = [payload for kind, payload in mapped if kind == "agent.tool_call"]
    assert sorted(row["id"] for row in rows) == [
        "eval-1", "js-write-1", "js-write-2", "js-write-direct", "w-1",
    ]
    for row in rows:
        if row["id"] in {"js-write-1", "js-write-2"}:
            assert (row["tool_name"], row["target"], row["ok"], row["parent_id"]) == (
                "add_features", "doc-5", True, "eval-1",
            )
            assert "subagent_id" not in row


@pytest.mark.parametrize("wire", ["sdk", "unknown-custom"])
def test_queued_sdk_tool_events_keep_nested_call_ownership(wire):
    from omp_rpc._wire import ToolExecutionStartEvent, ToolExecutionEndEvent
    from omp_rpc._wire_runtime import UnknownNotification

    def script(client, emit, call):
        if wire == "sdk":
            start = ToolExecutionStartEvent(tool_call_id="eval-1", tool_name="eval")
            end = ToolExecutionEndEvent(tool_call_id="eval-1", tool_name="eval", result={})
        else:
            start = UnknownNotification({"type": "custom", "customType": "tool_execution_start",
                                         "data": {"toolCallId": "eval-1", "toolName": "eval"}})
            end = UnknownNotification({"type": "custom", "customType": "tool_execution_end",
                                       "data": {"toolCallId": "eval-1", "toolName": "eval"}})
        emit(client, start)
        call(client, "js-write-nested-1")
        emit(client, end)

    emitted, _mapped = _nested_feed_run(script)
    nested = [frame for frame in emitted if frame.get("toolCallId") == "js-write-nested-1"]
    assert [frame["type"] for frame in nested] == ["host_tool_start", "host_tool_end"]
    assert all(frame["parentToolCallId"] == "eval-1" for frame in nested)


@pytest.mark.parametrize("as_json", [False, True], ids=["mapping", "json-text"])
@pytest.mark.parametrize("outer_failed", [False, True], ids=["handled", "raised"])
def test_nested_cad_failure_is_a_failed_row_without_raising(as_json, outer_failed):
    failure = {"ok": False, "error": "circle is partly hidden", "status": "invalid_geometry"}
    value = json.dumps(failure) if as_json else failure

    def script(client, emit, call):
        emit(client, {"type": "tool_execution_start", "toolCallId": "eval-1", "toolName": "eval"})
        result = call(client, "js-write-failure")
        assert result["details"] == failure
        # The model may inspect the diagnostic or raise in its own Eval code.
        emit(client, {"type": "tool_execution_end", "toolCallId": "eval-1", "toolName": "eval",
                      "isError": outer_failed, "result": {"content": []}})

    emitted, mapped = _nested_feed_run(script, handler=lambda doc_id, ops: value)
    end = next(frame for frame in emitted if frame.get("toolCallId") == "js-write-failure"
               and frame["type"] == "host_tool_end")
    assert end["ok"] is False
    row = next(payload for kind, payload in mapped
               if kind == "agent.tool_call" and payload["id"] == "js-write-failure")
    assert row["ok"] is False
    assert row["error_preview"] == failure["error"]
    assert row["outcome_kind"] == "tool_error"
    assert row["parent_id"] == "eval-1"


def test_concurrent_root_and_child_evals_keep_their_own_parent_and_child_identity():
    barrier = threading.Barrier(4, timeout=10)
    release = threading.Event()
    errors = []

    def handler(doc_id, ops):
        if doc_id != "direct":
            barrier.wait()
            assert release.wait(10)
        return {"ok": True, "doc_id": doc_id}

    def script(client, emit, call):
        emit(client, {"type": "tool_execution_start", "toolCallId": "root-eval",
                      "toolName": "eval"},
             _nested_child("Builder", {"type": "tool_execution_start",
                                       "toolCallId": "child-eval", "toolName": "eval"}),
             _nested_child("Reviewer", {"type": "tool_execution_start",
                                        "toolCallId": "other-eval", "toolName": "eval"}),
             _nested_child("Builder", {"type": "tool_execution_start",
                                       "toolCallId": "js-write-child-direct", "toolName": "write",
                                       "args": {"path": "xd://add_features", "content": "{}"}}))
        call(client, "js-write-child-direct", agent_id="Builder", parent_agent_id="Main", doc_id="direct")
        emit(client, _nested_child("Builder", {"type": "tool_execution_end",
                                              "toolCallId": "js-write-child-direct",
                                              "toolName": "add_features", "result": {"content": []}}))

        def worker(call_id, agent_id, parent_agent_id, doc_id):
            try:
                call(client, call_id, agent_id=agent_id, parent_agent_id=parent_agent_id,
                     doc_id=doc_id)
            except BaseException as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=worker, args=("js-write-root", "Main", None, "root")),
            threading.Thread(target=worker, args=("js-write-child", "Builder", "Main", "child")),
            # The subscription also identifies children with no parentId.
            threading.Thread(target=worker, args=("js-write-other", "Reviewer", None, "other")),
        ]
        try:
            for worker_thread in workers:
                worker_thread.start()
            barrier.wait()
            # A parent can settle or another eval start before the CAD call ends.
            emit(client, _nested_child("Reviewer", {"type": "tool_execution_end",
                                                    "toolCallId": "other-eval", "toolName": "eval",
                                                    "result": {"content": []}}),
                 {"type": "tool_execution_start", "toolCallId": "root-eval-2", "toolName": "eval"})
        finally:
            release.set()
            for worker_thread in workers:
                worker_thread.join(timeout=10)
        assert not errors
        assert all(not worker_thread.is_alive() for worker_thread in workers)

    emitted, mapped = _nested_feed_run(script, handler=handler)
    expected = {
        "js-write-root": ("root-eval", None),
        "js-write-child": ("child-eval", "Builder"),
        "js-write-other": ("other-eval", "Reviewer"),
    }
    nested = [frame for frame in emitted if frame["type"] in {"host_tool_start", "host_tool_end"}]
    assert len(nested) == 6
    for frame in nested:
        parent, child = expected[frame["toolCallId"]]
        assert frame["parentToolCallId"] == parent
        assert frame.get("subagentId") == child
    rows = {payload["id"]: payload for kind, payload in mapped if kind == "agent.tool_call"}
    for call_id, (parent, child) in expected.items():
        assert rows[call_id]["parent_id"] == parent
        assert rows[call_id].get("subagent_id") == child
    assert sum(row_id == "js-write-child-direct" for row_id in rows) == 1


def test_overlapping_same_agent_evals_never_guess_the_parent():
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def handler(doc_id, ops):
        if doc_id == "ambiguous":
            entered.set()
            assert release.wait(10)
        return {"ok": True}

    def script(client, emit, call):
        emit(client, {"type": "tool_execution_start", "toolCallId": "eval-background",
                      "toolName": "eval"},
             {"type": "tool_execution_end", "toolCallId": "eval-background", "toolName": "eval",
              "result": {"details": {"async": {"state": "running", "jobId": "bg-1",
                                                "type": "eval"}}}},
             {"type": "tool_execution_start", "toolCallId": "eval-foreground", "toolName": "eval"})
        def blocked_call():
            try:
                call(client, "js-write-ambiguous", doc_id="ambiguous")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=blocked_call)
        worker.start()
        try:
            assert entered.wait(10)
            # The first call stays parent-unknown even after ambiguity clears.
            emit(client, {"type": "tool_execution_update", "toolCallId": "eval-background",
                          "toolName": "eval",
                          "partialResult": {"details": {"async": {"state": "completed",
                                                                   "jobId": "bg-1", "type": "eval"}}}})
        finally:
            release.set()
            worker.join(timeout=10)
        assert not worker.is_alive()
        assert not errors
        call(client, "js-write-unambiguous")

    emitted, mapped = _nested_feed_run(script, handler=handler)
    ambiguous = [frame for frame in emitted if frame.get("toolCallId") == "js-write-ambiguous"]
    assert len(ambiguous) == 2
    assert all(frame["parent_unknown"] is True and "parentToolCallId" not in frame
               for frame in ambiguous)
    current = [frame for frame in emitted if frame.get("toolCallId") == "js-write-unambiguous"]
    assert all(frame["parentToolCallId"] == "eval-foreground" for frame in current)
    row = next(payload for kind, payload in mapped
               if kind == "agent.tool_call" and payload["id"] == "js-write-ambiguous")
    assert row["parent_unknown"] is True
    assert "parent_id" not in row


def _scripted_turn(events):
    class Scripted(FakeClient):
        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            for event in events:
                for listener in tuple(self.listeners):
                    listener(event)
            return ()

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(Scripted(factory, **kw))
        or factory.clients[-1]
    )
    return host.run_turn(
        {"request": "Do the work", "session_id": "scripted", "run_id": "r-scripted"},
        lambda _frame: None,
    )


def test_prompt_protocol_error_is_runtime_error():
    class ProtocolError(FakeClient):
        def wait_for_settled(self, timeout=None):
            event = {"type": "prompt_result", "id": "fake-prompt", "status": "error",
                     "sessionSettled": True, "error": {"message": "protocol failed"}}
            for listener in tuple(self.listeners):
                listener(event)

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(ProtocolError(factory, **kw))
        or factory.clients[-1]
    )
    result, _emitted = run(host, "protocol-error")
    assert result["ok"] is False
    assert result["stop_reason"] == "runtime_error"
    assert result["reason"] == "protocol failed"


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
    # Live 2026-09-24: a 400 is not retried, so OMP records it only as an
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


def test_meter_is_seeded_after_run_started_and_closed_with_omp_state():
    """2026-09-24: the meter read "~22k/1.0M" on a session holding ~296k tokens.

    No OMP frame carried the contract's `used`, so the GUI only ever showed its
    own transcript estimate. The host now reports OMP's contextUsage (the figure
    its footer shows) after the GUI's run_started reset and again at agent_end.
    """
    from services.agent.omp_events import map_rpc_frame

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
    host = OmpSessionHost(
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
    learned a conversation's OMP session file from the finished turn — two hours of
    agent history were left with no resume key. The key now rides right after agent_start.
    """
    from services.agent.omp_events import map_rpc_frame

    class StartsThenDies(FakeClient):
        def wait_for_idle(self, timeout=None):
            for listener in tuple(self.listeners):
                listener({"type": "agent_start"})
            raise RuntimeError("daemon connection closed")

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(StartsThenDies(factory, **kw))
        or factory.clients[-1]
    )
    feed = []
    host.run_turn(
        {"request": "Do the work", "session_id": "resume", "run_id": "r-resume"},
        lambda frame: feed.extend(map_rpc_frame(frame)),
    )

    bound = [payload for kind, payload in feed if kind == "agent.session"]
    assert [p["omp_session_file"] for p in bound] == [factory.clients[0].session_file]
    assert bound[0]["omp_session_id"] == factory.clients[0].session_id


def test_rejects_a_runtime_that_did_not_negotiate_protocol_v2():
    factory = FakeFactory(protocol_version=1)
    host = OmpSessionHost(client_factory=factory)

    result, _ = run(host, "old", "old-run")

    assert result["ok"] is False
    assert "protocol v2 is required" in result["reason"]
    assert factory.clients[0].stop_count == 1


def test_lru_disposes_only_the_least_recent_idle_session(monkeypatch):
    monkeypatch.setenv("CHUDWARE_OMP_MAX_SESSIONS", "2")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)

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

    host = OmpSessionHost(client_factory=make_client)
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


def test_resumes_explicit_persisted_omp_session_file(tmp_path):
    persisted = tmp_path / "persisted.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)

    result, _ = run(
        host,
        "resume-me",
        omp_session_file=str(persisted),
    )

    assert result["ok"] is True
    assert result["omp_session_file"] == str(persisted)
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

    host = OmpSessionHost(client_factory=make_client)
    failed, _ = run(host, "resume", "r1", omp_session_file=str(persisted))
    retried, _ = run(host, "resume", "r2", omp_session_file=str(persisted))

    assert failed["ok"] is False
    assert "transient session switch failure" in failed["reason"]
    assert factory.clients[0].stop_count == 1
    assert retried["ok"] is True
    assert factory.clients[1].stop_count == 0


def test_waits_past_nonterminal_agent_end_until_terminal_frame():
    factory = FakeFactory(block=True)
    host = OmpSessionHost(client_factory=factory)
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
    assert result["terminal"]["status"] == "completed"
    assert [frame["isTerminal"] for frame in emitted if frame["type"] == "agent_end"] == [False, True]


def test_cancel_and_steer_are_immediate_and_idempotent_during_terminal_wait():
    factory = FakeFactory(block=True)
    host = OmpSessionHost(client_factory=factory)
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


def test_steer_carries_attached_images_to_omp(tmp_path):
    # A picture sent while a turn runs is a steer; the GUI used to have no way to send it.
    picture = tmp_path / "board.png"
    picture.write_bytes(_PNG_1PX)
    factory = FakeFactory(block=True)
    host = OmpSessionHost(client_factory=factory)
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
        {"type": "image", "mimeType": "image/png", "data": _PNG_1PX_B64}
    ]



def test_failed_abort_is_reported_without_false_success():
    class FlakyControl(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.fail_abort = True
            self.fail_steer = True

        def abort(self):
            if self.fail_abort:
                self.fail_abort = False
                self.calls.append(("abort", {}))
                self.release.set()
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

    host = OmpSessionHost(client_factory=make_client)
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
    assert host.cancel("r-flaky")["cancelled"] is True
    thread.join(1)

    assert not thread.is_alive()
    assert outcome["turn"][0]["cancelled"] is True
    assert call_names(client).count("steer") == 2
    assert call_names(client).count("abort") == 1


@pytest.mark.parametrize("supported", [True, False])
def test_cad_session_selects_template_only_when_supported(monkeypatch, caplog, supported):
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    binary = "/fake/omp-template-test"
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: binary)
    monkeypatch.setattr(
        omp_host_module, "_binary_supports_flag",
        lambda path, flag: supported if flag == "--system-prompt-template" else False,
    )
    monkeypatch.setattr(omp_host_module, "_TEMPLATE_WARNED", set())
    try:
        assert run(host, "template-one", "template-run-one")[0]["ok"]
        assert run(host, "template-two", "template-run-two")[0]["ok"]
        for client in factory.clients:
            args = client.kwargs["extra_args"]
            if supported:
                index = args.index("--system-prompt-template")
                assert Path(args[index + 1]) == (
                    Path(omp_host_module.__file__).resolve().parent
                    / "prompts" / "omp_cad_system.md"
                )
            else:
                assert "--system-prompt-template" not in args
        warnings = [r for r in caplog.records if "default system prompt" in r.message]
        assert len(warnings) == (0 if supported else 1)
    finally:
        host.shutdown()


def test_nonconversation_client_does_not_use_cad_template(monkeypatch):
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/omp")
    monkeypatch.setattr(omp_host_module, "_binary_supports_flag", lambda *args: True)
    monkeypatch.setattr(omp_host_module, "_cad_session_overlay", lambda *args: None)
    client = host._make_client({}, no_session=True)
    try:
        assert "--system-prompt-template" not in client.kwargs["extra_args"]
    finally:
        client._omp_bridge.stop()
        client._omp_bridge_dir.cleanup()


@pytest.mark.parametrize(("preference", "todo", "has_todo"), [
    ("off", {"enabled": False}, False),
    ("option", {"enabled": True}, True),
    ("encourage", {"enabled": True, "eager": "preferred"}, True),
    ("require", {"enabled": True, "eager": "always"}, True),
])
def test_task_list_preference_is_the_conversation_todo_policy(
        monkeypatch, tmp_path, preference, todo, has_todo):
    import yaml
    from services import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/omp")
    monkeypatch.setattr(omp_host_module, "_binary_supports_flag", lambda *args: True)
    client = host._make_client({"task_list_preference": preference}, no_session=False)
    try:
        args = client.kwargs["extra_args"]
        # Chudware's settings layer first, the session's pins last: a later file wins.
        layer, pins = [args[index + 1] for index, arg in enumerate(args) if arg == "--config"]
        assert Path(layer) == tmp_path / "omp" / "cad-settings.yml"
        assert yaml.safe_load(Path(pins).read_text(encoding="utf-8")).get("todo") == todo
        assert ("todo" in client.kwargs["tools"]) is has_todo
    finally:
        client._omp_bridge.stop()
        client._omp_bridge_dir.cleanup()


@pytest.mark.parametrize("cell", [False, True])
@pytest.mark.parametrize("todo", sorted(omp_host_module._CAD_TODO_SETTINGS))
@pytest.mark.parametrize("memory", sorted(omp_host_module._CAD_MEMORY_SETTINGS))
def test_session_overlay_is_nested_yaml_that_pins_the_session_settings(monkeypatch, tmp_path, memory, todo, cell):
    import yaml
    from services import paths
    from services.agent import request_context

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    if cell:
        monkeypatch.setattr(request_context, "in_cell", lambda: True)
        monkeypatch.setattr(request_context, "actor_state_root", lambda: tmp_path / "actor")
    path = omp_host_module._cad_session_overlay(memory, {"enabled": True}, todo=todo)
    tree = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    # OMP reads nested YAML only: a dotted top-level key would be silently ignored.
    assert not [key for key in tree if "." in key]
    assert tree["task"] == {"maxRuntimeMs": 0}
    assert tree["memory"]["backend"] == memory
    assert tree["compaction"] == {"enabled": True}
    assert ("browser" in tree and "computer" in tree) is cell and "async" not in tree
    if cell:
        assert tree["browser"] == {"enabled": False} and tree["computer"] == {"enabled": False}
    assert Path(path).parent == (tmp_path / "actor" if cell else tmp_path) / "omp"


def test_lsp_is_off_for_a_conversation_until_chudwares_layer_turns_it_on(monkeypatch, tmp_path):
    from services import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    host = OmpSessionHost(client_factory=FakeFactory())
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/omp")
    monkeypatch.setattr(omp_host_module, "_binary_supports_flag", lambda *args: True)

    def launch():
        client = host._make_client({}, no_session=False)
        try:
            return client.kwargs["extra_args"], client.kwargs["tools"]
        finally:
            client._omp_bridge.stop()
            client._omp_bridge_dir.cleanup()

    args, tools = launch()
    assert "--no-lsp" not in args and "lsp" not in tools
    layer = tmp_path / "omp" / "cad-settings.yml"
    assert "enabled: false" in layer.read_text(encoding="utf-8")
    layer.write_text("lsp:\n  enabled: true\n", encoding="utf-8")
    args, tools = launch()
    assert "--no-lsp" not in args and "lsp" in tools


def test_forwards_events_attachments_callbacks_and_all_turn_settings(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(_PNG_1PX)
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    registered = []
    tool = StubBinding("fixture_tool")
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
    assert any(frame["type"] == "prompt_result" for frame in emitted)
    assert prompt_call["images"][0]["mimeType"] == "image/png"
    assert "spec: /project/spec.txt" in prompt_call["message"]
    assert registered == [client]
    assert client.kwargs["append_system_prompt"] == "CAD governance prompt"
    assert client._omp_bridge.definitions[0]["name"] == "fixture_tool"
    assert "set_host_uris" in call_names(client)

def test_video_attachment_reaches_prompt_with_metadata_and_keeps_images(tmp_path, monkeypatch):
    from services.vision import frames

    video = tmp_path / "orbit clip.mp4"
    video.write_bytes(b"video is passed by path")
    image = tmp_path / "view.png"
    image.write_bytes(_PNG_1PX)
    monkeypatch.setattr(frames, "probe_video", lambda path, timeout: {
        "duration_s": 3.25, "width": 1920, "height": 1080,
        "orientation_degrees_clockwise": 90,
    })
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    result, _ = run(host, "video-session", "video-run", request="reconstruct the object", attachments=[
        {"path": str(video), "name": video.name, "kind": "video"},
        {"path": str(image), "kind": "image", "mime": "image/png"},
        {"path": "/project/spec.txt", "kind": "file", "name": "spec"},
    ])
    call = next(payload for name, payload in factory.clients[0].calls if name == "prompt")
    assert result["ok"] is True
    assert len(call["images"]) == 1 and call["images"][0]["mimeType"] == "image/png"
    for part in ('"kind": "video"', str(video), '"duration_s": 3.25',
                 '"width": 1920', '"height": 1080', '"orientation_degrees_clockwise": 90',
                 "reveng_add_video", "reveng_start_splat", "reveng_reconstruct",
                 "spec: /project/spec.txt"):
        assert part in call["message"]


def test_video_attachment_without_probe_remains_usable(monkeypatch):
    from services.vision import frames

    monkeypatch.setattr(frames, "probe_video", lambda path, timeout: {})
    prompt, images = omp_host_module._prompt_payload(
        "use the video", [{"path": "/project/orbit.mkv", "kind": "video"}], None)
    assert images == []
    assert '"kind": "video"' in prompt and "/project/orbit.mkv" in prompt
    assert "frames were not sent" in prompt


def test_video_attachment_probe_uses_tenant_resolved_path(monkeypatch):
    from services.vision import frames

    seen = []
    monkeypatch.setattr(omp_host_module.tenancy, "cell_path",
                        lambda path: Path("/work") / path)
    monkeypatch.setattr(frames, "probe_video",
                        lambda path, timeout: seen.append(path) or {})
    prompt, _ = omp_host_module._prompt_payload(
        "use the video", [{"path": "scene/orbit.mp4", "kind": "video"}], None)
    assert seen == ["/work/scene/orbit.mp4"]
    assert '"path": "scene/orbit.mp4"' in prompt


def test_composer_effort_overrides_stale_thinking_without_coercion():
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
    host = OmpSessionHost(client_factory=factory)
    result, _ = run(host, "effort-precedence", provider="moonshot", model="kimi-k3",
                    settings={"thinking": "low", "reasoning_effort": "medium"})
    assert result["ok"]
    assert ("set_thinking_level", {"level": "medium"}) in factory.clients[0].calls

    result, _ = run(host, "effort-precedence", "effort-max", provider="moonshot",
                    model="kimi-k3", settings={"thinking": "low"},
                    reasoning_effort="xhigh")
    assert result["ok"]
    assert ("set_thinking_level", {"level": "xhigh"}) in factory.clients[0].calls


def test_exact_provider_identity_is_not_rewritten():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)

    result, _ = run(host, "alias", provider="gemini", model="gemini-2.5-pro")

    assert result["ok"] is True
    set_model = next(p for n, p in factory.clients[0].calls if n == "set_model")
    assert set_model == {"provider": "gemini", "modelId": "gemini-2.5-pro"}


@pytest.mark.parametrize("mode", ["simple", "advanced"])
def test_runtime_oauth_model_id_survives_role_settings_and_turn(mode, monkeypatch, tmp_path):
    from services.agent import role_models, worker_rpc

    config_path = tmp_path / "config.json"
    monkeypatch.setattr("services.paths.CONFIG", config_path)
    selection = {"provider": "openai-codex", "model": "runtime-model"}
    role_selection = {"provider": "anthropic", "model": "role-model"}
    settings = ({"assignment_mode": mode, "model_sizes": {"large": role_selection}}
                if mode == "simple" else
                {"assignment_mode": mode, "roles": {"chat": role_selection}})
    settings["chat_assignment"] = selection
    assert worker_rpc._ai_roles_set(settings, lambda _: None)["ok"]
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assignment = role_models.resolve_role_model("chat", cfg)
    assert (assignment.provider, assignment.model) == ("openai-codex", "runtime-model")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    result, _ = run(host, "runtime-id", provider=assignment.provider, model=assignment.model)
    assert result["ok"]
    assert ("set_model", {"provider": "openai-codex", "modelId": "runtime-model"}) in factory.clients[0].calls
    host.shutdown()


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
    host = OmpSessionHost(client_factory=factory)
    signed_out, _ = run(host, "signed-out", provider="openai", model="gpt-5")
    assert signed_out["ok"] is False
    assert "openai is not signed in" in signed_out["reason"]
    assert "Settings -> AI -> Providers" in signed_out["reason"]
    assert "anthropic" in signed_out["reason"]  # what IS signed in

    factory = RejectingFactory()
    host = OmpSessionHost(client_factory=factory)
    stale, _ = run(host, "stale", provider="anthropic", model="claude-sonnet-5")
    assert stale["ok"] is False
    assert "claude-sonnet-5 is not in the agent service's anthropic catalog" in stale["reason"]
    assert "claude" in stale["reason"]








_MNEMO_TOOLS = {"retain", "recall", "reflect"}







def test_missing_dependency_and_binary_are_actionable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "omp_rpc", None)
    missing_dep = OmpSessionHost()
    result = missing_dep.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "omp-rpc" in result["reason"]

    missing_binary = OmpSessionHost(
        client_factory=FakeFactory(), binary=tmp_path / "missing-omp"
    )
    result = missing_binary.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "not an executable file" in result["reason"]

    bun_script = tmp_path / "omp"
    bun_script.write_text(
        "#!/usr/bin/env bun\nimport './src/cli.ts'\n", encoding="utf-8"
    )
    bun_script.chmod(0o755)
    source_binary = OmpSessionHost(client_factory=FakeFactory(), binary=bun_script)
    result = source_binary.run_turn(
        {"request": "hi", "session_id": "s", "run_id": "r"}, lambda _frame: None
    )
    assert result["ok"] is False
    assert "source/Bun script" in result["reason"]


def test_runtime_adjacent_to_symlinked_venv_python(monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_OMP_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    system_python = tmp_path / "system-python"
    system_python.write_bytes(b"\x7fELF")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    interpreter = venv_bin / "python"
    try:
        interpreter.symlink_to(system_python)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    monkeypatch.setattr(omp_host_module.sys, "executable", str(interpreter))
    runtime = venv_bin / ("omp.exe" if omp_host_module.os.name == "nt" else "omp")
    runtime.write_bytes(b"\x7fELF")
    runtime.chmod(0o755)
    assert OmpSessionHost()._resolve_binary(required=True) == str(runtime)


def test_close_and_shutdown_stop_each_process_once_and_reject_new_work():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    run(host, "a", "a1")
    run(host, "b", "b1")

    host.close_session("a")
    host.close_session("a")
    host.shutdown()
    host.shutdown()

    assert [client.stop_count for client in factory.clients] == [1, 1]
    result, _ = run(host, "c", "c1")
    assert result == {"ok": False, "reason": "omp session host is shut down"}


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

    host = OmpSessionHost(client_factory=make_client)
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
    host = OmpSessionHost(client_factory=factory)
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


def test_raw_catalog_survives_sdk_decode_failure_and_remains_safe(monkeypatch):
    from services.agent import worker_rpc

    class NullableCatalogClient(FakeClient):
        def get_available_models(self):
            raise TypeError("SDK cannot decode null model price")

        def request_raw(self, command, **payload):
            if command == "get_available_models":
                return {"models": [
                    {"provider": "openai-codex", "id": "gpt/example", "name": "Example",
                     "contextWindow": None, "maxTokens": None, "cost": None,
                     "headers": {"Authorization": "fixture-secret"}}
                ]}
            return super().request_raw(command, **payload)

    factory = FakeFactory()
    def make_client(**kwargs):
        client = NullableCatalogClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = OmpSessionHost(client_factory=make_client)
    monkeypatch.setattr(worker_rpc, "get_default_omp_host", lambda: host)
    try:
        result = worker_rpc._omp_models_list({}, lambda _: None)
        assert result["ok"] and len(result["models"]) == 1
        row = result["models"][0]
        assert (row["provider"], row["id"]) == ("openai-codex", "gpt/example")
        assert row["contextWindow"] == row["maxTokens"] == 0
        assert "fixture-secret" not in repr(result)
        assert host.auth_call("auth_states", {"providers": ["openai-codex"]}) == {
            "providers": [{"provider": "openai-codex", "configured": True, "oauth": False}]}
        with pytest.raises(ValueError, match="invalid auth provider batch"):
            host.auth_call("auth_states", {"providers": ["x"] * 33})
        with pytest.raises(ValueError, match="invalid auth provider batch"):
            host.auth_call("auth_states", {"providers": ["x" * 129]})
    finally:
        host.shutdown()


def test_dead_service_process_is_replaced_on_the_next_transient_call():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    assert host.transient_call("get_login_providers")["providers"]
    first = factory.clients[0]
    first._closed_error = RuntimeError("process exited")
    assert host.transient_call("get_login_providers")["providers"]
    assert len(factory.clients) == 2
    assert first.stop_count == 1
    assert factory.clients[1].stop_count == 0












def test_live_session_calls_are_isolated_and_never_create_for_status():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    assert host.session_call("missing", "get_session_stats") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert factory.clients == []

    run(host, "a", "a1", host_tools=[StubBinding("cad_tool")])
    run(host, "b", "b1")
    count = len(factory.clients)
    stats = host.session_call("a", "get_session_stats")
    auto_compact = host.session_call("a", "set_auto_compaction", {"enabled": False})
    auto_retry = host.session_call("b", "set_auto_retry", {"enabled": True})
    state = host.session_call("a", "get_state")
    tool_state = host.session_call("a", "tool_state")
    handoff = host.session_call("a", "handoff", {"customInstructions": "Keep dimensions"})

    assert len(factory.clients) == count
    assert stats["sessionId"] == factory.clients[0].session_id
    assert auto_compact == {"ok": True, "enabled": False}
    assert auto_retry == {"ok": True, "enabled": True}
    assert state["_hostToolNames"] == ["cad_tool"]
    assert tool_state["tools"][0]["name"] == "cad_tool"
    assert ("tool_state", {}) in factory.clients[0]._omp_bridge.operations
    assert handoff["document"] == "fixture handoff"
    assert ("handoff", {"customInstructions": "Keep dimensions"}) in factory.clients[0]._omp_bridge.operations
    assert "get_session_stats" in call_names(factory.clients[0])


def test_recovery_methods_refuse_inactive_sessions_without_creating_processes():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)

    assert host.export_session("missing") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.branch_session("missing", "entry-1") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.session_call("missing", "get_messages") == {
        "ok": False,
        "reason": "session_not_live",
    }
    assert host.close_session("missing") == {"ok": True}
    assert factory.clients == []


def _wait_for_client(factory, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not factory.clients and time.monotonic() < deadline:
        time.sleep(0.01)
    return factory.clients[0]


def _compaction_params(persisted, workspace, **compact):
    return {
        "session_id": "c1",
        "run_id": "compact-1",
        "omp_session_file": str(persisted),
        "workspace_dir": str(workspace),
        "system_prompt": "ROLE PROMPT",
        "compact": compact,
    }


def test_compaction_resumes_a_conversation_that_is_not_live_and_keeps_it_live(tmp_path):
    persisted = tmp_path / "conversation.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    emitted = []

    try:
        result = host.run_compaction(
            _compaction_params(persisted, tmp_path, mode="soft"),
            emitted.append,
        )

        assert result["ok"] is True and result["cancelled"] is False
        assert result["compaction"]["summary"] == "trimmed"
        [client] = factory.clients
        assert "switch_session" in call_names(client)
        assert ("compact", {"mode": "soft"}) in client._omp_bridge.operations
        assert persisted.read_text(encoding="utf-8") == "{}\n"
        # The compaction started the conversation's own process: the next turn reuses it.
        turn, _ = run(host, "c1", "turn-1", omp_session_file=str(persisted))
        assert turn["ok"] is True
        assert factory.clients == [client] and client.stop_count == 0
    finally:
        host.shutdown()


def test_compaction_reports_runtime_context_and_keeps_summary_focus(tmp_path):
    persisted = tmp_path / "conversation.jsonl"
    persisted.write_text('{"opaque":"thinking-signature-and-cache"}\n', encoding="utf-8")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    emitted = []
    try:
        result = host.run_compaction(
            _compaction_params(persisted, tmp_path, mode="soft", instructions="keep the dimensions"),
            emitted.append,
        )
        assert result["ok"] and result["outcome"] == "committed"
        assert result["omp_session_file"] == str(persisted)
        assert result["compaction"]["method"] == "soft"
        assert result["compaction"]["tokensBefore"] == 100
        assert result["compaction"]["tokensAfter"] == 25
        assert result["compaction"]["tokensBeforeEstimated"] is False
        assert result["compaction"]["tokensAfterEstimated"] is True
        assert ("compact", {"mode": "soft", "instructions": "keep the dimensions"}) in \
            factory.clients[0]._omp_bridge.operations
        assert persisted.read_text(encoding="utf-8") == '{"opaque":"thinking-signature-and-cache"}\n'
        assert {"type": "context_update", "contextTokens": 25, "source": "estimate"} in emitted
        assert emitted[-1]["result"]["tokensAfter"] == 25
    finally:
        host.shutdown()

@pytest.mark.parametrize("after", [None, {"tokens": 25, "estimated": True}])
@pytest.mark.parametrize("lifecycle_first", [False, True])
def test_late_stop_preserves_committed_receipt_and_session(monkeypatch, tmp_path, after, lifecycle_first):
    monkeypatch.setattr(omp_host_module, "_CONTROL_CALL_SECONDS", 0.5)
    persisted = tmp_path / "conversation.jsonl"
    prefix = '{"opaque":"thinking-signature-and-cache"}\n'
    persisted.write_text(prefix, encoding="utf-8")
    committed, release_receipt = threading.Event(), threading.Event()
    original = FakeBridge.call

    def call(self, operation, arguments, *, timeout, cancel_event=None):
        if operation != "compact":
            return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        self.operations.append((operation, dict(arguments)))
        result = self.client.compact(arguments.get("instructions"))
        with persisted.open("a", encoding="utf-8") as journal:
            journal.write('{"type":"compaction","method":"soft"}\n')
        if lifecycle_first:
            for listener in tuple(self.client.listeners):
                listener({"type": "manual_compaction_end", "aborted": False, "result": result})
        committed.set()
        assert release_receipt.wait(2)
        return {"completed": True, "result": result, "method": "soft",
                "contextBefore": {"tokens": 100, "estimated": False}, "contextAfter": after}

    monkeypatch.setattr(FakeBridge, "call", call)
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    frames, results = [], []
    worker = threading.Thread(target=lambda: results.append(host.run_compaction(
        _compaction_params(persisted, tmp_path, mode="soft"), frames.append)))
    try:
        worker.start()
        assert committed.wait(2)
        assert host.cancel("compact-1")["cancelled"] is True
        release_receipt.set()
        worker.join(2)
        assert not worker.is_alive()
        [result] = results
        assert result["ok"] and result["outcome"] == "committed" and not result["cancelled"]
        assert result["omp_session_id"] == factory.clients[0].session_id
        assert result["omp_session_file"] == str(persisted)
        assert result["compaction"]["method"] == "soft"
        assert result["compaction"]["tokensBeforeEstimated"] is False
        assert persisted.read_text(encoding="utf-8") == prefix + '{"type":"compaction","method":"soft"}\n'
        assert host.cancel("compact-1")["cancelled"] is False
        assert frames[-1]["aborted"] is False
        if after is None:
            assert "tokensAfter" not in result["compaction"]
            assert {"type": "context_update", "contextTokens": None} in frames
        else:
            assert result["compaction"]["tokensAfter"] == 25
            assert result["compaction"]["tokensAfterEstimated"] is True
            assert {"type": "context_update", "contextTokens": 25, "source": "estimate"} in frames
        assert factory.clients[0].stop_count == 0
    finally:
        release_receipt.set()
        worker.join(2)
        host.shutdown()


def test_bridge_timeout_retires_only_unsettled_maintenance_runtime(monkeypatch, tmp_path):
    from services.agent.omp_bridge import BridgeError
    monkeypatch.setattr(omp_host_module, "_BUDGET_ABORT_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(omp_host_module, "_CONTROL_CALL_SECONDS", 3.0)
    persisted = tmp_path / "conversation.jsonl"
    prefix = '{"opaque":"original"}\n'
    persisted.write_text(prefix, encoding="utf-8")
    original = FakeBridge.call
    maintenance_started = threading.Event()
    abort_started, release_abort = threading.Event(), threading.Event()
    stop_started, release_stop = threading.Event(), threading.Event()
    maintenance_threads = []
    requested = []

    class UnresponsiveAbort(FakeClient):
        def abort_and_restore_queue(self):
            self.calls.append(("abort", {}))
            abort_started.set()
            assert release_abort.wait(5)
            return {"steering": [], "followUp": []}
        def stop(self):
            stop_started.set()
            assert release_stop.wait(5)
            super().stop()

    def call(self, operation, arguments, *, timeout, cancel_event=None):
        if operation != "compact":
            return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        self.operations.append((operation, dict(arguments)))
        requested.append(dict(arguments))
        def maintenance():
            maintenance_started.set()
            self.client.release.wait(5)
            if self.client.stop_count == 0:
                with persisted.open("a", encoding="utf-8") as journal:
                    journal.write('{"type":"compaction","method":"soft"}\n')
        thread = threading.Thread(target=maintenance)
        maintenance_threads.append(thread)
        thread.start()
        assert maintenance_started.wait(1)
        raise BridgeError("timeout", "extension operation timed out")

    monkeypatch.setattr(FakeBridge, "call", call)
    factory = FakeFactory()
    def make_client(**kwargs):
        client = UnresponsiveAbort(factory, **kwargs)
        factory.clients.append(client)
        return client
    host = OmpSessionHost(client_factory=make_client)
    frames, results = [], []
    worker = threading.Thread(target=lambda: results.append(host.run_compaction(
        _compaction_params(persisted, tmp_path, mode="soft"), frames.append)))
    try:
        other, _ = run(host, "other", "other-run")
        assert other["ok"]
        worker.start()
        assert abort_started.wait(2)
        assert worker.is_alive() and not host.status(session_id="c1")["live_session"]
        reentry, _ = run(host, "c1", "reentry")
        assert not reentry["ok"] and "closing" in reentry["reason"]
        release_abort.set()
        assert stop_started.wait(2)
        reentry, _ = run(host, "c1", "reentry-during-stop")
        assert not reentry["ok"] and "closing" in reentry["reason"]
        release_stop.set()
        worker.join(2)
        assert not worker.is_alive()
        [result] = results
        for thread in maintenance_threads:
            thread.join(2)
            assert not thread.is_alive()
        assert not result["ok"] and result["outcome"] == "failed"
        assert result["omp_runtime_live"] is False
        assert "unknown" in result["reason"]
        other_client, own_client = factory.clients
        assert "abort" in call_names(own_client) and own_client.stop_count == 1
        assert other_client.stop_count == 0
        assert not host.status(session_id="c1")["live_session"]
        assert host.status(session_id="other")["live_session"]
        assert persisted.read_text(encoding="utf-8") == prefix
        assert {"type": "context_update", "contextTokens": None} in frames
        assert requested == [{"mode": "soft"}]
        from services.agent import worker_rpc
        monkeypatch.setattr(worker_rpc, "omp_compaction", lambda *_: result)
        envelope = worker_rpc._admin_compaction(
            {}, {"run_id": "compact-1", "session_id": "c1",
                 "settings": {"compaction_strategy": "summary"}}, lambda _: None)
        assert not envelope["ok"] and envelope["turn"]["omp_runtime_live"] is False
    finally:
        release_abort.set()
        release_stop.set()
        if worker.ident is not None:
            worker.join(2)
        host.shutdown()


def test_committed_end_recovers_journal_method_after_bridge_timeout_and_stop(monkeypatch, tmp_path):
    from services.agent.omp_bridge import BridgeError
    persisted = tmp_path / "conversation.jsonl"
    prefix = '{"type":"message","id":"prior","parentId":null}\n'
    persisted.write_text(prefix, encoding="utf-8")
    committed, release_receipt = threading.Event(), threading.Event()
    original = FakeBridge.call

    class JournalClient(FakeClient):
        def request_raw(self, command, **payload):
            if command == "get_entries":
                entries = [json.loads(line) for line in persisted.read_text(encoding="utf-8").splitlines()]
                return {"entries": entries, "leafId": entries[-1]["id"]}
            return super().request_raw(command, **payload)

    def call(self, operation, arguments, *, timeout, cancel_event=None):
        if operation != "compact":
            return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        result = self.client.compact()
        entry = {"type": "compaction", "id": "commit", "parentId": "prior",
                 "summary": result["summary"], "firstKeptEntryId": result["firstKeptEntryId"],
                 "method": "soft"}
        with persisted.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(entry) + "\n")
        for listener in tuple(self.client.listeners):
            listener({"type": "manual_compaction_end", "aborted": False, "result": result})
        committed.set()
        assert release_receipt.wait(2)
        raise BridgeError("timeout", "extension operation timed out")

    monkeypatch.setattr(FakeBridge, "call", call)
    factory = FakeFactory()
    def make_client(**kwargs):
        client = JournalClient(factory, **kwargs)
        factory.clients.append(client)
        return client
    host = OmpSessionHost(client_factory=make_client)
    frames, results = [], []
    worker = threading.Thread(target=lambda: results.append(host.run_compaction(
        _compaction_params(persisted, tmp_path, mode="soft"), frames.append)))
    try:
        worker.start()
        assert committed.wait(2)
        assert host.cancel("compact-1")["cancelled"]
        release_receipt.set()
        worker.join(2)
        assert not worker.is_alive()
        [result] = results
        assert result["ok"] and result["outcome"] == "committed" and not result["cancelled"]
        assert result["compaction"]["method"] == "soft"
        assert result["omp_session_id"] == factory.clients[0].session_id
        assert result["omp_session_file"] == str(persisted)
        assert result["omp_runtime_live"] is False and factory.clients[0].stop_count == 1
        assert "tokensAfter" not in result["compaction"]
        assert {"type": "context_update", "contextTokens": None} in frames
        assert persisted.read_text(encoding="utf-8").startswith(prefix)
        assert host.cancel("compact-1")["cancelled"] is False
        from services.agent import worker_rpc
        monkeypatch.setattr(worker_rpc, "omp_compaction", lambda *_: result)
        envelope = worker_rpc._admin_compaction(
            {}, {"run_id": "compact-1", "session_id": "c1",
                 "settings": {"compaction_strategy": "summary"}}, lambda _: None)
        assert envelope["ok"] and envelope["turn"]["omp_runtime_live"] is False
        assert envelope["turn"]["outcome"] == "committed"
        assert envelope["turn"]["compaction"]["method"] == "soft"
        assert envelope["turn"]["omp_session_file"] == str(persisted)
    finally:
        release_receipt.set()
        worker.join(2)
        host.shutdown()



@pytest.mark.parametrize("after", [None, {"tokens": 150, "estimated": True}])
def test_compaction_uses_only_its_own_post_state(monkeypatch, after):
    original = FakeBridge.call
    def call(self, operation, arguments, *, timeout, cancel_event=None):
        value = original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        if operation == "compact":
            value["contextAfter"] = after
        return value
    monkeypatch.setattr(FakeBridge, "call", call)
    host = OmpSessionHost(client_factory=FakeFactory())
    frames = []
    try:
        result = host.run_compaction({"session_id": "s", "compact": {}}, frames.append)
        assert result["ok"] and result["outcome"] == "committed"
        if after is None:
            assert "tokensAfter" not in result["compaction"]
            assert {"type": "context_update", "contextTokens": None} in frames
        else:
            # A journal commit is not evidence that the context actually shrank.
            assert result["compaction"]["tokensAfter"] == 150
            assert {"type": "context_update", "contextTokens": 150, "source": "estimate"} in frames
    finally:
        host.shutdown()


def test_compaction_does_not_claim_a_different_committed_method(monkeypatch):
    original = FakeBridge.call
    def call(self, operation, arguments, *, timeout, cancel_event=None):
        value = original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        if operation == "compact":
            value["method"] = "snapcompact"
        return value
    monkeypatch.setattr(FakeBridge, "call", call)
    host = OmpSessionHost(client_factory=FakeFactory())
    frames = []
    try:
        result = host.run_compaction({"session_id": "s", "compact": {"mode": "soft"}}, frames.append)
        assert not result["ok"] and result["outcome"] == "failed"
        assert "requested compaction method" in result["reason"]
        assert frames[-1]["errorMessage"]
        assert {"type": "context_update", "contextTokens": 25, "source": "estimate"} in frames
    finally:
        host.shutdown()

def test_automatic_compaction_requires_a_verifiable_committed_method(monkeypatch):
    original = FakeBridge.call
    def call(self, operation, arguments, *, timeout, cancel_event=None):
        value = original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
        if operation == "compact":
            value.pop("method")
        return value
    monkeypatch.setattr(FakeBridge, "call", call)
    host = OmpSessionHost(client_factory=FakeFactory())
    frames = []
    try:
        result = host.run_compaction({"session_id": "s", "compact": {}}, frames.append)
        assert not result["ok"] and result["outcome"] == "failed"
        assert "method could not be verified" in result["reason"]
        assert {"type": "context_update", "contextTokens": 25, "source": "estimate"} in frames
    finally:
        host.shutdown()



def test_compaction_rejects_uncommitted_mapping(monkeypatch):
    original = FakeBridge.call
    def call(self, operation, arguments, *, timeout, cancel_event=None):
        if operation == "compact":
            return {"completed": False, "result": {"summary": "not committed"}}
        return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
    monkeypatch.setattr(FakeBridge, "call", call)
    host = OmpSessionHost(client_factory=FakeFactory())
    try:
        result = host.run_compaction({"session_id": "s", "compact": {}}, lambda _: None)
        assert not result["ok"] and result["outcome"] == "failed"
    finally:
        host.shutdown()


def test_context_state_preserves_identity_when_sdk_rejects_null_usage():
    class NullableState:
        def get_state(self):
            raise ValueError("contextUsage.tokens must be an integer")
        def request_raw(self, command):
            assert command == "get_state"
            return {"sessionId": "original", "sessionFile": "/isolated/session.jsonl",
                    "contextUsage": {"tokens": None, "contextWindow": 1000, "percent": None}}
    state = omp_host_module._safe_client_state(NullableState())
    assert state["sessionId"] == "original"
    assert state["contextUsage"]["tokens"] is None
    assert omp_host_module._invoke_session_canonical(NullableState(), "get_state", {}) == state
    assert omp_host_module._context_frame(state) == {
        "type": "context_update", "contextTokens": None,
        "contextWindow": 1000, "windowSource": "catalog",
    }


def test_stop_aborts_a_running_compaction_and_steers_cannot_start_a_turn(tmp_path):
    persisted = tmp_path / "conversation.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory(block=True)
    host = OmpSessionHost(client_factory=factory)
    emitted = []
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(
        host.run_compaction(_compaction_params(persisted, tmp_path), emitted.append)
    ))

    try:
        worker.start()
        assert _wait_for_client(factory).compact_started.wait(2.0)
        steer = host.steer("compact-1", "also do this")
        assert steer["steered"] is False and steer["reason"] == "compaction_in_progress"
        assert host.cancel("compact-1")["cancelled"] is True
        worker.join(2.0)

        assert outcome["ok"] is True and outcome["cancelled"] is True
        assert "abort" in call_names(factory.clients[0])
        assert "steer" not in call_names(factory.clients[0])
        assert emitted[-1]["type"] == "manual_compaction_end" and emitted[-1]["aborted"]
    finally:
        host.shutdown()


def test_a_refused_resume_never_continues_in_an_empty_session(tmp_path):
    persisted = tmp_path / "conversation.jsonl"
    persisted.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()

    def make_client(**kwargs):
        client = factory(**kwargs)
        # OMP cancels a resume recorded in another cwd and stays on a fresh session.
        client.switch_session = lambda path: {"cancelled": True}
        return client

    host = OmpSessionHost(client_factory=make_client)
    try:
        compacted = host.run_compaction(_compaction_params(persisted, tmp_path), lambda _f: None)
        turn, _ = run(host, "c1", "turn-1", omp_session_file=str(persisted))

        for result in (compacted, turn):
            assert result["ok"] is False
            assert result["reason"].startswith("session_file_mismatch")
        assert all("compact" not in call_names(c) and "prompt" not in call_names(c)
                   for c in factory.clients)
        assert all(c.stop_count == 1 for c in factory.clients)
    finally:
        host.shutdown()


def test_export_is_project_contained_and_atomically_published(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    run(host, "export/session", "export-run", workspace_dir=str(project))
    replace_calls = []
    real_replace = omp_host_module.os.replace

    def record_replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(omp_host_module.os, "replace", record_replace)
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
    assert not list(project.rglob("*.omp-export.tmp"))


def test_branch_retires_mutated_runtime_and_parent_and_child_resume_independently(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    parent_file = project / "parent.jsonl"
    parent_file.write_text("{}\n", encoding="utf-8")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    run(
        host,
        "parent",
        "parent-run",
        workspace_dir=str(project),
        omp_session_file=str(parent_file),
    )

    child = host.branch_session("parent", "entry-7")

    assert child["ok"] is True
    assert child["omp_session_id"].endswith("-child")
    assert child["omp_session_file"] != str(parent_file)
    assert Path(child["omp_session_file"]).is_file()
    assert factory.clients[0].stop_count == 1
    assert host.status(session_id="parent")["live_session"] is False

    parent, _ = run(
        host,
        "parent",
        "parent-resume",
        workspace_dir=str(project),
        omp_session_file=str(parent_file),
    )
    fork, _ = run(
        host,
        "fork",
        "fork-resume",
        workspace_dir=str(project),
        omp_session_file=child["omp_session_file"],
    )
    messages = host.session_call("fork", "get_messages")
    assert parent["omp_session_file"] == str(parent_file)
    assert fork["omp_session_file"] == child["omp_session_file"]
    assert messages["messages"][0]["role"] == "user"

    assert host.close_session("parent") == {"ok": True}
    assert host.close_session("parent") == {"ok": True}
    assert parent_file.is_file()
    assert host.status(session_id="parent")["live_session"] is False


def test_shutdown_cancels_an_inflight_transient_login_once():
    factory = FakeFactory(block=True)
    host = OmpSessionHost(client_factory=factory)
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
    """A host whose OMP run needs `needs_s` of controlled time to settle.

    Each `wait_for_idle(timeout)` slice that ends before the run settles advances the
    clock by `timeout` and raises the RPC client's wait timeout, exactly like a real
    omp_rpc wait on a healthy, still-working run.
    """
    clock = _ControlledClock()
    monkeypatch.setattr(omp_host_module, "_monotonic", clock)
    factory = FakeFactory()

    class LongTurn(FakeClient):
        prompted_at = 0.0

        def prompt(self, message, *, images=None):
            request_id = super().prompt(message, images=images)
            self.prompted_at = clock.now
            return request_id

        def wait_for_idle(self, timeout=None):
            self.idle_timeouts.append(timeout)
            assert timeout is not None, "None is omp_rpc's 60 s default, not a turn wait"
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

    return OmpSessionHost(client_factory=make_client), factory, clock


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
        assert result["terminal"]["status"] == "completed"
        assert "abort" not in call_names(client)
        assert any(f.get("type") == "agent_end" for f in emitted)
        assert sum(client.idle_timeouts) >= 3 * _OLD_TWO_HOUR_STOP_S
    finally:
        host.shutdown()

@pytest.mark.parametrize("failure_kind", ["eval", "host_tool"])
def test_operation_timeout_keeps_session_for_next_turn(monkeypatch, failure_kind):
    """Exercise the host against OMP-shaped events and a real CAD timeout dispatch."""
    import copy
    from services.agent.omp_tools import HostToolBinding

    rpc = pytest.importorskip("omp_rpc")

    # Use the real registry/generation and tool dispatch; the fake client below
    # stands in only for OMP's provider and extension-control responses.
    monkeypatch.setattr("services.agent.omp_bridge.ExtensionBridge", RealExtensionBridge)
    monkeypatch.setattr("services.agent.config.get_tool_options", lambda *_args, **_kwargs: {
        "enabled": True, "timeout_s": 0.02, "max_calls_per_turn": 100,
        "param_defaults": {}, "prompt_note": "",
    })
    clock = _ControlledClock()
    monkeypatch.setattr(omp_host_module, "_monotonic", clock)
    release = threading.Event()
    entered = threading.Event()
    workers = []
    executions = []

    def work():
        executions.append(1)
        workers.append(threading.current_thread())
        entered.set()
        assert release.wait(10), "test did not release the native worker"
        return {"ok": True}

    def binding(run_id):
        return HostToolBinding(
            name="timeout_probe", description="Timeout test tool",
            input_schema={"type": "object", "properties": {}},
            handler=work, context={"run_id": run_id, "session_id": "cache-chat"},
        )

    class TimeoutConversation(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.controls = []
            self.slice_count = 0
            self.started_tool = None

        def start(self):
            super().start()
            bridge = self._omp_bridge
            generation = bridge._handle("bootstrap", {})["generation"]
            assert bridge._handle("tool_ack", {"generation": generation})["accepted"]

            def control(operation, arguments, *, timeout, cancel_event=None):
                self.controls.append((operation, copy.deepcopy(arguments)))
                if operation == "auth_state":
                    return {"provider": arguments["provider"], "configured": True}
                if operation == "settings_set":
                    return {"compaction": dict(arguments["compaction"])}
                raise AssertionError(f"unexpected control: {operation}")

            bridge.call = control
            return self

        def notify(self, frame):
            for listener in tuple(self.listeners):
                listener(frame)

        def wait_for_settled(self, timeout=None):
            self.idle_timeouts.append(timeout)
            assert timeout is not None
            if self.model_prompt_count == 1:
                if self.slice_count == 0:
                    self.notify({"type": "agent_start"})
                    tool_name = "eval" if failure_kind == "eval" else "timeout_probe"
                    args = ({"language": "py", "code": "long_work()", "timeout": 1}
                            if failure_kind == "eval" else {})
                    self.notify({"type": "tool_execution_start", "toolCallId": "timed-call",
                                 "toolName": tool_name, "args": args})
                    if failure_kind == "host_tool":
                        started = self._omp_bridge._handle("tool_start", {
                            "name": tool_name, "arguments": args,
                            "context": {"tool_call_id": "timed-call", "agent_id": "main",
                                        "session_id": self.session_id},
                        })
                        self.started_tool = started["id"]
                # Simulate five SDK wait expirations, not five new prompts. The
                # native timeout below still runs on a real worker and clock.
                if self.slice_count < 5:
                    self.slice_count += 1
                    clock.now += timeout
                    raise rpc.RpcTimeoutError("Timed out waiting for session settlement")
                if failure_kind == "host_tool":
                    deadline = time.monotonic() + 3
                    while True:
                        state = self._omp_bridge._handle("tool_poll", {"id": self.started_tool})
                        assert not state.get("error"), state
                        if state["done"]:
                            result = state["result"]
                            break
                        assert time.monotonic() < deadline, "CAD timeout did not surface"
                        time.sleep(0.005)
                else:
                    # The actual OMP tool-call hook and explicit deadline are
                    # covered by the pinned-runtime extension fixture.
                    result = {"content": [{"type": "text", "text": "Eval timed out after 1 seconds"}],
                              "details": {"isError": True}}
                tool_name = "eval" if failure_kind == "eval" else "timeout_probe"
                self.notify({"type": "tool_execution_end", "toolCallId": "timed-call",
                             "toolName": tool_name, "result": result, "isError": failure_kind == "eval"})
            else:
                self.notify({"type": "agent_start"})
            answer = {"role": "assistant", "content": [{"type": "text", "text": "Continuing"}],
                      "stopReason": "stop"}
            self.notify({"type": "message_end", "message": answer})
            self.notify({"type": "agent_end", "messages": [], "isTerminal": True})
            self.notify({"type": "prompt_result", "id": "fake-prompt", "status": "completed",
                         "sessionSettled": True})

    factory = FakeFactory()
    host = OmpSessionHost(
        client_factory=lambda **kwargs:
        factory.clients.append(TimeoutConversation(factory, **kwargs)) or factory.clients[-1],
        clock=clock,
    )
    chosen = {"enabled": True, "methodOrder": ["soft"]}
    first_binding = binding("first")
    try:
        first, emitted = run(host, "cache-chat", "first", request="Do the operation",
                             mode="ask", host_tools=[first_binding],
                             settings={"compaction_settings": chosen})
        client = factory.clients[0]
        assert first["ok"] and not first["cancelled"]
        assert client.idle_timeouts == [omp_host_module._UNBOUNDED_WAIT_SLICE_SECONDS] * 6
        assert clock.now == 10_000 + 5 * omp_host_module._UNBOUNDED_WAIT_SLICE_SECONDS
        tool_frame = next(frame for frame in emitted if frame.get("type") == "tool_execution_end")
        result = tool_frame["result"]
        if failure_kind == "host_tool":
            assert result["details"]["timed_out"] is True
            assert result["details"]["status"] == "still_running"
            assert "timed_out" in result["content"][0]["text"]
            assert entered.wait(2), "native worker did not start"
            assert executions == [1] and workers[0].is_alive()
        else:
            assert "timed out" in result["content"][0]["text"].lower()
            assert not executions
        bridge = client._omp_bridge
        registry = copy.deepcopy(bridge._handle("bootstrap", {}))

        next_binding = binding("next")
        second, _ = run(host, "cache-chat", "next", request="Please continue",
                        mode="ask", host_tools=[next_binding],
                        settings={"compaction_settings": chosen})
        assert second["ok"] and not second["cancelled"]
        assert factory.clients == [client] and client.stop_count == 0
        assert first["omp_session_id"] == second["omp_session_id"] == client.session_id
        assert first["omp_session_file"] == second["omp_session_file"] == client.session_file
        assert bridge._handle("bootstrap", {}) == registry
        assert client._omp_bindings["timeout_probe"] is next_binding
        assert [name for name, _ in client.controls].count("settings_set") == 1
        assert client.controls[0] == ("settings_set", {"compaction": chosen})
        assert call_names(client) == ["start", "prompt", "prompt"]
        assert [payload for name, payload in client.calls if name == "prompt"] == [
            {"message": "Do the operation", "images": None},
            {"message": "Please continue", "images": None},
        ]
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)
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
        assert client.idle_timeouts[0] == omp_host_module._UNBOUNDED_WAIT_SLICE_SECONDS
        assert clock.now - start == 5400.0
        assert call_names(client).count("abort") == 1
        assert result["terminal"]["status"] == "aborted"
        assert any(f.get("type") == "agent_end" and f.get("aborted") for f in emitted)
        assert host.cancel("r-budgeted")["active"] is False
        assert call_names(client).count("abort") == 1
    finally:
        host.shutdown()


def test_stop_ends_an_unbudgeted_turn_past_two_hours(monkeypatch):
    host_ref: dict[str, OmpSessionHost] = {}
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
        assert result["terminal"]["status"] == "aborted"
        assert call_names(client).count("abort") == 1
        assert any(f.get("type") == "agent_end" and f.get("aborted") for f in emitted)
        assert host.status(run_id="r-stop")["active"] is False
    finally:
        host.shutdown()


def test_stop_that_never_settles_still_releases_an_unbudgeted_turn(monkeypatch):
    """Unbounded waiting must stay cancellable even if OMP never confirms the abort:
    after the abort grace the turn reports cancelled instead of holding the lane forever."""
    host_ref: dict[str, OmpSessionHost] = {}
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
        assert sum(slices_after_stop) <= 2 * omp_host_module._BUDGET_ABORT_GRACE_SECONDS
        assert host.status(run_id="r-stuck")["active"] is False
        assert client.stop_count == 1
    finally:
        host.shutdown()


def test_process_death_ends_an_unbudgeted_turn(monkeypatch):
    def die_after_two_hours(client, clock):
        if clock.now - client.prompted_at > _OLD_TWO_HOUR_STOP_S:
            raise RuntimeError("omp RPC process exited (code 137)")

    host, factory, _clock = _long_turn_host(
        monkeypatch, needs_s=10 * _OLD_TWO_HOUR_STOP_S, on_slice=die_after_two_hours
    )
    try:
        result, _emitted = run(host, "dies", run_id="r-dies")

        assert result["ok"] is False
        assert result["stop_reason"] == "runtime_error"
        assert "process exited" in result["reason"]
        assert "abort" not in call_names(factory.clients[0])
    finally:
        host.shutdown()




def test_budget_expiry_stops_the_run_instead_of_orphaning_it(monkeypatch):
    host, factory, _ = _long_turn_host(monkeypatch, needs_s=20_000)
    try:
        result, emitted = run(host, "budget", run_id="r-budget", timeout=7200)
        assert not result["ok"]
        assert result["stop_reason"] == "turn_budget"
        assert call_names(factory.clients[0]).count("abort") == 1
        assert any(frame.get("type") == "run_end" and frame.get("status") == "failed"
                   for frame in emitted)
        assert host.cancel("r-budget")["active"] is False
    finally:
        host.shutdown()


def test_default_host_accessor_is_lazy_and_process_global():
    injected = OmpSessionHost(client_factory=FakeFactory())
    previous = _set_default_omp_host_for_tests(injected)
    try:
        assert get_default_omp_host() is injected
        assert get_default_omp_host() is injected
    finally:
        _set_default_omp_host_for_tests(previous)






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

    host = OmpSessionHost(client_factory=make_client)
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
    assert ("set_model", {"provider": "gemini", "modelId": "model"}) in client.calls
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
    with pytest.raises(omp_host_module.HelperCompletionError):
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
    with pytest.raises(omp_host_module.HelperCompletionError):
        host.complete_text("", "repair", provider="openai", model="chosen")
    assert factory.clients[0].model_prompt_count == 0
    assert factory.clients[0].stop_count == 1


@pytest.mark.parametrize("ending", ["cancel", "shutdown", "timeout"])
def test_helper_running_call_stops_on_cancel_shutdown_or_budget(helper_runtime, monkeypatch, ending):
    host, factory = helper_runtime
    factory.block = True
    cancel = threading.Event()
    result = {}
    monkeypatch.setattr(omp_host_module, "_HELPER_WAIT_SLICE_SECONDS", 0.01)

    def complete():
        try:
            host.complete_text("", "repair", timeout=0.1 if ending == "timeout" else 10,
                               cancel=cancel.is_set)
        except omp_host_module.HelperCompletionError as exc:
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
    with pytest.raises(omp_host_module.HelperCompletionError):
        host.complete_text("", "repair", cancel=lambda: True)
    assert not factory.clients
    host.shutdown()
    with pytest.raises(omp_host_module.HelperCompletionError):
        host.complete_text("", "repair")
    assert not factory.clients


@pytest.mark.parametrize("reply", [None, "", "   "])
def test_helper_empty_response_is_not_a_success(helper_runtime, reply):
    host, factory = helper_runtime
    factory.reply = reply
    with pytest.raises(omp_host_module.HelperCompletionError):
        host.complete_text("", "repair")
    assert factory.clients[0].stop_count == 1


def test_helper_applies_selected_compaction_overlay(helper_runtime, monkeypatch, tmp_path):
    from services import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    host, factory = helper_runtime
    policies = []
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: "/fake/omp")
    monkeypatch.setattr(omp_host_module, "_binary_supports_flag", lambda *args: True)

    def overlay(memory, settings):
        policies.append((memory, settings))
        return "/fake/overlay.yml"

    monkeypatch.setattr(omp_host_module, "_cad_session_overlay", overlay)
    chosen = {"enabled": True, "methodOrder": ["shake"]}
    assert host.complete_text("", "repair", compaction_settings=chosen) == "helper answer"
    assert policies == [("off", chosen)]
    args = factory.clients[0].kwargs["extra_args"]
    layer = str(tmp_path / "omp" / "cad-settings.yml")
    assert args[:6] == ("--no-lsp", "--config", layer, "--config", "/fake/overlay.yml", "--no-mcp")
    assert "--no-extensions" in args and "--trusted-extension" in args


def test_scoped_online_tool_helper_inherits_exact_model_credentials_and_policy(tmp_path, monkeypatch):
    from services.agent.omp_tools import HostToolBinding, dispatch_host_tool
    from services.agent.providers.omp_provider import helper_provider

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
            request_id = super().prompt(message, images=images)
            assert dispatch_host_tool(binding, {}, None, lambda: False)["repair"] == "repaired"
            return request_id

    def make_client(**kwargs):
        client = (HelperClient if kwargs.get("tools") == [] else ParentClient)(factory, **kwargs)
        factory.clients.append(client)
        return client

    host = OmpSessionHost(client_factory=make_client)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "operator-openai-secret")
    try:
        result, events = run(
            host, "byok-helper", host_tools=[binding],
            omp_session_dir=str(tmp_path), provider="openai", model="selected-model",
            byok_key="alice-private-token", byok_provider="openai", byok_tenant="alice",
            settings={"compaction_settings": {"methodOrder": ["soft"]},
                      "thinking": "low", "auto_retry": False})
        assert result["ok"]
        helper = factory.clients[1]
        helper_key = helper.kwargs["provider_session_id"]
        assert helper_key.startswith("byok-helper-helper-") and helper_key != "byok-helper"
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
    with pytest.raises(omp_host_module.HelperCompletionError, match="byok_unsupported_online"):
        host.complete_text("", "repair", runtime_params={"byok_key": "sk-test", "byok_provider": "unknown"})
    assert not factory.clients


class QueueingClient(FakeClient):
    """OMP's steering queue: a steer waits there until a tool boundary takes it."""

    def __init__(self, factory, **kwargs):
        super().__init__(factory, **kwargs)
        self.queue: list[dict] = []

    def steer(self, message, *, images=None):
        super().steer(message, images=images)
        self.queue.append({"text": message, "images": list(images or [])})

    def remove_queued_message(self, message, queue):
        self.calls.append(("remove_queued_message", {"message": message, "queue": queue}))
        for index, item in enumerate(self.queue if queue == "steering" else []):
            if item["text"] == message:
                del self.queue[index]
                return {"removed": True}
        return {"removed": False}

    def abort_and_restore_queue(self):
        self.calls.append(("abort_and_restore_queue", {}))
        restored = self.queue[:]
        self.queue.clear()
        self.abort()
        return {"steering": restored, "followUp": []}

    def boundary(self):
        """Inject every queued steer as its user frame, as the agent loop does."""
        for item in self.queue:
            for listener in tuple(self.listeners):
                listener({"type": "message_start",
                          "message": {"role": "user", "content": [
                              {"type": "text", "text": item["text"]}, *item["images"]]}})
        self.queue.clear()


def _queueing_host(block=True):
    factory = FakeFactory(block=block)

    def make(**kwargs):
        client = QueueingClient(factory, **kwargs)
        factory.clients.append(client)
        return client

    return OmpSessionHost(client_factory=make), factory


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
    # `prompted` fires inside the prompt RPC; the run counts as submitted (and a Stop aborts
    # through OMP instead of force-stopping) only once that call has returned.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with host._lock:
            if any(r.run_id == run_id and r.started and not r.submitting for r in host._runs.values()):
                break
        time.sleep(0.001)
    return thread, out, client


def test_stop_withdraws_an_unread_steer_and_the_next_prompt_opens_with_it():
    # c252: a steer queued behind a 15-minute wait_job; Stop aborted, and OMP's settle
    # drain answered the steer in a fresh model call nobody observed.
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "c", "r1")
    host.steer("r1", "skip the FEA, just eyeball it", steer_id="s-1")

    cancel = host.cancel("r1")
    thread.join(1.0)

    assert "abort_and_restore_queue" in call_names(client)
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
    tool = StubBinding("wait_job", context)
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
    assert "abort_and_restore_queue" in call_names(client)
    assert cancel["carried_steer_ids"] == []
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-9", "delivered")
    ]
    assert "user_steer_pending" not in context


def test_slow_emit_does_not_delay_attached_steer_delivery_state(tmp_path):
    host, factory = _queueing_host()
    context: dict = {}
    tool = StubBinding("wait_job", context)
    entered, release = threading.Event(), threading.Event()
    emitted, output = [], {}
    image = tmp_path / "board.png"
    image.write_bytes(_PNG_1PX)

    def emit(frame):
        if frame["type"] == "agent_start" and not entered.is_set():
            entered.set()
            assert release.wait(3)
        emitted.append(frame)

    turn = threading.Thread(target=lambda: output.setdefault(
        "result", host.run_turn({"request": "Work", "session_id": "steer-slow",
                                 "run_id": "steer-slow-run", "host_tools": [tool]}, emit)
    ))
    try:
        turn.start()
        deadline = time.monotonic() + 2
        while not factory.clients and time.monotonic() < deadline:
            time.sleep(0.001)
        assert factory.clients
        client = factory.clients[0]
        assert client.prompted.wait(1)
        for listener in tuple(client.listeners):
            listener({"type": "agent_start"})
        assert entered.wait(1)
        assert host.steer("steer-slow-run", "use this board",
                          [{"path": str(image), "kind": "image", "mime": "image/png"}],
                          steer_id="with-image")["steered"]
        assert context["user_steer_pending"]()
        client.boundary()
        assert not context["user_steer_pending"]()
        assert host.cancel("steer-slow-run")["carried_steer_ids"] == []
        release.set()
        turn.join(3)
        assert not turn.is_alive()
        user_frame = next(index for index, frame in enumerate(emitted)
                          if frame["type"] == "message_start")
        status = next(index for index, frame in enumerate(emitted)
                      if frame.get("steer_id") == "with-image")
        assert status == user_frame + 1
        assert emitted[status]["status"] == "delivered"
    finally:
        release.set()
        if turn.ident is not None:
            turn.join(4)
        host.shutdown()


def test_a_steer_still_queued_at_the_turn_end_is_carried_not_left_for_omp():
    host, factory = _queueing_host(block=False)

    class LateSteer(QueueingClient):
        def wait_for_idle(self, timeout=None):
            host.steer("late", "one more thing", steer_id="s-late")
            self.boundary()
            return super().wait_for_idle(timeout)

    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(LateSteer(factory, **kw))
        or factory.clients[-1]
    )
    result, emitted = run(host, "late", "late")
    client = factory.clients[-1]
    assert result["ok"] is True
    assert client.queue == []
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-late", "delivered")
    ]


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


def test_withdrawn_steer_never_reaches_the_model_and_its_text_can_be_sent_again():
    host, factory = _queueing_host()
    thread, out, client = _start(host, factory, "w", "r-withdraw")
    host.steer("r-withdraw", "add a roof", steer_id="s-roof")
    host.steer("r-withdraw", "make it PETG", steer_id="s-material")

    taken = host.withdraw_steer("r-withdraw", "s-roof")
    assert taken["withdrawn"] is True and taken["active"] is True
    assert [item["text"] for item in client.queue] == ["make it PETG"]
    # The edited message goes back in: not refused as a duplicate of the withdrawn one.
    again = host.steer("r-withdraw", "add a roof", steer_id="s-roof-2")
    assert again["steered"] is True and "duplicate" not in again

    client.boundary()
    late = host.withdraw_steer("r-withdraw", "s-material")
    assert late["ok"] is True and late["withdrawn"] is False
    cancel = host.cancel("r-withdraw")
    thread.join(1.0)
    _, emitted = out["value"]
    assert cancel["carried_steer_ids"] == []
    assert [(f["steer_id"], f["status"]) for f in emitted if f.get("type") == "agent_steer"] == [
        ("s-material", "delivered"), ("s-roof-2", "delivered")]
    assert host.withdraw_steer("r-withdraw", "s-roof-2") == {
        "ok": True, "run_id": "r-withdraw", "steer_id": "s-roof-2", "withdrawn": False,
        "active": False}


def test_withdraw_refuses_when_omp_cannot_tell_same_text_steers_apart():
    host, factory = _queueing_host()
    thread, _, client = _start(host, factory, "w", "r-same-text")
    host.steer("r-same-text", "use this", [{"data": "Zmlyc3Q=", "mime": "image/png"}],
               steer_id="s-first")
    host.steer("r-same-text", "use this", [{"data": "c2Vjb25k", "mime": "image/png"}],
               steer_id="s-second")

    refused = host.withdraw_steer("r-same-text", "s-second")
    assert refused["withdrawn"] is False and refused["reason"] == "ambiguous"
    assert len(client.queue) == 2
    assert "remove_queued_message" not in call_names(client)
    host.cancel("r-same-text")
    thread.join(1.0)


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
    monkeypatch.setattr(OmpSessionHost, "_resolve_binary", lambda self, required: None)
    return tmp_path


def test_cell_has_no_credential_route_and_never_launches(cell_runtime):
    from services.agent import request_context as tenancy

    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    try:
        with tenancy.bind_context(_cell_context("alice")):
            tenancy.actor_state_root().mkdir(parents=True)
            for params in ({}, {"byok_key": "upstream-key-forbidden", "byok_provider": "openai"}):
                result, emitted = run(host, "conversation", "run", **params)
                assert result["code"] == "credential_isolation_unsupported"
                assert not emitted
            with pytest.raises(omp_host_module.HelperCompletionError,
                               match="credential_isolation_unsupported"):
                host.complete_text("system", "diagnose", runtime_params={})
        assert not factory.clients
    finally:
        host.shutdown()


def test_cell_prestart_controls_and_client_session_paths_fail_closed(cell_runtime):
    from services.agent import request_context as tenancy

    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    owner_ctx = _cell_context("alice")
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}
    try:
        with tenancy.bind_context(owner_ctx):
            session = tenancy.mint_handle("session")
            run_id = tenancy.mint_handle("run", parent=session)
            assert host.cancel(run_id)["cancelled"]
            result, _ = run(host, session, "path-run",
                            omp_session_file="/actor/another-history.jsonl")
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
    monkeypatch.setattr(tool_images.base64, "b64encode",
                        lambda data: pytest.fail("oversized file reached base64 allocation"))
    with pytest.raises(ValueError, match="attachment_image_unavailable.*byte limit"):
        omp_host_module._prompt_payload("question", [{"path": str(image), "kind": "image"}], None)
    with pytest.raises(tool_images.ImageUnavailable, match="byte limit"):
        tool_images.load_model_image(str(image))


def test_online_inline_attachment_count_and_total_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(omp_host_module, "_PROMPT_MAX_IMAGE_BYTES", 6)
    image = {"data": "YWJj", "mimeType": "image/png"}
    assert len(omp_host_module._prompt_payload("question", [image, image], None)[1]) == 2
    with pytest.raises(ValueError, match="attachment_image_bytes_exceeded"):
        omp_host_module._prompt_payload("question", [image, image, image], None)
    with pytest.raises(ValueError, match="attachment_image_count_exceeded"):
        omp_host_module._prompt_payload("question", None, [image] * 9)


def test_online_image_attachments_are_sent_at_model_limits_not_source_size(tmp_path):
    # Screenshot-sized sources whose combined bytes exceed any per-prompt budget are
    # each normalized to the model limits, so the set is delivered, not refused.
    from PIL import Image
    from services.agent import tool_images
    source = tmp_path / "screenshot.png"
    Image.new("RGB", (2000, 2000), (40, 90, 160)).save(source, compress_level=0)
    count = 3
    assert count * source.stat().st_size > omp_host_module._PROMPT_MAX_IMAGE_BYTES
    _, images = omp_host_module._prompt_payload(
        "question", [{"path": str(source), "kind": "image", "mime": "image/png"}] * count, None)
    assert len(images) == count
    for block in images:
        raw = base64.b64decode(block["data"], validate=True)
        assert block["mimeType"] == "image/png" and len(raw) <= tool_images.MAX_BYTES
        with Image.open(io.BytesIO(raw)) as image:
            assert max(image.size) == tool_images.MAX_EDGE_PX


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


def test_settlement_wait_does_not_end_on_prompt_yield():
    class Client:
        def __init__(self):
            self.yielded = threading.Event()
            self.settled = threading.Event()

        def wait_for_idle(self, timeout=None):
            raise AssertionError("prompt yield is not session settlement")

        def wait_for_settled(self, timeout=None):
            self.yielded.set()
            assert self.settled.wait(2)

    client = Client()
    host = OmpSessionHost()
    session = omp_host_module._Session("s", client, None, None)
    run = omp_host_module._Run("r", "s")
    result = []
    thread = threading.Thread(target=lambda: result.append(host._await_turn_idle(run, session, None)))
    thread.start()
    assert client.yielded.wait(1)
    assert not result
    client.settled.set()
    thread.join(2)
    assert result == ["idle"]


def _pending_settlement_probe(monkeypatch):
    """A turn whose `get_state` settlement probe answers long after its wait slice."""
    monkeypatch.setattr(omp_host_module, "_UNBOUNDED_WAIT_SLICE_SECONDS", 0.05)
    monkeypatch.setattr(omp_host_module, "_CONTROL_CALL_SECONDS", 0.02)
    release = threading.Event()

    class Client:
        probes = 0

        def wait_for_settled(self, timeout=None):
            Client.probes += 1
            assert release.wait(5)

    client = Client()
    session = omp_host_module._Session("s", client, None, None)
    run = omp_host_module._Run("r", "s")
    result = []
    thread = threading.Thread(
        target=lambda: result.append(OmpSessionHost()._await_turn_idle(run, session, None)))
    thread.start()
    time.sleep(0.3)  # several slices pass while the one probe is still unanswered
    return Client, session, run, release, result, thread


def test_slow_settlement_probe_does_not_end_a_live_turn(monkeypatch):
    # 2026-10-30 c316: OMP answered a settlement get_state 35 s late mid-stream and the
    # 1.5 s slack closed the healthy session ("OMP control request timed out").
    client_type, session, _run, release, result, thread = _pending_settlement_probe(monkeypatch)
    assert result == [] and not session.closed
    release.set()
    thread.join(2)
    assert result == ["idle"]
    assert not session.closed
    assert client_type.probes == 1  # never stacks a second probe behind the pending one


def test_stop_is_honored_while_settlement_probe_is_pending(monkeypatch):
    monkeypatch.setattr(omp_host_module, "_BUDGET_ABORT_GRACE_SECONDS", 0.05)
    _client_type, _session, run, release, result, thread = _pending_settlement_probe(monkeypatch)
    run.cancel_requested = True
    thread.join(2)
    release.set()
    assert result == ["cancel_unsettled"]


@pytest.mark.parametrize("ending", ["stop", "closed"])
def test_stop_does_not_wait_out_the_pending_probe_slice(monkeypatch, ending):
    # 2026-11-04 c344: Stop landed while a full 30 s settlement probe was pending (and the
    # abort itself failed, closing the session); the turn only returned when that slice
    # expired, so the stopped conversation's composer refused every follow-up meanwhile.
    monkeypatch.setattr(omp_host_module, "_BUDGET_ABORT_GRACE_SECONDS", 0.05)
    release = threading.Event()

    class Client:
        def wait_for_settled(self, timeout=None):
            assert timeout == omp_host_module._UNBOUNDED_WAIT_SLICE_SECONDS
            release.wait(timeout)

    session = omp_host_module._Session("s", Client(), None, None)
    run = omp_host_module._Run("r", "s")
    outcome = []

    def wait():
        try:
            outcome.append(OmpSessionHost()._await_turn_idle(run, session, None))
        except RuntimeError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.1)
    run.cancel_requested = True
    if ending == "closed":
        session.closed = True
    thread.join(2)
    release.set()
    thread.join(1)
    assert outcome == (["cancel_unsettled"] if ending == "stop" else ["OMP RPC session was closed"])


def test_stop_restores_queued_images_and_correlation():
    image = {"type": "image", "data": "YWJj", "mimeType": "image/png"}

    class Client:
        def abort_and_restore_queue(self):
            return {"steering": [{"text": "adjust", "images": [image]}], "followUp": []}

    host = OmpSessionHost()
    run = omp_host_module._Run("r", "s", client=Client(), started=True, session_key="s")
    run.undelivered.append(omp_host_module._Steer("adjust", [image], "gui-1"))
    result = host._cancel_run(run)
    assert result["carried_steer_ids"] == ["gui-1"]
    assert host._carried_steers["s"][0].images == [image]


def test_blocked_abort_is_bounded_and_does_not_hold_host_lock(monkeypatch):
    monkeypatch.setattr(omp_host_module, "_CONTROL_CALL_SECONDS", 0.02)
    blocked = threading.Event()

    class Client:
        def abort_and_restore_queue(self):
            blocked.wait(2)
            return {"steering": [], "followUp": []}

    client = Client()
    host = OmpSessionHost()
    session = omp_host_module._Session("s", client, None, None, active_turns=1)
    host._sessions["s"] = session
    run = omp_host_module._Run("r", "s", client=client, started=True, session_key="s")
    result = host._cancel_run(run)
    assert not result["ok"]
    assert session.closed
    assert host.status()["ok"]
    blocked.set()


def test_manual_compaction_abort_without_response_settles(monkeypatch):
    monkeypatch.setattr(omp_host_module, "_BUDGET_ABORT_GRACE_SECONDS", 0.02)
    entered = threading.Event()
    release = threading.Event()

    class Client:
        _closed_error = None
        def on_notification(self, listener):
            return lambda: None
        def compact(self):
            entered.set()
            release.wait(2)
            return {"completed": True, "result": {"summary": "late"}, "method": "soft"}
        def abort_and_restore_queue(self):
            return {"steering": [], "followUp": []}
        def stop(self):
            release.set()
        def get_state(self):
            return {"model": {"provider": "fixture", "id": "fixture-model"}}

    client = Client()
    class BlockingBridge(FakeBridge):
        def call(self, operation, arguments, *, timeout, cancel_event=None):
            if operation == "compact":
                return client.compact()
            return super().call(operation, arguments, timeout=timeout, cancel_event=cancel_event)
    client._omp_bridge = BlockingBridge("", None)
    host = OmpSessionHost()
    session = omp_host_module._Session("s", client, None, None, active_turns=1, registry_key="s")
    host._sessions["s"] = session
    monkeypatch.setattr(host, "_get_or_create_session", lambda *args: session)
    monkeypatch.setattr(host, "_refresh_compaction", lambda *args: None)
    monkeypatch.setattr(host, "_configure_turn", lambda *args: None)
    frames, result = [], []
    thread = threading.Thread(target=lambda: result.append(
        host.run_compaction({"session_id": "s", "run_id": "r", "compact": {}}, frames.append)))
    thread.start()
    assert entered.wait(1)
    assert host.cancel("r")["ok"]
    thread.join(2)
    assert result and result[0]["outcome"] == "cancelled"
    assert not result[0]["ok"]
    assert any(f.get("type") == "manual_compaction_end" and f.get("aborted") for f in frames)
    assert not any(f.get("result", {}).get("summary") == "late" for f in frames)


def test_sdk_auth_and_project_memory_use_bridge_and_isolated_cwd(tmp_path, monkeypatch):
    from services.agent import config
    monkeypatch.setattr(config, "memory_backend", lambda: "off")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    try:
        auth = host.auth_call("auth_state", {"provider": "openai"})
        assert auth == {"provider": "openai", "configured": True, "oauth": False}
        result = host.memory_call("memory_search", {"root": str(tmp_path), "query": "material"})
        assert result == {"results": []}
        assert factory.clients[1].kwargs["cwd"] == str(tmp_path.resolve())
        assert factory.clients[1].stop_count == 1
        with pytest.raises(ValueError, match="unsupported"):
            host.auth_call("validate_api_key", {})
        with pytest.raises(ValueError, match="kind"):
            host.memory_call("memory_save", {"root": str(tmp_path), "content": "note", "kind": "decision"})
    finally:
        host.shutdown()


def test_manual_remote_and_incompatible_focus_refuse_without_starting_runtime():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    for compact in ({"mode": "remote"}, {"mode": "snapcompact", "instructions": "focus"}):
        result = host.run_compaction({"session_id": "s", "compact": compact}, lambda _: None)
        assert not result["ok"]
        assert result["stop_reason"] == "compaction_refused"
    assert not factory.clients


def test_trusted_extension_is_explicit_and_ambient_extensions_are_disabled():
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    try:
        result, _ = run(host, "s", "r")
        assert result["ok"]
        args = factory.clients[0].kwargs["extra_args"]
        assert "--no-extensions" in args
        extension = args[args.index("--trusted-extension") + 1]
        assert Path(extension).is_absolute()
        assert Path(extension).name == "omp_extension.ts"
        assert "--extension" not in args
    finally:
        host.shutdown()


def test_skipped_manual_compaction_is_not_reported_as_committed():
    class Skipped(FakeClient):
        def compact(self, custom_instructions=None):
            for listener in tuple(self.listeners):
                listener({"type": "manual_compaction_end", "skipped": True,
                          "errorMessage": "Nothing to compact"})
            raise RuntimeError("Nothing to compact")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=lambda **kw:
                          factory.clients.append(Skipped(factory, **kw)) or factory.clients[-1])
    try:
        result = host.run_compaction({"session_id": "s", "compact": {}}, lambda _: None)
        assert not result["ok"]
        assert result["outcome"] == "skipped"
    finally:
        host.shutdown()


def test_delayed_skip_event_wins_over_bridge_exception():
    class DelayedSkip(FakeClient):
        def compact(self, custom_instructions=None):
            listeners = tuple(self.listeners)
            def notify():
                time.sleep(0.05)
                for listener in listeners:
                    listener({"type": "manual_compaction_end", "skipped": True,
                              "errorMessage": "Nothing to compact"})
            threading.Thread(target=notify).start()
            raise RuntimeError("Nothing to compact")
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=lambda **kw:
                          factory.clients.append(DelayedSkip(factory, **kw)) or factory.clients[-1])
    frames = []
    try:
        result = host.run_compaction({"session_id": "s", "compact": {}}, frames.append)
        assert not result["ok"] and result["outcome"] == "skipped"
        assert frames[-1]["skipped"] is True
        assert not any(frame.get("type") == "context_update" for frame in frames)
    finally:
        host.shutdown()


@pytest.mark.parametrize(("configured", "opted_in", "allowed"), [
    (False, False, False),
    (False, True, True),
    (True, False, True),
])
def test_selected_runtime_model_requires_keyless_opt_in(monkeypatch, tmp_path, configured, opted_in, allowed):
    from services.agent import config
    original = FakeBridge.call
    def auth(self, operation, arguments, *, timeout, cancel_event=None):
        if operation == "auth_state" and arguments["provider"] == "zenmux":
            return {"provider": "zenmux", "configured": configured, "keylessMarker": not configured}
        return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
    monkeypatch.setattr(FakeBridge, "call", auth)
    monkeypatch.setattr(config, "load_config", lambda: {"agent": {
        "keyless_providers": ["zenmux"] if opted_in else []}})
    class DefaultKeyless(FakeClient):
        def __init__(self, factory, **kwargs):
            super().__init__(factory, **kwargs)
            self.model = {"provider": "zenmux", "id": "exact-existing-model"}
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=lambda **kw:
                          factory.clients.append(DefaultKeyless(factory, **kw)) or factory.clients[-1])
    journal = tmp_path / "saved.jsonl"
    original_journal = b'{"type":"message","thinkingSignature":"opaque-cache"}\n'
    journal.write_bytes(original_journal)
    try:
        result, _ = run(host, "existing", "turn", omp_session_file=str(journal))
        assert result["ok"] is allowed
        assert factory.clients[0].model_prompt_count == int(allowed)
        assert factory.clients[0].model["id"] == "exact-existing-model"
        assert journal.read_bytes() == original_journal
        if not allowed:
            assert "explicitly enable" in result["reason"]
    finally:
        host.shutdown()


def test_tool_free_helper_rejects_unapproved_keyless_default(helper_runtime, monkeypatch):
    from services.agent import config
    host, factory = helper_runtime
    monkeypatch.setattr(config, "load_config", lambda: {"agent": {"keyless_providers": []}})
    monkeypatch.setattr(FakeClient, "get_state", lambda self: {
        "model": {"provider": "zenmux", "id": "default"}, "sessionFile": self.session_file})
    original = FakeBridge.call
    def auth(self, operation, arguments, *, timeout, cancel_event=None):
        if operation == "auth_state" and arguments["provider"] == "zenmux":
            return {"provider": "zenmux", "configured": False, "keylessMarker": True}
        return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
    monkeypatch.setattr(FakeBridge, "call", auth)
    with pytest.raises(omp_host_module.HelperCompletionError, match="explicitly enable"):
        host.complete_text("", "private helper")
    assert factory.clients[0].model_prompt_count == 0
    assert factory.clients[0].kwargs["tools"] == []


def test_live_keyless_revocation_reaches_child_policy_before_next_turn(monkeypatch):
    from services.agent import config
    selected = ["zenmux"]
    monkeypatch.setattr(config, "load_config", lambda: {"agent": {"keyless_providers": list(selected)}})
    original = FakeBridge.call
    def auth(self, operation, arguments, *, timeout, cancel_event=None):
        if operation == "auth_state" and arguments["provider"] == "zenmux":
            return {"provider": "zenmux", "configured": False}
        return original(self, operation, arguments, timeout=timeout, cancel_event=cancel_event)
    monkeypatch.setattr(FakeBridge, "call", auth)
    factory = FakeFactory()
    host = OmpSessionHost(client_factory=factory)
    try:
        first, _ = run(host, "s", "r1", provider="zenmux", model="exact")
        assert first["ok"]
        assert factory.clients[0]._omp_bridge.keyless_providers == ["zenmux"]
        selected.clear()
        second, _ = run(host, "s", "r2", provider="zenmux", model="exact")
        assert not second["ok"]
        assert factory.clients[0]._omp_bridge.keyless_providers == []
        assert factory.clients[0].model_prompt_count == 1
    finally:
        host.shutdown()


_CAD_LOADS_RUNTIME_FIXTURE = r'''
import { rename, unlink } from "node:fs/promises";
import { getBundledModels } from "@oh-my-pi/pi-catalog/models";

async function respond(value) {
  const report = process.env.OMP_CAD_LOADS_TEST_REPORT;
  await Bun.write(report + ".tmp", JSON.stringify(value));
  await rename(report + ".tmp", report);
}

export default function (pi) {
  pi.on("session_start", async (_event, ctx) => {
    if (ctx.agent.kind !== "main") return;
    if (process.env.OMP_CAD_LOADS_TEST_PREFIX === "1") {
      const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
      if (!session || session.sessionManager !== ctx.sessionManager) throw new Error("session unavailable");
      const model = getBundledModels("anthropic")
        .map(item => ctx.modelRegistry.find(item.provider, item.id))
        .find(item => item?.thinking?.prefixBinding === true);
      if (!model) throw new Error("prefix-binding model unavailable in bundled registry");
      if (!ctx.modelRegistry.hasConfiguredAuth(model)) throw new Error("fixture auth unavailable");
      session.agent.setModel(model);
      await session.refreshBaseSystemPrompt();
    }
    let busy = false;
    ctx.setInterval(() => {
      if (busy) return;
      busy = true;
      void (async () => {
        let command;
        try {
          command = JSON.parse(await Bun.file(process.env.OMP_CAD_LOADS_TEST_COMMAND).text());
          await unlink(process.env.OMP_CAD_LOADS_TEST_COMMAND);
        } catch { return; }
        try {
          const session = pi.pi.AgentRegistry.global().get(ctx.agent.id)?.session;
          if (!session || session.sessionManager !== ctx.sessionManager) throw new Error("session unavailable");
          let result;
          if (command.action === "seed") {
            await session.sessionManager.ensureOnDisk();
            await session.sessionManager.flush();
            result = { id: session.sessionManager.getSessionId(), file: session.sessionManager.getSessionFile(),
                       cwd: session.sessionManager.getHeader()?.cwd };
          } else if (command.action === "prefix_seed") {
            if (!await session.newSession({ providerPromptCacheKey: "fixture-cache-affinity" })) {
              throw new Error("cannot initialize the synthetic session");
            }
            const manager = session.sessionManager;
            const model = session.agent.state.model;
            if (!model?.thinking?.prefixBinding) throw new Error("prefix binding not active");
            await session.refreshBaseSystemPrompt();
            session.agent.promptCacheKey = "fixture-cache-affinity";
            const prompt = session.baseSystemPrompt;
            let digest = BigInt(prompt.length);
            for (const block of prompt) digest = Bun.hash.wyhash(block, digest);
            const user = { role: "user", content: [{ type: "text", text: "fixture request" }], timestamp: Date.now() };
            const reply = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
              content: [{ type: "thinking", thinking: "fixture", thinkingSignature: "fixture-signature" },
                { type: "toolCall", id: "fixture-enable", name: "enable_tools", arguments: { tools: ["cad_guide"] } }],
              usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
              stopReason: "toolUse", timestamp: Date.now() };
            manager.appendMessage(user);
            manager.appendCustomEntry("system-prompt-digest", digest.toString(16));
            manager.appendMessage(reply);
            session.agent.replaceMessages([user, reply]);
            await manager.flush();
            result = { id: manager.getSessionId(), file: manager.getSessionFile(), cwd: manager.getHeader()?.cwd };
          } else if (command.action === "prefix_snapshot") {
            const model = session.agent.state.model;
            const reply = session.agent.state.messages.findLast(item => item.role === "assistant");
            const thinking = reply?.content?.find(item => item.type === "thinking");
            result = { base: session.baseSystemPrompt, effective: session.agent.state.systemPrompt,
              cache: session.getPromptCacheKeyForModel(model) ?? session.agent.sessionId,
              prefixBinding: model?.thinking?.prefixBinding === true,
              signature: thinking?.thinkingSignature ?? null,
              providerTools: session.agent.state.tools.map(tool => tool.name),
              mounted: [...session.getMountedXdevToolNames()].sort() };
          } else if (command.action === "branch_point") {
            const manager = session.sessionManager;
            const entry = manager.appendMessage({ role: "user",
              content: [{ type: "text", text: "isolated fork boundary" }], timestamp: Date.now() });
            await manager.flush();
            result = { entry };
          } else if (command.action === "legacy_seed") {
            const manager = session.sessionManager;
            let sequence = 0;
            const record = (toolName, details, isError, text = "fixture") =>
              manager.appendMessage({ role: "toolResult", toolName,
                toolCallId: `legacy-${++sequence}`, content: [{ type: "text", text }],
                details, isError, timestamp: Date.now() });
            record("enable_tools", { ok: true, added: ["add_features"] }, false);
            manager.appendResetBoundary();
            const activeParent = record("enable_tools", { ok: true, added: ["cad_guide"] }, false);
            record("enable_tools", { ok: true, added: ["add_features"] }, false);
            manager.branch(activeParent);
            record("enable_tools", { ok: true, added: ["add_features"] }, true);
            record("enable_tools", { ok: false, added: ["add_features"] }, false);
            record("enable_tools", { ok: true, added: ["add_features", 42] }, false);
            record("other_tool", { ok: true, added: ["add_features"] }, false);
            record("enable_tools", undefined, false, '{"ok":true,"added":["add_features"]}');
            await manager.flush();
            result = { id: manager.getSessionId(), file: manager.getSessionFile() };
          } else {
            const tool = session.extensionRunner?.getRegisteredTool(command.name)?.definition;
            if (!tool || !pi.getActiveTools().includes(command.name)) throw new Error("tool is not active");
            result = await tool.execute("cad-loads-test", command.args, undefined, undefined,
                                        session.extensionRunner.createContext());
            if (command.action === "prefix_enable") {
              const toolResult = { role: "toolResult", toolName: command.name, toolCallId: "fixture-enable",
                content: result.content, details: result.details, isError: false, timestamp: Date.now() };
              session.sessionManager.appendMessage(toolResult);
              session.agent.replaceMessages([...session.agent.state.messages, toolResult]);
              await session.sessionManager.flush();
            }
          }
          await respond({ ok: true, result });
        } catch (error) {
          await respond({ ok: false, error: String(error) });
        }
      })().finally(() => { busy = false; });
    }, 50);
  });
}
'''


_CAD_LOADS_RUNTIME_RUNNER = r'''
import json
import os
from pathlib import Path
import time

from mcp.server.fastmcp import FastMCP
from omp_rpc import RpcClient
from services.agent import config, tool_categories
from services.agent.omp_host import OmpSessionHost
from services.agent.omp_roles import _register_category_refresh
from services.agent.omp_tools import build_host_tools
from services.agent.worker_rpc import _legacy_tool_loads_owner
from services.mcp.cad_doc_tools import register_cad_doc_tools

root = Path(os.environ["OMP_CAD_LOADS_TEST_ROOT"])
command_file = root / "probe-command.json"
report_file = root / "probe-report.json"
config.get_tool_options = lambda *_: {
    "enabled": True, "timeout_s": None, "max_calls_per_turn": None,
    "param_defaults": {}, "prompt_note": "",
}
config.tool_timeout_s_default = lambda: 0

def resolve(tokens):
    found, unknown = set(), []
    for token in tokens:
        if token in {"cad_guide", "add_features"}:
            found.add(token)
        else:
            unknown.append(token)
    return found, unknown

tool_categories.resolve_tool_requests = resolve
mcp = FastMCP("cad-loads-persistence-test")
register_cad_doc_tools(mcp)
entries = {name: mcp._tool_manager._tools[name] for name in ("cad_guide", "add_features")}

class Policy:
    def deny_reason(self, name):
        return None

def factory(**kwargs):
    kwargs["extra_args"] = (*kwargs.get("extra_args", ()), "--trusted-extension",
                            str(root / "probe.ts"))
    kwargs["env"] = {
        **kwargs.get("env", {}),
        "OMP_CAD_LOADS_TEST_COMMAND": str(command_file),
        "OMP_CAD_LOADS_TEST_REPORT": str(report_file),
    }
    if os.environ.get("OMP_CAD_LOADS_TEST_PREFIX") == "1":
        # OMP's official test-runtime guard prevents model discovery network I/O.
        kwargs["env"]["PI_TEST_RUNTIME"] = "1"
        # Synthetic startup auth only; the fixture never sends a model request.
        kwargs["env"]["ANTHROPIC_API_KEY"] = "fixture-only-never-sent"
    return RpcClient(**kwargs)

def probe(action, name="", args=None):
    report_file.unlink(missing_ok=True)
    temporary = root / "probe-command.tmp"
    temporary.write_text(json.dumps({"action": action, "name": name, "args": args or {}}))
    os.replace(temporary, command_file)
    deadline = time.monotonic() + 30
    while not report_file.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("runtime probe timed out")
        time.sleep(0.05)
    response = json.loads(report_file.read_text())
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response["result"]

def open_session(host, conversation, resume=None, tenant=""):
    categories = []
    context = {
        "session_id": conversation, "run_id": "test-run", "workspace_dir": str(root),
        "project_dir": str(root), "mode": "agent", "allowed_tools": None,
        "categories": categories, "enabled_categories": categories,
        "tool_load_tenant": tenant,
        "persist_tool_loads": True, "tool_entries": entries,
        "resolver": lambda name: entries[name].fn if name in entries else None,
        "legacy_tool_loads_owner": lambda: _legacy_tool_loads_owner(
            {"sessions_dir": str(root / "sessions"), "omp_session_file": resume},
            conversation, str(root)),
        "policy_registry": Policy(), "omp_builtin_names": (),
    }
    bindings = build_host_tools(context)
    assert not next(binding for binding in bindings if binding.name == "cad_guide").active
    params = {
        "workspace_dir": str(root), "omp_session_dir": str(root / "sessions"),
        "host_tools": bindings, "register_callbacks": _register_category_refresh(context, bindings),
    }
    session = host._get_or_create_session(conversation, params, resume)
    host._configure_turn(session, params)
    return session

def active(session):
    state = session.client._omp_bridge.call("tool_state", {}, timeout=20)
    return {item["name"] for item in state["tools"] if item["available"]}

host = OmpSessionHost(client_factory=factory)
try:
    phase = os.environ["OMP_CAD_LOADS_TEST_PHASE"]
    if phase in {"enable", "legacy_seed", "prefix_seed"}:
        session = open_session(host, "conversation")
        assert "cad_guide" not in active(session)
        identity = probe("prefix_seed" if phase == "prefix_seed" else "seed")
        assert Path(identity["cwd"]).resolve() == root.resolve(), (
            f"OMP journal workspace {identity['cwd']!r} differs from {str(root)!r}"
        )
        path = Path(identity["file"])
        prefix = path.read_bytes().hex()
        if phase == "legacy_seed":
            probe("legacy_seed")
        else:
            before = probe("prefix_snapshot") if phase == "prefix_seed" else None
            enabled = probe("prefix_enable" if phase == "prefix_seed" else "call",
                            "enable_tools", {"tools": ["cad_guide"]})
            assert enabled["details"]["ok"] is True
            assert "cad_guide" in active(session)
            if before is not None:
                after = probe("prefix_snapshot")
                assert before["prefixBinding"] and before["signature"] == "fixture-signature"
                assert before["cache"] == "fixture-cache-affinity"
                assert before["base"] == after["base"], "tool loading changed the signed base prompt"
                assert before["effective"] == after["effective"], "tool loading changed the effective prompt"
                assert before["cache"] == after["cache"], "tool loading changed cache lineage"
                assert ("cad_guide" in after["providerTools"] or "cad_guide" in after["mounted"])
                identity["before"] = before
                identity["after"] = after
        assert path.read_bytes().startswith(bytes.fromhex(prefix))
        print(json.dumps({**identity, "prefix": prefix}))
    else:
        path = os.environ["OMP_CAD_LOADS_TEST_JOURNAL"]
        expected = os.environ["OMP_CAD_LOADS_TEST_ID"]
        session = open_session(host, "conversation", path)
        assert session.client.get_state().session_id == expected
        if phase == "legacy_unowned":
            assert "cad_guide" not in active(session)
            print(json.dumps({"id": expected, "resumed": False}))
        else:
            selected = active(session)
            assert "add_features" not in selected, f"{phase}: unexpected mutator: {sorted(selected)}"
            assert "cad_guide" in selected, f"{phase}: enabled tool missing: {sorted(selected)}"
            result = probe("call", "cad_guide", {"topic": "sketch"})
            assert isinstance(result["details"], str) and "sketch" in result["details"].lower()
            if phase == "prefix_resume":
                snapshot = probe("prefix_snapshot")
                assert snapshot["prefixBinding"] and snapshot["signature"] == "fixture-signature"
                assert snapshot["cache"] == "fixture-cache-affinity"
                print(json.dumps({"id": expected, "resumed": True, "snapshot": snapshot}))
            elif phase == "fork":
                point = probe("branch_point")
                parent_bytes = Path(path).read_bytes()
                host._release_live_session(session)
                child = host.branch_session("conversation", point["entry"])
                assert child["ok"], child
                assert child["omp_session_id"] != expected
                child_path = Path(child["omp_session_file"])
                assert child_path != Path(path)
                child_prefix = child_path.read_bytes()

                foreign = open_session(host, "foreign-fork", str(child_path), tenant="other-tenant")
                assert "cad_guide" not in active(foreign)
                try:
                    probe("call", "enable_tools", {"tools": ["cad_guide"]})
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("another credential owner claimed inherited fork tools")
                assert "cad_guide" not in active(foreign)
                assert child_path.read_bytes() == child_prefix
                host._release_live_session(foreign)
                assert host.release_session("foreign-fork")["released"]

                fork = open_session(host, "fork", str(child_path))
                assert "cad_guide" in active(fork)
                loaded = probe("call", "enable_tools", {"tools": ["add_features"]})
                assert loaded["details"]["ok"] is True
                assert {"cad_guide", "add_features"} <= active(fork)
                host._release_live_session(fork)
                assert host.release_session("fork")["released"]

                claimed_prefix = child_path.read_bytes()
                wrong = open_session(host, "wrong-fork", str(child_path))
                assert not ({"cad_guide", "add_features"} & active(wrong))
                try:
                    probe("call", "enable_tools", {"tools": ["cad_guide"]})
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("another conversation claimed the same fork journal")
                assert not ({"cad_guide", "add_features"} & active(wrong))
                assert child_path.read_bytes() == claimed_prefix
                assert child_path.read_bytes().startswith(child_prefix)
                assert Path(path).read_bytes() == parent_bytes
                print(json.dumps({"id": expected, "resumed": True, "fork": True}))
            else:
                host._release_live_session(session)
                assert host.release_session("conversation")["released"]
                wrong_owner = open_session(host, "other-conversation", path)
                assert wrong_owner.client.get_state().session_id == expected
                assert "cad_guide" not in active(wrong_owner)
                host._release_live_session(wrong_owner)
                assert host.release_session("other-conversation")["released"]
                other = open_session(host, "new-conversation")
                assert "cad_guide" not in active(other)
                print(json.dumps({"id": expected, "resumed": True}))
finally:
    host.shutdown()
'''


@pytest.mark.parametrize("case", ["ordinary", "legacy", "prefix", "fork"])
def test_official_omp_restores_enabled_tools_in_fresh_worker(tmp_path, case):
    configured = os.environ.get("CHUDWARE_OMP_TEST_BIN")
    if not configured:
        pytest.skip("set CHUDWARE_OMP_TEST_BIN to the digest-pinned official OMP artifact")
    pytest.importorskip("omp_rpc")
    from scripts.pin_omp import host_target, load_lock

    binary = Path(configured).expanduser().resolve(strict=True)
    target = os.environ.get("CHUDWARE_OMP_TEST_TARGET") or host_target()
    with binary.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == load_lock()["artifacts"][target]["sha256"]
    repo = Path(__file__).resolve().parents[2]
    (tmp_path / "probe.ts").write_text(_CAD_LOADS_RUNTIME_FIXTURE, encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    environment = {key: os.environ[key] for key in (
        "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    environment.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(tmp_path / "appdata"), "LOCALAPPDATA": str(tmp_path / "local"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "PYTHONPATH": str(repo),
        "CHUDWARE_OMP_BIN": str(binary), "OMP_CAD_LOADS_TEST_ROOT": str(tmp_path),
        "CHUDWARE_PROJECT_DIR": str(tmp_path), "CHUDWARE_PROJECT_SAVE_DIR": str(tmp_path),
    })
    environment["OMP_CAD_LOADS_TEST_PREFIX"] = "1" if case == "prefix" else "0"

    def phase(name, **extra):
        completed = subprocess.run(
            [sys.executable, "-c", _CAD_LOADS_RUNTIME_RUNNER], cwd=tmp_path,
            env={**environment, "OMP_CAD_LOADS_TEST_PHASE": name, **extra},
            capture_output=True, text=True, check=False, timeout=150,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout
        return json.loads(completed.stdout)

    first = phase({"legacy": "legacy_seed", "prefix": "prefix_seed"}.get(case, "enable"))
    journal = Path(first["file"])
    assert journal.read_bytes().startswith(bytes.fromhex(first["prefix"]))
    before_resume = journal.read_bytes()
    identity = {"OMP_CAD_LOADS_TEST_JOURNAL": str(journal),
                "OMP_CAD_LOADS_TEST_ID": first["id"]}
    if case == "legacy":
        unowned = phase("legacy_unowned", **identity)
        assert unowned == {"id": first["id"], "resumed": False}
        gui = tmp_path / "sessions" / "conversation.json"
        link = {"id": "conversation", "project": str(tmp_path),
                "omp": {"session_id": "wrong-runtime-id", "session_file": str(journal)}}
        gui.write_text(json.dumps(link), encoding="utf-8")
        gui.chmod(0o600)
        assert phase("legacy_unowned", **identity) == unowned
        link["omp"]["session_id"] = first["id"]
        gui.write_text(json.dumps(link), encoding="utf-8")
        resumed = phase("legacy_resume", **identity)
        assert resumed == {"id": first["id"], "resumed": True}
        assert journal.read_bytes().startswith(before_resume)
        migrated_prefix = journal.read_bytes()
        gui.unlink()
        assert phase("resume", **identity) == resumed
        assert journal.read_bytes().startswith(migrated_prefix)
    elif case == "prefix":
        resumed = phase("prefix_resume", **identity)
        assert resumed["id"] == first["id"] and resumed["resumed"] is True
        before, enabled, restored = first["before"], first["after"], resumed["snapshot"]
        assert before["base"] == enabled["base"] == restored["base"]
        assert before["effective"] == enabled["effective"] == restored["effective"]
        assert before["cache"] == enabled["cache"] == restored["cache"] == "fixture-cache-affinity"
        assert restored["signature"] == before["signature"] == "fixture-signature"
        assert restored["providerTools"] == enabled["providerTools"]
        assert restored["mounted"] == enabled["mounted"]
    elif case == "fork":
        assert phase("fork", **identity) == {"id": first["id"], "resumed": True, "fork": True}
    else:
        resumed = phase("resume", **identity)
        assert resumed == {"id": first["id"], "resumed": True}
    assert journal.read_bytes().startswith(before_resume)


# --- public demo: quota refusals and the lane-only cell -----------------------------------

_QUOTA_429 = ('429 {"error":{"code":"demo_quota_exhausted",'
              '"message":"This demo session has used all of its AI requests."}}')
_QUOTA_TEXT = "This demo session has used all of its AI requests."


class QuotaClient(FakeClient):
    """OMP as it behaves on a front-door 429: announce a retry, then fail it when cancelled."""

    frames: list = []

    def abort_retry(self):
        self.calls.append(("abort_retry", {}))
        self.factory.aborted.set()

    def wait_for_idle(self, timeout=None):
        self.idle_timeouts.append(timeout)
        for event in self.frames:
            for listener in tuple(self.listeners):
                listener(event)
        return ()


def _quota_host(frames):
    factory = FakeFactory()
    factory.aborted = threading.Event()

    class Client(QuotaClient):
        pass

    Client.frames = frames
    host = OmpSessionHost(
        client_factory=lambda **kw: factory.clients.append(Client(factory, **kw)) or factory.clients[-1])
    return host, factory


def _turn(host, name):
    return host.run_turn({"request": "Do the work", "session_id": name, "run_id": f"r-{name}"},
                         lambda _frame: None)


def test_demo_quota_refusal_cancels_omp_retry_and_reports_the_lanes_message():
    host, factory = _quota_host([
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 10, "delayMs": 500,
         "errorMessage": _QUOTA_429},
        {"type": "auto_retry_end", "success": False, "attempt": 1, "finalError": "Retry cancelled"},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ])
    try:
        result = _turn(host, "quota")
        assert factory.aborted.wait(2), "OMP's automatic retry was left running"
        assert result["ok"] is False
        assert result["stop_reason"] == "provider_error"
        assert result["reason"] == _QUOTA_TEXT
    finally:
        host.shutdown()


def test_demo_quota_refusal_as_a_plain_assistant_error_reads_as_the_lanes_message():
    host, factory = _quota_host([
        {"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "errorMessage": _QUOTA_429}},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ])
    try:
        result = _turn(host, "plain")
        assert (result["stop_reason"], result["reason"]) == ("provider_error", _QUOTA_TEXT)
        assert not factory.aborted.is_set()
    finally:
        host.shutdown()


@pytest.mark.parametrize("message", [
    "429 rate limit exceeded", "503 upstream busy",
    '429 {"error":{"code":"upstream_rate_limited","message":"slow down"}}',
])
def test_other_provider_failures_keep_omp_retry_and_their_own_error(message):
    host, factory = _quota_host([
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 10, "delayMs": 500,
         "errorMessage": message},
        {"type": "auto_retry_end", "success": False, "attempt": 10, "finalError": message},
        {"type": "agent_end", "messages": [], "isTerminal": True},
    ])
    try:
        result = _turn(host, "transient")
        assert not factory.aborted.wait(0.3)
        assert result["stop_reason"] == "provider_error"
        assert result["reason"] == f"provider retry exhausted: {message}"
    finally:
        host.shutdown()


def test_abort_retry_never_blocks_on_a_client_that_cannot_answer():
    release = threading.Event()
    started = threading.Event()

    class Stuck:
        def abort_retry(self):
            started.set()
            release.wait(5)

    began = time.monotonic()
    omp_host_module._abort_retry(Stuck())
    assert time.monotonic() - began < 1.0  # the RPC reader thread would otherwise deadlock itself
    assert started.wait(2)
    release.set()
    omp_host_module._abort_retry(object())  # a client without the call is simply skipped


def test_helper_completion_reports_a_demo_quota_refusal_and_cancels_retry(helper_runtime):
    host, factory = helper_runtime
    original = host._client_factory
    aborted = threading.Event()

    def make_client(**kwargs):
        client = original(**kwargs)
        client.abort_retry = aborted.set
        client.events.insert(1, {"type": "auto_retry_start", "attempt": 1, "errorMessage": _QUOTA_429})
        client.events.insert(2, {"type": "auto_retry_end", "success": False, "finalError": "Retry cancelled"})
        return client

    host._client_factory = make_client
    with pytest.raises(omp_host_module.HelperCompletionError, match=_QUOTA_TEXT):
        host.complete_text("", "repair")
    assert aborted.wait(2)


ROWS = [
    {"id": "example/model-a:free", "name": "Model A (free)", "provider": "openrouter",
     "contextWindow": 65536, "maxTokens": 8192},
    {"id": "example/model-b:free", "name": "Model B (free)", "provider": "openrouter"},
]


@pytest.fixture
def demo_cell(monkeypatch, tmp_path):
    """A cell of a demo container: the lane is mounted, nothing else about the cell changes."""
    from services.agent import demo_lane

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "actor-data"))
    monkeypatch.setattr(demo_lane, "enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(demo_lane, "models", lambda **kwargs: [dict(row) for row in ROWS])
    return tmp_path


def test_non_demo_cell_still_has_no_credential_route_even_if_a_socket_exists(cell_runtime, monkeypatch):
    from services.agent import demo_lane, request_context as tenancy

    monkeypatch.setattr(demo_lane, "enabled", lambda *args, **kwargs: False)
    with tenancy.bind_context(_cell_context("alice")):
        with pytest.raises(ValueError, match="credential_isolation_unsupported"):
            omp_host_module._credential_scope({})


def test_demo_cell_credential_scope_is_the_placeholder_lane_key_and_per_actor(demo_cell):
    from services.agent import request_context as tenancy

    scopes = {}
    for actor in ("alice", "bob"):
        with tenancy.bind_context(_cell_context(actor)):
            scopes[actor] = omp_host_module._credential_scope({})
            assert omp_host_module._credential_identity({}) == omp_host_module._credential_identity({})
    assert scopes["alice"] == ("alice", "openrouter", "chudware-demo-lane", "")
    assert scopes["bob"][0] == "bob"
    with tenancy.bind_context(_cell_context("alice")):
        alice = omp_host_module._credential_identity({})
    with tenancy.bind_context(_cell_context("bob")):
        assert omp_host_module._credential_identity({}) != alice


@pytest.mark.parametrize("extra", [
    {"api_key": "sk-visitor"}, {"byok_key": "sk-visitor"}, {"byok_key": None},
    {"byok_key": "sk-visitor", "byok_provider": "openai"},
])
def test_demo_cell_refuses_a_visitors_own_key(demo_cell, extra):
    from services.agent import request_context as tenancy

    with tenancy.bind_context(_cell_context("alice")):
        with pytest.raises(ValueError, match="credential_isolation_unsupported"):
            omp_host_module._credential_scope(extra)
        factory = FakeFactory()
        host = OmpSessionHost(client_factory=factory)
        try:
            result, _ = run(host, "conversation", "run", **extra)
            assert result["code"] == "credential_isolation_unsupported"
            assert not factory.clients
        finally:
            host.shutdown()


def test_demo_keyless_opt_in_is_the_lane_provider_and_reads_no_config(demo_cell, monkeypatch):
    from services.agent import config

    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("a demo cell reads no user config"))
    assert omp_host_module._keyless_provider_opt_ins() == ["openrouter"]


def test_demo_client_state_is_actor_private_and_points_openrouter_at_the_lane(demo_cell):
    from services.agent import request_context as tenancy

    with tenancy.bind_context(_cell_context("alice")):
        state = tenancy.actor_state_root()
        tempdir, session_dir, env = omp_host_module._demo_client_scope("abc123", None)
        try:
            assert set(env) == {"PI_CODING_AGENT_DIR"}  # nothing else is rebuilt or blanked
            agent = Path(env["PI_CODING_AGENT_DIR"])
            assert state in agent.parents  # beneath /actor: visible to the nested OMP sandbox
            assert Path(session_dir) == state / "Sessions" / "demo-abc123"
            assert (Path(session_dir).stat().st_mode & 0o777) == 0o700
            config = agent / "models.yml"
            assert (config.stat().st_mode & 0o777) == 0o600
            provider = json.loads(config.read_text())["providers"]["openrouter"]
            assert provider["baseUrl"] == "http://127.0.0.1:18081/v1"
            assert [m["id"] for m in provider["models"]] == [r["id"] for r in ROWS]
            assert "sk-" not in config.read_text()
        finally:
            tempdir.cleanup()
        assert not agent.exists()


def test_demo_client_state_survives_an_unlistable_lane_with_an_override_only_config(demo_cell, monkeypatch):
    from services.agent import demo_lane, request_context as tenancy

    def down(**kwargs):
        raise demo_lane.LaneError("demo model lane unavailable: URLError")

    monkeypatch.setattr(demo_lane, "models", down)
    with tenancy.bind_context(_cell_context("alice")):
        tempdir, _, env = omp_host_module._demo_client_scope("abc123", None)
        try:
            provider = json.loads((Path(env["PI_CODING_AGENT_DIR"]) / "models.yml").read_text())[
                "providers"]["openrouter"]
            assert "models" not in provider and provider["baseUrl"] == "http://127.0.0.1:18081/v1"
        finally:
            tempdir.cleanup()


def test_demo_client_scope_ignores_client_chosen_session_directories(demo_cell):
    from services.agent import request_context as tenancy

    with tenancy.bind_context(_cell_context("alice")):
        tenancy.actor_state_root().mkdir(parents=True)
        tempdir, session_dir, _ = omp_host_module._credential_client_scope(
            {}, "/work/attacker-chosen", None)
        try:
            assert Path(session_dir).is_relative_to(tenancy.actor_state_root())
            assert not Path("/work/attacker-chosen").exists()
        finally:
            tempdir.cleanup()


def test_demo_client_scope_refuses_a_journal_from_another_scope_and_symlinked_roots(demo_cell):
    from services.agent import request_context as tenancy

    with tenancy.bind_context(_cell_context("alice")):
        state = tenancy.actor_state_root()
        state.mkdir(parents=True)
        stray = demo_cell / "stray.jsonl"
        stray.write_text("{}")
        with pytest.raises(RuntimeError, match="another credential scope"):
            omp_host_module._demo_client_scope("abc123", str(stray))
        (state / "Sessions").symlink_to(demo_cell)
        with pytest.raises(RuntimeError, match="symlink"):
            omp_host_module._demo_client_scope("abc123", None)


def test_cell_extension_bridge_lives_beneath_the_actor_runtime_root(monkeypatch, tmp_path):
    from services.agent import request_context as tenancy

    runtime_root = tmp_path / "actor-run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    assert omp_host_module._bridge_parent() is None  # desktop: the system temporary directory
    monkeypatch.setattr(tenancy, "in_cell", lambda: True)
    assert omp_host_module._bridge_parent() == str(runtime_root)
    assert runtime_root.is_dir() and (runtime_root.stat().st_mode & 0o777) == 0o700


def test_demo_cell_launches_omp_with_the_lane_config_and_a_bridge_the_sandbox_shares(
    demo_cell, monkeypatch
):
    from services.agent import request_context as tenancy

    runtime_root = demo_cell / "actor-run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(OmpSessionHost, "_resolve_binary", lambda self, required: "/opt/chudware/bin/omp")
    monkeypatch.setattr(omp_host_module, "_binary_supports_flag", lambda binary, flag: True)
    monkeypatch.setattr(omp_host_module, "_cad_system_prompt_template", lambda binary: None)
    launched = []

    def capture(kwargs, params):
        # Read the config while the client's private state still exists.
        agent = Path(kwargs["env"]["PI_CODING_AGENT_DIR"])
        launched.append({**kwargs, "models_yml": json.loads((agent / "models.yml").read_text())})
        kwargs["command"] = ["/opt/chudware/bin/chudware-actor-sandbox", "hex", "rw", "--", "omp"]

    monkeypatch.setattr(omp_host_module, "_sandbox_client_kwargs", capture)
    runtime_dirs = []

    class RecordingBridge(FakeBridge):
        def __init__(self, runtime_dir, dispatch):
            runtime_dirs.append(runtime_dir)
            super().__init__(runtime_dir, dispatch)

    monkeypatch.setattr("services.agent.omp_bridge.ExtensionBridge", RecordingBridge)
    factory = FakeFactory()

    def make_client(**kwargs):
        client = factory(**kwargs)
        # A cell only links journals beneath its own actor state root.
        client.session_file = str(tenancy.actor_state_root() / "Sessions" / "demo-journal.jsonl")
        return client

    host = OmpSessionHost(client_factory=make_client)
    try:
        with tenancy.bind_context(_cell_context("alice")):
            tenancy.actor_state_root().mkdir(parents=True)
            session = tenancy.mint_handle("session")
            result, _ = run(host, session, tenancy.mint_handle("run", parent=session))
        assert result["ok"], result
    finally:
        host.shutdown()
    kwargs = launched[0]
    assert Path(runtime_dirs[0]).parent == runtime_root
    provider = kwargs["models_yml"]["providers"]["openrouter"]
    assert provider["baseUrl"] == "http://127.0.0.1:18081/v1"
    assert [m["id"] for m in provider["models"]] == [r["id"] for r in ROWS]
    assert kwargs["env"]["OMP_CAD_BRIDGE_ENDPOINT"] == "/private/test.sock"
    assert Path(kwargs["session_dir"]).name.startswith("demo-")


_DIE_WITH_PARENT_CHILD = (
    "import ctypes, signal, sys, time\n"
    "ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG, what bwrap --die-with-parent does\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_client_launch_thread_outlives_a_die_with_parent_sandbox_until_stopped():
    # bubblewrap's --die-with-parent is PR_SET_PDEATHSIG, which the kernel acts on when the *thread*
    # that forked the child exits. client.start() forks the sandboxed OMP, so a launch thread that
    # returned as soon as start() did killed the sandbox right after its extension loaded.
    class Client:
        def start(self):
            self.launcher = threading.current_thread()
            self.child = subprocess.Popen(
                [sys.executable, "-c", _DIE_WITH_PARENT_CHILD], stdout=subprocess.PIPE, text=True)
            assert self.child.stdout.readline().strip() == "ready"  # PDEATHSIG armed before start returns

        def stop(self):
            self.child.kill()
            self.child.wait(5)
            return True

    client = Client()
    omp_host_module._start_client(client)
    try:
        time.sleep(0.5)
        assert client.launcher.is_alive()
        assert client.child.poll() is None, "the sandbox died with its launching thread"
    finally:
        omp_host_module._stop_client(client)
        client.child.stdout.close()
    client.launcher.join(5)
    assert not client.launcher.is_alive()


def test_failed_client_launch_does_not_park_its_thread():
    class Client:
        def start(self):
            self.launcher = threading.current_thread()
            raise RuntimeError("no such binary")

    client = Client()
    with pytest.raises(RuntimeError, match="no such binary"):
        omp_host_module._start_client(client)
    client.launcher.join(5)
    assert not client.launcher.is_alive()
