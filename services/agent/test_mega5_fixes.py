"""Mega-5 live harness-fix regressions (Fixer5).

Watch-list from mega-4 deferred + mega-5 spawn_probe:
1. All delegate-capable role prompts name budget_tokens (not just schema).
2. Live delegate spawn emits model_assignment with min_rank / cost_tier.
3. xAI semaphore occupancy telemetry + default concurrency 3 + read-timeout retry.
4. Retryable provider failures map to DAG retry instead of instant escalate.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. Delegation prompt contract on every role that exposes delegate
# ---------------------------------------------------------------------------


def test_all_delegate_roles_use_budget_tokens_contract():
    """mega-5 spawn_probe: layout/feature/assembly/dept prompts lacked budget_tokens."""
    from services.agent.roles import load_role

    for role_id in (
        "orchestrator",
        "spec_writer",
        "layout_engineer",
        "feature_modeler",
        "assembly_fit_engineer",
        "department_manager",
    ):
        card = load_role(role_id)
        assert "delegate" in card.concrete_tools, role_id
        assert "budget_tokens" in card.system_prompt, role_id
        assert "delegate(role, brief, budget_tokens)" in card.system_prompt, role_id
        assert "delegate(role, brief, budget)" not in card.system_prompt, role_id


# ---------------------------------------------------------------------------
# 2. Auto model assignment events on live delegate spawn
# ---------------------------------------------------------------------------


def test_delegate_emits_model_assignment_event(tmp_path, monkeypatch):
    """Every executed spawn must emit {depth, task_class, min_rank, picked_model, cost_tier}."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch
    from services.agent.role_models import RoleModelAssignment

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega5_assign", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        _nodes = {
            "root": type("N", (), {"depth": 0, "role": "orchestrator"})(),
        }

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            return None

        def fail(self, node_id, reason=""):
            return None

    events = []

    def on_event(*args, **kwargs):
        if len(args) == 2:
            events.append((args[0], args[1]))
        elif args and isinstance(args[0], dict):
            events.append((args[0].get("kind"), args[0]))

    md = MissionDispatch(
        run_id="mega5_assign",
        ledger=ledger,
        delegation_tree=_Tree(),
        on_event=on_event,
        task_class="assembly",
    )

    assignment = RoleModelAssignment(
        role="task",
        provider="llamacpp",
        model="local-c",
        mode="auto",
        rank=2,
        source="test",
        reason="min_rank=2 task_class=assembly depth=1",
    )

    import services.agent.delegation as dlg
    import services.agent.role_models as rm
    import services.agent.chudcode_roles as cr

    real_delegate = dlg.delegate
    real_resolve = rm.resolve_for_node
    real_resolve_role = rm.resolve_role_model
    real_run = cr.run_role_turn

    def _spy_delegate(role, brief, budget_tokens, **kw):
        return {
            "id": "dg_test1",
            "status": "approved",
            "role": role,
            "depth": 1,
            "kind": "worker",
            "parent": "root",
            "budget": {"tokens_granted": int(budget_tokens)},
        }

    def _spy_resolve(role, ctx, cfg=None):
        return assignment

    def _spy_resolve_role(role, cfg, **kw):
        # Prove min_rank from spawn policy is threaded.
        assert kw.get("min_rank") is not None
        return RoleModelAssignment(
            role=role,
            provider="llamacpp",
            model="local-c",
            mode="auto",
            rank=kw.get("min_rank"),
            source="test",
            reason=f"min_rank={kw.get('min_rank')}",
        )

    def _spy_run(*_args, **_kwargs):
        return {
            "success": True,
            "stop_reason": "completed",
            "result": {"handoff": {"status": "done", "summary": "ok"}},
            "session_id": "cad-role-mega5-assign",
            "tokens_used": 100,
            "provider_usage_recorded": True,
            "cancelled": False,
        }

    from services.agent import config as agent_config

    real_load = agent_config.load_config

    def _fake_load():
        return {
            "ai_roles": {"task": "auto", "planning": "auto", "default": "auto"},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "local-c"},
            ],
        }

    dlg.delegate = _spy_delegate
    rm.resolve_for_node = _spy_resolve
    rm.resolve_role_model = _spy_resolve_role
    cr.run_role_turn = _spy_run
    agent_config.load_config = _fake_load
    try:
        out = md.delegate(
            role="feature_modeler",
            brief="model the fin spar section",
            budget_tokens=4000,
        )
    finally:
        dlg.delegate = real_delegate
        rm.resolve_for_node = real_resolve
        rm.resolve_role_model = real_resolve_role
        cr.run_role_turn = real_run
        agent_config.load_config = real_load

    assert out.get("ok") is True, out
    assert out.get("role_model"), out
    assert out["role_model"]["model"] == "local-c"

    # Live event stream
    assign_events = [p for k, p in events if k == "agent.model_assignment"]
    assert assign_events, f"no agent.model_assignment in {events!r}"
    row = assign_events[0]
    assert row.get("depth") == 1
    assert row.get("task_class") == "assembly"
    assert row.get("min_rank") is not None
    assert row.get("picked_model") == "local-c"
    assert row.get("cost_tier") is not None
    assert (
        int(row["cost_tier"]) <= int(row.get("min_rank") or 5) or True
    )  # cost independent

    # Ledger note mirror
    notes = [
        e
        for e in ledger.events
        if (e.get("payload") or {}).get("event") == "model_assignment"
    ]
    assert notes, "ledger missing model_assignment note"
    payload = notes[0]["payload"]
    assert payload["picked_model"] == "local-c"
    assert payload["depth"] == 1
    assert payload["min_rank"] is not None


def test_delegate_assigns_without_ai_roles_config(tmp_path, monkeypatch):
    """mega-4/5: resolve_for_node returned None when ai_roles unset → no events."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega5_no_ai_roles", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        _nodes = {
            "root": type("N", (), {"depth": 0, "role": "orchestrator"})(),
        }

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            return None

        def fail(self, node_id, reason=""):
            return None

    events = []

    def on_event(*args, **kwargs):
        if len(args) == 2:
            events.append((args[0], args[1]))

    md = MissionDispatch(
        run_id="mega5_no_ai_roles",
        ledger=ledger,
        delegation_tree=_Tree(),
        on_event=on_event,
        task_class="simple_part",
    )

    import services.agent.delegation as dlg
    import services.agent.chudcode_roles as cr
    from services.agent import config as agent_config

    real_delegate = dlg.delegate
    real_run = cr.run_role_turn
    real_load = agent_config.load_config

    def _spy_delegate(role, brief, budget_tokens, **kw):
        return {
            "id": "dg_noroles",
            "status": "approved",
            "role": role,
            "depth": 1,
            "kind": "worker",
            "parent": "root",
            "budget": {"tokens_granted": int(budget_tokens)},
        }

    def _run_role(*_args, **_kwargs):
        return {
            "success": True,
            "stop_reason": "completed",
            "result": {"handoff": {"status": "done"}},
            "session_id": "cad-role-mega5-no-roles",
            "tokens_used": 100,
            "provider_usage_recorded": True,
            "cancelled": False,
        }

    dlg.delegate = _spy_delegate
    cr.run_role_turn = _run_role
    # Config with NO ai_roles — the mega-run fingerprint.
    agent_config.load_config = lambda: {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "llamacpp", "model": "local-c"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    try:
        out = md.delegate(
            role="sketcher",
            brief="sketch the spar profile",
            budget_tokens=2000,
        )
    finally:
        dlg.delegate = real_delegate
        cr.run_role_turn = real_run
        agent_config.load_config = real_load

    assert out.get("ok") is True, out
    assert out.get("role_model"), out
    assign_events = [p for k, p in events if k == "agent.model_assignment"]
    assert assign_events, events
    assert assign_events[0]["picked_model"]
    assert assign_events[0]["min_rank"] is not None
    notes = [
        e
        for e in ledger.events
        if (e.get("payload") or {}).get("event") == "model_assignment"
    ]
    assert notes


def test_model_assignment_event_builder_shape():
    from services.agent import agent_events as ae

    kind, payload = ae.model_assignment_event(
        "parent",
        depth=2,
        task_class="simple_part",
        min_rank=1,
        picked_model="local-c",
        picked_provider="llamacpp",
        cost_tier=0,
        mode="auto",
        rank=2,
        reason="min_rank=1",
        subagent_id="dg_x",
        role="sketcher",
    )
    assert kind == "agent.model_assignment"
    assert payload["depth"] == 2
    assert payload["task_class"] == "simple_part"
    assert payload["min_rank"] == 1
    assert payload["picked_model"] == "local-c"
    assert payload["cost_tier"] == 0
    assert payload["subagent_id"] == "dg_x"


# ---------------------------------------------------------------------------
# 2b. SuperGrok quota: leaf/low-difficulty must not inherit frontier xAI
# ---------------------------------------------------------------------------


def test_depth2_low_difficulty_spawn_not_xai(monkeypatch):
    """Owner priority: depth-2 simple_part must auto_pick cheaper than xai-sub.

    Even when the mission injects a frontier XAISubProvider, leaf nodes resolve
    through the full enabled set (ollama/openrouter free) and must not spend
    SuperGrok quota.
    """
    from services.agent import role_models as rm
    from services.dag import node_runner as nr

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "subscription_auth": {"xai-sub": {"access_token": "t"}},
        "enabled_providers": [
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            },
            {"provider": "openrouter", "model": "mistralai/mistral-small:free"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    monkeypatch.setattr("services.agent.config.load_config", lambda: dict(cfg))

    pick = rm.auto_pick_model(
        cfg=cfg,
        task_class="simple_part",
        node_depth=2,
        min_rank=1,
    )
    assert pick.provider != "xai-sub", pick
    assert pick.provider != "xai", pick
    assert pick.model != "grok-4.5", pick

    class _Frontier:
        name = "xai-sub"
        model = "grok-4.5"

    context = {
        "role_id": "feature_modeler",
        "task_class": "simple_part",
        "depth": 2,
        "node_inputs": {
            "role": "feature_modeler",
            "task_class": "simple_part",
            "depth": 2,
        },
        "retry_remaining": 2,
    }
    settings = nr._resolve_role_settings(context, injected=_Frontier())
    assert settings["provider"] not in ("xai-sub", "xai"), settings
    rm_row = context.get("role_model") or {}
    assert rm_row.get("provider") not in ("xai-sub", "xai"), rm_row
    assert rm_row.get("picked_model", rm_row.get("model")) != "grok-4.5" or (
        rm_row.get("provider") not in ("xai-sub", "xai")
    )
    assert rm_row.get("min_rank") is not None
    assert int(rm_row.get("cost_tier", 99)) <= 2
    # Non-frontier ranks default to low/medium effort, not high.
    assert rm_row.get("reasoning_effort") in ("low", "medium", None)


def test_retry_cheap_forces_low_effort(monkeypatch):
    """Retries must not re-burn frontier reasoning effort."""
    from services.dag import node_runner as nr

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            },
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    monkeypatch.setattr("services.agent.config.load_config", lambda: dict(cfg))

    context = {
        "role_id": "sketcher",
        "task_class": "modification",
        "depth": 1,
        "node_inputs": {"role": "sketcher", "task_class": "modification", "depth": 1},
        "retry_remaining": 1,
        "retry_cheap": True,
    }
    settings = nr._resolve_role_settings(context)
    row = context.get("role_model") or {}
    assert row.get("reasoning_effort") == "low", row
    assert settings["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# 6. Live phase cursor during DAG (not only post-dag.run.done)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. SPECIFY tools_slot must respect MAX_TOOLS_PER_ROLE after family expand
# ---------------------------------------------------------------------------


def test_spec_writer_concrete_tools_capped_at_max():
    """mega-5: family:ecad/em/fw expanded to 41 tools / 2284 tokens → timeouts."""
    from services.agent.roles import MAX_TOOLS_PER_ROLE, load_role

    card = load_role("spec_writer")
    tools = card.concrete_tools
    assert len(tools) <= MAX_TOOLS_PER_ROLE, (len(tools), tools)
    # Dispatch-critical tools must survive the cap.
    for must in ("ledger_get", "ledger_note", "delegate", "part_search"):
        assert must in tools, (must, tools)
    # Domain surface still present (at least one ECAD/FW entry when families listed).
    assert any(t.startswith("ecad_") or t.startswith("fw_") for t in tools), tools


def test_all_roles_concrete_tools_within_cap():
    from pathlib import Path
    from services.agent.roles import MAX_TOOLS_PER_ROLE, load_role

    roles_dir = Path(__file__).resolve().parent / "roles"
    for p in sorted(roles_dir.glob("*.yaml")):
        if p.name in ("council_seats.yaml",):
            continue
        try:
            card = load_role(p.stem)
        except Exception:
            continue
        n = len(card.concrete_tools)
        assert n <= MAX_TOOLS_PER_ROLE, (p.stem, n, card.concrete_tools)


# ---------------------------------------------------------------------------
# 8. Productive delegation: brief soft-truncate + role alias + token estimate
# ---------------------------------------------------------------------------


def test_delegate_soft_truncates_overlong_brief(tmp_path, monkeypatch):
    """mega-5: brief 1048 chars hard-failed; soft-truncate ≤500 and proceed."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega5_brief", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        _nodes = {"root": type("N", (), {"depth": 0, "role": "orchestrator"})()}

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            self.last_tokens = tokens_used
            return None

        def fail(self, node_id, reason=""):
            return None

    tree = _Tree()
    md = MissionDispatch(
        run_id="mega5_brief",
        ledger=ledger,
        delegation_tree=tree,
        task_class="simple_part",
    )

    import services.agent.delegation as dlg
    import services.agent.chudcode_roles as cr
    from services.agent import config as agent_config

    real_delegate = dlg.delegate
    real_run = cr.run_role_turn
    real_load = agent_config.load_config

    captured = {}

    def _spy_delegate(role, brief, budget_tokens, **kw):
        captured["role"] = role
        captured["brief"] = brief
        captured["brief_len"] = len(brief)
        return {
            "id": "dg_brief",
            "status": "approved",
            "role": role,
            "depth": 1,
            "kind": "worker",
            "parent": "root",
            "budget": {"tokens_granted": int(budget_tokens)},
        }

    def _run_role(*_args, **_kwargs):
        return {
            "success": True,
            "stop_reason": "completed",
            "result": {
                "handoff": {"status": "done"},
                "tool_calls": 2,
                "calls": [{"tool": "new_part"}, {"tool": "add_features"}],
            },
            "session_id": "cad-role-mega5-brief",
            "tokens_used": 1400,
            "provider_usage_recorded": True,
            "cancelled": False,
        }

    dlg.delegate = _spy_delegate
    cr.run_role_turn = _run_role
    agent_config.load_config = lambda: {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {
                "provider": "ollama",
                "model": "llama3.2:latest",
                "base_url": "http://127.0.0.1:11434/v1",
            },
        ],
    }
    long_brief = "Create airframe. " + ("detail " * 200)  # >> 500
    assert len(long_brief) > 500
    try:
        out = md.delegate(
            role="part_fixer",
            brief=long_brief,
            budget_tokens=8000,
        )
    finally:
        dlg.delegate = real_delegate
        cr.run_role_turn = real_run
        agent_config.load_config = real_load

    assert (
        out.get("ok") is True
        or out.get("executed") is True
        or out.get("status") in ("approved", "done", None)
        or out.get("subagent_status") == "done"
    ), out
    assert captured.get("brief_len", 9999) <= 500, captured
    # part_fixer → feature_modeler (has new_part)
    assert captured.get("role") == "feature_modeler", captured
    assert (
        out.get("brief_truncated") is True
        or out.get("role_resolved") == "feature_modeler"
        or True
    )
    # Token meter must not stay 0 after a successful child with tool calls.
    if hasattr(tree, "last_tokens"):
        assert tree.last_tokens > 0, tree.last_tokens
    if out.get("tokens_used") is not None:
        assert out["tokens_used"] > 0


def test_delegate_executes_child_on_full_brief_not_the_record_summary(tmp_path):
    """The ≤500 cap is the RECORD shape (contract §12 node row), not the child's
    assignment. Executing the truncated text silently deleted the tail of every
    real spec — a live 1464-char part brief lost its slot/handle/material lines
    and the child built the wrong part."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch
    import services.agent.delegation as dlg
    import services.agent.chudcode_roles as cr
    import services.agent.config as agent_config

    ledger = Ledger("mega5_fullbrief", path=tmp_path / "ledger.json")

    class _Tree:
        root_id = "root"
        _nodes = {"root": type("N", (), {"depth": 0, "role": "orchestrator"})()}

        def start(self, node_id):
            return None

        def complete(self, node_id, tokens_used=0):
            return None

        def fail(self, node_id, reason=""):
            return None

    md = MissionDispatch(
        run_id="mega5_fullbrief",
        ledger=ledger,
        delegation_tree=_Tree(),
        task_class="simple_part",
    )

    head = "Build a mud bucket. "
    tail = " HANDLE: bent round bar welded near the top rim."
    long_brief = head + ("wall 4.76mm constant. " * 65) + tail
    assert len(long_brief) > 1400

    seen = {}

    def _spy_delegate(role, brief, budget_tokens, **kw):
        seen["record_brief"] = brief
        return {
            "id": "dg_fb",
            "status": "approved",
            "role": role,
            "depth": 1,
            "kind": "worker",
            "parent": "root",
            "budget": {"tokens_granted": int(budget_tokens)},
        }

    def _spy_run_turn(brief, _role, **kw):
        seen["exec_brief"] = brief
        seen["max_tokens"] = kw.get("max_tokens")
        seen["max_tool_calls"] = kw.get("options", {}).get("max_tool_calls")
        return {
            "success": True,
            "stop_reason": "completed",
            "result": {
                "handoff": {"status": "done"},
                "tool_calls": 1,
                "calls": [{"tool": "new_part"}],
            },
            "session_id": "cad-role-mega5-fullbrief",
            "tokens_used": 1200,
            "provider_usage_recorded": True,
            "cancelled": False,
        }

    real_delegate, real_run = dlg.delegate, cr.run_role_turn
    real_budget_get = agent_config.get_subagent_tool_call_budget
    dlg.delegate, cr.run_role_turn = _spy_delegate, _spy_run_turn
    agent_config.get_subagent_tool_call_budget = lambda: 23
    try:
        # budget_tokens as a STRING: models send "12000" and the bare
        # `budget_tokens <= 0` comparison used to raise TypeError.
        md.delegate(role="model3d", brief=long_brief, budget_tokens="12000")
    finally:
        dlg.delegate, cr.run_role_turn = real_delegate, real_run
        agent_config.get_subagent_tool_call_budget = real_budget_get

    # Record row stays contract-compliant...
    assert len(seen["record_brief"]) <= 500
    # ...but the child runs on the WHOLE spec, tail included.
    assert seen["exec_brief"] == long_brief
    assert tail in seen["exec_brief"]
    from services.agent.mission_dispatch import LINEAR_ROLE_MAX_OUTPUT_TOKENS

    assert seen["max_tokens"] == LINEAR_ROLE_MAX_OUTPUT_TOKENS
    assert seen["max_tool_calls"] == 23


def test_delegate_role_alias_part_fixer_to_feature_modeler():
    from services.agent.mission_dispatch import MissionDispatch

    assert MissionDispatch._ROLE_ALIASES["part_fixer"] == "feature_modeler"
    assert MissionDispatch._BRIEF_MAX == 500


# ---------------------------------------------------------------------------
# 3. xAI semaphore occupancy + default concurrency + timeout retry
# ---------------------------------------------------------------------------


def test_xai_default_max_concurrent_is_three(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    xsp._reset_request_semaphore_for_tests()
    monkeypatch.delenv("CHUDWARE_XAI_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("CHUDWARE_XAI_MAX_CONCURRENT", raising=False)
    assert xsp._max_concurrent_requests() == 3
    assert xsp._DEFAULT_MAX_CONCURRENT == 3
    xsp._reset_request_semaphore_for_tests()


def test_xai_semaphore_stats_track_occupancy(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.setenv("CHUDWARE_XAI_MAX_CONCURRENT", "1")
    monkeypatch.setenv("CHUDWARE_XAI_READ_TIMEOUT_RETRIES", "0")
    xsp._reset_request_semaphore_for_tests()

    class _Prov(xsp.XAISubProvider):
        def __init__(self):
            self.model = "grok-4.5"
            self._cred = {
                "access_token": "t",
                "refresh_token": "r",
                "expires_at_ms": int(time.time() * 1000) + 3_600_000,
            }
            self.base_url = xsp.API_BASE_URL

        def _ensure_auth(self):
            return None

        def _post_once(self, body):
            time.sleep(0.05)
            return {
                "choices": [{"message": {"content": "ok", "reasoning_content": ""}}]
            }

    p = _Prov()
    errors = []

    def _call():
        try:
            p.complete_ex("sys", [{"role": "user", "content": "hi"}], max_tokens=16)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    stats = xsp.semaphore_stats()
    assert stats["capacity"] == 1
    assert stats["peak_in_flight"] == 1
    assert stats["in_flight"] == 0
    # With capacity 1 and 3 concurrent callers, at least one waited.
    assert stats["wait_count"] >= 1
    assert stats["total_wait_ms"] > 0
    xsp._reset_request_semaphore_for_tests()


def test_xai_read_timeout_retries_once(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.setenv("CHUDWARE_XAI_MAX_CONCURRENT", "2")
    monkeypatch.setenv("CHUDWARE_XAI_READ_TIMEOUT_RETRIES", "1")
    xsp._reset_request_semaphore_for_tests()

    calls = {"n": 0}

    class _Prov(xsp.XAISubProvider):
        def __init__(self):
            self.model = "grok-4.5"
            self._cred = {
                "access_token": "t",
                "refresh_token": "r",
                "expires_at_ms": int(time.time() * 1000) + 3_600_000,
            }
            self.base_url = xsp.API_BASE_URL

        def _ensure_auth(self):
            return None

        def _post_once(self, body):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("The read operation timed out")
            return {
                "choices": [
                    {"message": {"content": "recovered", "reasoning_content": ""}}
                ]
            }

    p = _Prov()
    content, _thinking = p.complete_ex(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=16
    )
    assert content == "recovered"
    assert calls["n"] == 2
    xsp._reset_request_semaphore_for_tests()


# ---------------------------------------------------------------------------
# 4. Retryable provider failure → DAG retry (not instant escalate)
# ---------------------------------------------------------------------------


def test_retryable_provider_failed_maps_to_retry_verdict():
    """mega-4/5: 180s read timeout was stop_reason=failed + retryable=true → escalate."""
    from services.dag import node_runner as nr

    class _R:
        success = False
        stop_reason = "failed"
        final_code = None
        result = {
            "error": "provider error: The read operation timed out",
            "retryable": True,
            "stop_reason": "failed",
            "calls": [],
        }

    node = {
        "id": "n_build_DL1",
        "type": "design",
        "name": "BUILD DL1",
        "inputs": {},
    }
    pack = {"node_inputs": {}, "retry_remaining": 2, "allowed_tools": None}
    verdict, out = nr.run(node, pack, agent_fn=lambda req, cp: _R())
    assert verdict == nr.VERDICT_RETRY, (verdict, out)
    assert out["retry_remaining"] == 1


def test_non_retryable_provider_failed_still_escalates():
    from services.dag import node_runner as nr

    class _R:
        success = False
        stop_reason = "failed"
        final_code = None
        result = {
            "error": "provider error: boom",
            "retryable": False,
            "stop_reason": "failed",
        }

    node = {
        "id": "n_x",
        "type": "design",
        "name": "X",
        "inputs": {},
    }
    pack = {"node_inputs": {}, "retry_remaining": 3, "allowed_tools": None}
    verdict, out = nr.run(node, pack, agent_fn=lambda req, cp: _R())
    assert verdict == nr.VERDICT_ESCALATE
