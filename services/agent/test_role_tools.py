"""agent.role_tools — per-harness-role default tool families."""
from __future__ import annotations

import services.agent.config as cfg


def test_get_role_tools_defaults():
    assert "cad" in cfg.get_role_tools("default", {})
    assert "office" in cfg.get_role_tools("default", {})
    assert "advisor" in cfg.get_role_tools("advisor", {})
    assert cfg.get_role_tools("redaction", {}) == []


def test_get_role_tools_respects_config():
    tools = cfg.get_role_tools(
        "task",
        {"agent": {"role_tools": {"task": ["cad", "office", "cad"]}}},
    )
    assert tools == ["cad", "office"]


def test_get_role_tools_partial_config_keeps_other_defaults():
    # Customizing only "task" must not rewrite "advisor" to task's list.
    tools = cfg.get_role_tools(
        "advisor",
        {"agent": {"role_tools": {"task": ["cad"]}}},
    )
    assert tools == list(cfg.DEFAULT_ROLE_TOOLS["advisor"])


def test_get_role_tools_unknown_falls_to_default_key():
    tools = cfg.get_role_tools("not_a_real_role", {})
    assert tools == list(cfg.DEFAULT_ROLE_TOOLS["default"])



def test_harness_role_for_yaml():
    assert cfg.harness_role_for_yaml("general_agent") == "default"
    assert cfg.harness_role_for_yaml("feature_modeler") == "task"
    assert cfg.harness_role_for_yaml("orchestrator") == "planning"
    assert cfg.harness_role_for_yaml("cam") == "cam"


def test_get_tool_families_unions_role_tools():
    fams = cfg.get_tool_families(
        {
            "agent": {
                "role_tools": {
                    "advisor": ["advisor"],
                    "task": ["cad"],
                    "default": [],
                    "planning": [],
                    "vision": [],
                    "plm": [],
                    "redaction": [],
                    "sketch2d": [],
                    "model3d": [],
                    "memory": [],
                    "cam": [],
                    "sim": [],
                    "ecad": [],
                    "drawing": [],
                    "reveng": [],
                }
            }
        }
    )
    assert fams["advisor"] is True
    assert fams["cad"] is True
    assert fams["em"] is False  # not in any role assignment


def test_tool_families_kill_switch_overrides_union():
    fams = cfg.get_tool_families(
        {
            "agent": {
                "role_tools": {"default": ["cad", "office"]},
                "tool_families": {"cad": False},
            }
        }
    )
    assert fams["cad"] is False
    assert fams["office"] is True


def test_direct_agent_full_tools_default_on():
    assert cfg.direct_agent_full_tools({}) is True


def test_auto_mission_routing_default_off():
    assert cfg.auto_mission_routing({}) is False


def test_plan_preference_default_off():
    assert cfg.get_plan_preference({}) == "off"
