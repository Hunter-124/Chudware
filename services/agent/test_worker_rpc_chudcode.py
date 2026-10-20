from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import hashlib

import math
import os
import threading

import pytest

from services.agent import (
    chudcode_tools,
    config,
    mission_dispatch,
    project_context,
    roles,
    subagent_registry,
    worker_rpc,
)
from services.mcp import cad_common
from services.agent.test_chudcode_host import cell_launch_runtime


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


class _Binding:
    def __init__(self, name: str):
        self.name = name

    def to_host_tool(self, factory):
        return self.name


class _FakeHost:
    def __init__(self):
        self.turns: list[dict] = []
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
        self.stt_states: dict[str, str] = {}
        self.shutdown_count = 0

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
        return {
            "ok": True,
            "run_id": params["run_id"],
            "session_id": session_id,
            "chudcode_session_id": f"cc-{id(token)}",
            "chudcode_session_file": f"/sessions/{session_id}.jsonl",
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
        return {"ok": True, "command": command, "params": params}

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
        if command == "get_auth_state":
            provider_id = payload.get("providerId") or "openai"
            return {
                "providers": [
                    {
                        "providerId": provider_id,
                        "name": provider_id.title(),
                        "authenticated": True,
                        "hasApiKey": False,
                        "hasOAuth": True,
                        "origin": "oauth",
                        "accounts": [
                            {
                                "accountId": "acct-1",
                                "email": "user@example.com",
                                "orgName": "Example",
                                "refreshToken": "must-not-escape",
                            }
                        ],
                    }
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
        if command == "validate_api_key" and payload.get("providerId") == "bad":
            return {
                "ok": False,
                "reason": f"provider rejected {payload['apiKey']}",
                "apiKey": payload["apiKey"],
            }
        if command == "validate_api_key":
            return {
                "providerId": payload["providerId"],
                "valid": True,
                "apiKey": payload["apiKey"],
            }
        if command == "set_api_key":
            return {
                "provider": {
                    "providerId": payload["providerId"],
                    "name": payload["providerId"].title(),
                    "authenticated": True,
                    "hasApiKey": True,
                    "hasOAuth": False,
                    "origin": "api_key",
                    "accounts": [],
                },
                "apiKey": payload["apiKey"],
            }
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
        if command == "stt_status":
            if session_id == "unavailable":
                return {
                    "ok": True,
                    "available": False,
                    "state": "idle",
                    "model": "whisper-small",
                    "error": "microphone unavailable",
                    "privateDevice": "/dev/secret",
                }
            return {
                "ok": True,
                "available": True,
                "state": self.stt_states.get(session_id, "idle"),
                "model": "whisper-small",
                "progress": 125,
                "privateDevice": "/dev/secret",
            }
        if command == "stt_start":
            self.stt_states[session_id] = "recording"
            return {
                "ok": True,
                "state": "recording",
                "model": "whisper-small",
                "privateDevice": "/dev/secret",
            }
        if command == "stt_stop":
            self.stt_states[session_id] = "idle"
            return {
                "ok": True,
                "state": "idle",
                "text": "make the bore wider",
                "segments": [{"secret": "must-not-escape"}],
            }
        if command == "stt_cancel":
            self.stt_states[session_id] = "idle"
            return {"ok": True, "state": "idle", "cancelToken": "must-not-escape"}
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
        if command == "compact":
            return {
                "ok": True,
                "summary": "trimmed",
                "shortSummary": "short",
                "firstKeptEntryId": "entry-2",
                "tokensBefore": 999,
                "preserveData": {"secret": "must-not-escape"},
            }
        if command == "get_state":
            return {
                "ok": True,
                "_hostToolNames": ["cad_tool"],
                "dumpTools": [
                    {"name": "read", "description": "Read files", "parameters": {}},
                    {"name": "eval", "description": "Run browser/desktop preludes", "parameters": {}},
                    {"name": "cad_tool", "description": "CAD", "parameters": {}},
                ],
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
            "chudcode_session_id": f"cc-{session_id}-child",
            "chudcode_session_file": f"/sessions/{session_id}-child.jsonl",
            "secret": "must-not-escape",
        }

    def repair_session(self, session_id, repair=False):
        self.recovery_calls.append(("repair", session_id, repair))
        if session_id not in self.sessions:
            return {"ok": False, "reason": "session_not_live"}
        return {
            "ok": True,
            "mode": "repair" if repair else "dry-run",
            "status": "healthy",
            "changed": False,
            "finding_count": 0,
            "repairable_finding_count": 0,
            "remaining_finding_count": 0,
            "counts": {},
            "findings": [],
            "backup_path": None,
            "chudcode_session_id": f"cc-{session_id}",
            "chudcode_session_file": f"/sessions/{session_id}.jsonl",
            "apiKey": "must-not-escape",
        }

    def close_session(self, session_id):
        self.recovery_calls.append(("close", session_id))
        self.sessions.pop(session_id, None)
        return {"ok": True}

    def shutdown(self):
        self.shutdown_count += 1


@pytest.fixture
def online_lane(monkeypatch, tmp_path):
    host = _FakeHost()
    saved: list[str] = []
    tool_contexts: list[dict] = []
    prompt_calls: list[dict] = []

    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: host)
    with worker_rpc._HISTORY_SEED_LOCK:
        worker_rpc._HISTORY_SEEDED_SESSIONS.clear()
    with worker_rpc._SESSION_TOOL_LOADS_LOCK:
        worker_rpc._SESSION_TOOL_LOADS.clear()

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
    monkeypatch.setattr(config, "clear_pooled_credentials", lambda: None)
    monkeypatch.setattr(config, "bind_subscription_grant", lambda grant: None)

    real_dispatch = mission_dispatch.MissionDispatch
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

    monkeypatch.setattr(chudcode_tools, "build_host_tools", fake_tools)
    monkeypatch.setattr(subagent_registry, "bind_session", lambda *args: None)

    def fake_save():
        saved.append("save")
        return {"saved": ["part.step+"], "errors": [], "persistence_failure": None}

    monkeypatch.setattr(cad_common, "save_touched_docs", fake_save)
    return SimpleNamespace(
        host=host,
        saved=saved,
        tool_contexts=tool_contexts,
        prompt_calls=prompt_calls,
        workspace=tmp_path,
        dispatch_type=real_dispatch,
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


@pytest.mark.parametrize("outcome", ["success", "cancel", "error", "crash", "preflight"])
def test_admin_turn_retires_only_its_preflight_and_finalization_connections(
    online_lane, monkeypatch, outcome
):
    from services.mcp import daemon_bridge

    class Connection:
        def __init__(self, *, writer_group):
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
    save = cad_common.save_touched_docs

    def final_save():
        assert daemon_bridge.client() is owned[0]
        assert not owned[0].closed
        return save()

    monkeypatch.setattr(cad_common, "save_touched_docs", final_save)
    run_turn = online_lane.host.run_turn

    def nested_turn(params, emit):
        with daemon_bridge.turn_connection_scope(host_pool):
            nested.append(daemon_bridge.client())
            assert nested[-1] is not owned[0]
            return run_turn(params, emit)

    online_lane.host.run_turn = nested_turn
    if outcome == "preflight":
        def failed_prompt(*args, **kwargs):
            raise RuntimeError("prompt assembly failed after the CAD preflight")
        monkeypatch.setattr(mission_dispatch, "role_prompt", failed_prompt)
    else:
        online_lane.host.mode = outcome
    try:
        with daemon_bridge.turn_connection_scope(caller_pool):
            caller = daemon_bridge.client()
            result = worker_rpc._agent_admin_turn(
                _params(online_lane, run_id=outcome,
                        chudcode_session_file="/sessions/conversation-1.jsonl"),
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


@pytest.mark.parametrize("failure", ["error", "crash"])
def test_failed_first_turn_retries_legacy_history_seed(online_lane, failure):
    history = [{"role": "user", "text": "Earlier question"}]
    online_lane.host.mode = failure
    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-failed", history=history),
        lambda _event: None,
    )
    assert first["ok"] is False
    assert "Earlier question" in online_lane.host.turns[0]["request"]

    online_lane.host.mode = "success"
    retry = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-retry", history=history),
        lambda _event: None,
    )
    assert retry["ok"] is True
    assert "Earlier question" in online_lane.host.turns[1]["request"]

    followup = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-followup", history=history),
        lambda _event: None,
    )
    assert followup["ok"] is True
    assert online_lane.host.turns[2]["request"] == "Do the work"


def test_failed_followup_does_not_reseed_successful_session(online_lane):
    history = [{"role": "user", "text": "Earlier question"}]
    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-first", history=history),
        lambda _event: None,
    )
    online_lane.host.mode = "error"
    failed = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-failed-followup", history=history),
        lambda _event: None,
    )
    online_lane.host.mode = "success"
    retry = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-next", history=history),
        lambda _event: None,
    )
    assert first["ok"] is True
    assert failed["ok"] is False
    assert retry["ok"] is True
    assert online_lane.host.turns[2]["request"] == "Do the work"


def test_resumed_conversation_first_turn_names_the_live_document_ids(
    online_lane, monkeypatch
):
    """2026-09-28 c221: the app restarted mid-run; the resumed turn reused the transcript's
    doc-cpp00000003 and render_part failed "no such document" (a different open order
    would have hit the wrong part). The first turn of a persisted conversation in a fresh
    worker must say which ids are live now; later turns in the same process must not."""
    listing = {"docs": [{"doc_id": "doc-cpp00000002", "name": "sixaxis_arm",
                         "kind": "assembly", "path": "/p/sixaxis_arm.step+"}]}
    monkeypatch.setattr(worker_rpc, "_editor_engine_call",
                        lambda method, params: listing if method == "doc_list" else {})
    resume = {"chudcode_session_file": "/sessions/conversation-1.jsonl"}

    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-resumed", **resume), lambda _event: None
    )
    second = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-next", **resume), lambda _event: None
    )

    assert first["ok"] is True and second["ok"] is True
    sent = online_lane.host.turns[0]["request"]
    assert sent.startswith("Do the work\n\nRESUMED CONVERSATION")
    assert 'doc-cpp00000002: "sixaxis_arm" (assembly, /p/sixaxis_arm.step+)' in sent
    assert "import_part" in sent
    assert online_lane.host.turns[1]["request"] == "Do the work"


def test_resume_note_survives_an_unreachable_engine(online_lane, monkeypatch):
    def unreachable(method, params):
        raise ConnectionError("engine down")

    monkeypatch.setattr(worker_rpc, "_editor_engine_call", unreachable)
    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-resumed",
                chudcode_session_file="/sessions/conversation-1.jsonl"),
        lambda _event: None,
    )

    assert result["ok"] is True
    assert "call list_parts() before using any doc_id" in online_lane.host.turns[0]["request"]


def test_tools_loaded_in_one_turn_stay_loaded_for_the_next_turn_of_that_session(
    online_lane,
):
    """c219: turn 1 loaded inspect_section/render_animation with enable_tools; turn 3
    rebuilt the tool set from the GUI's request alone, so the model's calls hit
    "No such tool: xd://inspect_section". A load belongs to the conversation."""
    real_run_turn = online_lane.host.run_turn

    class Client:
        def set_custom_tools(self, tools, *, loadable_tools=None):
            pass

    def run_turn_loading(params, emit):
        params["register_callbacks"](Client())
        online_lane.tool_contexts[-1]["enable_categories"](["inspect_section"])
        return real_run_turn(params, emit)

    online_lane.host.mode = "error"  # a turn that dies after the load still loaded it
    online_lane.host.run_turn = run_turn_loading
    first = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-load"), lambda _event: None
    )
    online_lane.host.mode = "success"
    online_lane.host.run_turn = real_run_turn
    followup = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-next", categories=["analysis"]),
        lambda _event: None,
    )
    other = worker_rpc._agent_admin_turn(
        {**_params(online_lane, run_id="run-other"), "session_id": "conversation-2"},
        lambda _event: None,
    )

    turn_contexts = list({id(ctx): ctx for ctx in online_lane.tool_contexts}.values())
    assert first["ok"] is False
    assert followup["ok"] is True
    assert turn_contexts[1]["categories"] == ["analysis", "inspect_section"]
    assert turn_contexts[2]["categories"] == []


def test_cancelled_first_turn_retries_legacy_history_seed(online_lane):
    history = [{"role": "user", "text": "Earlier question"}]
    online_lane.host.mode = "cancel"
    cancelled = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-cancel", history=history),
        lambda _event: None,
    )
    online_lane.host.mode = "success"
    retry = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="run-retry", history=history),
        lambda _event: None,
    )
    assert cancelled["ok"] is True
    assert cancelled["turn"]["cancelled"] is True
    assert retry["ok"] is True
    assert "Earlier question" in online_lane.host.turns[1]["request"]


def test_persistent_reuse_one_time_history_seed_and_mapped_streaming(online_lane):
    emitted: list[dict] = []
    history = [
        {"role": "user", "text": "Earlier question"},
        {"role": "assistant", "text": "Earlier answer"},
    ]

    first = worker_rpc._agent_admin_turn(
        _params(
            online_lane,
            run_id="run-1",
            history=history,
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
    assert first["turn"]["chudcode_session_id"] == second["turn"]["chudcode_session_id"]
    assert (
        "<<<CHUDWARE_HISTORY_MIGRATION_V1_CONTEXT_ONLY>>>"
        in online_lane.host.turns[0]["request"]
    )
    assert "Earlier question" in online_lane.host.turns[0]["request"]
    # The explicit budget is announced at turn start (online_turn.turn_budget_notice);
    # this lane's default work style is check_with_me, i.e. read-only Plan mode.
    assert online_lane.host.turns[1]["request"] == (
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


def test_credit_denial_never_reaches_chudcode(online_lane, monkeypatch):

    monkeypatch.setattr(
        config,
        "pooled_credit_preflight",
        lambda *args, **kwargs: {"ok": False, "reason": "insufficient_credits"},
    )
    actor = {
        "email": "user@example.test",
        "plan": "metered",
        "credits_remaining": 0,
        "key_source": "pool",
        "_pool": {"provider": "openai", "api_key": "pool-test-key"},
        "_pool_key_sha256": hashlib.sha256(b"pool-test-key").hexdigest(),
        "_rate": {
            "model": "gpt-test",
            "prompt_usd_per_mtok": "1",
            "completion_usd_per_mtok": "1",
        },
    }

    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="denied", _chudware_actor=actor), lambda event: None
    )

    assert result == {"ok": False, "reason": "insufficient_credits"}
    assert online_lane.host.turns == []


def test_subscription_requires_digest_and_only_passes_supported_token_to_host(online_lane):
    actor = {
        "email": "tenant-a@example.test", "plan": "studio",
        "credits_remaining": 0, "key_source": "subscription",
        "_subscription": {"provider": "anthropic-sub", "access_token": "tenant-a-bearer"},
        "_subscription_token_sha256": hashlib.sha256(b"tenant-a-bearer").hexdigest(),
    }
    params = _params(online_lane, run_id="tenant-a", provider="anthropic",
                     model="claude-sonnet-4", _chudware_actor=actor)
    assert worker_rpc._agent_admin_turn(params, lambda event: None)["ok"]
    host_turn = online_lane.host.turns[-1]
    assert (host_turn["web_key_source"], host_turn["web_provider"],
            host_turn["web_tenant"], host_turn["web_credential"]) == (
                "subscription", "anthropic-sub", actor["email"], "tenant-a-bearer")
    assert "byok_key" not in host_turn

    for changed, reason in [
        ({**actor, "_subscription_token_sha256": "0" * 64}, "subscription_credential_mismatch"),
        ({**actor, "_subscription": None}, "invalid_subscription_grant"),
        ({**actor, "_subscription": {"provider": "openai-codex", "access_token": "codex"}},
         "subscription_isolation_unsupported"),
    ]:
        denied = worker_rpc._agent_admin_turn(
            {**params, "_chudware_actor": changed, "run_id": reason}, lambda event: None)
        assert denied["reason"] == reason
    assert len(online_lane.host.turns) == 1


def test_pool_requires_proven_credential_and_preserves_credit_preflight(online_lane, monkeypatch):
    monkeypatch.setattr(config, "pooled_credit_preflight",
                        lambda *_args, **_kw: {"ok": True, "max_output_tokens": 8192})
    actor = {
        "email": "tenant@example.test", "plan": "studio",
        "credits_remaining": 100, "key_source": "pool",
        "_pool": {"provider": "openrouter", "api_key": "pool-secret"},
        "_pool_key_sha256": hashlib.sha256(b"pool-secret").hexdigest(),
    }
    params = _params(online_lane, run_id="pool", provider="openrouter",
                     model="vendor/cheap", _chudware_actor=actor)
    assert worker_rpc._agent_admin_turn(params, lambda event: None)["ok"]
    host_turn = online_lane.host.turns[-1]
    assert (host_turn["web_key_source"], host_turn["web_provider"],
            host_turn["web_credential"]) == ("pool", "openrouter", "pool-secret")
    for changed, reason in [
        ({**actor, "_pool": None}, "pool_credential_unconfigured"),
        ({**actor, "_pool_key_sha256": "0" * 64}, "pool_credential_mismatch"),
        ({**actor, "key_source": ""}, "web_credential_source_invalid"),
        ({**actor, "key_source": "unknown"}, "web_credential_source_invalid"),
    ]:
        denied = worker_rpc._agent_admin_turn(
            {**params, "run_id": reason, "_chudware_actor": changed}, lambda event: None)
        assert denied["reason"] == reason
    assert len(online_lane.host.turns) == 1


def test_pool_preflight_estimates_the_exact_sent_request(online_lane, monkeypatch):
    from services.agent.online_turn import compose_request_text

    actor = {
        "email": "tenant@example.test", "plan": "studio",
        "credits_remaining": 100, "key_source": "pool",
        "_pool": {"provider": "openrouter", "api_key": "pool-secret"},
        "_pool_key_sha256": hashlib.sha256(b"pool-secret").hexdigest(),
    }
    estimates = []

    def preflight(_actor, *, prompt_tokens_estimate, max_output_tokens):
        estimates.append(prompt_tokens_estimate)
        return {"ok": True, "max_output_tokens": max_output_tokens}

    monkeypatch.setattr(config, "pooled_credit_preflight", preflight)
    result = worker_rpc._agent_admin_turn(
        _params(
            online_lane, run_id="preflight-same-text", provider="openrouter",
            model="vendor/cheap", _chudware_actor=actor,
            history=[{"role": "user", "text": "Earlier question"}],
            scope={"doc_id": "selected-doc", "feature_ids": ["feature-7"]},
        ),
        lambda _event: None,
    )
    assert result["ok"]
    sent = online_lane.host.turns[-1]["request"]
    migration = worker_rpc._bounded_history_seed(
        [{"role": "user", "text": "Earlier question"}]
    )
    editor = worker_rpc._editor_context_block(
        {"scope": {"doc_id": "selected-doc", "feature_ids": ["feature-7"]}}
    )
    assert sent == compose_request_text("Do the work", migration, editor)
    assert estimates == [max(1, math.ceil(len(sent.encode("utf-8")) / 4))]
    followup = worker_rpc._agent_admin_turn(
        _params(
            online_lane, run_id="preflight-followup", provider="openrouter",
            model="vendor/cheap", _chudware_actor=actor,
            history=[{"role": "user", "text": "Earlier question"}],
            scope={"doc_id": "selected-doc", "feature_ids": ["feature-7"]},
        ),
        lambda _event: None,
    )
    assert followup["ok"]
    next_sent = online_lane.host.turns[-1]["request"]
    assert next_sent == compose_request_text("Do the work", "", editor)
    assert estimates[-1] == max(1, math.ceil(len(next_sent.encode("utf-8")) / 4))


def test_turn_builds_root_tools_before_activation_and_callbacks_keep_selected_root(
    online_lane, tmp_path, monkeypatch,
):
    from services.agent import chudcode_roles, request_context as tenancy, workspace
    from services.agent.test_chudcode_tools import (
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
    real_build = chudcode_roles.build_host_tools
    assembly_roots = []

    def build(context):
        assembly_roots.append(tenancy.root_value("CHUDWARE_PROJECT_DIR"))
        context.update(
            tool_entries=entries, policy_registry=_Policy(),
            resolver=lambda name: getattr(entries.get(name), "fn", None),
        )
        return real_build(context)

    monkeypatch.setattr(chudcode_tools, "build_host_tools", build)
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
        assert params["mode"] == "agent"
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
    assert assembly_roots == [str(repository)]
    assert observed["workspace_status"]["ok"], observed["workspace_status"]
    assert observed["workspace_status"]["project_dir"] == str(selected)
    assert observed["workspace_status"]["docs"][0]["location"] == "project"
    assert observed["browse_models"]["ok"], observed["browse_models"]
    assert observed["browse_models"]["root"] == str(selected)
    assert [row["file"] for row in observed["browse_models"]["models"]] == ["arm.stl"]
    assert observed["fs_list"]["ok"], observed["fs_list"]
    assert observed["fs_list"]["path"] == str(selected)
    assert [row["name"] for row in observed["fs_list"]["entries"]] == ["arm.stl"]


def test_crashed_turn_restores_workspace_and_credentials(online_lane, monkeypatch):
    original_cwd = os.getcwd()
    original = {
        key: os.environ.get(key)
        for key in ("CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR")
    }
    clear_calls = []
    monkeypatch.setattr(config, "clear_pooled_credentials", lambda: clear_calls.append(True))
    online_lane.host.mode = "crash"
    response = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id="restore-after-crash"), lambda _event: None
    )
    assert response["ok"] is False
    assert response["persistence"]["saved"] == ["part.step+"]
    assert os.getcwd() == original_cwd
    assert {key: os.environ.get(key) for key in original} == original
    assert len(clear_calls) == 2


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


def test_cancel_steer_status_and_auth_memory_proxies(online_lane):
    assert worker_rpc.WORKER_METHODS["agent_cancel"](
        {"run_id": "run-c"}, lambda event: None
    )["cancelled"]
    assert worker_rpc.WORKER_METHODS["agent_steer"](
        {"run_id": "run-s", "text": "change course"}, lambda event: None
    )["steered"]
    assert worker_rpc.chudcode_control("status", {"run_id": "run-z"})["active"]
    assert online_lane.host.cancel_calls == ["run-c"]
    assert online_lane.host.steer_calls == [("run-s", "change course")]
    assert online_lane.host.status_calls == [("run-z", None)]

    auth_cases = {
        "chudcode_auth_state": {"providerId": "openai"},
        "chudcode_validate_key": {
            "providerId": "openai",
            "apiKey": "never-return-this",
        },
        "chudcode_set_key": {
            "providerId": "openai",
            "apiKey": "never-return-this",
        },
        "chudcode_remove_key": {"providerId": "openai"},
        "chudcode_logout": {"providerId": "openai"},
    }
    for method, payload in auth_cases.items():
        result = worker_rpc.WORKER_METHODS[method](payload, lambda event: None)
        assert result["ok"] is True
        assert "never-return-this" not in repr(result)
    rejected = worker_rpc.WORKER_METHODS["chudcode_validate_key"](
        {"providerId": "bad", "apiKey": "super-secret-key"}, lambda event: None
    )
    assert rejected["ok"] is False
    assert "super-secret-key" not in repr(rejected)
    assert "<redacted>" in rejected["reason"]
    memory_cases = {
        "memory_remember": (
            "memory_remember",
            {"root": "/project", "content": "Remember this"},
        ),
        "memory_recall": (
            "memory_recall",
            {"root": "/project", "query": "this"},
        ),
        "memory_stats": ("memory_stats", {"root": "/project"}),
    }
    for method, (command, payload) in memory_cases.items():
        result = worker_rpc.WORKER_METHODS[method](payload, lambda event: None)
        assert result["command"] == command
    worker_rpc.chudcode_shutdown()
    assert online_lane.host.shutdown_count == 1


def test_registers_exact_shared_chudcode_worker_methods():
    expected = {
        "chudcode_provider_list",
        "chudcode_auth_state",
        "chudcode_validate_key",
        "chudcode_set_key",
        "chudcode_remove_key",
        "chudcode_logout",
        "chudcode_login",
        "chudcode_models_list",
        "chudcode_session_stats",
        "chudcode_export_session",
        "chudcode_branch_session",
        "chudcode_repair_session",
        "chudcode_close_session",
        "chudcode_compact",
        "chudcode_set_auto_compaction",
        "chudcode_set_auto_retry",
        "chudcode_tool_state",
        "chudcode_stt_status",
        "chudcode_stt_start",
        "chudcode_stt_stop",
        "chudcode_stt_cancel",
    }
    assert expected <= worker_rpc.WORKER_METHODS.keys()


def test_provider_auth_model_and_login_results_are_bounded_and_secret_free(online_lane):
    emit: list[dict] = []
    providers = worker_rpc.WORKER_METHODS["chudcode_provider_list"]({}, emit.append)
    auth = worker_rpc.WORKER_METHODS["chudcode_auth_state"](
        {"providerId": "openai"}, emit.append
    )
    models = worker_rpc.WORKER_METHODS["chudcode_models_list"](
        {"providerId": "openai"}, emit.append
    )
    login = worker_rpc.WORKER_METHODS["chudcode_login"](
        {"providerId": "openai"}, emit.append
    )

    assert providers["ok"] is True
    assert providers["providers"][0]["state"] == "oauth_connected"
    assert providers["providers"][0]["authKinds"] == ["oauth", "api_key"]
    assert len(providers["providers"]) <= worker_rpc._CHUDCODE_MAX_PROVIDERS
    assert auth["state"] == "oauth_connected"
    assert auth["accounts"] == [
        {"accountId": "acct-1", "email": "user@example.com", "orgName": "Example"}
    ]
    # Filter by provider before bounding: unrelated providers cannot hide a late model.
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
        "vision",
        "input",
        "cost",
    }
    # Chudcode's per-model ladder drives the composer's effort menu; without it a new
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


def test_live_session_management_is_isolated_bounded_and_capability_honest(online_lane):
    online_lane.host.sessions["live"] = object()
    emit = lambda _event: None

    stats = worker_rpc.WORKER_METHODS["chudcode_session_stats"](
        {"session_id": "live"}, emit
    )
    compact = worker_rpc.WORKER_METHODS["chudcode_compact"](
        {"session_id": "live", "instructions": "Keep decisions"}, emit
    )
    auto_compact = worker_rpc.WORKER_METHODS["chudcode_set_auto_compaction"](
        {"session_id": "live", "enabled": False}, emit
    )
    auto_retry = worker_rpc.WORKER_METHODS["chudcode_set_auto_retry"](
        {"session_id": "live", "enabled": True}, emit
    )
    tools = worker_rpc.WORKER_METHODS["chudcode_tool_state"](
        {"session_id": "live"}, emit
    )
    missing = worker_rpc.WORKER_METHODS["chudcode_session_stats"](
        {"session_id": "inactive"}, emit
    )

    assert stats["cost"] == 0.25
    assert stats["tokens"]["total"] == 18
    assert "sessionFile" not in stats
    assert compact == {
        "ok": True,
        "summary": "trimmed",
        "shortSummary": "short",
        "firstKeptEntryId": "entry-2",
        "tokensBefore": 999,
    }
    assert auto_compact["ok"] is True
    assert auto_retry["ok"] is True

    by_name = {tool["name"]: tool for tool in tools["tools"]}
    assert by_name["eval"]["available"] is True
    assert by_name["eval"]["loadMode"] == "essential"
    assert "browser" not in by_name and "computer" not in by_name
    assert "cad_tool" not in by_name
    assert missing == {"ok": False, "reason": "session_not_live"}
    assert set(online_lane.host.sessions) == {"live"}
    assert (
        "live",
        "compact",
        {"customInstructions": "Keep decisions"},
    ) in online_lane.host.session_calls


def test_recovery_worker_methods_are_exact_secret_free_and_never_activate_sessions(
    online_lane,
):
    online_lane.host.sessions.update({"live": object(), "close-me": object()})
    emit = lambda _event: None

    exported = worker_rpc.WORKER_METHODS["chudcode_export_session"](
        {"session_id": "live"}, emit
    )
    diagnosed = worker_rpc.WORKER_METHODS["chudcode_repair_session"](
        {"session_id": "live", "repair": False}, emit
    )
    branched = worker_rpc.WORKER_METHODS["chudcode_branch_session"](
        {"session_id": "live", "entry_id": "entry-3"}, emit
    )
    closed = worker_rpc.WORKER_METHODS["chudcode_close_session"](
        {"session_id": "close-me"}, emit
    )
    closed_again = worker_rpc.WORKER_METHODS["chudcode_close_session"](
        {"session_id": "close-me"}, emit
    )
    inactive = worker_rpc.WORKER_METHODS["chudcode_export_session"](
        {"session_id": "inactive"}, emit
    )

    assert exported == {
        "ok": True,
        "path": "/project/chat-exports/live.html",
    }
    assert diagnosed == {
        "ok": True,
        "mode": "dry-run",
        "status": "healthy",
        "changed": False,
        "finding_count": 0,
        "repairable_finding_count": 0,
        "remaining_finding_count": 0,
        "counts": {},
        "findings": [],
        "backup_path": None,
        "chudcode_session_id": "cc-live",
        "chudcode_session_file": "/sessions/live.jsonl",
    }
    assert branched == {
        "ok": True,
        "chudcode_session_id": "cc-live-child",
        "chudcode_session_file": "/sessions/live-child.jsonl",
    }
    assert closed == {"ok": True}
    assert closed_again == {"ok": True}
    assert inactive == {"ok": False, "reason": "session_not_live"}
    assert "must-not-escape" not in repr((exported, diagnosed, branched))
    assert set(online_lane.host.sessions) == set()


def test_live_stt_worker_methods_are_bounded_isolated_and_cancellable(online_lane):
    online_lane.host.sessions.update(
        {"voice-a": object(), "voice-b": object(), "unavailable": object()}
    )
    emit = lambda _event: None

    started = worker_rpc.WORKER_METHODS["chudcode_stt_start"](
        {"session_id": "voice-a"}, emit
    )
    status_a = worker_rpc.WORKER_METHODS["chudcode_stt_status"](
        {"session_id": "voice-a"}, emit
    )
    status_b = worker_rpc.WORKER_METHODS["chudcode_stt_status"](
        {"session_id": "voice-b"}, emit
    )
    stopped = worker_rpc.WORKER_METHODS["chudcode_stt_stop"](
        {"session_id": "voice-a"}, emit
    )
    cancelled = worker_rpc.WORKER_METHODS["chudcode_stt_cancel"](
        {"session_id": "voice-b"}, emit
    )
    unavailable = worker_rpc.WORKER_METHODS["chudcode_stt_status"](
        {"session_id": "unavailable"}, emit
    )
    missing = worker_rpc.WORKER_METHODS["chudcode_stt_status"](
        {"session_id": "missing"}, emit
    )
    unknown = worker_rpc.WORKER_METHODS["chudcode_stt_cancel"](
        {"session_id": "voice-b", "device": "wrong-layer"}, emit
    )

    assert started == {
        "ok": True,
        "available": True,
        "state": "recording",
        "model": "whisper-small",
        "progress": 100,
    }
    assert status_a["state"] == "recording"
    assert status_a["progress"] == 100
    assert status_b["state"] == "idle"
    assert set(status_b) == {"ok", "available", "state", "model", "progress"}
    assert stopped == {
        "ok": True,
        "available": True,
        "state": "idle",
        "text": "make the bore wider",
    }
    assert cancelled == {
        "ok": True,
        "available": True,
        "state": "idle",
    }
    assert unavailable == {
        "ok": True,
        "available": False,
        "state": "idle",
        "model": "whisper-small",
        "error": "microphone unavailable",
    }
    assert missing == {"ok": False, "reason": "session_not_live"}
    assert unknown == {
        "ok": False,
        "reason": "chudcode_stt_cancel has unknown params",
    }
    assert online_lane.host.stt_states == {"voice-a": "idle", "voice-b": "idle"}
    assert (
        "voice-a",
        "stt_start",
        {},
    ) in online_lane.host.session_calls
    assert (
        "voice-b",
        "stt_start",
        {},
    ) not in online_lane.host.session_calls


def test_legacy_session_adapters_never_rewrite_online_chudcode_history(monkeypatch):
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

    assert compact == {
        "ok": True,
        "summary": "trimmed",
        "short_summary": "short",
        "strategy": "summary",
        "firstKeptEntryId": "entry-3",
        "tokensBefore": 400,
    }
    assert compact_calls[0][2]["compaction_settings"]["methodOrder"] == ["soft"]
    assert compact_calls[0][1] == "chudcode"
    assert compact_calls[0][2]["session_id"] == "live"
    assert compact_calls[0][2]["instructions"] == "Keep decisions"
    assert "provider" not in compact_calls[0][2]
    assert "messages" not in compact
    assert "kept_messages" not in compact
    assert shake["ok"] is False
    assert shake["owner"] == "chudcode"
    assert shake_calls == []


def test_online_registration_uses_only_chudcode(online_lane, monkeypatch):

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
def test_auto_rate_limit_fallback_keeps_the_callers_budget_or_none(
    online_lane, monkeypatch, budget
):
    """The Auto cost-class fallback used to share an invented 7200 s budget, so a GUI turn
    that fell back got a hidden two-hour stop. No caller budget means none on the
    fallback either; an explicit one is shared across the fallbacks."""
    from services.agent import role_models

    first = role_models.RoleModelAssignment(
        role="chat", provider="anthropic-sub", model="claude-sub", mode="auto"
    )
    fallback = role_models.RoleModelAssignment(
        role="chat", provider="openai", model="gpt-test", mode="auto"
    )
    real_resolve = worker_rpc.resolve_model_and_effort

    def auto_resolve(*args, **kwargs):
        preferences, provider, model, _assignment, _candidates = real_resolve(*args, **kwargs)
        return preferences, provider, model, first, [{"provider": "openai", "model": "gpt-test"}]

    monkeypatch.setattr(worker_rpc, "resolve_model_and_effort", auto_resolve)
    monkeypatch.setattr(role_models, "resolve_role_model", lambda *a, **k: fallback)

    real_run_turn = online_lane.host.run_turn
    turn_budgets: list[object] = []
    requests: list[str] = []

    def rate_limited_once(params, emit):
        turn_budgets.append(params.get("timeout", "absent"))
        requests.append(params["request"])
        if len(turn_budgets) == 1:
            online_lane.host.turns.append(params)
            return {"ok": False, "reason": "429 rate limit exceeded after retries"}
        return real_run_turn(params, emit)

    monkeypatch.setattr(online_lane.host, "run_turn", rate_limited_once)
    extra = {} if budget is None else {"timeout": budget}

    result = worker_rpc._agent_admin_turn(
        _params(online_lane, run_id=f"auto-{budget}", **extra), lambda _event: None
    )

    assert result["ok"] is True, result
    assert len(turn_budgets) == 2
    if budget is None:
        assert turn_budgets == ["absent", "absent"]
    else:
        assert turn_budgets[0] == budget
        assert 0 < turn_budgets[1] <= budget
    # Each budgeted attempt is told its own remaining budget; an unbudgeted one never is.
    assert all(("hard" in request and "budget" in request) == (budget is not None)
               for request in requests)


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
    sent = online_lane.host.turns[-1]["request"]
    if notice is None:
        assert sent == "Do the work"
        assert "timeout" not in online_lane.host.turns[-1]
    else:
        assert sent == f"Do the work\n\n{notice}"
        assert online_lane.host.turns[-1]["timeout"] == 600.0


@pytest.mark.parametrize("mode", ["ask", "plan", "agent"])
def test_cell_single_turn_does_not_authorize_delegate(online_lane, monkeypatch, mode):
    from services.agent import request_context as tenancy

    monkeypatch.setenv("XDG_DATA_HOME", str(online_lane.workspace / "cell-state"))
    monkeypatch.setattr(mission_dispatch, "MissionDispatch", online_lane.dispatch_type)
    monkeypatch.setattr(worker_rpc, "resolve_workspace", lambda params: ("/work", "/work", None))
    ctx = {"cell": True, "workspace_key": "team:worker/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    with tenancy.bind_context(ctx):
        session = tenancy.mint_handle("session")
        run_id = tenancy.mint_handle("run", parent=session)
        result = worker_rpc._agent_admin_turn(_params(
            online_lane, run_id=run_id, session_id=session, mode=mode,
            _chudware_actor={"email": "alice@example.test", "plan": "pro",
                             "credits_remaining": 10, "key_source": "byok"},
            provider_grant="cpg1.fixture.signature",
        ), lambda event: None)
        assert result["ok"], result
        dispatch = online_lane.tool_contexts[0]["mission_dispatch"]
        assert dispatch.execution_options["max_subagent_depth"] == 0
        refusal = dispatch.delegate(role="reviewer", brief="inspect only", budget_tokens=16000)
        assert refusal["ok"] is False and refusal["reason"] == "spawn_depth_policy", refusal
        assert len(online_lane.host.turns) == 1


@pytest.mark.parametrize("mode", ["ask", "plan", "agent"])
def test_cell_turn_delegate_uses_actual_parent_session_and_broker_scope(
    online_lane, cell_launch_runtime, monkeypatch, mode
):
    from services.agent import chudcode_host, chudcode_roles, request_context as tenancy, role_models
    from services.agent.test_chudcode_host import CellLaunchFactory

    factory = CellLaunchFactory()
    host = chudcode_host.ChudcodeSessionHost(client_factory=factory)
    ctx = {"cell": True, "workspace_key": "team:worker/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    actor = {"email": "alice@example.test", "plan": "pro", "credits_remaining": 10,
             "key_source": "byok"}
    monkeypatch.setattr(mission_dispatch, "MissionDispatch", online_lane.dispatch_type)
    monkeypatch.setattr(worker_rpc, "resolve_workspace", lambda params: ("/work", "/work", None))
    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: host)
    monkeypatch.setattr(role_models, "resolve_for_node", lambda *args, **kwargs: None)
    monkeypatch.setattr(role_models, "resolve_role_model", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "get_subagent_tool_call_budget", lambda: 0)
    monkeypatch.setattr(config, "load_config", lambda: {
        "provider": "openai", "model": "gpt-test",
        "agent": {"compaction_strategy": "snapcompact"},
    })
    monkeypatch.setattr(chudcode_roles, "get_default_chudcode_host", lambda: host)
    monkeypatch.setattr(chudcode_host, "get_default_chudcode_host", lambda: host)
    try:
        with tenancy.bind_context(ctx):
            session = tenancy.mint_handle("session")
            run_id = tenancy.mint_handle("run", parent=session)
            params = _params(
                online_lane, run_id=run_id, session_id=session, workspace_dir="/work",
                request="Inspect two documents", doc_count=2,
                mode=mode, _chudware_actor=actor, provider_grant="cpg1.fixture.signature",
            )
            result = worker_rpc._agent_admin_turn(params, lambda event: None)
            assert result["ok"], result
            dispatch = online_lane.tool_contexts[0]["mission_dispatch"]
            assert dispatch.execution_options["session_id"] == session
            parent = host.session_launch_params(session)
            child = dispatch.delegate(role="reviewer", brief="inspect only", budget_tokens=16000)
            if mode == "ask":
                # Answer-only parents do not authorize delegation. Exercise the
                # private live-parent role lane without changing that policy.
                assert child["ok"] is False and child["reason"] == "spawn_depth_policy", child
                assert len(factory.clients) == 1
                child = chudcode_roles.run_role_turn(
                    "inspect only", "reviewer", session_id=session, run_id="private-review",
                )
                assert child["success"], child
            else:
                assert child.get("executed") and child["subagent_status"] == "done", child
            inherited = host.session_launch_params(child["session_id"])
            assert child["session_id"] not in (session, run_id)
            assert inherited["web_provider_grant"] == params["provider_grant"]
            assert inherited["web_tenant"] == "alice"
            assert inherited["mode"] == mode
            assert inherited["settings"]["compaction_settings"] == parent["settings"]["compaction_settings"]
            assert inherited["settings"]["compaction_settings"]["methodOrder"] == ["snapcompact"]
            assert inherited["workspace_dir"] == "/work"
            assert not {"api_key", "web_credential", "byok_key"} & inherited.keys()
            client = factory.clients[-1]
            assert client.kwargs["command"][:4] == [
                "/opt/chudware/bin/chudware-actor-sandbox", ctx["actor_hex"],
                "rw" if mode == "agent" else "ro", "--",
            ]
            assert client.kwargs["env"]["CHUDWARE_PROVIDER_GRANT"] == params["provider_grant"]
            assert client.kwargs["env"]["ANTHROPIC_API_KEY"] == ""
            assert "inspect only" in Path(client.session_file).read_text()
            before = len(factory.clients)
            assert chudcode_roles.run_role_turn(
                "inspect", "reviewer", session_id=run_id, run_id="child"
            )["stop_reason"] == "not_found"
            assert len(factory.clients) == before
            if mode == "agent":
                pytest.importorskip("langgraph")
                from services.agent.test_chudcode_tools import _Policy
                assert host.release_session(child["session_id"]) == {"ok": True, "released": True}
                # Real role bindings retain the owning roots after the turn exits.
                # Preserve those roots here so the released-parent check is reached.
                stale_child = chudcode_tools.HostToolBinding(
                    "delegate", "Delegate from the owning role", {"type": "object"},
                    context={"session_id": child["session_id"], "mode": "agent",
                             "mission_dispatch": dispatch, "policy_registry": _Policy()},
                    project_root="/work", save_root="/work",
                )
                refused = chudcode_tools.dispatch_host_tool(
                    stale_child, {"role": "reviewer", "brief": "inspect only", "budget_tokens": 16000},
                    None, None,
                )
                assert refused["ok"] is False and refused["stop_reason"] == "not_found", refused
                assert len(factory.clients) == before
                assert dispatch.execution_options["session_id"] == session
                from services.dag.test_mcp_tools import _build_clients
                from services.dag.test_executor import _two_node_graph
                target = _build_clients()["dag_run"]
                binding = chudcode_tools.HostToolBinding(
                    "dag_run", "Run governed DAG", {"type": "object"}, handler=target,
                    context={"session_id": session, "mode": "agent", "policy_registry": _Policy()},
                    project_root="/work", save_root="/work",
                )
                dag_result = chudcode_tools.dispatch_host_tool(
                    binding, {"graph_json": _two_node_graph(), "autonomy_default": "auto",
                              "initial_state": {"session_id": "foreign-session"}}, None, None,
                )
                import json
                dag_result = json.loads(dag_result) if isinstance(dag_result, str) else dag_result
                assert dag_result["ok"], dag_result
                assert dag_result["status"] == "done", dag_result
                assert set(dag_result["nodes"]) == {"n_plan", "n_report"}
                for node in dag_result["nodes"].values():
                    assert node["status"] == "done", node
                    turn = node["output"]["data"]["agent_result"]
                    assert turn["success"] and turn["stop_reason"] == "completed", turn
                    assert turn["session_id"] not in (session, run_id, "foreign-session")
                    launched = host.session_launch_params(turn["session_id"])
                    assert launched["web_provider_grant"] == params["provider_grant"]
                    assert launched["settings"]["compaction_settings"] == parent["settings"]["compaction_settings"]
                    assert launched["workspace_dir"] == "/work"
                    assert Path(turn["chudcode_session_file"]).is_file()
    finally:
        host.shutdown()


@pytest.mark.parametrize("field", ["api_key", "web_credential", "byok_key", "web_provider_grant"])
def test_cell_worker_refuses_upstream_or_internal_credential_fields(online_lane, monkeypatch, field):
    from services.agent import request_context as tenancy
    monkeypatch.setenv("XDG_DATA_HOME", str(online_lane.workspace / "cell-state"))
    ctx = {"cell": True, "workspace_key": "team:worker/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    monkeypatch.setattr(worker_rpc, "resolve_workspace", lambda params: ("/work", "/work", None))
    with tenancy.bind_context(ctx):
        session = tenancy.mint_handle("session")
        run_id = tenancy.mint_handle("run", parent=session)
        result = worker_rpc._agent_admin_turn(
            _params(online_lane, run_id=run_id, session_id=session,
                    _chudware_actor={"key_source": "byok"}, **{field: "forbidden"}),
            lambda event: None,
        )
        assert result["reason"] == "credential_isolation_unsupported"
        assert not online_lane.host.turns


def test_broker_grants_are_redacted_from_nested_service_results():
    assert worker_rpc._redact_sensitive_response({
        "nested": [{"provider_grant": "cpg1.actor.signature",
                    "web_provider_grant": "cpg1.actor.signature", "session_id": "opaque"}]
    }) == {
        "nested": [{"provider_grant": "<redacted>", "web_provider_grant": "<redacted>",
                    "session_id": "opaque"}]
    }
