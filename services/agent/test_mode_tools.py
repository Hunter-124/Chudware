"""Offline tests for copilot mode → tools / roles / advisor defaults.

Covers work items C3/D/E/F (Python path) + shared-defaults / tool-categories:
  * SHARED_DEFAULT_TOOLS ⊆ every restricted mode allowlist (ask/plan/edit)
  * services/agent/swarm unrestricted (None)
  * enable_tools / list_tool_categories always allowed
  * resolve_categories expands packs with a cap
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
from services.agent import tool_categories as tcat


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

    def test_mission_role_mapping_is_shared_across_child_lanes(self):
        assert rm.harness_role_for_mission_role("reviewer") == "vision"
        assert rm.harness_role_for_mission_role("advisor") == "advisor"
        assert rm.harness_role_for_mission_role("overseer") == "planning"
        assert rm.harness_role_for_mission_role("feature_modeler") == "task"


# ---------------------------------------------------------------------------
# tools_for_mode / apply_mode_tools
# ---------------------------------------------------------------------------


class TestModeToolAllowlists:
    def test_shared_defaults_subset_of_restricted_modes(self):
        """SHARED_DEFAULT_TOOLS is a floor for every restricted mode allowlist."""
        for mode in ("ask", "plan", "edit"):
            out = cmt.apply_mode_tools(mode, None)
            assert out is not None, mode
            for name in cmt.SHARED_DEFAULT_TOOLS:
                assert name in out, f"{name} missing from {mode}"

    def test_ask_keeps_reads_and_shared_floor(self):
        tools = cmt.tools_for_mode("ask")
        assert tools is not None
        assert "describe_part" in tools
        assert "measure_part" in tools
        assert "cad_guide" in tools
        assert "research_web_search" in tools
        # Mode card still excludes feature mutators; shared floor re-adds doc lifecycle.
        assert "add_features" not in tools
        assert "edit_feature" not in tools
        out = cmt.apply_mode_tools("ask", None)
        assert out is not None
        assert "new_part" in out
        assert "save_part" in out
        assert "close_part" in out
        assert "import_part" in out
        assert "export_part" in out
        assert "advise" in out
        assert "ask_user" in out
        assert "enable_tools" in out
        assert "list_tool_categories" in out
        assert "add_features" not in out
        assert "edit_feature" not in out

    def test_plan_broad_with_shared_doc_lifecycle(self):
        """Plan = agent-broad minus geometry mutators + SHARED_DEFAULT_TOOLS."""
        tools = cmt.tools_for_mode("plan")
        assert tools is not None
        assert "buildgraph_plan" in tools
        assert "dag_plan" in tools
        assert "ledger_get" in tools
        assert "describe_part" in tools
        # Mode card excludes feature mutators.
        assert "add_features" not in tools
        assert "edit_feature" not in tools
        out = cmt.apply_mode_tools("plan", None)
        assert out is not None
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
            # Shared floor (plan can build simple parts)
            "new_part",
            "save_part",
            "close_part",
            "import_part",
            "export_part",
            "advise",
            "enable_tools",
        ):
            assert name in out, name
        # Geometry commit mutators still excluded (not in shared floor).
        for name in (
            "add_features",
            "new_assembly",
            "edit_feature",
            "asm_add_component",
            "cam_generate",
            "ecad_place_symbol",
        ):
            assert name not in out, name

    def test_plan_role_card_strips_mutators_keeps_shared(self):
        # Role card may include mutators; plan strips them but shared floor
        # re-adds new_part / close / import etc.
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
        assert "edit_feature" not in out
        # Shared floor restores new_part even though stripped as mutator.
        assert "new_part" in out
        assert "enable_tools" in out

    def test_edit_has_features_and_shared_doc_lifecycle(self):
        tools = cmt.tools_for_mode("edit")
        assert tools is not None
        assert "add_features" in tools
        assert "edit_feature" in tools
        assert "measure_part" in tools
        assert "describe_part" in tools
        # Mode card excludes new_part; shared floor re-adds it.
        assert "new_part" not in tools
        assert "corporate_activate" not in tools
        out = cmt.apply_mode_tools("edit", None)
        assert out is not None
        assert "add_features" in out
        assert "new_part" in out
        assert "close_part" in out
        assert "import_part" in out
        assert "export_part" in out
        assert "save_part" in out
        assert "advise" in out
        assert "enable_tools" in out
        assert "corporate_activate" not in out

    def test_agent_unrestricted(self):
        assert cmt.tools_for_mode("agent") is None
        assert cmt.apply_mode_tools("agent", None) is None
        # T-lean-floor: with a role allowlist, agent unions in the main-agent
        # file-handling floor (AGENT_DEFAULT_TOOLS only — NOT
        # SHARED_DEFAULT_TOOLS, which is for ask/plan/edit/swarm) on top of
        # the role card's own tools — a narrowing role card must not lose
        # fs_write/fs_edit/fs_list/research_fetch_page, while shell and CAD
        # document-lifecycle tools stay behind enable_tools instead.
        out = cmt.apply_mode_tools("agent", ["add_features", "new_part", "delegate"])
        assert out == sorted(
            {"add_features", "new_part", "delegate"} | cmt.AGENT_DEFAULT_TOOLS
        )
        for tool in ("fs_write", "fs_edit", "fs_list", "research_fetch_page"):
            assert tool in out, (tool, out)
        assert "run_shell" not in out
        for tool in ("close_part", "save_part", "measure_part", "cad_guide", "advise",
                     "import_part", "export_part"):
            assert tool not in out, (tool, out)


    def test_agent_explicit_empty_is_restricted_and_lane_specific(self):
        main = cmt.apply_mode_tools("agent", [])
        child = cmt.apply_mode_tools("agent", [], subagent=True)

        assert main == sorted(cmt.AGENT_DEFAULT_TOOLS)
        assert child == sorted(cmt.SUBAGENT_DEFAULT_TOOLS)
        assert "run_shell" not in main
        assert "run_shell" not in child
        for name in ("fs_read", "fs_glob", "fs_grep"):
            assert name in child
    def test_swarm_unrestricted_or_unions_extras(self):
        assert cmt.tools_for_mode("swarm") is None
        assert cmt.apply_mode_tools("swarm", None) is None
        out = cmt.apply_mode_tools("swarm", ["add_features"])
        assert "add_features" in out
        assert "delegate" in out
        assert "corporate_activate" in out
        for tool in ("fs_write", "fs_edit", "fs_list", "research_fetch_page"):
            assert tool in out, (tool, out)
        assert "run_shell" not in out

    def test_apply_intersects_role_with_ask_plus_shared(self):
        role = ["describe_part", "add_features", "new_part", "measure_part"]
        out = cmt.apply_mode_tools("ask", role)
        assert out is not None
        assert "describe_part" in out
        assert "measure_part" in out
        assert "add_features" not in out
        # Shared floor restores new_part (doc lifecycle).
        assert "new_part" in out
        assert "enable_tools" in out

    def test_plan_only_forces_plan_tools(self):
        # Even under agent mode, plan_only uses plan semantics (strip mutators;
        # keep non-mutators from the role card) + shared floor.
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
        assert "new_part" in out  # shared floor

    def test_is_tool_allowed(self):
        assert cmt.is_tool_allowed("describe_part", "ask") is True
        assert cmt.is_tool_allowed("add_features", "ask") is False
        assert cmt.is_tool_allowed("add_features", "edit") is True
        # Shared floor: new_part allowed in edit/ask/plan.
        assert cmt.is_tool_allowed("new_part", "edit") is True
        assert cmt.is_tool_allowed("new_part", "ask") is True
        assert cmt.is_tool_allowed("new_part", "agent") is True
        assert cmt.is_tool_allowed("delegate", "swarm") is True
        assert cmt.is_tool_allowed("enable_tools", "ask") is True
        assert cmt.is_tool_allowed("list_tool_categories", "plan") is True

    def test_is_tool_allowed_plan_broad(self):
        assert cmt.is_tool_allowed("run_fea_static", "plan") is True
        assert cmt.is_tool_allowed("research_web_search", "plan") is True
        assert cmt.is_tool_allowed("delegate", "plan") is True
        assert cmt.is_tool_allowed("part_source", "plan") is True
        assert cmt.is_tool_allowed("ask_user", "plan") is True
        assert cmt.is_tool_allowed("inspect_part", "plan") is True
        assert cmt.is_tool_allowed("new_part", "plan") is True  # shared floor
        assert cmt.is_tool_allowed("add_features", "plan") is False
        assert cmt.is_tool_allowed("edit_feature", "plan") is False
        assert cmt.is_tool_allowed("new_assembly", "plan") is False
        assert cmt.is_tool_allowed("asm_mate", "plan") is False
        assert cmt.is_tool_allowed("cam_generate", "plan") is False
        assert cmt.is_tool_allowed("ecad_place_symbol", "plan") is False

    def test_print_media_inventory_is_read_only_and_publish_is_refused_read_only(self):
        # The main chat advertises both; a user-picked Ask/Plan must keep the drive
        # inventory but never publish (it writes and retires files on printer media).
        core = list(tcat.MAIN_CHAT_CORE_TOOLS)
        assert {"print_media_inventory", "publish_print_media"} <= set(core)
        for mode in ("ask", "plan"):
            allowed = cmt.apply_mode_tools(mode, core)
            assert "print_media_inventory" in allowed, mode
            assert "publish_print_media" not in allowed, mode
            assert cmt.is_tool_allowed("publish_print_media", mode) is False
        plan_only = cmt.apply_mode_tools("agent", core, plan_only=True)
        assert "publish_print_media" not in plan_only
        assert cmt.apply_mode_tools("agent", None) is None  # unrestricted keeps it
        assert "publish_print_media" in cmt.apply_mode_tools("agent", core)


class TestToolCategories:
    def test_category_packs_cover_required(self):
        required = {
            "modeling", "sketch", "measure", "inspect", "research", "sim",
            "cam", "ecad", "assembly", "sourcing", "drawing", "advisor",
            "delegate", "sheetmetal",
        }
        assert required <= set(tcat.CATEGORY_PACKS.keys())

    def test_resolve_categories_expands_and_caps(self):
        got = tcat.resolve_categories(["research", "measure"])
        assert "research_web_search" in got
        assert "measure_part" in got
        assert "wall_thickness" in got
        assert "cad_guide" in got
        # Cap is respected even when requesting everything.
        all_names = list(tcat.CATEGORY_PACKS.keys())
        capped = tcat.resolve_categories(all_names, cap=5)
        assert len(capped) <= 5

    def test_category_catalog_shape(self):
        cat = tcat.category_catalog()
        assert cat
        assert all(isinstance(n, str) and isinstance(d, str) for n, d in cat)
        names = {n for n, _ in cat}
        assert "modeling" in names
        assert "research" in names

    def test_meta_tools_always_allowed_in_registry(self):
        from services.mcp import tool_registry as reg
        reg.ensure_meta_tools()
        assert "enable_tools" in reg.catalog()
        assert "list_tool_categories" in reg.catalog()
        assert reg.catalog()["enable_tools"]["family"] == "workspace"
        assert reg.catalog()["enable_tools"]["risk"] == "read"
        # Even under a tight allow-list policy, meta tools are permitted.
        reg.set_policy(allow=["describe_part"], deny_risk=None)
        try:
            assert reg.is_allowed("enable_tools") is True
            assert reg.is_allowed("list_tool_categories") is True
            assert reg.is_allowed("describe_part") is True
            assert reg.is_allowed("add_features") is False
        finally:
            reg.reset_policy()


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
        monkeypatch.delenv("CHUDWARE_ADVISOR", raising=False)
        # Empty agent block → default True
        assert agent_config.advisor_enabled({"agent": {}}) is True
        assert agent_config.advisor_enabled({}) is True

    def test_config_false(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_ADVISOR", raising=False)
        assert agent_config.advisor_enabled({"agent": {"advisor": False}}) is False

    def test_config_true(self, monkeypatch):
        monkeypatch.delenv("CHUDWARE_ADVISOR", raising=False)
        assert agent_config.advisor_enabled({"agent": {"advisor": True}}) is True

    def test_env_overrides_config_off(self, monkeypatch):
        monkeypatch.setenv("CHUDWARE_ADVISOR", "0")
        assert agent_config.advisor_enabled({"agent": {"advisor": True}}) is False

    def test_env_overrides_config_on(self, monkeypatch):
        monkeypatch.setenv("CHUDWARE_ADVISOR", "1")
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
