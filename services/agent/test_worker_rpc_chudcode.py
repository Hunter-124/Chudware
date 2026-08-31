from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.agent import (
    agent_turn,
    chudcode_tools,
    config,
    mission_dispatch,
    project_context,
    roles,
    subagent_registry,
    worker_rpc,
)
from services.mcp import cad_common


class _Binding:
    def __init__(self, name: str):
        self.name = name


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

    def steer(self, run_id, text):
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
                        "input": ["text", "image"],
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
        if command in {"validate_api_key", "set_api_key"}:
            return {
                "providerId": payload["providerId"],
                "valid": True,
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
                    {"name": "browser", "description": "Browse", "parameters": {}},
                    {"name": "cad_tool", "description": "CAD", "parameters": {}},
                ],
            }
        return {"ok": True, "enabled": payload["enabled"]}

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
    monkeypatch.setattr(agent_turn, "resolver_for", lambda dispatch: lambda name: None)

    class FakeDispatch:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.ledger = None

    monkeypatch.setattr(mission_dispatch, "MissionDispatch", FakeDispatch)

    def fake_prompt(role, **kwargs):
        prompt_calls.append({"role": role, **kwargs})
        return "RESOLVED ROLE PROMPT", ["cad_tool", "enable_tools"], None

    monkeypatch.setattr(mission_dispatch, "linear_role_prompt", fake_prompt)

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
    assert online_lane.host.turns[1]["request"] == "Continue"
    assert online_lane.host.turns[0]["system_prompt"] == "RESOLVED ROLE PROMPT"
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
        "chudcode_compact",
        "chudcode_set_auto_compaction",
        "chudcode_set_auto_retry",
        "chudcode_tool_state",
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
    assert len(models["models"]) == 256
    assert set(models["models"][0]) == {
        "id",
        "name",
        "provider",
        "contextWindow",
        "maxTokens",
        "reasoning",
        "input",
        "cost",
    }
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
    assert by_name["browser"]["available"] is True
    assert by_name["browser"]["loadMode"] == "discoverable"
    assert by_name["computer"]["available"] is False
    assert by_name["computer"]["loadMode"] == "essential"
    assert "cad_tool" not in by_name
    assert missing == {"ok": False, "reason": "session_not_live"}
    assert set(online_lane.host.sessions) == {"live"}
    assert (
        "live",
        "compact",
        {"customInstructions": "Keep decisions"},
    ) in online_lane.host.session_calls


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

    compact = worker_rpc.SESSION_METHODS["session_compact"](
        {
            "session_id": "live",
            "strategy": "off",
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
        "firstKeptEntryId": "entry-3",
        "tokensBefore": 400,
    }
    assert compact_calls[0][1] == "chudcode"
    assert compact_calls[0][2]["session_id"] == "live"
    assert compact_calls[0][2]["instructions"] == "Keep decisions"
    assert "provider" not in compact_calls[0][2]
    assert "messages" not in compact
    assert "kept_messages" not in compact
    assert shake["ok"] is False
    assert shake["owner"] == "chudcode"
    assert shake_calls == []


def test_online_registration_never_selects_legacy_provider_loop(
    online_lane, monkeypatch
):

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy provider loop selected")

    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", forbidden)
    monkeypatch.setattr(worker_rpc, "_legacy_offline_agent_admin_turn", forbidden)

    result = worker_rpc.WORKER_METHODS["agent_admin_turn"](
        _params(online_lane, run_id="clean-cutover"), lambda event: None
    )

    assert result["ok"] is True
    assert worker_rpc.ADMIN_METHODS["agent_admin_turn"] is worker_rpc._agent_admin_turn
    assert worker_rpc._agent_admin_turn is not forbidden
