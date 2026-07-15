"""Mega-11 LegProduction regressions (LazyVulture).

Axes: K/L/M support — ECAD/firmware/antenna legs must (1) resolve role cards
with domain tools + delegate, (2) be classified to tool-capable tiers on
BUILD (not one-shot ollama leaves), and (3) carry concrete entry-tool briefs.

Run: python3 -m pytest services/agent/test_mega11_legs.py -q
"""
from __future__ import annotations

from services.agent.roles import MAX_TOOLS_PER_ROLE, load_role
from services.agent.role_models import (
    domain_tags_from_context,
    is_leaf_task_class,
    is_multi_domain_plan_or_build,
    min_rank_for_difficulty,
)
from services.dag.mission_template import (
    _build_job_for,
    _resolve_build_role,
    build_mission_graph,
)


# ---------------------------------------------------------------------------
# 1. Role cards: domain tools + delegate
# ---------------------------------------------------------------------------


def test_ecad_engineer_role_has_domain_tools_and_delegate():
    card = load_role("ecad_engineer")
    tools = card.concrete_tools
    assert len(tools) <= MAX_TOOLS_PER_ROLE, (len(tools), tools)
    assert "delegate" in tools
    for must in (
        "ecad_new_schematic",
        "board_from_schematic",
        "ecad_run_erc",
        "ecad_run_drc",
        "ecad_simulate",
        "ecad_antenna_design",
        "ledger_get",
        "ledger_note",
    ):
        assert must in tools, (must, tools)
    prompt = card.system_prompt.lower()
    assert "ecad_new_schematic" in prompt
    assert "board_from_schematic" in prompt


def test_firmware_engineer_role_has_domain_tools_and_delegate():
    card = load_role("firmware_engineer")
    tools = card.concrete_tools
    assert len(tools) <= MAX_TOOLS_PER_ROLE, (len(tools), tools)
    assert "delegate" in tools
    for must in (
        "fw_new_project",
        "fw_build",
        "fw_sim_run",
        "fw_verify_board",
        "fw_status",
        "ledger_get",
        "ledger_note",
    ):
        assert must in tools, (must, tools)
    prompt = card.system_prompt.lower()
    assert "fw_new_project" in prompt
    assert "fw_build" in prompt


def test_producing_roles_not_plan_capability_split():
    """Producing legs KEEP domain mutators (unlike layout/orchestrator)."""
    for rid in ("ecad_engineer", "firmware_engineer"):
        tools = load_role(rid).concrete_tools
        assert "add_features" not in tools  # not geometry
        assert "ledger_note" in tools
        assert "delegate" in tools


# ---------------------------------------------------------------------------
# 2. Role resolution + job briefs
# ---------------------------------------------------------------------------


def test_resolve_build_role_ecad_by_name():
    d = {
        "id": "DL3",
        "name": "flight_computer_ecad",
        "kind": "analysis",
        "description": "schematic + board + antenna",
    }
    assert _resolve_build_role(d) == "ecad_engineer"


def test_resolve_build_role_firmware_by_name():
    d = {
        "id": "DL4",
        "name": "firmware_sensor_loop",
        "kind": "analysis",
        "description": "fw_build + renode sensor loop",
    }
    assert _resolve_build_role(d) == "firmware_engineer"


def test_resolve_build_role_generic_analysis_stays_sim():
    d = {
        "id": "DLX",
        "name": "modal_study",
        "kind": "analysis",
        "description": "FEA modal frequencies",
    }
    assert _resolve_build_role(d) == "sim_engineer"


def test_build_job_names_entry_tools():
    ecad = {
        "id": "DL3",
        "name": "flight_computer_ecad",
        "kind": "analysis",
    }
    job = _build_job_for(ecad, "ecad_engineer")
    assert "ecad_new_schematic" in job
    assert "board_from_schematic" in job
    assert "ecad_run_drc" in job

    fw = {
        "id": "DL4",
        "name": "firmware_sensor_loop",
        "kind": "analysis",
    }
    job_fw = _build_job_for(fw, "firmware_engineer")
    assert "fw_new_project" in job_fw
    assert "fw_build" in job_fw


# ---------------------------------------------------------------------------
# 3. Mission graph: analysis is buildable + tool-capable tier
# ---------------------------------------------------------------------------


def _fc_manifest() -> dict:
    return {
        "request_verbatim": (
            "Design a rocket plane with flight computer ECAD and firmware."
        ),
        "ambiguities": [],
        "deliverables": [
            {
                "id": "DL1",
                "name": "rocket_plane_airframe",
                "kind": "part",
                "description": "boost-glide airframe",
                "acceptance_criteria": [],
            },
            {
                "id": "DL3",
                "name": "flight_computer_ecad",
                "kind": "analysis",
                "description": (
                    "Flight-computer E2E: part sourcing, schematic, SPICE OP, "
                    "PCB place/route, antenna/RF, DRC/DFM."
                ),
                "acceptance_criteria": [
                    {
                        "id": "AC4",
                        "text": "schematic + board artifacts",
                        "check": {
                            "tool": "manual",
                            "pass_rule": "legs parts/schematic/board not FAIL",
                        },
                    },
                ],
            },
            {
                "id": "DL4",
                "name": "firmware_sensor_loop",
                "kind": "analysis",
                "description": "fw_build + renode sensor-in-the-loop",
                "acceptance_criteria": [
                    {
                        "id": "AC7",
                        "text": "fw_build succeeds",
                        "check": {
                            "tool": "manual",
                            "pass_rule": "fw_build returncode==0",
                        },
                    },
                ],
            },
            {
                "id": "DL5",
                "name": "mission_report_card",
                "kind": "report",
                "description": "report card",
                "acceptance_criteria": [],
            },
        ],
    }


def test_analysis_deliverables_get_build_nodes():
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}
    assert "n_build_DL3" in by_id
    assert "n_build_DL4" in by_id
    assert "n_verify_DL3" in by_id
    assert "n_verify_DL4" in by_id
    # report stays non-buildable
    assert "n_build_DL5" not in by_id


def test_ecad_fw_build_roles_and_briefs():
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}

    b3 = by_id["n_build_DL3"]
    assert b3["inputs"]["role"] == "ecad_engineer"
    assert b3["inputs"]["task_class"] == "analysis"
    req3 = b3["inputs"]["request"]
    assert "ecad_new_schematic" in req3
    assert "board_from_schematic" in req3
    assert b3["inputs"].get("domain_tags") == ["ecad", "analysis"]

    b4 = by_id["n_build_DL4"]
    assert b4["inputs"]["role"] == "firmware_engineer"
    assert b4["inputs"]["task_class"] == "analysis"
    req4 = b4["inputs"]["request"]
    assert "fw_new_project" in req4
    assert "fw_build" in req4
    assert b4["inputs"].get("domain_tags") == ["firmware", "analysis"]


def test_producing_legs_tool_capable_not_oneshot_leaf():
    """Dispatch classification: analysis BUILD is NOT leaf/simple → no one-shot."""
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}
    for nid in ("n_build_DL3", "n_build_DL4"):
        tc = by_id[nid]["inputs"]["task_class"]
        assert not is_leaf_task_class(tc), (nid, tc)
        rank = min_rank_for_difficulty(task_class=tc, node_depth=0)
        # analysis base min_rank is 4 → tool-capable tier, not ollama rank-1
        assert rank >= 3, (nid, tc, rank)


def test_mechanical_build_unchanged():
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}
    b1 = by_id["n_build_DL1"]
    assert b1["inputs"]["role"] == "feature_modeler"
    assert b1["inputs"]["task_class"] == "multi_feature_part"
    assert "verify_battery" in b1["inputs"]["request"]


def test_producing_legs_skip_multi_domain_delegate_gate():
    """ecad/firmware engineers execute flat — not forced into delegate gate."""
    assert not is_multi_domain_plan_or_build(
        role_id="ecad_engineer",
        node_type="design",
        domain_tags=["ecad", "analysis"],
        task_class="analysis",
    )
    assert not is_multi_domain_plan_or_build(
        role_id="firmware_engineer",
        node_type="design",
        domain_tags=["firmware", "analysis"],
        task_class="analysis",
    )


def test_domain_tags_from_producing_roles():
    tags = domain_tags_from_context(
        {"role_id": "ecad_engineer", "task_class": "analysis"},
        {},
    )
    assert "ecad" in tags
    assert "analysis" in tags
    tags_fw = domain_tags_from_context(
        {"role_id": "firmware_engineer", "task_class": "analysis"},
        {},
    )
    assert "firmware" in tags_fw


def test_mission_dispatch_classifies_ecad_fw_to_tool_tier():
    """End-to-end: graph node inputs → min_rank tool-capable for ECAD/fw BUILD."""
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}
    for nid, expect_role in (
        ("n_build_DL3", "ecad_engineer"),
        ("n_build_DL4", "firmware_engineer"),
    ):
        node = by_id[nid]
        inputs = node["inputs"]
        assert inputs["role"] == expect_role
        tc = inputs["task_class"]
        mr = min_rank_for_difficulty(task_class=tc, node_depth=0)
        assert mr >= 3, (nid, mr)
        # tools_slot would load the producing card
        card = load_role(expect_role)
        assert any(
            t.startswith("ecad_") or t.startswith("fw_") or t.startswith("board_")
            for t in card.concrete_tools
        ), card.concrete_tools
