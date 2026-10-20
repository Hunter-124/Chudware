from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.agent import chudcode_roles as roles
from services.agent.roles import RoleError

_REAL_SETTINGS_FOR_ROLE = roles._settings_for_role


class _Binding:
    def __init__(self, name: str):
        self.name = name


class _Host:
    def __init__(self, *, cancelled: bool = False, fail: str = ""):
        self.cancelled = cancelled
        self.fail = fail
        self.calls: list[dict] = []
        self.closed: list[str] = []

    def run_turn(self, params, emit):
        self.calls.append(dict(params))
        if self.fail:
            return {"ok": False, "reason": self.fail, "cancelled": self.cancelled}
        emit(
            {
                "type": "usage",
                "usage": {
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "promptTokens": 11,
                    "completionTokens": 7,
                    "totalTokens": 18,
                    "contextTokens": 15,
                },
            }
        )
        emit(
            {
                "type": "agent_end",
                "isTerminal": True,
                "status": "cancelled" if self.cancelled else "completed",
                "text": "fixture answer",
            }
        )
        return {
            "ok": True,
            "run_id": params["run_id"],
            "session_id": params["session_id"],
            "cancelled": self.cancelled,
            "result": "fixture answer",
            "terminal": {
                "type": "agent_end",
                "status": "cancelled" if self.cancelled else "completed",
            },
            "chudcode_session_id": "rpc-session",
            "chudcode_session_file": "/tmp/rpc-session.jsonl",
        }

    def close_session(self, session_id, *, credential_params=None):
        self.closed.append(session_id)


@pytest.fixture(autouse=True)
def _reset_history_seed():
    with roles._HISTORY_LOCK:
        roles._HISTORY_SEEDED_SESSIONS.clear()
    yield
    with roles._HISTORY_LOCK:
        roles._HISTORY_SEEDED_SESSIONS.clear()


@pytest.fixture
def online_runtime(monkeypatch):
    card = SimpleNamespace(system_prompt="ROLE PROMPT", concrete_tools=["measure_part"])
    monkeypatch.setattr(roles, "load_role", lambda _role: card)
    monkeypatch.setattr(
        roles, "_resolve_tier_capability", lambda tier, cap, _opts: (tier, cap)
    )
    monkeypatch.setattr(
        roles,
        "_settings_for_role",
        lambda _role, _opts: {
            "provider": "fixture-provider",
            "model": "fixture-model",
            "thinking": "low",
        },
    )

    def assemble(
        _card,
        _role,
        _run_id,
        _parent_id,
        _options,
        _tier,
        _capability,
        _actor,
        _on_event,
    ):
        context = {"categories": [], "enabled_categories": []}
        tools = [_Binding("measure_part"), _Binding("render_part")]
        return (
            "RESOLVED ROLE PROMPT",
            ["measure_part", "render_part"],
            tools,
            object(),
            context,
        )

    monkeypatch.setattr(roles, "_assemble_role_runtime", assemble)
    monkeypatch.setattr(roles, "_record_provider_usage", lambda _payload, _actor: None)
    return card


def test_online_role_prompt_tools_lineage_settings_history_attachments_and_result(
    monkeypatch, online_runtime
):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    events: list[tuple[str, dict]] = []
    actor = object()
    recorded: list[tuple[dict, object]] = []
    monkeypatch.setattr(
        roles,
        "_record_provider_usage",
        lambda payload, value: recorded.append((dict(payload), value)),
    )

    result = roles.run_role_turn(
        "inspect the bracket",
        "reviewer",
        session_id="conversation-7",
        run_id="run-9",
        parent_id="node-3",
        on_event=lambda kind, payload: events.append((kind, payload)),
        options={"workspace_dir": "/workspace"},
        history=[
            {"role": "user", "text": "make a bracket"},
            {"role": "assistant", "text": "built it"},
        ],
        attachments=[{"path": "/tmp/view.png", "kind": "image"}],
        max_tokens=4096,
        max_total_tokens=16000,
        actor=actor,
    )

    call = host.calls[0]
    assert call["system_prompt"] == "RESOLVED ROLE PROMPT"
    assert [item.name for item in call["host_tools"]] == [
        "measure_part",
        "render_part",
    ]
    assert call["settings"] == {
        "provider": "fixture-provider",
        "model": "fixture-model",
        "thinking": "low",
    }
    assert call["attachments"] == [{"path": "/tmp/view.png", "kind": "image"}]
    assert call["workspace_dir"] == "/workspace"
    assert call["max_tokens"] == 4096
    assert call["max_total_tokens"] == 16000
    assert "CHUDWARE_HISTORY_MIGRATION" in call["request"]
    assert "inspect the bracket" in call["request"]
    assert call["session_id"].startswith("cad-role-")
    assert result == {
        "success": True,
        "stop_reason": "completed",
        "result": "fixture answer",
        "role": "reviewer",
        "allowed_tools": ["measure_part", "render_part"],
        "run_id": "run-9",
        "session_id": call["session_id"],
        "parent_id": "node-3",
        "chudcode_session_id": "rpc-session",
        "chudcode_session_file": "/tmp/rpc-session.jsonl",
        "terminal": {"type": "agent_end", "status": "completed"},
        "cancelled": False,
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "context_tokens": 15,
            "calls": 1,
            "providers": [
                {
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "context_tokens": 15,
                }
            ],
        },
        "tokens_used": 18,
        "provider_usage_recorded": True,
        "calls": [],
        "tool_calls": 0,
        "offline": False,
    }
    usage_event = next(
        payload for kind, payload in events if kind == "agent.provider_usage"
    )
    assert usage_event["parent_id"] == "node-3"
    assert usage_event["session_id"] == call["session_id"]
    assert recorded and recorded[0][1] is actor


def test_persistent_child_session_is_stable_and_history_is_seeded_once(
    monkeypatch, online_runtime
):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    kwargs = {
        "session_id": "conversation",
        "run_id": "run",
        "parent_id": "parent",
        "history": [{"role": "user", "text": "prior"}],
    }

    first = roles.run_role_turn("first", "reviewer", **kwargs)
    second = roles.run_role_turn("second", "reviewer", **kwargs)

    assert first["session_id"] == second["session_id"]
    assert "CHUDWARE_HISTORY_MIGRATION" in host.calls[0]["request"]
    assert host.calls[1]["request"] == "second"
    assert host.closed == []


def test_distinct_parent_run_or_role_never_share_a_child_session(
    monkeypatch, online_runtime
):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    ids = {
        roles.run_role_turn(
            "a", "reviewer", session_id="s", run_id="r1", parent_id="p1"
        )["session_id"],
        roles.run_role_turn(
            "b", "reviewer", session_id="s", run_id="r2", parent_id="p1"
        )["session_id"],
        roles.run_role_turn(
            "c", "reviewer", session_id="s", run_id="r1", parent_id="p2"
        )["session_id"],
        roles.run_role_turn("d", "fixer", session_id="s", run_id="r1", parent_id="p1")[
            "session_id"
        ],
    }
    assert len(ids) == 4


def test_one_shot_session_closes_and_may_seed_history_again(
    monkeypatch, online_runtime
):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    kwargs = {
        "session_id": "conversation",
        "run_id": "advisor-run",
        "parent_id": "advisor-card",
        "history": [{"role": "user", "text": "prior"}],
        "close_session": True,
    }

    first = roles.run_role_turn("audit", "reviewer", **kwargs)
    second = roles.run_role_turn("audit again", "reviewer", **kwargs)

    assert host.closed == [first["session_id"], second["session_id"]]
    assert first["session_id"] == second["session_id"]
    assert all("CHUDWARE_HISTORY_MIGRATION" in call["request"] for call in host.calls)


def test_cancelled_host_result_preserves_events_usage_and_compatibility(
    monkeypatch, online_runtime
):
    host = _Host(cancelled=True)
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    events: list[tuple[str, dict]] = []

    result = roles.run_role_turn(
        "stop now",
        "reviewer",
        session_id="conversation",
        run_id="cancel-run",
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    assert result["success"] is False
    assert result["stop_reason"] == "cancelled"
    assert result["cancelled"] is True
    assert result["tokens_used"] == 18
    finished = [payload for kind, payload in events if kind == "run_finished"]
    assert finished and finished[-1]["status"] == "cancelled"


def test_explicit_offline_uses_only_safe_local_role_loop(monkeypatch):
    card = SimpleNamespace(system_prompt="offline role", concrete_tools=[])
    monkeypatch.setattr(roles, "load_role", lambda _role: card)
    monkeypatch.setattr(
        roles, "_resolve_tier_capability", lambda tier, cap, _opts: (tier, cap)
    )
    monkeypatch.setattr(roles, "_settings_for_role", lambda _role, _opts: {})
    provider = SimpleNamespace(name="mock", offline_safe=True)
    monkeypatch.setattr(roles, "_offline_provider", lambda *_args: provider)
    host_used = []
    monkeypatch.setattr(
        roles,
        "get_default_chudcode_host",
        lambda: host_used.append(True),
    )
    calls = []
    from services.agent import mission_dispatch

    def local_turn(request, role, received_provider, **kwargs):
        calls.append((request, role, received_provider, kwargs))
        return {
            "success": True,
            "stop_reason": "confirmed",
            "result": {"handoff": {"status": "done"}},
            "role": role,
            "allowed_tools": ["measure_part"],
        }

    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", local_turn)

    result = roles.run_role_turn(
        "local audit",
        "reviewer",
        session_id="conversation",
        run_id="offline-run",
        parent_id="parent",
        options={"offline": True, "offline_provider": provider},
        history=[{"role": "user", "text": "prior"}],
        attachments=[{"path": "/tmp/a.txt", "kind": "file"}],
        max_tokens=2000,
        max_total_tokens=8000,
        actor="credit-actor",
    )

    assert not host_used
    assert calls[0][2] is provider
    assert calls[0][3]["history"] == [{"role": "user", "text": "prior"}]
    assert calls[0][3]["attachments"] == [{"path": "/tmp/a.txt", "kind": "file"}]
    assert calls[0][3]["max_tokens"] == 2000
    assert calls[0][3]["max_total_tokens"] == 8000
    assert calls[0][3]["chudware_actor"] == "credit-actor"
    assert result["success"] is True
    assert result["result"] == {"handoff": {"status": "done"}}
    assert result["offline"] is True
    assert result["parent_id"] == "parent"


def test_offline_unsafe_provider_is_refused_without_running_loop(monkeypatch):
    monkeypatch.setattr(
        roles,
        "load_role",
        lambda _role: SimpleNamespace(system_prompt="role", concrete_tools=[]),
    )
    monkeypatch.setattr(
        roles, "_resolve_tier_capability", lambda tier, cap, _opts: (tier, cap)
    )
    monkeypatch.setattr(roles, "_settings_for_role", lambda _role, _opts: {})
    from services.agent import config, mission_dispatch

    monkeypatch.setattr(config, "provider_offline_safe", lambda _provider: False)
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *_args, **_kwargs: pytest.fail("unsafe provider reached local loop"),
    )

    result = roles.run_role_turn(
        "audit",
        "reviewer",
        session_id="conversation",
        options={
            "offline": True,
            "offline_provider": SimpleNamespace(name="cloud", offline_safe=False),
        },
    )

    assert result["success"] is False
    assert result["stop_reason"] == "offline_unavailable"


def test_invalid_role_returns_compatible_failure(monkeypatch):
    monkeypatch.setattr(
        roles, "load_role", lambda _role: (_ for _ in ()).throw(RoleError("missing"))
    )

    result = roles.run_role_turn(
        "audit", "not-a-role", session_id="conversation", run_id="run"
    )

    assert result["success"] is False
    assert result["stop_reason"] == "invalid_role"
    assert result["result"] == {"error": "invalid_role"}
    assert result["role"] == "not-a-role"
    assert result["allowed_tools"] == []


def test_online_path_never_constructs_provider_or_calls_deterministic_loop(
    monkeypatch, online_runtime
):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    from services.agent import agent_turn, config

    monkeypatch.setattr(
        config,
        "get_provider",
        lambda *_args, **_kwargs: pytest.fail("online path constructed a provider"),
    )
    monkeypatch.setattr(
        agent_turn,
        "run_turn",
        lambda *_args, **_kwargs: pytest.fail(
            "online path called deterministic agent_turn"
        ),
    )

    result = roles.run_role_turn(
        "audit", "reviewer", session_id="conversation", run_id="online-run"
    )

    assert result["success"] is True
    assert len(host.calls) == 1


def test_host_failure_retains_nested_result_shape(monkeypatch, online_runtime):
    host = _Host(fail="provider disconnected")
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)

    result = roles.run_role_turn(
        "audit", "reviewer", session_id="conversation", run_id="failed-run"
    )

    assert result["success"] is False
    assert result["stop_reason"] == "provider_error"
    assert result["result"] == {"error": "provider disconnected"}
    assert result["role"] == "reviewer"
    assert result["allowed_tools"] == ["measure_part", "render_part"]
    assert result["tokens_used"] == 0
    assert result["provider_usage_recorded"] is False


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"mission_dispatch": object()}, ["role_tool"]),
        ({"mission_dispatch": object(), "allowed_tools": None}, None),
        ({"mission_dispatch": object(), "allowed_tools": []}, []),
        (
            {
                "mission_dispatch": object(),
                "allowed_tools": ["exact_tool", "exact_tool", ""],
            },
            ["exact_tool", "exact_tool"],
        ),
    ],
)
def test_role_runtime_preserves_explicit_allowed_tool_tristate(
    monkeypatch,
    options,
    expected,
):
    from services.agent import mission_dispatch as mission

    captured = {}
    monkeypatch.setattr(
        mission,
        "role_prompt",
        lambda *_args, **_kwargs: ("SYSTEM", ["role_tool"]),
    )

    def fake_build_host_tools(context):
        captured.update(context)
        return []

    monkeypatch.setattr(roles, "build_host_tools", fake_build_host_tools)
    card = SimpleNamespace(concrete_tools=["role_tool"])
    roles._assemble_role_runtime(
        card,
        "reviewer",
        "run",
        "parent",
        {**options, "categories": ["analysis"]},
        None,
        None,
        None,
        None,
    )
    assert captured["allowed_tools"] == expected
    assert captured["categories"] == ["analysis"]


def test_bounded_tool_calls_preserve_order_doc_id_and_redact_secrets():
    records = [
        {
            "tool": "new_part",
            "args": {"name": "bracket", "api_key": "sk-secret"},
            "ok": True,
            "result": {"ok": True, "doc_id": "doc-7", "mesh": "x" * 8000},
        }
    ] + [
        {
            "tool": f"measure_{index}",
            "args": {"authorization": "Bearer raw-token", "index": index},
            "ok": True,
            "result": {"ok": True, "value": index},
        }
        for index in range(44)
    ]

    calls, total = roles._bounded_tool_calls(records)

    assert total == 45
    assert len(calls) == 40
    assert calls[0]["tool"] == "new_part"
    assert calls[0]["args"]["api_key"] == "[redacted]"
    assert calls[0]["result"]["doc_id"] == "doc-7"
    assert calls[1]["args"]["authorization"] == "[redacted]"
    assert len(str(calls[0]["result"])) < 2200


def test_role_events_preserve_single_dict_live_sink(monkeypatch, online_runtime):
    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    events = []

    roles.run_role_turn(
        "audit",
        "reviewer",
        session_id="conversation",
        run_id="run",
        parent_id="node",
        on_event=lambda event: events.append(event),
    )

    assert any(event.get("kind") == "agent.provider_usage" for event in events)
    assert all(event.get("parent_id") == "node" for event in events)


def test_cell_role_history_seeds_and_credentials_are_actor_scoped(monkeypatch, online_runtime):
    import hashlib
    from services.agent import request_context as tenancy

    host = _Host()
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    history = [{"role": "user", "content": "prior human request"}]
    for actor in ("alice", "bob"):
        ctx = {
            "cell": True, "workspace_key": "team:roles/work", "actor_key": actor,
            "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
            "right": "editor", "epoch": 1,
        }
        credential = {
            "web_actor": True, "web_key_source": "byok", "web_tenant": actor,
            "web_provider": "openai", "web_provider_grant": f"grant-{actor}",
            "workspace_dir": "/work", "mode": "plan",
        }
        monkeypatch.setattr(host, "session_launch_params", lambda session: dict(credential), raising=False)
        with tenancy.bind_context(ctx):
            first = roles.run_role_turn(
                "build", "general_agent", session_id="same", run_id="same-run",
                options=credential, history=history,
            )
            assert first["success"]
            assert "prior human request" in host.calls[-1]["request"]
            assert host.calls[-1]["web_provider_grant"] == credential["web_provider_grant"]
            assert host.calls[-1]["web_tenant"] == actor
            assert host.calls[-1]["mode"] == "plan"
            repeat = roles.run_role_turn(
                "build", "general_agent", session_id="same", run_id="same-run",
                options=credential, history=history,
            )
            assert repeat["success"]
            assert "prior human request" not in host.calls[-1]["request"]
    assert len(roles._HISTORY_SEEDED_SESSIONS) == 2


def test_cell_nested_role_inherits_parent_credential_scope(monkeypatch, online_runtime):
    import hashlib
    from services.agent import request_context as tenancy

    actor = "alice"
    ctx = {
        "cell": True, "workspace_key": "team:roles/work", "actor_key": actor,
        "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
        "right": "editor", "epoch": 1,
    }
    host = _Host()
    inherited = {
        "web_actor": True, "web_key_source": "byok", "web_tenant": actor,
        "web_provider": "openai", "web_provider_grant": "parent-scoped-grant",
        "workspace_dir": "/work",
    }
    monkeypatch.setattr(host, "session_launch_params", lambda session: dict(inherited), raising=False)
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    with tenancy.bind_context(ctx):
        result = roles.run_role_turn(
            "advise", "general_agent", session_id="parent", run_id="child",
            options={"mode": "ask"}, close_session=True,
        )
    assert result["success"]
    assert host.calls[-1]["web_provider_grant"] == "parent-scoped-grant"
    assert host.calls[-1]["mode"] == "ask"
    assert host.closed == [host.calls[-1]["session_id"]]


def test_role_preserves_trusted_manager_stop_terminal_audit(monkeypatch, online_runtime):
    delegate = {"actor_key": "manager", "right": "manager", "action": "cancel"}

    class ManagerStoppedHost(_Host):
        def run_turn(self, params, emit):
            def stamped(frame):
                if frame.get("type") == "agent_end":
                    frame = {**frame, "reason": "manager_stop", "control_delegate": delegate}
                emit(frame)

            return {**super().run_turn(params, stamped),
                    "reason": "manager_stop", "control_delegate": delegate}

    host = ManagerStoppedHost(cancelled=True)
    events = []
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    monkeypatch.setattr(roles, "_emit", lambda sink, kind, payload: events.append((kind, payload)))
    result = roles.run_role_turn(
        "work", "general_agent", session_id="owner-chat", run_id="owner-run",
    )
    assert not result["success"]
    assert result["stop_reason"] == "manager_stop"
    assert result["control_delegate"] == delegate
    audited = [payload for _, payload in events if payload.get("stop_reason") == "manager_stop"]
    assert audited and audited[-1]["control_delegate"] == delegate


@pytest.mark.parametrize("parent_mode,child_mode,expected", [
    ("ask", "agent", "ask"), ("plan", "agent", "plan"), ("plan", "ask", "ask"),
])
def test_cell_nested_role_cannot_change_parent_grant_mode_or_compaction(
    monkeypatch, online_runtime, parent_mode, child_mode, expected
):
    import hashlib
    from services.agent import request_context as tenancy

    host = _Host()
    ctx = {"cell": True, "workspace_key": "team:roles/work", "actor_key": "alice",
           "actor_hex": hashlib.sha256(b"alice").hexdigest()[:32], "right": "editor", "epoch": 1}
    parent = {
        "web_actor": True, "web_key_source": "byok", "web_tenant": "alice",
        "web_provider": "openai", "web_provider_grant": "cpg1.parent.signature",
        "workspace_dir": "/work", "mode": parent_mode,
        "settings": {"provider": "openai", "model": "selected",
                     "compaction_settings": {"methodOrder": ["snapcompact"]}},
    }
    seen = []
    def inherit(session):
        seen.append(session)
        if session != "real-parent-session":
            raise KeyError("not_found")
        return dict(parent)
    monkeypatch.setattr(host, "session_launch_params", inherit, raising=False)
    monkeypatch.setattr(roles, "get_default_chudcode_host", lambda: host)
    monkeypatch.setattr(roles, "_settings_for_role", _REAL_SETTINGS_FOR_ROLE)
    with tenancy.bind_context(ctx):
        result = roles.run_role_turn(
            "advise", "general_agent", session_id="real-parent-session", run_id="different-run",
            options={"mode": child_mode, "web_provider_grant": "cpg1.foreign.signature",
                     "workspace_dir": "/work/foreign", "provider": "anthropic", "model": "foreign",
                     "settings": {"provider": "anthropic", "model": "foreign",
                                  "compaction_settings": {"methodOrder": ["soft"]}}},
        )
        assert result["success"]
        call = host.calls[-1]
        assert call["web_provider_grant"] == parent["web_provider_grant"]
        assert call["mode"] == expected
        assert call["settings"] == parent["settings"]
        assert call["workspace_dir"] == parent["workspace_dir"]
        assert roles.run_role_turn(
            "advise", "general_agent", session_id="different-run", run_id="child"
        )["stop_reason"] == "not_found"
        assert len(host.calls) == 1
    assert seen == ["real-parent-session", "different-run"]
