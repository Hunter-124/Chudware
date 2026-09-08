"""test_router_dispatch.py — live linear-turn and routing regression coverage.

Exercises the explicit linear-turn tool dispatch and the build-swarm DAG compiler.
Copilot preference policy is supplied independently of the task classifier.

Discipline: scripted providers only (T-mega24-style), no model/network/port.
Run: pytest services/agent/test_router_dispatch.py -q
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def direct_work_preferences(monkeypatch):
    from services.agent import config

    monkeypatch.setattr(
        config, "load_config", lambda: {"copilot": {"work_style": "do_it"}},
    )

from services.agent.mission_dispatch import run_linear_role_turn


class _ScriptedProvider:
    """Returns a fixed sequence of replies; ignores the prompt (mirrors
    the mega24 offline scripted provider discipline)."""

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "no more"


class _LoopingProvider:
    """Always proposes the same tool call and never finishes on its own."""

    name = "looping"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```'


_CLEANUP_REQUEST = (
    "clean up my project files and put them into PLM, archive deprecated folders"
)
_BUILD_REQUEST = "design a two-part hinge assembly with clearance fit"


class TestLinearRoleTurn:
    """The worker-facing single-turn entry executes the requested role directly."""

    def test_general_agent_cleanup_uses_main_chat_loadout(self):
        dispatched: list[str] = []

        def _dispatch(tool, args):
            dispatched.append(tool)
            return {"ok": True}

        provider = _ScriptedProvider([
            '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
            '```json\n{"tool": "plm_checkpoint", "args": {"message": "cleanup"}}\n```',
            "Cleaned up the project and checkpointed it; nothing else to do.",
        ])

        turn = run_linear_role_turn(
            _CLEANUP_REQUEST,
            "general_agent",
            provider,
            tool_dispatch=_dispatch,
        )

        assert turn["success"] is True
        assert turn["role"] == "general_agent"
        assert dispatched == ["fs_list", "plm_checkpoint"]

        # Explicit Do it permits filesystem/project work.
        allowed = turn["allowed_tools"]
        for t in ("fs_write", "fs_edit", "plm_open", "plm_checkpoint"):
            assert t in allowed, f"{t} missing from general_agent turn tools"
        # Security hardening 3aaee658: the contract keeps shell behind explicit
        # role/category enablement, never on the automatic main-chat floor.
        assert "run_shell" not in allowed, "run_shell must not be in the main-chat floor"
        for t in ("fs_read", "fs_glob", "fs_grep"):  # SUBAGENT_DEFAULT_TOOLS floor
            assert t in allowed, f"{t} (subagent floor) missing"
        assert "enable_tools" in allowed
        assert "delegate" not in allowed
        assert "mission_start" not in allowed
        # The broad CAD core is loaded up front — the model never has to
        # enable_tools(['modeling']) before a fillet.
        for t in ("new_part", "add_features", "asm_add_component", "asm_mate",
                  "describe_part", "measure_part", "verify_battery"):
            assert t in allowed, f"{t} must be in the main-chat core loadout"

    def test_main_chat_prompt_lists_every_unloaded_tool_by_name(self):
        """Nothing is hidden: every registered tool outside the core is in the
        MORE TOOLS menu by exact name, and nothing loaded is repeated there."""
        from services.agent.mission_dispatch import (
            _apply_session_preferences,
            effective_role_system_prompt,
            linear_role_allowed_tools,
        )
        from services.agent.roles import load_role
        from services.agent.tool_categories import _live_catalog
        from services.mcp import tool_registry

        tool_registry.warm_catalog()
        card = load_role("general_agent")
        allowed, system = _apply_session_preferences(
            "general_agent",
            linear_role_allowed_tools(card),
            effective_role_system_prompt(card),
        )
        assert "MORE TOOLS" in system
        menu = system.split("MORE TOOLS", 1)[1]
        listed = {
            tok.strip()
            for line in menu.splitlines()
            if line.startswith("  ")
            for tok in line.split(",")
        }
        registry = set(_live_catalog())
        unloaded = registry - set(allowed)
        assert unloaded, "registry must be warm for this test to mean anything"
        assert unloaded <= listed, sorted(unloaded - listed)[:20]
        assert not (listed & set(allowed)), sorted(listed & set(allowed))[:20]

    def test_main_chat_runs_past_old_thirty_call_limit(self):
        """The old linear budget killed a composed 31st dispatch. Main chat now
        runs until the model itself emits a finish envelope.

        The 31 dispatches carry DISTINCT paths on purpose. They used to be one
        identical ``fs_list {"path": "."}`` repeated 31 times, which is a wedged
        model, not a long one: the loop guard refused 29 of the 31 and the test
        only passed because nothing bounded a refusal streak. Real work that
        legitimately runs long makes DIFFERENT calls, so that is what this pins —
        the identical-call spin is pinned separately by
        test_harness_integrity.py::test_identical_call_spin_stops_at_the_refusal_ceiling.
        """
        from services.agent import agent_turn

        tools = [
            '```json\n{"tool": "fs_list", "args": {"path": "./d%d"}}\n```' % i
            for i in range(31)
        ]
        done = (
            '```json\n{"done": true, "handoff": {"status": "done", '
            '"summary": "all work finished", "outputs": {}}}\n```'
        )
        provider = _ScriptedProvider(tools + [done])
        turn = agent_turn.run_turn(
            _CLEANUP_REQUEST,
            provider,
            allowed_tools=["fs_list"],
            max_tool_calls=30,
            tool_dispatch=lambda t, a: {"ok": True},
            subagent_id=None,
        )

        assert turn.stop_reason == "confirmed"
        assert len(turn.result["calls"]) == 31

    def test_subagent_soft_warning_fires_once_and_dispatch_continues(self):
        from services.agent import agent_turn

        tool = '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```'
        done = (
            '```json\n{"done": true, "handoff": {"status": "done", '
            '"summary": "converged", "outputs": {}}}\n```'
        )

        class _WarningProvider(_ScriptedProvider):
            def __init__(self):
                super().__init__([tool, tool, tool, tool, done])
                self.warning_counts: list[int] = []

            def complete(self, system, messages, max_tokens=8000):
                self.warning_counts.append(
                    str(messages).count("[TOOL-CALL BUDGET]")
                )
                return super().complete(system, messages, max_tokens=max_tokens)

        provider = _WarningProvider()
        turn = agent_turn.run_turn(
            "do the delegated work",
            provider,
            allowed_tools=["fs_list"],
            max_tool_calls=2,
            tool_dispatch=lambda t, a: {"ok": True},
            subagent_id="dg_soft_budget",
        )

        assert turn.stop_reason == "confirmed"
        assert len(turn.result["calls"]) == 3
        assert max(provider.warning_counts) == 1

    def test_subagent_stops_at_separate_hard_ceiling(self, monkeypatch):
        from services.agent import agent_turn

        monkeypatch.setattr(agent_turn, "SUBAGENT_HARD_TOOL_CALL_CEILING", 6)
        provider = _LoopingProvider()
        turn = agent_turn.run_turn(
            "loop",
            provider,
            allowed_tools=["fs_list"],
            max_tool_calls=1,
            tool_dispatch=lambda t, a: {"ok": True},
            subagent_id="dg_hard_ceiling",
        )

        assert turn.stop_reason == "max_iters"
        assert turn.success is False
        assert turn.result["tool_calls"] == 6
        assert turn.result["forced_stop"] == "subagent_hard_ceiling"

    def test_linear_turn_threads_lazy_dispatch_ledger(self, monkeypatch):
        from services.agent import agent_turn
        from services.agent.mission_dispatch import MissionDispatch

        bound_ledger = object()
        seen: dict = {}

        monkeypatch.setattr(
            MissionDispatch, "_ledger", lambda self: bound_ledger
        )

        def _fake_run_turn(*args, **kwargs):
            seen["ledger"] = kwargs.get("ledger")
            return agent_turn.TurnResult(
                True,
                "confirmed",
                {"handoff": {"status": "done", "summary": "ok"}},
            )

        monkeypatch.setattr(agent_turn, "run_turn", _fake_run_turn)
        run_linear_role_turn(
            "organize the project", "general_agent", provider=object()
        )
        assert seen["ledger"] is bound_ledger

    def test_subagent_budget_config_value_and_fallbacks(self):
        from services.agent.config import get_subagent_tool_call_budget

        assert get_subagent_tool_call_budget(
            {"agent": {"subagent_tool_call_budget": 27}}
        ) == 27
        assert get_subagent_tool_call_budget({}) == 0
        assert get_subagent_tool_call_budget(
            {"agent": {"subagent_tool_call_budget": "garbage"}}
        ) == 0


class TestBuildSwarmCompilerUnchanged:
    """The build swarm the model escalates to still compiles unchanged."""

    def test_build_mission_graph_still_compiles_classic_dag(self):
        from services.dag.mission_template import build_mission_graph

        manifest = {
            "request_verbatim": _BUILD_REQUEST,
            "ambiguities": [],
            "deliverables": [{
                "id": "d1", "name": "Hinge assembly", "kind": "assembly",
                "description": "Two-part hinge with a clearance fit.",
                "acceptance_criteria": [],
            }],
        }
        graph = build_mission_graph(manifest, graph_id="t_router_dispatch_dag")
        roles = {n["inputs"].get("role") for n in graph["nodes"]}
        assert "intake_analyst" in roles
        assert "assembly_fit_engineer" in roles or "feature_modeler" in roles
        assert "reviewer" in roles
