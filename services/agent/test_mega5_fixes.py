"""Mega-5 live harness-fix regressions (Fixer5).

Watch-list from mega-4 deferred + mega-5 spawn_probe:
1. Live delegate spawn emits model_assignment with cost_tier (and no invented rank).
2. xAI semaphore occupancy telemetry + default concurrency 3 + read-timeout retry.
3. Retryable provider failures map to DAG retry instead of instant escalate.
"""

from __future__ import annotations

import threading
import time


# ---------------------------------------------------------------------------
# 1. Model assignment events on live delegate spawn
# ---------------------------------------------------------------------------


def test_delegate_assigns_without_ai_roles_config(tmp_path, monkeypatch):
    """mega-4/5: an unset ai_roles config still assigns a model and emits the event."""
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
    import services.agent.omp_roles as cr
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
    assert "rank" not in assign_events[0] and "min_rank" not in assign_events[0]
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
        picked_model="local-c",
        picked_provider="llamacpp",
        cost_tier=0,
        mode="fixed",
        reason="Using the configured sketcher model.",
        subagent_id="dg_x",
        role="sketcher",
    )
    assert kind == "agent.model_assignment"
    assert payload["depth"] == 2
    assert payload["task_class"] == "simple_part"
    assert payload["picked_model"] == "local-c"
    assert payload["cost_tier"] == 0
    assert payload["subagent_id"] == "dg_x"
    assert "rank" not in payload and "min_rank" not in payload




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
    import services.agent.omp_roles as cr
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
    import services.agent.omp_roles as cr
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
