"""Parity test for the copilot mode tool-gating contract
(contracts/copilot_modes.json + .md).

Encodes the IDENTICAL mode × tool verdict matrix as the C++ doctest
core/tests/test_copilot_modes.cpp — both runtimes load the same contract JSON
and must produce the same allow/deny verdicts. Any change to the contract data
or rules must keep both tests green in the same commit.

Also pins that the Python module's public sets are exactly the contract data
(no private copy that could drift from what C++ reads).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.agent import copilot_mode_tools as cmt

_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "copilot_modes.json"


@pytest.fixture(scope="module")
def contract() -> dict:
    with open(_CONTRACT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# THE parity matrix: same rows/columns/verdicts as test_copilot_modes.cpp.
#                 tool                 agent plan swarm edit  ask
_MATRIX = [
    ("describe_part",                   True, True, True, True, True),
    ("measure_part",                    True, True, True, True, True),
    ("research_web_search",             True, True, True, True, True),
    ("ask_user",                        True, True, True, True, True),
    ("enable_tools",                    True, True, True, True, True),
    ("list_tool_categories",            True, True, True, True, True),
    ("new_part",                        True, True, True, True, True),  # shared floor
    ("save_part",                       True, True, True, True, True),
    ("add_features",                    True, False, True, True, False),
    ("edit_feature",                    True, False, True, True, False),
    ("new_assembly",                    True, False, True, False, False),
    ("cam_generate",                    True, False, True, False, False),
    ("delegate",                        True, True, True, True, False),
    ("corporate_activate",              True, False, True, False, False),
    ("run_fea_static",                  True, True, True, False, False),
    ("inspect_part",                    True, True, True, True, False),
    ("dag_run",                         True, False, True, False, False),
    # T-file-floor: agent_default_tools main-agent floor. Absent from ask/
    # plan mode_tools and shared_default_tools -> unrestricted services/agent/swarm
    # only (no role card here, so both are None-unrestricted regardless).
    ("fs_write",                        True, False, True, False, False),
    ("fs_edit",                         True, False, True, False, False),
    ("fs_list",                         True, False, True, False, False),
    ("run_shell",                       True, False, True, False, False),
    # research_fetch_page is already in ask/plan/edit mode_tools directly.
    ("research_fetch_page",             True, True, True, True, True),
]

_MODES = ("agent", "plan", "swarm", "edit", "ask")


class TestContractDataIsWhatTheModuleExposes:
    def test_module_sets_match_contract_json(self, contract):
        assert cmt.COPILOT_MODES == frozenset(contract["modes"])
        assert cmt.DEFAULT_MODE == contract["default_mode"]
        assert cmt.SHARED_DEFAULT_TOOLS == frozenset(contract["shared_default_tools"])
        assert cmt.ASK_TOOLS == frozenset(contract["mode_tools"]["ask"])
        assert cmt.PLAN_TOOLS == frozenset(contract["mode_tools"]["plan"])
        assert cmt.EDIT_TOOLS == frozenset(contract["mode_tools"]["edit"])
        assert cmt._MUTATING_TOOLS == frozenset(contract["mutating_tools"])
        assert cmt._EDIT_DENIED == frozenset(contract["edit_denied"])
        assert cmt.SWARM_EXTRA_TOOLS == frozenset(contract["swarm_extra_tools"])
        assert cmt.AGENT_DEFAULT_TOOLS == frozenset(contract["agent_default_tools"])
        assert cmt.SUBAGENT_DEFAULT_TOOLS == frozenset(contract["subagent_default_tools"])
        assert set(contract["unrestricted_modes"]) == {"agent", "swarm"}

    def test_schema_tag(self, contract):
        assert contract["schema"] == "aicad.copilot_modes/1"


class TestVerdictMatrix:
    @pytest.mark.parametrize("tool,agent,plan,swarm,edit,ask", _MATRIX)
    def test_verdicts(self, tool, agent, plan, swarm, edit, ask):
        expected = dict(zip(_MODES, (agent, plan, swarm, edit, ask)))
        for mode, want in expected.items():
            got = cmt.is_tool_allowed(tool, mode)
            assert got is want, f"{tool!r} under {mode!r}: expected {want}, got {got}"


class TestRoleCardSemantics:
    def test_agent_swarm_unrestricted_without_role(self):
        assert cmt.apply_mode_tools("agent", None) is None
        assert cmt.apply_mode_tools("swarm", None) is None

    def test_agent_keeps_role_set(self):
        # T-lean-floor: agent mode floors with AGENT_DEFAULT_TOOLS (the
        # main-agent fs/shell/web floor) only — SHARED_DEFAULT_TOOLS (CAD
        # document-lifecycle tools) is NOT unioned onto agent-mode role
        # cards; those packs are pulled on demand via enable_tools.
        out = cmt.apply_mode_tools("agent", ["add_features", "new_part", "delegate"])
        assert out == sorted(
            {"add_features", "new_part", "delegate"} | cmt.AGENT_DEFAULT_TOOLS
        )

    def test_agent_role_card_no_longer_gains_shared_default_floor(self):
        # T-lean-floor: CAD document-lifecycle tools (SHARED_DEFAULT_TOOLS)
        # must NOT leak onto an agent-mode role card that doesn't request
        # them; they stay reachable via enable_tools/family packs instead.
        out = cmt.apply_mode_tools("agent", ["add_features"])
        for tool in (
            "close_part", "new_part", "save_part", "measure_part",
            "cad_guide", "advise", "import_part", "export_part",
        ):
            assert tool not in out, (tool, out)

    def test_agent_narrowed_role_card_gains_file_handling_floor(self):
        out = cmt.apply_mode_tools("agent", ["add_features"])
        for tool in ("fs_write", "fs_edit", "fs_list", "research_fetch_page"):
            assert tool in out, (tool, out)
        assert "run_shell" not in out

    def test_swarm_unions_extras(self):
        out = cmt.apply_mode_tools("swarm", ["add_features"])
        assert "add_features" in out
        assert "delegate" in out
        assert "corporate_activate" in out
        for tool in ("fs_write", "fs_edit", "fs_list", "research_fetch_page"):
            assert tool in out, (tool, out)
        assert "run_shell" not in out

    def test_plan_strips_mutators_keeps_shared_floor(self):
        out = cmt.apply_mode_tools(
            "plan",
            ["describe_part", "add_features", "run_fea_static", "delegate",
             "new_part", "research_web_search", "edit_feature"],
        )
        assert "describe_part" in out
        assert "run_fea_static" in out
        assert "delegate" in out
        assert "research_web_search" in out
        assert "add_features" not in out
        assert "edit_feature" not in out
        assert "new_part" in out      # shared floor restores it
        assert "enable_tools" in out  # meta floor

    def test_ask_intersects_role_strips_mutators(self):
        out = cmt.apply_mode_tools(
            "ask", ["describe_part", "add_features", "new_part", "measure_part"])
        assert "describe_part" in out
        assert "measure_part" in out
        assert "add_features" not in out
        assert "new_part" in out  # shared floor

    def test_ask_plan_do_not_gain_agent_default_floor(self):
        # T-file-floor: fs_write/fs_edit/fs_list/run_shell are the main-agent
        # (services/agent/swarm-only) floor; ask/plan keep only the read-only shared
        # floor and must never gain them, narrowing role card or not.
        ask_out = cmt.apply_mode_tools("ask", ["describe_part"])
        plan_out = cmt.apply_mode_tools("plan", ["describe_part"])
        for tool in ("fs_write", "fs_edit", "fs_list", "run_shell"):
            assert tool not in ask_out, (tool, ask_out)
            assert tool not in plan_out, (tool, plan_out)

    def test_plan_only_forces_plan_semantics(self):
        out = cmt.apply_mode_tools(
            "agent",
            ["add_features", "describe_part", "buildgraph_plan", "new_part",
             "run_fea_static", "delegate"],
            plan_only=True,
        )
        assert "buildgraph_plan" in out
        assert "describe_part" in out
        assert "run_fea_static" in out
        assert "delegate" in out
        assert "add_features" not in out
        assert "new_part" in out  # shared floor

    def test_empty_tool_denied(self):
        assert cmt.is_tool_allowed("", "agent") is False
        assert cmt.is_tool_allowed("   ", "swarm") is False

    def test_delegate_gate_verdicts(self):
        # Mirrors the handle_delegate mode gate in core/agent/run.cpp.
        assert cmt.is_tool_allowed("delegate", "") is True  # empty -> agent
        assert cmt.is_tool_allowed("delegate", "agent") is True
        assert cmt.is_tool_allowed("delegate", "plan") is True
        assert cmt.is_tool_allowed("delegate", "edit") is True
        assert cmt.is_tool_allowed("delegate", "swarm") is True
        assert cmt.is_tool_allowed("delegate", "ask") is False
