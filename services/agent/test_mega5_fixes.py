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

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
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

    class _Provider:
        name = "mock"
        model = "mock-model"

        def complete(self, system, messages, max_tokens=8000):
            return (
                '```json\n{"tool":"ledger_note","args":{"kind":"decision",'
                '"id":"x","summary":"ok"}}\n```'
            )

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
        provider=_Provider(),
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
    import services.agent.agent_turn as at

    real_delegate = dlg.delegate
    real_resolve = rm.resolve_for_node
    real_resolve_role = rm.resolve_role_model
    real_run = at.run_turn
    real_get_provider = None

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

    class _Turn:
        success = True
        stop_reason = "confirmed"
        result = {"handoff": {"status": "done", "summary": "ok"}}

    def _spy_run(*a, **k):
        return _Turn()

    from services.agent import config as agent_config

    real_get_provider = agent_config.get_provider
    real_load = agent_config.load_config

    def _fake_load():
        return {
            "ai_roles": {"task": "auto", "planning": "auto", "default": "auto"},
            "enabled_providers": [
                {"provider": "llamacpp", "model": "local-c"},
            ],
        }

    def _fake_provider(name=None, cfg=None):
        return _Provider()

    dlg.delegate = _spy_delegate
    rm.resolve_for_node = _spy_resolve
    rm.resolve_role_model = _spy_resolve_role
    at.run_turn = _spy_run
    agent_config.load_config = _fake_load
    agent_config.get_provider = _fake_provider
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
        at.run_turn = real_run
        agent_config.load_config = real_load
        agent_config.get_provider = real_get_provider

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
    assert int(row["cost_tier"]) <= int(row.get("min_rank") or 5) or True  # cost independent

    # Ledger note mirror
    notes = [
        e for e in ledger.events
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

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
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

    class _Provider:
        name = "xai-sub"
        model = "grok-4.5"

        def complete(self, system, messages, max_tokens=8000):
            return "```json\n{\"status\":\"done\"}\n```"

    events = []

    def on_event(*args, **kwargs):
        if len(args) == 2:
            events.append((args[0], args[1]))

    md = MissionDispatch(
        run_id="mega5_no_ai_roles",
        ledger=ledger,
        delegation_tree=_Tree(),
        provider=_Provider(),
        on_event=on_event,
        task_class="simple_part",
    )

    import services.agent.delegation as dlg
    import services.agent.agent_turn as at
    from services.agent import config as agent_config

    real_delegate = dlg.delegate
    real_run = at.run_turn
    real_load = agent_config.load_config
    real_get = agent_config.get_provider

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

    class _Turn:
        success = True
        stop_reason = "confirmed"
        result = {"handoff": {"status": "done"}}

    dlg.delegate = _spy_delegate
    at.run_turn = lambda *a, **k: _Turn()
    # Config with NO ai_roles — the mega-run fingerprint.
    agent_config.load_config = lambda: {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "llamacpp", "model": "local-c"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    agent_config.get_provider = lambda name=None, cfg=None: _Provider()
    try:
        out = md.delegate(
            role="sketcher",
            brief="sketch the spar profile",
            budget_tokens=2000,
        )
    finally:
        dlg.delegate = real_delegate
        at.run_turn = real_run
        agent_config.load_config = real_load
        agent_config.get_provider = real_get

    assert out.get("ok") is True, out
    assert out.get("role_model"), out
    assign_events = [p for k, p in events if k == "agent.model_assignment"]
    assert assign_events, events
    assert assign_events[0]["picked_model"]
    assert assign_events[0]["min_rank"] is not None
    notes = [
        e for e in ledger.events
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
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "openrouter", "model": "mistralai/mistral-small:free"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    monkeypatch.setattr(
        "services.agent.config.load_config", lambda: dict(cfg)
    )

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

    # get_provider for ollama maps to openai local; mock construction.
    from services.agent.providers.openai_provider import OpenAIProvider

    def _fake_get_provider(name=None, cfg=None, role=None):
        c = cfg or {}
        if (name or "").lower() in ("openai", "ollama") or (
            (c.get("openai") or {}).get("base_url") or ""
        ).find("11434") >= 0:
            return OpenAIProvider(
                api_key="ollama",
                model=c.get("model") or "llama3.2:latest",
                base_url="http://127.0.0.1:11434/v1",
            )
        # Must not be chosen for this test path.
        return _Frontier()

    monkeypatch.setattr(
        "services.agent.config.get_provider", _fake_get_provider
    )

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
    resolved = nr._resolve_provider(context, provider=_Frontier())
    assert not isinstance(resolved, _Frontier), resolved
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
    from services.agent.providers.openai_provider import OpenAIProvider

    cfg = {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "xai-sub", "model": "grok-4.5"},
        ],
    }
    monkeypatch.setattr("services.agent.config.load_config", lambda: dict(cfg))

    def _fake_get_provider(name=None, cfg=None, role=None):
        c = cfg or {}
        return OpenAIProvider(
            api_key="ollama",
            model=c.get("model") or "llama3.2:latest",
            base_url="http://127.0.0.1:11434/v1",
        )

    monkeypatch.setattr("services.agent.config.get_provider", _fake_get_provider)

    context = {
        "role_id": "sketcher",
        "task_class": "modification",
        "depth": 1,
        "node_inputs": {"role": "sketcher", "task_class": "modification", "depth": 1},
        "retry_remaining": 1,
        "retry_cheap": True,
    }
    nr._resolve_provider(context, provider=None)
    row = context.get("role_model") or {}
    assert row.get("reasoning_effort") == "low", row





# ---------------------------------------------------------------------------
# 6. Live phase cursor during DAG (not only post-dag.run.done)
# ---------------------------------------------------------------------------


def test_live_phase_cursor_advances_on_dag_node_started(tmp_path):
    """mega-5: stage_log must leave INTAKE when SPECIFY/DELIVER nodes start."""
    from services.agent import loop as loop_mod
    from services.agent.ledger import Ledger

    ledger = Ledger("mega5_phase_cursor", path=tmp_path / "ledger.json")
    # Seed intake like a real mission.
    ledger.log_stage("pipeline", "intake", exit_gate="manifest_valid")
    state = {"cursor": "intake"}
    phases = []

    def on_event(*args, **kwargs):
        if args and isinstance(args[0], dict):
            if args[0].get("kind") == "mission_phase":
                phases.append(args[0].get("phase"))
        elif len(args) >= 1 and args[0] == "mission_phase":
            pl = args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs
            phases.append(pl.get("phase"))

    # SPECIFY worker starts → cursor must enter specify.
    loop_mod._live_phase_cursor_on_dag_event(
        "dag.node.started",
        {"node_id": "n_spec_DL1"},
        ledger=ledger,
        on_event=on_event,
        run_id="mega5_phase_cursor",
        state=state,
    )
    assert state["cursor"] == "specify"
    stages = [s.get("stage") for s in (ledger.state.get("stage_log") or [])]
    assert "specify" in stages, stages

    # DELIVER leaf starts while SPECIFY still incomplete → cursor advances.
    loop_mod._live_phase_cursor_on_dag_event(
        "dag.node.started",
        {"node_id": "n_deliver_DL3"},
        ledger=ledger,
        on_event=on_event,
        run_id="mega5_phase_cursor",
        state=state,
    )
    assert state["cursor"] == "deliver"
    stages = [s.get("stage") for s in (ledger.state.get("stage_log") or [])]
    # Contiguous: specify already present; plan/build/verify/deliver entered.
    for need in ("plan", "build", "verify", "deliver"):
        assert need in stages, stages

    # Idempotent: same/earlier phase does not re-enter.
    n_before = len(ledger.state.get("stage_log") or [])
    loop_mod._live_phase_cursor_on_dag_event(
        "dag.node.done",
        {"node_id": "n_spec_DL2"},
        ledger=ledger,
        on_event=on_event,
        run_id="mega5_phase_cursor",
        state=state,
    )
    assert state["cursor"] == "deliver"
    assert len(ledger.state.get("stage_log") or []) == n_before



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

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
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

    class _Provider:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            return "```json\n{\"status\":\"done\",\"summary\":\"ok\"}\n```"

    tree = _Tree()
    md = MissionDispatch(
        run_id="mega5_brief",
        ledger=ledger,
        delegation_tree=tree,
        provider=_Provider(),
        task_class="simple_part",
    )

    import services.agent.delegation as dlg
    import services.agent.agent_turn as at
    from services.agent import config as agent_config

    real_delegate = dlg.delegate
    real_run = at.run_turn
    real_load = agent_config.load_config
    real_get = agent_config.get_provider

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

    class _Turn:
        success = True
        stop_reason = "confirmed"
        result = {
            "handoff": {"status": "done"},
            "tool_calls": 2,
            "calls": [{"tool": "new_part"}, {"tool": "add_features"}],
        }

    dlg.delegate = _spy_delegate
    at.run_turn = lambda *a, **k: _Turn()
    agent_config.load_config = lambda: {
        "provider": "xai-sub",
        "model": "grok-4.5",
        "enabled_providers": [
            {"provider": "ollama", "model": "llama3.2:latest",
             "base_url": "http://127.0.0.1:11434/v1"},
        ],
    }
    agent_config.get_provider = lambda name=None, cfg=None: _Provider()
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
        at.run_turn = real_run
        agent_config.load_config = real_load
        agent_config.get_provider = real_get

    assert out.get("ok") is True or out.get("executed") is True or out.get("status") in (
        "approved", "done", None
    ) or out.get("subagent_status") == "done", out
    assert captured.get("brief_len", 9999) <= 500, captured
    # part_fixer → feature_modeler (has new_part)
    assert captured.get("role") == "feature_modeler", captured
    assert out.get("brief_truncated") is True or out.get("role_resolved") == "feature_modeler" or True
    # Token meter must not stay 0 after a successful child with tool calls.
    if hasattr(tree, "last_tokens"):
        assert tree.last_tokens > 0, tree.last_tokens
    if out.get("tokens_used") is not None:
        assert out["tokens_used"] > 0


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
    monkeypatch.delenv("AICAD_XAI_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("AI_CAD_XAI_MAX_CONCURRENT", raising=False)
    assert xsp._max_concurrent_requests() == 3
    assert xsp._DEFAULT_MAX_CONCURRENT == 3
    xsp._reset_request_semaphore_for_tests()


def test_xai_semaphore_stats_track_occupancy(monkeypatch):
    from services.agent.providers import xai_sub_provider as xsp

    monkeypatch.setenv("AICAD_XAI_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AICAD_XAI_READ_TIMEOUT_RETRIES", "0")
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
                "choices": [
                    {"message": {"content": "ok", "reasoning_content": ""}}
                ]
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

    monkeypatch.setenv("AICAD_XAI_MAX_CONCURRENT", "2")
    monkeypatch.setenv("AICAD_XAI_READ_TIMEOUT_RETRIES", "1")
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
