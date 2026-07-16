"""Offline tests for copilot mode → tools / roles / advisor defaults.

Covers work items C3/D/E/F (Python path):
  * allowlist per mode (ask read-only, plan no mutation, edit no new_part,
    agent/swarm full)
  * role_for_copilot_mode mapping
  * advisor_enabled default True (+ env override)
  * mode_options_from_mapping couples swarm→corporate_force, plan→plan_only
No network, no engine.
"""
from __future__ import annotations

import os

import pytest

from services.agent import config as agent_config
from services.agent import copilot_mode_tools as cmt
from services.agent import role_models as rm


# ---------------------------------------------------------------------------
# role_for_copilot_mode
# ---------------------------------------------------------------------------


class TestRoleForCopilotMode:
    def test_agent_default(self):
        assert rm.role_for_copilot_mode("agent") == "default"
        assert rm.role_for_copilot_mode(None) == "default"
        assert rm.role_for_copilot_mode("") == "default"
        assert rm.role_for_copilot_mode("unknown") == "default"

    def test_plan_planning(self):
        assert rm.role_for_copilot_mode("plan") == "planning"
        assert rm.role_for_copilot_mode("PLAN") == "planning"

    def test_swarm_default(self):
        assert rm.role_for_copilot_mode("swarm") == "default"

    def test_edit_task(self):
        assert rm.role_for_copilot_mode("edit") == "task"

    def test_ask_default(self):
        assert rm.role_for_copilot_mode("ask") == "default"

    def test_roles_are_known(self):
        for mode in ("agent", "plan", "swarm", "edit", "ask"):
            role = rm.role_for_copilot_mode(mode)
            assert role in rm.KNOWN_ROLES


# ---------------------------------------------------------------------------
# tools_for_mode / apply_mode_tools
# ---------------------------------------------------------------------------


class TestModeToolAllowlists:
    def test_ask_read_only_excludes_add_features(self):
        tools = cmt.tools_for_mode("ask")
        assert tools is not None
        assert "describe_part" in tools
        assert "measure_part" in tools
        assert "cad_guide" in tools
        assert "research_web_search" in tools
        assert "add_features" not in tools
        assert "new_part" not in tools
        assert "edit_feature" not in tools

    def test_plan_excludes_mutation(self):
        tools = cmt.tools_for_mode("plan")
        assert tools is not None
        assert "buildgraph_plan" in tools
        assert "dag_plan" in tools
        assert "ledger_get" in tools
        assert "describe_part" in tools
        assert "add_features" not in tools
        assert "new_part" not in tools
        assert "edit_feature" not in tools

    def test_plan_is_broad_non_mutator(self):
        """Plan = agent-broad minus commit mutators (not a locked-down read set)."""
        tools = cmt.tools_for_mode("plan")
        assert tools is not None
        for name in (
            "run_fea_static",
            "run_cfd_flow",
            "research_web_search",
            "delegate",
            "part_source",
            "ask_user",
            "inspect_part",
            "design_review",
            "fit_calc",
            "bom_rollup",
            "layout_plan",
            "preflight_ops",
        ):
            assert name in tools, name
        # Commit mutators must never appear in the mode set.
        for name in (
            "add_features",
            "new_part",
            "new_assembly",
            "edit_feature",
            "asm_add_component",
            "cam_generate",
            "ecad_place_symbol",
        ):
            assert name not in tools, name
            assert name not in cmt.PLAN_TOOLS

    def test_plan_role_card_does_not_intersect_narrow_set(self):
        # Role card may include mutators; plan strips them but keeps the rest
        # (no intersection with a locked-down PLAN_TOOLS).
        role = [
            "describe_part",
            "add_features",
            "run_fea_static",
            "delegate",
            "new_part",
            "research_web_search",
            "edit_feature",
        ]
        out = cmt.apply_mode_tools("plan", role)
        assert out is not None
        assert "describe_part" in out
        assert "run_fea_static" in out
        assert "delegate" in out
        assert "research_web_search" in out
        assert "add_features" not in out
        assert "new_part" not in out
        assert "edit_feature" not in out

    def test_edit_excludes_new_part(self):
        tools = cmt.tools_for_mode("edit")
        assert tools is not None
        assert "add_features" in tools
        assert "edit_feature" in tools
        assert "measure_part" in tools
        assert "describe_part" in tools
        assert "new_part" not in tools
        assert "new_assembly" not in tools
        assert "corporate_activate" not in tools

    def test_agent_unrestricted(self):
        assert cmt.tools_for_mode("agent") is None
        assert cmt.apply_mode_tools("agent", None) is None
        # With a role allowlist, agent keeps the role set intact.
        out = cmt.apply_mode_tools("agent", ["add_features", "new_part", "delegate"])
        assert out == ["add_features", "delegate", "new_part"]

    def test_swarm_unrestricted_or_unions_extras(self):
        assert cmt.tools_for_mode("swarm") is None
        assert cmt.apply_mode_tools("swarm", None) is None
        out = cmt.apply_mode_tools("swarm", ["add_features"])
        assert "add_features" in out
        assert "delegate" in out
        assert "corporate_activate" in out

    def test_apply_intersects_role_with_ask(self):
        role = ["describe_part", "add_features", "new_part", "measure_part"]
        out = cmt.apply_mode_tools("ask", role)
        assert out is not None
        assert "describe_part" in out
        assert "measure_part" in out
        assert "add_features" not in out
        assert "new_part" not in out

    def test_plan_only_forces_plan_tools(self):
        # Even under agent mode, plan_only uses plan semantics (strip mutators;
        # keep non-mutators from the role card — no narrow PLAN_TOOLS intersect).
        out = cmt.apply_mode_tools(
            "agent",
            ["add_features", "describe_part", "buildgraph_plan", "new_part",
             "run_fea_static", "delegate"],
            plan_only=True,
        )
        assert out is not None
        assert "buildgraph_plan" in out
        assert "describe_part" in out
        assert "run_fea_static" in out
        assert "delegate" in out
        assert "add_features" not in out
        assert "new_part" not in out

    def test_is_tool_allowed(self):
        assert cmt.is_tool_allowed("describe_part", "ask") is True
        assert cmt.is_tool_allowed("add_features", "ask") is False
        assert cmt.is_tool_allowed("add_features", "edit") is True
        assert cmt.is_tool_allowed("new_part", "edit") is False
        assert cmt.is_tool_allowed("new_part", "agent") is True
        assert cmt.is_tool_allowed("delegate", "swarm") is True

    def test_is_tool_allowed_plan_broad(self):
        assert cmt.is_tool_allowed("run_fea_static", "plan") is True
        assert cmt.is_tool_allowed("research_web_search", "plan") is True
        assert cmt.is_tool_allowed("delegate", "plan") is True
        assert cmt.is_tool_allowed("part_source", "plan") is True
        assert cmt.is_tool_allowed("ask_user", "plan") is True
        assert cmt.is_tool_allowed("inspect_part", "plan") is True
        assert cmt.is_tool_allowed("add_features", "plan") is False
        assert cmt.is_tool_allowed("new_part", "plan") is False
        assert cmt.is_tool_allowed("edit_feature", "plan") is False
        assert cmt.is_tool_allowed("new_assembly", "plan") is False
        assert cmt.is_tool_allowed("asm_mate", "plan") is False
        assert cmt.is_tool_allowed("cam_generate", "plan") is False
        assert cmt.is_tool_allowed("ecad_place_symbol", "plan") is False


# ---------------------------------------------------------------------------
# mode_options_from_mapping
# ---------------------------------------------------------------------------


class TestModeOptionsFromMapping:
    def test_defaults(self):
        o = cmt.mode_options_from_mapping(None)
        assert o["mode"] == "agent"
        assert o["plan_only"] is False
        assert o["corporate_force"] is False
        assert o["advisor"] is True

    def test_swarm_forces_corporate(self):
        o = cmt.mode_options_from_mapping({"mode": "swarm"})
        assert o["mode"] == "swarm"
        assert o["corporate_force"] is True

    def test_plan_sets_plan_only(self):
        o = cmt.mode_options_from_mapping({"mode": "plan"})
        assert o["mode"] == "plan"
        assert o["plan_only"] is True

    def test_explicit_plan_only_false_on_agent(self):
        o = cmt.mode_options_from_mapping({"mode": "agent", "plan_only": False})
        assert o["plan_only"] is False

    def test_scope_and_approved_plan(self):
        o = cmt.mode_options_from_mapping({
            "mode": "edit",
            "scope": {"doc_id": "d1", "feature_ids": ["f1"]},
            "approved_plan": "do the thing",
            "execute_as": "swarm",
            "advisor": False,
        })
        assert o["scope"]["doc_id"] == "d1"
        assert o["approved_plan"] == "do the thing"
        assert o["execute_as"] == "swarm"
        assert o["advisor"] is False


# ---------------------------------------------------------------------------
# advisor_enabled
# ---------------------------------------------------------------------------


class TestAdvisorEnabled:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("AI_CAD_ADVISOR", raising=False)
        # Empty agent block → default True
        assert agent_config.advisor_enabled({"agent": {}}) is True
        assert agent_config.advisor_enabled({}) is True

    def test_config_false(self, monkeypatch):
        monkeypatch.delenv("AI_CAD_ADVISOR", raising=False)
        assert agent_config.advisor_enabled({"agent": {"advisor": False}}) is False

    def test_config_true(self, monkeypatch):
        monkeypatch.delenv("AI_CAD_ADVISOR", raising=False)
        assert agent_config.advisor_enabled({"agent": {"advisor": True}}) is True

    def test_env_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv("AI_CAD_ADVISOR", "0")
        assert agent_config.advisor_enabled({"agent": {"advisor": True}}) is False

    def test_env_overrides_config_on(self, monkeypatch):
        monkeypatch.setenv("AI_CAD_ADVISOR", "1")
        assert agent_config.advisor_enabled({"agent": {"advisor": False}}) is True

    def test_default_constant(self):
        assert agent_config.DEFAULT_ADVISOR is True
        assert agent_config.ADVISOR_KEY == "advisor"


# ---------------------------------------------------------------------------
# MissionDispatch mode wiring (lightweight)
# ---------------------------------------------------------------------------


class TestMissionDispatchMode:
    def test_mode_swarm_sets_corporate_force(self):
        from services.agent.mission_dispatch import MissionDispatch

        md = MissionDispatch(run_id="r_mode_test", mode="swarm")
        assert md.mode == "swarm"
        assert md.corporate_force is True

    def test_mode_plan_sets_plan_only(self):
        from services.agent.mission_dispatch import MissionDispatch

        md = MissionDispatch(run_id="r_mode_plan", mode="plan")
        assert md.mode == "plan"
        assert md.plan_only is True

    def test_options_dict(self):
        from services.agent.mission_dispatch import MissionDispatch

        md = MissionDispatch(
            run_id="r_opts",
            options={"mode": "edit", "advisor": False, "scope": {"doc_id": "x"}},
        )
        assert md.mode == "edit"
        assert md.advisor is False
        assert md.options.get("scope", {}).get("doc_id") == "x"


# ---------------------------------------------------------------------------
# corporate force alias
# ---------------------------------------------------------------------------


class TestCorporateForceAlias:
    def test_should_activate_alias(self):
        from services.agent import corporate as corp

        assert corp.should_activate_corporate is corp.should_use_corporate

    def test_force_true_enables_without_config(self):
        from services.agent.corporate import should_use_corporate

        tasks = [
            {"role": "feature_modeler", "task_class": "geometry"},
            {"role": "cam_programmer", "task_class": "cam"},
            {"role": "fea_analyst", "task_class": "sim"},
        ]
        # force=True should not require corporate_mode config on
        assert should_use_corporate(tasks, cfg={"agent": {"corporate_mode": False}}, force=True) is True
        assert should_use_corporate(tasks, cfg={"agent": {"corporate_mode": False}}, force=False) is False
