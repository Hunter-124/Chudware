from __future__ import annotations

from types import SimpleNamespace
import hashlib
import json

import os
import threading

import pytest

from services.agent import (
    omp_tools,
    config,
    mission_dispatch,
    project_context,
    roles,
    subagent_registry,
    worker_rpc,
)
from services.mcp import cad_common


def test_child_tool_frames_keep_the_root_turn_routing_identity():
    events = []
    emit = worker_rpc._rpc_event_emitter(
        {"run_id": "root-run", "session_id": "root-session"}, events.append
    )
    emit({"type": "subagent_event", "payload": {"id": "renderer", "event": {
        "type": "tool_execution_start", "toolCallId": "child-call",
        "toolName": "render_part", "run_id": "child-run", "session_id": "child-session",
    }}})
    assert len(events) == 1
    assert events[0]["run_id"] == "root-run"
    assert events[0]["session_id"] == "root-session"
    assert events[0]["subagent_id"] == "renderer"
    assert events[0]["id"] == "child-call"


def test_selected_nested_workspace_stays_authoritative(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    selected = repository / "nested workspace"
    selected.mkdir(parents=True)
    (repository / ".git").mkdir()
    monkeypatch.chdir(repository)
    project, save_dir, error = worker_rpc.resolve_workspace(
        {"workspace_dir": str(selected)}
    )
    assert error is None
    assert project == save_dir == str(selected.resolve())


def test_legacy_tool_load_owner_requires_the_exact_persisted_gui_link(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    journal = sessions / "runtime.jsonl"
    journal.write_text('{"type":"session"}\n', encoding="utf-8")
    foreign = sessions / "foreign.jsonl"
    foreign.write_text('{"type":"session"}\n', encoding="utf-8")
    gui = sessions / "c1.json"
    params = {"sessions_dir": str(sessions), "omp_session_file": str(journal)}
    valid = {
        "id": "c1", "project": str(workspace),
        "omp": {"session_id": "runtime-id", "session_file": str(journal)},
        "messages": [{"text": "not used as tool state"}],
    }
    assert worker_rpc._legacy_tool_loads_owner(params, "c1", str(workspace)) is None

    def save(value):
        gui.write_text(json.dumps(value), encoding="utf-8")
        gui.chmod(0o600)

    save(valid)
    expected = {"sessionId": "runtime-id", "sessionFile": str(journal.resolve()),
                "workspace": str(workspace.resolve())}
    assert worker_rpc._legacy_tool_loads_owner(params, "c1", str(workspace)) == expected
    for invalid in (
        {**valid, "id": "different"},
        {**valid, "project": str(tmp_path)},
        {**valid, "omp": {"session_id": "runtime-id", "session_file": str(foreign)}},
        {**valid, "omp": {"session_file": str(journal)}},
    ):
        save(invalid)
        assert worker_rpc._legacy_tool_loads_owner(params, "c1", str(workspace)) is None
    save({**valid, "chudcode": {"sessionId": "runtime-id", "sessionFile": str(journal)},
          "omp": None})
    assert worker_rpc._legacy_tool_loads_owner(params, "c1", str(workspace)) is None
    save({key: value for key, value in valid.items() if key != "omp"} | {
        "chudcode": {"sessionId": "runtime-id", "sessionFile": str(journal)},
    })
    assert worker_rpc._legacy_tool_loads_owner(params, "c1", str(workspace)) == expected
    assert worker_rpc._legacy_tool_loads_owner(params, "../c1", str(workspace)) is None


class _Binding:
    def __init__(self, name: str):
        self.name = name

    def to_host_tool(self, factory):
        return self.name


class _FakeHost:
    def __init__(self):
        self.turns: list[dict] = []
        self.compactions: list[dict] = []
        self.sessions: dict[str, object] = {}
        self.mode = "success"
        self.cancel_calls: list[str] = []
        self.steer_calls: list[tuple[str, str]] = []
        self.status_calls: list[tuple[object, object]] = []
        self.auth_calls: list[tuple[str, dict]] = []
        self.memory_calls: list[tuple[str, dict]] = []
        self.transient_calls: list[tuple[str, dict]] = []
        self.session_calls: list[tuple[str, str, dict]] = []
        self.recovery_calls: list[tuple] = []
        self.shutdown_count = 0



    def run_compaction(self, params, emit):
        self.compactions.append(params)
        emit({"type": "compaction_start"})
        emit({"type": "compaction_end", "result": {"tokensBefore": 120_000}})
        return {
            "ok": True,
            "run_id": params["run_id"],
            "session_id": params["session_id"],
            "cancelled": False,
            "outcome": "committed",
            "compaction": {"summary": "trimmed", "tokensBefore": 120_000, "tokensAfter": 9_000},
        }

    def run_turn(self, params, emit):
        self.turns.append(params)
        session_id = params["session_id"]
        token = self.sessions.setdefault(session_id, object())
        if self.mode == "crash":
            raise RuntimeError("host exploded")
        if self.mode == "error":
            return {"ok": False, "reason": "provider refused"}
        emit(
            {
                "type": "message_update",
                "message": {"id": "message-1"},
                "assistantMessageEvent": {"type": "text_delta", "delta": "working"},
            }
        )
        emit(
            {
                "type": "usage",
                "provider": "openai",
                "model": "gpt-test",
                "promptTokens": 11,
                "completionTokens": 4,
                "totalTokens": 15,
            }
        )
        cancelled = self.mode == "cancel"
        terminal = {
            "type": "agent_end",
            "isTerminal": True,
            "aborted": cancelled,
        }
        emit(terminal)
        emit({
            "type": "prompt_result", "id": params["run_id"],
            "agentInvoked": True, "status": "aborted" if cancelled else "completed",
            "sessionSettled": True,
        })
        return {
            "ok": True,
            "run_id": params["run_id"],
            "session_id": session_id,
            "omp_session_id": f"cc-{id(token)}",
            "omp_session_file": f"/sessions/{session_id}.jsonl",
            "cancelled": cancelled,
            "terminal": terminal,
            "result": "done",
        }

    def cancel(self, run_id):
        self.cancel_calls.append(run_id)
        return {"ok": True, "run_id": run_id, "cancelled": True, "active": True}

    def steer(self, run_id, text, attachments=None, *, steer_id=None):
        self.steer_calls.append((run_id, text))
        return {"ok": True, "run_id": run_id, "steered": True, "active": True}

    def status(self, run_id=None, session_id=None):
        self.status_calls.append((run_id, session_id))
        return {"ok": True, "active": bool(run_id), "run_id": run_id}

    def auth_call(self, command, params):
        self.auth_calls.append((command, params))
        if command == "setup_providers":
            return {"providers": []}
        if command == "auth_states":
            return {"providers": [
                {"provider": provider, "configured": True, "oauth": True,
                 "hasApiKey": False, "effectiveSource": "oauth"}
                for provider in params["providers"]
            ]}
        return {"ok": True, "provider": params["provider"], "configured": True,
                "oauth": True, "hasApiKey": False, "effectiveSource": "oauth"}

    def session_launch_params(self, session_id):
        if session_id not in self.sessions:
            raise KeyError("not_found")
        return {"session_id": session_id}

    def memory_call(self, command, params):
        self.memory_calls.append((command, params))
        return {"ok": True, "command": command, "params": params}

    def transient_call(self, command, params=None, emit=None):
        payload = dict(params or {})
        self.transient_calls.append((command, payload))
        if command == "get_login_providers":
            return {
                "providers": [
                    {
                        "id": "openai",
                        "name": "OpenAI",
                        "available": True,
                        "authenticated": True,
                        "accessToken": "must-not-escape",
                    }
                ]
            }
        if command == "get_available_models":
            return {
                "models": [
                    {
                        "id": f"model-{index}",
                        "name": f"Model {index}",
                        "provider": "openai" if index % 2 == 0 else "other",
                        "contextWindow": 200_000,
                        "maxTokens": 8_192,
                        "reasoning": index % 2 == 0,
                        "inputModalities": ["text", "image"],
                        "thinking": {
                            "mode": "anthropic-adaptive",
                            "efforts": ["low", "medium", "high", "xhigh", "max"],
                        },
                        "cost": {
                            "input": 1.0,
                            "output": 2.0,
                            "cacheRead": 0.1,
                            "cacheWrite": 0.2,
                        },
                        "baseUrl": "https://secret.invalid",
                        "headers": {"Authorization": "secret"},
                    }
                    for index in range(530)
                ]
            }
        if command == "login":
            if emit is not None:
                emit(
                    {
                        "type": "extension_ui_request",
                        "method": "open_url",
                        "kind": "open_url",
                        "url": "https://login.example/authorize",
                    }
                )
            return {"providerId": payload["providerId"]}
        return {
            "providerId": payload.get("providerId"),
            "removed": True,
            "loggedOut": True,
        }

    def session_call(self, session_id, command, params=None):
        payload = dict(params or {})
        self.session_calls.append((session_id, command, payload))
        if session_id not in self.sessions:
            return {"ok": False, "reason": "session_not_live"}
        if command == "tool_state":
            return {"ok": True, "tools": [
                {"name": "eval", "description": "Run code",
                 "builtin": True, "available": True}]}
        if command == "get_session_stats":
            return {
                "ok": True,
                "sessionId": f"cc-{session_id}",
                "userMessages": 2,
                "assistantMessages": 2,
                "toolCalls": 3,
                "toolResults": 3,
                "totalMessages": 7,
                "tokens": {
                    "input": 10,
                    "output": 5,
                    "cacheRead": 2,
                    "cacheWrite": 1,
                    "total": 18,
                },
                "premiumRequests": 1,
                "cost": 0.25,
                "sessionFile": "/private/session.jsonl",
            }
        return {"ok": True, "enabled": payload["enabled"]}

    def export_session(self, session_id, output_path=None):
        self.recovery_calls.append(("export", session_id, output_path))
        if session_id not in self.sessions:
            return {"ok": False, "reason": "session_not_live"}
        return {
            "ok": True,
            "path": output_path or f"/project/chat-exports/{session_id}.html",
            "accessToken": "must-not-escape",
        }

    def branch_session(self, session_id, entry_id):
        self.recovery_calls.append(("branch", session_id, entry_id))
        if session_id not in self.sessions:
            return {"ok": False, "reason": "session_not_live"}
        self.sessions.pop(session_id)
        return {
            "ok": True,
            "omp_session_id": f"cc-{session_id}-child",
            "omp_session_file": f"/sessions/{session_id}-child.jsonl",
            "secret": "must-not-escape",
        }


    def close_session(self, session_id):
        self.recovery_calls.append(("close", session_id))
        self.sessions.pop(session_id, None)
        return {"ok": True}

    def shutdown(self):
        self.shutdown_count += 1


@pytest.fixture
def online_lane(monkeypatch, tmp_path):
    # Import the role module before substituting the worker's builder; otherwise
    # its from-import can retain this fixture's fake after monkeypatch teardown.
    from services.agent import omp_roles

    real_build_tools = omp_tools.build_host_tools
    monkeypatch.setattr(omp_roles, "build_host_tools", real_build_tools)
    host = _FakeHost()
    saved: list[str] = []
    tool_contexts: list[dict] = []
    prompt_calls: list[dict] = []

    monkeypatch.setattr(worker_rpc, "get_default_omp_host", lambda: host)
    with worker_rpc._HISTORY_SEED_LOCK:
        worker_rpc._HISTORY_SEEDED_SESSIONS.clear()
        worker_rpc._SESSION_DAEMON_GENERATIONS.clear()
    monkeypatch.setattr(worker_rpc, "_daemon_generation", lambda: None)

    card = SimpleNamespace(
        id="general_agent",
        system_prompt="ROLE PROMPT",
        concrete_tools=("cad_tool",),
    )
    monkeypatch.setattr(roles, "load_role", lambda role: card)
    monkeypatch.setattr(project_context, "resolve_project_root", lambda path: path)
    monkeypatch.setattr(
        config, "load_config", lambda: {"provider": "openai", "model": "gpt-test"}
    )

    real_dispatch = mission_dispatch.MissionDispatch
    real_role_prompt = mission_dispatch.role_prompt
    class FakeDispatch:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.ledger = None

    monkeypatch.setattr(mission_dispatch, "MissionDispatch", FakeDispatch)

    def fake_prompt(role, **kwargs):
        prompt_calls.append({"role": role, **kwargs})
        return "RESOLVED ROLE PROMPT", ["cad_tool", "enable_tools"]

    monkeypatch.setattr(mission_dispatch, "role_prompt", fake_prompt)

    def fake_tools(context):
        tool_contexts.append(context)
        return [_Binding("cad_tool"), _Binding("enable_tools")]

    monkeypatch.setattr(omp_tools, "build_host_tools", fake_tools)
    monkeypatch.setattr(subagent_registry, "bind_session", lambda *args: None)

    def fake_save(**kwargs):
        saved.append("save")
        return {"saved": ["part.step+"], "errors": [], "persistence_failure": None}

    monkeypatch.setattr(cad_common, "save_touched_docs", fake_save)
    return SimpleNamespace(
        host=host,
        saved=saved,
        tool_contexts=tool_contexts,
        prompt_calls=prompt_calls,
        real_build_tools=real_build_tools,
        workspace=tmp_path,
        dispatch_type=real_dispatch,
        real_role_prompt=real_role_prompt,
    )


def _params(lane, *, run_id, request="Do the work", **extra):
    return {
        "request": request,
        "run_id": run_id,
        "session_id": "conversation-1",
        "workspace_dir": str(lane.workspace),
        "provider": "openai",
        "model": "gpt-test",
        **extra,
    }


def _without_turn_preferences(request: str) -> str:
    """The sent prompt minus the TURN PREFERENCES block every turn's tail carries."""
    blocks = request.split("\n\n")
    kept = [block for block in blocks if not block.startswith("TURN PREFERENCES")]
    assert len(kept) == len(blocks) - 1, request
    return "\n\n".join(kept)


@pytest.mark.parametrize("outcome", ["success", "cancel", "error", "crash", "preflight"])
def test_admin_turn_retires_only_its_preflight_and_finalization_connections(
    online_lane, monkeypatch, outcome
):
    from services.mcp import daemon_bridge

    class Connection:
        def __init__(self, *, writer_group, agent_run_scope=""):
            self.closed = False

        def call(self, method, params=None):
            assert not self.closed
            return {"docs": []}

        def close(self):
            self.closed = True

    monkeypatch.setattr(daemon_bridge, "DaemonClient", Connection)
    monkeypatch.setattr(daemon_bridge, "_clients", threading.local())
    monkeypatch.setattr(daemon_bridge, "_call_connections", threading.local())
    standalone = daemon_bridge.client()
    caller_pool = daemon_bridge.TurnConnections()
    host_pool = daemon_bridge.TurnConnections()
    owned, nested = [], []

    def preflight(method, params):
        owned.append(daemon_bridge.client())
        return owned[-1].call(method, params)

    monkeypatch.setattr(worker_rpc, "_editor_engine_call", preflight)
    monkeypatch.setattr(worker_rpc, "_daemon_generation",
                        lambda: (preflight("daemon_status", {}), None)[1])
    save = cad_common.save_touched_docs

    def final_save(**kwargs):
        assert daemon_bridge.client() is owned[0]
        assert not owned[0].closed
        return save(**kwargs)

    monkeypatch.setattr(cad_common, "save_touched_docs", final_save)
    run_turn = online_lane.host.run_turn

    def nested_turn(params, emit):
        with daemon_bridge.turn_connection_scope(host_pool):
            nested.append(daemon_bridge.client())
            assert nested[-1] is not owned[0]
            return run_turn(params, emit)

    online_lane.host.run_turn = nested_turn
    if outcome == "preflight":
        def failed_tools(*args, **kwargs):
            raise RuntimeError("host tool assembly failed after the CAD preflight")
        monkeypatch.setattr(omp_tools, "build_host_tools", failed_tools)
    else:
        online_lane.host.mode = outcome
    try:
        with daemon_bridge.turn_connection_scope(caller_pool):
            caller = daemon_bridge.client()
            result = worker_rpc._agent_admin_turn(
                _params(online_lane, run_id=outcome,
                        omp_session_file="/sessions/conversation-1.jsonl"),
                lambda event: None,
            )
            assert result["ok"] is (outcome in {"success", "cancel"})
            assert owned and all(connection.closed for connection in owned)
            assert daemon_bridge.client() is caller and not caller.closed
            assert all(not connection.closed for connection in nested)
        assert daemon_bridge.client() is standalone and not standalone.closed
        assert online_lane.saved == ([] if outcome == "preflight" else ["save"])
        assert online_lane.host.shutdown_count == 0
    finally:
        caller_pool.close()
        host_pool.close()
        standalone.close()


def test_legacy_text_history_requires_original_journal(online_lane):
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="legacy", history=[{"role": "user", "text": "Earlier question"}]),
        lambda _event: None,
    )
    assert result["ok"] is False
    assert result["code"] == "unsupported_history"
    assert online_lane.host.turns == []


def test_existing_session_never_reseeds_text_history(online_lane):
    first = worker_rpc._agent_admin_turn(_params(online_lane, run_id="first"), lambda _: None)
    second = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="second", history=[{"role": "user", "text": "Earlier question"}]),
        lambda _: None,
    )
    assert first["ok"] and second["ok"]
    assert "Earlier question" not in online_lane.host.turns[1]["request"]


def test_resumed_conversation_first_turn_warns_without_listing_documents(
    online_lane, monkeypatch
):
    """Only a known daemon generation change warrants a restart notice, and it never
    discloses which documents the GUI has open."""
    def no_engine_reads(method, params):
        raise AssertionError(f"resume note must not call the engine: {method}")

    monkeypatch.setattr(worker_rpc, "_editor_engine_call", no_engine_reads)
    worker_rpc._remember_daemon_generation("conversation-1", "",
                                           {"pid": 11, "start_id": "old"})
    monkeypatch.setattr(worker_rpc, "_daemon_generation",
                        lambda: {"pid": 12, "start_id": "new"})
    resume = {"omp_session_file": "/sessions/conversation-1.jsonl"}

    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-resumed", **resume), lambda _event: None
    )
    second = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-next", **resume), lambda _event: None
    )

    assert first["ok"] is True and second["ok"] is True
    sent = online_lane.host.turns[0]["request"]
    assert sent.startswith("Do the work\n\nRESUMED CONVERSATION")
    assert "import_part" in sent and "list_parts()" in sent
    assert "open documents" not in sent
    assert _without_turn_preferences(online_lane.host.turns[1]["request"]) == "Do the work"


def test_cancelled_first_turn_does_not_claim_a_restart(online_lane, monkeypatch):
    monkeypatch.setattr(worker_rpc, "_daemon_generation",
                        lambda: {"pid": 11, "start_id": "same"})
    online_lane.host.mode = "cancel"
    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="cancelled", sessions_dir=str(online_lane.workspace)),
        lambda _event: None,
    )
    online_lane.host.mode = "success"
    second = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="resumed", sessions_dir=str(online_lane.workspace),
                omp_session_file="/sessions/conversation-1.jsonl"),
        lambda _event: None,
    )
    assert first["turn"]["cancelled"] and second["ok"]
    assert "RESUMED CONVERSATION" not in online_lane.host.turns[1]["request"]


def test_generation_seed_survives_worker_restart(online_lane, monkeypatch):
    directory = str(online_lane.workspace)
    worker_rpc._remember_daemon_generation("conversation-1", directory,
                                           {"pid": 11, "start_id": "old"})
    with worker_rpc._HISTORY_SEED_LOCK:
        worker_rpc._SESSION_DAEMON_GENERATIONS.clear()
    assert worker_rpc._previous_daemon_generation("conversation-1", directory) == {
        "pid": 11, "start_id": "old"}
    assert worker_rpc._daemon_generation_changed(
        {"pid": 11, "start_id": "old"}, {"pid": 11, "start_id": "new"})
    assert not worker_rpc._daemon_generation_changed(None, {"pid": 12, "start_id": "new"})


def test_unknown_daemon_generation_never_claims_a_restart(online_lane):
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="unknown",
                omp_session_file="/sessions/conversation-1.jsonl"),
        lambda _event: None,
    )
    assert result["ok"]
    assert "RESUMED CONVERSATION" not in online_lane.host.turns[0]["request"]


def test_tools_loaded_in_failed_turn_survive_the_next_turn_only_for_its_conversation(
    online_lane, monkeypatch,
):
    from services.agent import tool_categories

    original_resolve = tool_categories.resolve_tool_requests
    def resolve(tokens):
        if list(tokens) == ["inspect_section"]:
            return {"inspect_section"}, []
        return original_resolve(tokens)
    monkeypatch.setattr(tool_categories, "resolve_tool_requests", resolve)
    monkeypatch.setattr(config, "get_tool_options", lambda *_: {})

    def build(context):
        online_lane.tool_contexts.append(context)
        return [
            omp_tools.HostToolBinding("enable_tools", "Enable tools", {}, context=context),
            omp_tools.HostToolBinding("inspect_section", "Inspect", {}, active=False,
                                      load_mode="discoverable", context=context),
        ]
    monkeypatch.setattr(omp_tools, "build_host_tools", build)
    stored: dict[str, list[str]] = {}
    observed = {}
    real_run_turn = online_lane.host.run_turn

    class Bridge:
        def call(self, operation, args, *, timeout):
            if operation == "cad_tool_loads_put":
                stored[args["scope"]] = list(args["tools"])
            return {"tools": list(stored.get(args["scope"], []))}

        def publish_tools(self, definitions):
            pass

    def run_turn(params, emit):
        client = SimpleNamespace(_omp_bridge=Bridge())
        params["register_callbacks"](client)
        if params["run_id"] == "run-load":
            online_lane.tool_contexts[-1]["enable_categories"](["inspect_section"])
        observed[params["run_id"]] = {
            binding.name for binding in params["host_tools"] if binding.active
        }
        return real_run_turn(params, emit)

    online_lane.host.run_turn = run_turn
    online_lane.host.mode = "error"
    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-load", mode="agent"), lambda _event: None
    )
    online_lane.host.mode = "success"
    followup = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-next", mode="agent"), lambda _event: None
    )
    other = worker_rpc._agent_admin_turn(
        {**_params(online_lane, run_id="run-other", mode="agent"),
         "session_id": "conversation-2"}, lambda _event: None
    )

    assert first["ok"] is False and followup["ok"] is True and other["ok"] is True
    assert "inspect_section" in observed["run-next"]
    assert "inspect_section" not in observed["run-other"]


def test_persisted_loads_are_revalidated_for_scope_role_policy_and_registry(
    online_lane, tmp_path, monkeypatch,
):
    from services.agent import omp_roles, tool_categories
    from services.agent.test_omp_tools import _Entry, _NEUTRAL_TOOL_OPTIONS, _Policy

    safe = "inspect_section"
    mutate = "add_features"
    removed = "render_animation"
    names = {safe, mutate, removed}

    def resolve(tokens):
        out, unknown = set(), []
        for token in tokens:
            if token == "fixture_category":
                out.update(names)
            elif token == "measure":
                out.update({"measure", mutate})
            elif token in names:
                out.add(token)
            else:
                unknown.append(token)
        return out, unknown

    monkeypatch.setattr(tool_categories, "resolve_tool_requests", resolve)
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(_NEUTRAL_TOOL_OPTIONS))
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    records = {}

    class Bridge:
        latest_scope = ""
        legacy = []

        def call(self, operation, args, *, timeout):
            self.__class__.latest_scope = args["scope"]
            if operation == "cad_tool_loads_legacy":
                return {"tools": list(self.legacy)}
            if operation == "cad_tool_loads_put":
                records[args["scope"]] = list(args["tools"])
            found = args["scope"] in records
            return {"tools": list(records.get(args["scope"], [])),
                    "found": found, "claimed": found}

        def publish_tools(self, definitions):
            self.definitions = definitions

    class Role:
        id = "general_agent"
        def __init__(self, allowed=None):
            self.allowed = allowed
            self.concrete_tools = list(allowed or ())

        def apply_tool_policy(self, registry):
            registry.set_policy(allow=self.allowed)
            return list(self.allowed or ())

    def runtime(*, session="conversation", workspace=tmp_path, mode="agent",
                tenant="", denied=(), role_allowed=None, explicit=None, catalog=names,
                legacy_owner=None):
        def probe():
            return {"ok": True, "value": "called"}
        entries = {
            name: _Entry(probe, name, {"type": "object", "properties": {}})
            for name in catalog
        }
        categories = []
        context = {
            "session_id": session, "workspace_dir": str(workspace),
            "project_dir": str(workspace), "tool_load_tenant": tenant,
            "role_card": Role(role_allowed), "allowed_tools": None,
            "restore_allowed_tools": explicit, "mode": mode,
            "persist_tool_loads": True, "categories": categories,
            "legacy_tool_loads_owner": legacy_owner,
            "enabled_categories": categories, "tool_entries": entries,
            "resolver": lambda name: getattr(entries.get(name), "fn", None),
            "policy_registry": _Policy(denied), "omp_builtin_names": (),
        }
        bindings = online_lane.real_build_tools(context)
        client = SimpleNamespace(_omp_bridge=Bridge())
        omp_roles._register_category_refresh(context, bindings)(client)
        return context, {binding.name: binding for binding in bindings}

    def active(bindings, name):
        return name in bindings and bindings[name].active

    context, initial = runtime()
    assert not any(active(initial, name) for name in names)
    result = context["enable_categories"](["fixture_category"])
    assert names <= set(result["added"])
    assert set(next(iter(records.values()))) >= names

    _, restored = runtime()
    assert all(active(restored, name) for name in names)
    assert omp_tools.dispatch_host_tool(restored[safe], {}, None, None)["value"] == "called"

    # Read-only mode never changes what is mounted (prefix-cache invariant); the
    # mutator's call is refused at dispatch instead.
    _, readonly = runtime(mode="plan")
    assert active(readonly, safe) and active(readonly, mutate)
    refusal = omp_tools.dispatch_host_tool(readonly[mutate], {}, None, None)
    assert refusal["status"] == "mode_refused" and refusal["executed"] is False
    _, constrained_role = runtime(role_allowed=[safe])
    assert active(constrained_role, safe) and not active(constrained_role, mutate)
    _, explicit_role = runtime(explicit=[])
    assert not any(active(explicit_role, name) for name in names)
    _, denied = runtime(denied=[safe])
    assert not active(denied, safe) and active(denied, mutate)
    _, current_catalog = runtime(catalog={safe, mutate})
    assert removed not in current_catalog and active(current_catalog, safe)

    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    for kwargs in (
        {"session": "different"}, {"workspace": other_workspace}, {"tenant": "different"},
    ):
        _, other = runtime(**kwargs)
        assert not any(active(other, name) for name in names)

    # A read-only or denied turn must not delete choices from the journal.
    _, allowed_again = runtime()
    assert all(active(allowed_again, name) for name in names)

    # A concrete restored name is not reinterpreted as a category token.
    runtime(session="collision", catalog={"measure", mutate})
    records[Bridge.latest_scope] = ["measure"]
    _, collision = runtime(session="collision", catalog={"measure", mutate})
    assert active(collision, "measure") and not active(collision, mutate)

    # Legacy metadata is narrowed to the current catalog before its first durable
    # custom entry; a read-only turn keeps the user's choices.
    Bridge.legacy = [safe, mutate, "removed_tool"]
    owner = {"sessionId": "id", "sessionFile": "/session", "workspace": str(tmp_path)}
    _, migrated = runtime(session="legacy", mode="plan", legacy_owner=lambda: owner)
    assert active(migrated, safe) and active(migrated, mutate)
    assert records[Bridge.latest_scope] == sorted([safe, mutate])

    failed_context, _ = runtime(session="failed-persistence")
    original_call = Bridge.call
    def fail_write(self, operation, args, *, timeout):
        if operation == "cad_tool_loads_put":
            raise RuntimeError("journal unavailable")
        return original_call(self, operation, args, timeout=timeout)
    with monkeypatch.context() as patch:
        patch.setattr(Bridge, "call", fail_write)
        with pytest.raises(RuntimeError, match="journal unavailable"):
            failed_context["enable_categories"](["fixture_category"])
    assert failed_context["categories"] == []

    from services.agent import request_context as tenancy
    monkeypatch.setattr(tenancy, "owner_key", lambda: ("tenant", "different-actor"))
    _, other_actor = runtime()
    assert not any(active(other_actor, name) for name in names)




def test_persistent_reuse_keeps_original_history_and_maps_settlement(online_lane):
    emitted: list[dict] = []
    history = [
        {"role": "user", "text": "Earlier question"},
        {"role": "assistant", "text": "Earlier answer"},
    ]

    first = worker_rpc._agent_admin_turn(
        _params(
            online_lane,
            run_id="run-1",
            attachments=[
                {"path": "/tmp/image.png", "kind": "image", "mime": "image/png"},
                "invalid",
            ],
            reasoning_effort="high",
            auto_compaction=True,
            auto_retry=False,
            steering_mode="all",
        ),
        emitted.append,
    )
    second = worker_rpc._agent_admin_turn(
        _params(
            online_lane,
            run_id="run-2",
            request="Continue",
            history=history,
            timeout=14400,
        ),
        emitted.append,
    )

    assert first["ok"] is True and second["ok"] is True
    assert len(online_lane.host.sessions) == 1
    assert first["turn"]["omp_session_id"] == second["turn"]["omp_session_id"]
    assert "Earlier question" not in online_lane.host.turns[0]["request"]
    # The explicit budget is announced at turn start (online_turn.turn_budget_notice);
    # this lane's default work style is check_with_me, i.e. read-only Plan mode.
    assert _without_turn_preferences(online_lane.host.turns[1]["request"]) == (
        "Continue\n\nThis turn has a hard 240 min budget; reach your answer early, then "
        "use the remaining time to check it."
    )
    assert online_lane.host.turns[0]["system_prompt"] == "RESOLVED ROLE PROMPT"
    # The turn budget is the caller's (mission driver --timeout-s); an absent one means no
    # wall-clock stop rather than an invented number.
    assert "timeout" not in online_lane.host.turns[0]
    assert online_lane.host.turns[1]["timeout"] == 14400.0
    assert [tool.name for tool in online_lane.host.turns[0]["host_tools"]] == [
        "cad_tool",
        "enable_tools",
    ]
    assert online_lane.host.turns[0]["settings"] == {
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_effort": "high",
        "steering_mode": "all",
        "auto_compaction": True,
        "auto_retry": False,
        # Sub-agent CAD calls reach the trace/watchdog only via subagent_event frames.
        "subagent_subscription": "events",
        # No agent.compaction_strategy configured: the agent service's own order.
        "compaction_strategy": "auto",
        "compaction_settings": {
            "supersedeReads": True,
            "dropUseless": True,
            "keepRecentTokens": 20000,
        },
    }
    assert online_lane.host.turns[0]["attachments"] == [
        {"path": "/tmp/image.png", "kind": "image", "mime": "image/png"}
    ]
    assert online_lane.prompt_calls[0]["role"] == "general_agent"
    context = online_lane.tool_contexts[0]
    assert context["role_card"].system_prompt == "ROLE PROMPT"
    assert context["allowed_tools"] == ["cad_tool", "enable_tools"]
    assert context["workspace_dir"] == str(online_lane.workspace)
    assert {event["kind"] for event in emitted} >= {
        "assistant_delta",
        "agent.provider_usage",
        "agent.context",
        "run_finished",
    }
    assert all("type" not in event for event in emitted)
    assert first["turn"]["usage"]["total_tokens"] == 15
    assert len(online_lane.saved) == 2


def test_turn_callbacks_keep_selected_nested_workspace(
    online_lane, tmp_path, monkeypatch,
):
    from services.agent import request_context as tenancy, workspace
    from services.agent.test_omp_tools import (
        _NEUTRAL_TOOL_OPTIONS, _Policy, _root_tool_entries,
    )
    from services.mcp import core_bridge

    repository = tmp_path / "repository"
    selected = repository / "nested" / "selected"
    selected.mkdir(parents=True)
    (repository / ".git").mkdir()
    (selected / "arm.stl").write_text("solid arm\nendsolid arm\n")
    online_lane.workspace = selected / ".." / "selected"
    for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_SAVE_DIR"):
        monkeypatch.setenv(name, str(repository))
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(_NEUTRAL_TOOL_OPTIONS))
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    monkeypatch.setattr(workspace, "_session_registry", {})
    monkeypatch.setattr(
        mission_dispatch, "role_prompt",
        lambda *_args, **_kwargs: (
            "Use the workspace", ["workspace_status", "browse_models", "fs_list"],
        ),
    )
    entries = _root_tool_entries()
    real_build = online_lane.real_build_tools

    def build(context):
        context.update(
            tool_entries=entries, policy_registry=_Policy(),
            resolver=lambda name: getattr(entries.get(name), "fn", None),
        )
        return real_build(context)

    monkeypatch.setattr(omp_tools, "build_host_tools", build)
    monkeypatch.setattr(
        core_bridge, "call",
        lambda method, params=None: {
            "docs": [{"doc_id": "arm", "path": str(selected / "arm.stl")}],
        },
    )
    real_run = online_lane.host.run_turn
    observed = {}
    failures = []

    def run(params, emit):
        bindings = {binding.name: binding for binding in params["host_tools"]}
        assert {"workspace_status", "browse_models", "fs_list"} <= bindings.keys()

        def callback():
            try:
                assert tenancy.current_context() == {}
                host_context = SimpleNamespace(
                    cancelled=False, tool_call_id="js-write-workspace",
                    agent_id="ArmBuilder", parent_agent_id="Main",
                )
                for name, args in (
                    ("workspace_status", {}),
                    ("browse_models", {"sheets": False, "pattern": "*.stl"}),
                    ("fs_list", {}),
                ):
                    tool = bindings[name].to_host_tool(lambda **kwargs: kwargs)
                    observed[name] = tool["execute"](args, host_context)["details"]
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=callback)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert not failures
        return real_run(params, emit)

    monkeypatch.setattr(online_lane.host, "run_turn", run)
    # The fixture's unset work style defaults to Plan, whose frozen read-only
    # roster excludes workspace_status. Exercise the actual Agent tool assembly.
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="selected-root-tools", mode="agent"),
        lambda _event: None,
    )
    assert result["ok"], result
    assert observed["workspace_status"]["ok"], observed["workspace_status"]
    assert observed["workspace_status"]["project_dir"] == str(selected)
    assert observed["workspace_status"]["docs"][0]["location"] == "project"
    assert observed["browse_models"]["ok"], observed["browse_models"]
    assert observed["browse_models"]["root"] == str(selected)
    assert [row["file"] for row in observed["browse_models"]["models"]] == ["arm.stl"]
    assert observed["fs_list"]["ok"], observed["fs_list"]
    assert observed["fs_list"]["path"] == str(selected)
    assert [row["name"] for row in observed["fs_list"]["entries"]] == ["arm.stl"]


def test_crashed_turn_restores_workspace(online_lane):
    original_cwd = os.getcwd()
    original = {
        key: os.environ.get(key)
        for key in ("CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR")
    }
    online_lane.host.mode = "crash"
    response = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="restore-after-crash"), lambda _event: None
    )
    assert response["ok"] is False
    assert response["persistence"]["saved"] == ["part.step+"]
    assert os.getcwd() == original_cwd
    assert {key: os.environ.get(key) for key in original} == original
    assert response["stop_reason"] == "runtime_error"


@pytest.mark.parametrize("reason", ["runtime_error", "provider_error", "turn_budget", None])
def test_failed_turn_preserves_stop_reason(online_lane, monkeypatch, reason):
    host_result = {"ok": False, "reason": "turn failed"}
    if reason is not None:
        host_result["stop_reason"] = reason
    monkeypatch.setattr(online_lane.host, "run_turn", lambda _params, _emit: host_result)
    response = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="failed-stop-reason"), lambda _event: None
    )
    expected = reason or "runtime_error"
    assert response["ok"] is False
    assert response["stop_reason"] == expected
    assert response["turn"]["stop_reason"] == expected


@pytest.mark.parametrize(
    ("mode", "expected_ok", "expected_stop"),
    [
        ("success", True, "completed"),
        ("error", False, None),
        ("cancel", True, "cancelled"),
        ("crash", False, None),
    ],
)
def test_terminal_save_finalization_covers_every_host_outcome(
    online_lane, mode, expected_ok, expected_stop
):
    online_lane.host.mode = mode

    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id=f"run-{mode}"), lambda event: None
    )

    assert result["ok"] is expected_ok
    assert result["persistence"]["saved"] == ["part.step+"]
    assert len(online_lane.saved) == 1
    if expected_stop is not None:
        assert result["turn"]["stop_reason"] == expected_stop


@pytest.mark.parametrize("persistence", [
    {"errors": ["doc-20: save_failed: disk full"]},
    {"warnings": [{"doc_id": "doc-20", "reason": "missing_unverified",
                   "path": "/project/bracket.step+", "older_recovery_id": "older"}]},
    {"persistence_failure": {"reason": "skipped_human_edits",
                             "docs": [{"doc_id": "doc-20", "name": "bracket"}]}},
])
def test_persistence_problem_warns_without_failing_completed_turn(
        online_lane, monkeypatch, persistence):
    monkeypatch.setattr(cad_common, "save_touched_docs", lambda **_kwargs: persistence)
    events = []
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="save-warning"), events.append)

    assert result["ok"] is True
    assert result["turn"]["success"] is True
    assert result["turn"]["stop_reason"] == "completed"
    assert result["persistence"] == persistence
    assert result["persistence_warnings"]
    assert "Save warning" in result["turn"]["result"]
    assert "recover_parts(all=True)" in result["turn"]["result"]
    assert any(event.get("kind") == "assistant_delta" and
               "Save warning" in event.get("text", "") for event in events)


def test_preserved_missing_document_does_not_add_a_save_warning(online_lane, monkeypatch):
    persistence = {"saved": [], "errors": [], "warnings": [], "skipped": [
        {"doc_id": "doc-20", "reason": "preserved_in_recovery", "recovery_id": "snapshot"}]}
    monkeypatch.setattr(cad_common, "save_touched_docs", lambda **_kwargs: persistence)
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="recovered-doc"), lambda _event: None)
    assert result["ok"] is True and result["turn"]["success"] is True
    assert "persistence_warnings" not in result
    assert "Save warning" not in result["turn"]["result"]


def test_cancel_steer_status_and_auth_memory_proxies(online_lane):
    assert worker_rpc.WORKER_METHODS["agent_cancel"](
        {"run_id": "run-c"}, lambda event: None
    )["cancelled"]
    assert worker_rpc.WORKER_METHODS["agent_steer"](
        {"run_id": "run-s", "text": "change course"}, lambda event: None
    )["steered"]
    assert worker_rpc.omp_control("status", {"run_id": "run-z"})["active"]
    assert online_lane.host.cancel_calls == ["run-c"]
    assert online_lane.host.steer_calls == [("run-s", "change course")]
    assert online_lane.host.status_calls == [("run-z", None)]

    auth_cases = {
        "omp_auth_state": {"providerId": "openai"},
        "omp_set_key": {
            "providerId": "openai",
            "apiKey": "never-return-this",
        },
        "omp_remove_key": {"providerId": "openai"},
        "omp_logout": {"providerId": "openai", "credentialId": 1},
    }
    for method, payload in auth_cases.items():
        result = worker_rpc.WORKER_METHODS[method](payload, lambda event: None)
        assert result["ok"] is True
        assert "never-return-this" not in repr(result)
    memory_cases = {
        "memory_remember": (
            "memory_save",
            {"root": "/project", "content": "Remember this"},
        ),
        "memory_recall": (
            "memory_search",
            {"root": "/project", "query": "this"},
        ),
        "memory_stats": ("memory_status", {"root": "/project"}),
    }
    for method, (command, payload) in memory_cases.items():
        result = worker_rpc.WORKER_METHODS[method](payload, lambda event: None)
        assert result["command"] == command
    worker_rpc.omp_shutdown()
    assert online_lane.host.shutdown_count == 1


def test_registers_exact_shared_omp_worker_methods():
    expected = {
        "omp_provider_list",
        "omp_auth_state",
        "omp_set_key",
        "omp_remove_key",
        "omp_logout_accounts",
        "omp_logout",
        "omp_login",
        "omp_models_list",
        "omp_session_stats",
        "omp_export_session",
        "omp_branch_session",
        "omp_close_session",
        "omp_apply_compaction_policy",
        "omp_set_auto_compaction",
        "omp_set_auto_retry",
        "omp_tool_state",
    }
    assert expected <= worker_rpc.WORKER_METHODS.keys()
    assert "omp_validate_key" not in worker_rpc.WORKER_METHODS


def test_logout_accounts_uses_official_redacted_rows(online_lane, monkeypatch):
    def call(command, params=None, emit=None):
        assert command == "get_logout_accounts"
        assert params == {"providerId": "openai"}
        return {"accounts": [{"credentialId": 3, "provider": "openai",
                              "type": "oauth", "label": "Account",
                              "detail": "oauth #3", "active": True,
                              "accessToken": "never-return"}]}
    monkeypatch.setattr(online_lane.host, "transient_call", call)
    result = worker_rpc.WORKER_METHODS["omp_logout_accounts"](
        {"providerId": "openai"}, lambda _: None)
    assert result["accounts"][0]["credentialId"] == 3
    assert "never-return" not in repr(result)


def test_catalog_does_not_invent_thinking_metadata():
    model = worker_rpc._sanitize_model({"id": "m", "provider": "p"})
    assert "thinking_levels" not in model
    assert "vision" not in model
    known_empty = worker_rpc._sanitize_model(
        {"id": "m", "provider": "p", "thinking": {"efforts": []}})
    assert known_empty["thinking_levels"] == []


def test_provider_auth_model_and_login_results_are_bounded_and_secret_free(online_lane):
    emit: list[dict] = []
    providers = worker_rpc.WORKER_METHODS["omp_provider_list"]({}, emit.append)
    auth = worker_rpc.WORKER_METHODS["omp_auth_state"](
        {"providerId": "openai"}, emit.append
    )
    models = worker_rpc.WORKER_METHODS["omp_models_list"](
        {"providerId": "openai"}, emit.append
    )
    login = worker_rpc.WORKER_METHODS["omp_login"](
        {"providerId": "openai"}, emit.append
    )

    assert providers["ok"] is True
    assert providers["providers"][0]["state"] == "oauth_connected"
    assert providers["providers"][0]["authKinds"] == ["oauth"]
    assert type(providers["providers"][0]["modelsAvailable"]) is int
    assert providers["providers"][0]["modelsAvailable"] == 265
    assert providers["providers"]
    assert auth["state"] == "oauth_connected"
    assert auth["accounts"] == []
    # Filtering retains matching rows even after many unrelated models.
    assert len(models["models"]) == 265
    assert any(model["id"] == "model-528" for model in models["models"])
    assert set(models["models"][0]) == {
        "id",
        "name",
        "provider",
        "contextWindow",
        "maxTokens",
        "reasoning",
        "reasoningEfforts",
        "thinking_levels",
        "vision",
        "input",
        "cost",
    }
    # Omp's per-model ladder drives the composer's effort menu; without it a new
    # release fell back to a hardcoded low/medium/high and hid xhigh/max.
    assert models["models"][0]["reasoningEfforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["models"][0]["vision"] is True
    assert login["state"] == "oauth_connected"
    assert emit == [
        {
            "type": "extension_ui_request",
            "method": "open_url",
            "kind": "open_url",
            "url": "https://login.example/authorize",
        }
    ]
    assert "must-not-escape" not in repr((providers, auth, models, login))


def test_catalog_keeps_late_runtime_providers_and_all_models(online_lane, monkeypatch):
    rows = [{"provider": "aaa", "id": f"model-{i}", "name": f"Model {i}"}
            for i in range(530)]
    rows.extend([
        {"provider": "openai-codex", "id": "runtime-codex-model", "name": "Codex"},
        {"provider": "anthropic", "id": "runtime-claude-model", "name": "Claude"},
        {"provider": "revoked", "id": "revoked-model", "name": "Revoked"},
    ])
    login = [{"id": f"aaa-{i}", "name": f"AAA {i}"} for i in range(130)]
    def call(command, params=None, emit=None):
        return {"get_login_providers": {"providers": login},
                "get_available_models": {"models": rows}}[command]
    def auth_call(command, params):
        if command == "setup_providers":
            return {"providers": []}
        assert command == "auth_states"
        return {"providers": [
            {"provider": provider, "configured": provider in {"openai-codex", "anthropic"},
             "oauth": provider in {"openai-codex", "anthropic"},
             "effectiveSource": "oauth" if provider in {"openai-codex", "anthropic"} else None}
            for provider in params["providers"]
        ]}
    monkeypatch.setattr(online_lane.host, "auth_call", auth_call)
    monkeypatch.setattr(online_lane.host, "transient_call", call)
    providers = worker_rpc._omp_provider_list({}, lambda _: None)
    by_id = {p["id"]: p for p in providers["providers"]}
    for provider in ("openai-codex", "anthropic"):
        assert by_id[provider]["state"] == "oauth_connected"
        assert by_id[provider]["authenticated"] and by_id[provider]["modelsAvailable"]
    assert by_id["revoked"]["state"] == "signed_out"
    assert not by_id["revoked"]["authenticated"]
    assert len(by_id) > 130
    result = worker_rpc._omp_models_list({}, lambda _: None)
    assert len(result["models"]) == len(rows)
    assert {(m["provider"], m["id"]) for m in result["models"]} >= {
        ("openai-codex", "runtime-codex-model"), ("anthropic", "runtime-claude-model")}


def test_setup_catalog_does_not_infer_capabilities_from_models(online_lane, monkeypatch):
    def transient(command, params=None, emit=None):
        return {
            "get_login_providers": {"providers": []},
            "get_available_models": {"models": [{"provider": "local", "id": "model"}]},
        }[command]

    def auth(command, params):
        if command == "setup_providers":
            return {"providers": [{"id": "groq", "name": "Groq", "authKinds": ["api_key"]}]}
        assert command == "auth_states"
        return {"providers": [
            {"provider": provider, "configured": False, "oauth": False, "hasApiKey": False}
            for provider in params["providers"]
        ]}

    monkeypatch.setattr(online_lane.host, "transient_call", transient)
    monkeypatch.setattr(online_lane.host, "auth_call", auth)
    monkeypatch.setattr(config, "load_config", lambda: {"agent": {"keyless_providers": ["local"]}})
    result = worker_rpc._omp_provider_list({}, lambda _: None)
    rows = {item["id"]: item for item in result["providers"]}
    assert rows["groq"]["authKinds"] == ["api_key"]
    assert rows["groq"]["modelsAvailable"] == 0 and not rows["groq"]["available"]
    assert rows["local"]["authKinds"] == []
    assert rows["local"]["modelsAvailable"] == 1 and rows["local"]["available"]


def test_login_uses_canonical_credential_identity_from_bridge(online_lane, monkeypatch):
    def auth(command, params):
        assert command == "auth_state"
        assert params["provider"] == "openai-codex-device"
        return {"provider": params["provider"], "credentialProvider": "openai-codex",
                "configured": True, "oauth": True, "hasApiKey": False, "effectiveSource": "oauth"}

    monkeypatch.setattr(online_lane.host, "auth_call", auth)
    result = worker_rpc._omp_login({"providerId": "openai-codex-device"}, lambda _: None)
    assert result["state"] == "oauth_connected"
    assert result["id"] == "openai-codex-device"
    assert result["credentialProvider"] == "openai-codex"


def test_effective_env_source_is_configured_even_with_keyless_marker():
    descriptor = worker_rpc._auth_provider({
        "provider": "example", "credentialProvider": "example",
        "configured": True, "effectiveSource": "env", "effectiveSourceConcrete": False,
        "keylessMarker": True, "oauth": False, "hasApiKey": False,
    })
    assert descriptor["state"] == "configured"
    assert descriptor["authenticated"] is True
    assert descriptor["origin"] == "env"


def test_catalog_failure_is_not_reported_as_empty(online_lane, monkeypatch):
    def bad_catalog(command, params=None, emit=None):
        return {"models": None} if command == "get_available_models" else {"providers": []}
    monkeypatch.setattr(online_lane.host, "transient_call", bad_catalog)
    assert not worker_rpc._omp_models_list({}, lambda _: None)["ok"]
    assert not worker_rpc._omp_provider_list({}, lambda _: None)["ok"]


def test_provider_auth_failures_are_isolated_and_recover(online_lane, monkeypatch):
    ids = ["healthy", "broken", "signed-out", "keyless"]
    def transient(command, params=None, emit=None):
        return {"get_login_providers": {"providers": []},
                "get_available_models": {"models": [
                    {"id": "same/model", "provider": provider} for provider in ids
                ]}}[command]

    failed = True
    def auth(command, params):
        if command == "setup_providers":
            return {"providers": []}
        assert command == "auth_states"
        return {"providers": [
            {"provider": provider, "configured": "false" if failed and provider in {"broken", "keyless"}
             else provider == "healthy", "oauth": provider == "healthy",
             "effectiveSource": "oauth" if provider == "healthy" else None}
            for provider in params["providers"]
        ]}

    monkeypatch.setattr(online_lane.host, "transient_call", transient)
    monkeypatch.setattr(online_lane.host, "auth_call", auth)
    monkeypatch.setattr(config, "load_config", lambda: {"agent": {"keyless_providers": ["keyless"]}})
    result = worker_rpc._omp_provider_list({}, lambda _: None)
    assert result["ok"] and result["refreshError"]
    rows = {item["id"]: item for item in result["providers"]}
    assert rows["healthy"]["available"] and rows["healthy"]["authenticated"]
    assert not rows["signed-out"]["available"]
    for provider in ("broken", "keyless"):
        assert rows[provider]["state"] == "unknown"
        assert "available" not in rows[provider] and "authenticated" not in rows[provider]

    failed = False
    result = worker_rpc._omp_provider_list({}, lambda _: None)
    assert result["ok"] and "refreshError" not in result
    rows = {item["id"]: item for item in result["providers"]}
    assert rows["healthy"]["available"] and not rows["broken"]["available"]
    assert rows["keyless"]["available"] and not rows["keyless"]["authenticated"]


def test_provider_batch_failure_does_not_hide_later_providers(online_lane, monkeypatch):
    ids = [f"provider-{index:02}" for index in range(34)]
    monkeypatch.setattr(
        online_lane.host, "transient_call",
        lambda command, params=None, emit=None: {
            "get_login_providers": {"providers": [{"id": provider} for provider in ids]},
            "get_available_models": {"models": [{"provider": provider, "id": "model"} for provider in ids]},
        }[command],
    )
    batches = []
    def auth(command, params):
        if command == "setup_providers":
            return {"providers": []}
        batches.append(params["providers"])
        if len(batches) == 1:
            raise RuntimeError("fixture failure")
        return {"providers": [{"provider": provider, "configured": True}
                              for provider in params["providers"]]}

    monkeypatch.setattr(online_lane.host, "auth_call", auth)
    result = worker_rpc._omp_provider_list({}, lambda _: None)
    rows = {item["id"]: item for item in result["providers"]}
    assert [len(batch) for batch in batches] == [32, 2]
    assert result["ok"] and result["refreshError"]
    assert rows[ids[0]]["state"] == "unknown" and "available" not in rows[ids[0]]
    assert rows[ids[-1]]["available"]


def test_model_and_provider_identities_are_never_truncated(online_lane, monkeypatch):
    long_provider = "p" * 129
    long_model = "m" * 257
    rows = [{"provider": long_provider, "id": "model"},
            {"provider": "exact/provider", "id": long_model},
            {"provider": "exact/provider", "id": "model/with/slashes"}]
    monkeypatch.setattr(
        online_lane.host, "transient_call",
        lambda command, params=None, emit=None: {"models": rows},
    )
    result = worker_rpc._omp_models_list({"providerId": "exact/provider"}, lambda _: None)
    assert [(m["provider"], m["id"]) for m in result["models"]] == [
        ("exact/provider", "model/with/slashes")]
    assert not worker_rpc._omp_models_list({"providerId": long_provider}, lambda _: None)["ok"]
    assert not worker_rpc._omp_auth_state({"providerId": long_provider}, lambda _: None)["ok"]
    assert not worker_rpc._omp_set_key(
        {"providerId": long_provider, "apiKey": "fixture-secret"}, lambda _: None)["ok"]
    assert not online_lane.host.auth_calls


def test_live_session_management_is_isolated_bounded_and_capability_honest(online_lane):
    online_lane.host.sessions["live"] = object()
    emit = lambda _event: None

    stats = worker_rpc.WORKER_METHODS["omp_session_stats"](
        {"session_id": "live"}, emit
    )
    auto_compact = worker_rpc.WORKER_METHODS["omp_set_auto_compaction"](
        {"session_id": "live", "enabled": False}, emit
    )
    auto_retry = worker_rpc.WORKER_METHODS["omp_set_auto_retry"](
        {"session_id": "live", "enabled": True}, emit
    )
    tools = worker_rpc.WORKER_METHODS["omp_tool_state"](
        {"session_id": "live"}, emit
    )
    missing = worker_rpc.WORKER_METHODS["omp_session_stats"](
        {"session_id": "inactive"}, emit
    )

    assert stats["cost"] == 0.25
    assert stats["tokens"]["total"] == 18
    assert "sessionFile" not in stats
    assert auto_compact["ok"] is True
    assert auto_retry["ok"] is True

    assert tools["tools"] == [{"name": "eval", "description": "Run code",
                               "builtin": True, "available": True}]
    assert missing == {"ok": False, "reason": "session_not_live"}
    assert set(online_lane.host.sessions) == {"live"}


def test_recovery_worker_methods_are_exact_secret_free_and_never_activate_sessions(
    online_lane,
):
    online_lane.host.sessions.update({"live": object(), "close-me": object()})
    emit = lambda _event: None

    exported = worker_rpc.WORKER_METHODS["omp_export_session"](
        {"session_id": "live"}, emit
    )
    branched = worker_rpc.WORKER_METHODS["omp_branch_session"](
        {"session_id": "live", "entry_id": "entry-3"}, emit
    )
    closed = worker_rpc.WORKER_METHODS["omp_close_session"](
        {"session_id": "close-me"}, emit
    )
    closed_again = worker_rpc.WORKER_METHODS["omp_close_session"](
        {"session_id": "close-me"}, emit
    )
    inactive = worker_rpc.WORKER_METHODS["omp_export_session"](
        {"session_id": "inactive"}, emit
    )

    assert exported == {
        "ok": True,
        "path": "/project/chat-exports/live.html",
    }
    assert branched == {
        "ok": True,
        "omp_session_id": "cc-live-child",
        "omp_session_file": "/sessions/live-child.jsonl",
    }
    assert closed == {"ok": True}
    assert closed_again == {"ok": True}
    assert inactive == {"ok": False, "reason": "session_not_live"}
    assert "must-not-escape" not in repr((exported, branched))
    assert set(online_lane.host.sessions) == set()




def test_legacy_session_adapters_never_rewrite_online_omp_history(monkeypatch):
    compact_calls = []
    shake_calls = []

    def compact_session(messages, strategy, **kwargs):
        compact_calls.append((messages, strategy, kwargs))
        return {
            "summary": "trimmed",
            "shortSummary": "short",
            "firstKeptEntryId": "entry-3",
            "tokensBefore": 400,
            "messages": [],
            "kept_messages": [],
        }

    commands = SimpleNamespace(
        _offline_enabled=lambda: False,
        compact_session=compact_session,
        shake_session=lambda *args, **kwargs: shake_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(worker_rpc, "_cmds", lambda: commands)
    monkeypatch.setattr(
        config, "load_config", lambda: {"agent": {"compaction_strategy": "summary"}}
    )

    compact = worker_rpc.SESSION_METHODS["session_compact"](
        {
            "session_id": "live",
            "messages": [{"role": "user", "content": "must survive"}],
            "provider": "removed-provider",
            "instructions": "Keep decisions",
        },
        lambda _event: None,
    )
    shake = worker_rpc.SESSION_METHODS["session_shake"](
        {
            "session_id": "live",
            "messages": [{"role": "user", "content": "must survive"}],
        },
        lambda _event: None,
    )

    # Online conversations compact through the agent_admin_turn compaction run; the
    # transcript-rewriting adapters refuse instead of touching Omp's history.
    for refused in (compact, shake):
        assert refused["ok"] is False
        assert refused["owner"] == "omp"
    assert compact_calls == []
    assert shake["ok"] is False
    assert shake["owner"] == "omp"
    assert shake_calls == []


def test_offline_compaction_preserves_explicit_method_and_refusal(monkeypatch):
    from services.agent import session_cmds

    monkeypatch.setattr(worker_rpc, "_cmds", lambda: session_cmds)
    monkeypatch.setattr(config, "get_compaction_strategy", lambda: "auto")
    original = [{"role": "user", "content": "keep the dimensions"}]
    for strategy in ("auto", "summary"):
        result = worker_rpc.SESSION_METHODS["session_compact"](
            {"offline": True, "strategy": strategy, "messages": original}, lambda _: None
        )
        assert result["ok"] is False
        assert "offline compaction" in result["reason"]
        assert "session_compact failed" not in result["reason"]
        assert original == [{"role": "user", "content": "keep the dimensions"}]


def test_online_registration_uses_only_omp(online_lane, monkeypatch):

    def forbidden(*args, **kwargs):
        raise AssertionError("deterministic Python provider loop selected")

    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", forbidden)

    result = worker_rpc.WORKER_METHODS["agent_admin_turn"](
        _params(online_lane, run_id="clean-cutover"), lambda event: None
    )

    assert result["ok"] is True
    assert worker_rpc.ADMIN_METHODS["agent_admin_turn"] is worker_rpc._agent_admin_turn
    assert worker_rpc._agent_admin_turn is not forbidden


@pytest.mark.parametrize("budget", [None, 600.0])
def test_rate_limit_keeps_exact_model_and_caller_budget_without_app_fallback(
    online_lane, monkeypatch, budget
):
    calls = []
    def rate_limited(params, emit):
        calls.append(params)
        return {"ok": False, "reason": "429 rate limit exceeded after retries"}
    monkeypatch.setattr(online_lane.host, "run_turn", rate_limited)
    extra = {} if budget is None else {"timeout": budget}
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id=f"rate-limit-{budget}", **extra), lambda _event: None
    )
    assert result["ok"] is False
    assert "429" in result["reason"]
    assert len(calls) == 1
    assert calls[0]["settings"]["provider"] == "openai"
    assert calls[0]["settings"]["model"] == "gpt-test"
    if budget is None:
        assert "timeout" not in calls[0]
    else:
        assert calls[0]["timeout"] == budget


@pytest.mark.parametrize(("extra", "notice"), [
    ({"timeout": 600, "mode": "agent"}, "This turn has a hard 10 min budget; save "
                                        "deliverables as soon as they are correct, then verify."),
    ({"timeout": 600, "plan_only": True}, "This turn has a hard 10 min budget; reach your "
                                          "answer early, then use the remaining time to check it."),
    ({"timeout": 600, "mode": "ask"}, "This turn has a hard 10 min budget; reach your "
                                      "answer early, then use the remaining time to check it."),
    ({}, None),
    ({"timeout": None}, None),
    ({"timeout": 0}, None),
])
def test_explicit_turn_budget_is_announced_in_the_assembled_prompt(online_lane, extra, notice):
    """A benchmark trial that never knew its 20 min budget over-verified past expiry.
    Only a caller-sent positive timeout is announced; GUI turns send none."""
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="budget-notice", **extra), lambda _event: None
    )
    assert result["ok"] is True, result
    sent = _without_turn_preferences(online_lane.host.turns[-1]["request"])
    if notice is None:
        assert sent == "Do the work"
        assert "timeout" not in online_lane.host.turns[-1]
    else:
        assert sent == f"Do the work\n\n{notice}"
        assert online_lane.host.turns[-1]["timeout"] == 600.0


def test_turn_policy_never_reaches_the_cached_prefix(online_lane, monkeypatch):
    """Prefix-cache invariant: two turns of one conversation with different request
    kinds and Copilot modes send the identical system prompt and host-tool
    definitions. The per-turn policy rides the new message's tail only, and a
    read-only mode still refuses a mutator when it is dispatched."""
    from collections import OrderedDict

    from services.agent.test_omp_tools import _NEUTRAL_TOOL_OPTIONS, _Policy, _entry

    ran = []

    def add_features():
        ran.append("add_features")
        return {"ok": True}

    def measure_part():
        return {"ok": True}

    entries = OrderedDict(
        (name, _entry(fn, f"{name} tool")) for name, fn in (
            ("add_features", add_features), ("measure_part", measure_part),
            ("delegate", measure_part), ("advise", measure_part),
        )
    )
    monkeypatch.setattr(config, "get_tool_options", lambda *_: dict(_NEUTRAL_TOOL_OPTIONS))
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)
    monkeypatch.setattr(mission_dispatch, "role_prompt", online_lane.real_role_prompt)

    def build(context):
        context.update(
            tool_entries=entries, policy_registry=_Policy(), omp_builtin_names=(),
            resolver=lambda name: getattr(entries.get(name), "fn", None),
        )
        return online_lane.real_build_tools(context)

    monkeypatch.setattr(omp_tools, "build_host_tools", build)
    for run_id, request, mode in (
        ("build", "Create a 40 mm bracket with two 5 mm holes", "agent"),
        ("question", "What does this part weigh?", "ask"),
    ):
        result = worker_rpc._agent_admin_turn(
            _params(online_lane, run_id=run_id, request=request, mode=mode),
            lambda _event: None,
        )
        assert result["ok"] is True, result
    agent_turn, ask_turn = online_lane.host.turns

    assert "ROLE PROMPT" in agent_turn["system_prompt"]
    assert agent_turn["system_prompt"] == ask_turn["system_prompt"]
    definitions = [binding.as_dict() for binding in agent_turn["host_tools"]]
    assert {"add_features", "measure_part", "delegate", "advise"} <= {
        item["name"] for item in definitions
    }
    assert definitions == [binding.as_dict() for binding in ask_turn["host_tools"]]
    assert "Work style: do_it" in agent_turn["request"]
    assert "Read-only turn" not in agent_turn["request"]
    assert "Work style: answer_only" in ask_turn["request"]
    assert "Read-only turn: mutating tools are refused" in ask_turn["request"]

    ask_tools = {binding.name: binding for binding in ask_turn["host_tools"]}
    refusal = omp_tools.dispatch_host_tool(ask_tools["add_features"], {}, None, None)
    assert refusal["status"] == "mode_refused" and refusal["executed"] is False
    assert ran == []
    assert omp_tools._turn_policy_refusal("add_features", ask_tools["add_features"].context) is not None
    agent_context = {binding.name: binding for binding in agent_turn["host_tools"]}[
        "add_features"].context
    assert omp_tools._turn_policy_refusal("add_features", agent_context) is None


def test_cell_turn_has_no_credential_route_and_never_reaches_omp(online_lane, monkeypatch):
    from services.agent import request_context as tenancy
    monkeypatch.setenv("XDG_DATA_HOME", str(online_lane.workspace / "cell-state"))
    ctx = {"cell": True, "workspace_key": "team:worker/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    refusal = {"ok": False, "reason": "credential_isolation_unsupported",
               "code": "credential_isolation_unsupported"}
    with tenancy.bind_context(ctx):
        session = tenancy.mint_handle("session")
        run_id = tenancy.mint_handle("run", parent=session)
        for extra in ({}, {"api_key": "forbidden"}, {"byok_key": "forbidden"}):
            assert worker_rpc._agent_admin_turn(
                _params(online_lane, run_id=run_id, session_id=session, **extra),
                lambda event: None,
            ) == refusal
        assert worker_rpc._agent_admin_turn(
            _params(online_lane, run_id="absent", session_id=session), lambda event: None,
        ) == {"ok": False, "reason": "not_found", "code": "not_found"}
        assert worker_rpc._omp_models_list({}, lambda event: None) == {
            "ok": True, "models": [], "refreshing": False,
        }
    assert not online_lane.host.turns
    assert not online_lane.saved


def test_turn_and_resumed_conversation_bind_selected_root_on_host_threads(
    online_lane, monkeypatch, tmp_path
):
    from concurrent.futures import ThreadPoolExecutor
    from services.agent import request_context
    from services.mcp.cad_render_tools import _render_save_target

    ambient = tmp_path / "ambient"
    projects = [tmp_path / "workspace" / "first", tmp_path / "workspace" / "second"]
    for path in [ambient, *projects]:
        path.mkdir(parents=True)
    (projects[0].parent / ".git").mkdir()
    for name in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR", "CHUDWARE_FS_ROOT"):
        monkeypatch.setenv(name, str(ambient))
    monkeypatch.delenv("CHUDWARE_RENDER_SAVE_ANYWHERE", raising=False)
    monkeypatch.setattr(config, "get_tool_options", lambda *_: {})
    monkeypatch.setattr(config, "tool_timeout_s_default", lambda: 0)

    def probe(path):
        return {
            "path": cad_common.resolve_agent_path(path, "output path"),
            "render": _render_save_target("figures/output.png"),
            "save": request_context.root_value("CHUDWARE_PROJECT_SAVE_DIR"),
        }

    entry = {"fn": probe, "description": "Workspace root probe",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}

    def build(context):
        context["tool_entries"] = {"render_part": entry}
        context["allowed_tools"] = ["render_part", "enable_tools"]
        context["resolver"] = lambda name: probe if name == "render_part" else None
        context["omp_builtin_names"] = ()
        return online_lane.real_build_tools(context)

    monkeypatch.setattr(omp_tools, "build_host_tools", build)
    original_run = online_lane.host.run_turn
    observed = []

    def run_turn(params, emit):
        root = params["workspace_dir"]
        assert request_context.root_value("CHUDWARE_PROJECT_DIR") == root
        binding = next(item for item in params["host_tools"] if item.name == "render_part")
        assert binding.project_root == binding.save_root == root

        def sdk_thread():
            assert request_context.root_value("CHUDWARE_PROJECT_DIR") == str(ambient)
            execute = binding.to_host_tool(lambda **kwargs: kwargs["execute"])
            result = execute(
                {"path": "parts/test.step+"},
                SimpleNamespace(cancelled=False, tool_call_id="js-write-123",
                                agent_id="builder", parent_agent_id="main"),
            )
            assert request_context.root_value("CHUDWARE_PROJECT_DIR") == str(ambient)
            return result["details"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(sdk_thread).result(timeout=10))
        return original_run(params, emit)

    monkeypatch.setattr(online_lane.host, "run_turn", run_turn)
    for index, project in enumerate((projects[0], projects[1], projects[0])):
        extra = {"session_id": f"conversation-{0 if index != 1 else 1}"}
        if index == 2:
            extra["omp_session_file"] = str(tmp_path / "resumed-session.jsonl")
        result = worker_rpc._agent_admin_turn(
            _params(online_lane, run_id=f"root-turn-{index}", workspace_dir=str(project), **extra),
            lambda _event: None,
        )
        assert result["ok"], result
        assert request_context.root_value("CHUDWARE_PROJECT_DIR") == str(ambient)
    assert observed == [
        {"path": str(root / "parts" / "test.step+"),
         "render": str(root / "figures" / "output.png"), "save": str(root)}
        for root in (projects[0], projects[1], projects[0])
    ]
