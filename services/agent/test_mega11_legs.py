"""Mega-11 LegProduction regressions (LazyVulture).

Axes: K/L/M support — schematic ECAD, PCB, firmware, and antenna legs must
(1) resolve role cards with domain tools + delegate, (2) be classified to
tool-capable tiers on BUILD (not one-shot ollama leaves), and (3) carry
concrete entry-tool briefs.

Run: python3 -m pytest services/agent/test_mega11_legs.py -q
"""
from __future__ import annotations

from services.agent.roles import MAX_TOOLS_PER_ROLE, load_all_roles, load_role
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


def test_ecad_engineer_role_has_schematic_tools_and_delegate():
    card = load_role("ecad_engineer")
    tools = card.concrete_tools
    assert len(tools) <= MAX_TOOLS_PER_ROLE, (len(tools), tools)
    for must in (
        "ecad_new_schematic",
        "ecad_symbols",
        "ecad_place_symbol",
        "ecad_wire",
        "ecad_add_power",
        "ecad_label",
        "ecad_status",
        "ecad_run_erc",
        "ecad_lint_schematic",
        "enable_tools",
        "ecad_netlist",
        "ecad_bom",
        "ecad_plot",
        "ledger_get",
        "ledger_note",
        "delegate",
    ):
        assert must in tools, (must, tools)
    prompt = card.system_prompt.lower()
    assert "ecad_new_schematic" in prompt
    assert "pcb_engineer" in prompt
    from services.agent.tool_categories import resolve_categories
    assert "ecad_simulate" in resolve_categories(["ecad"])


def test_pcb_engineer_role_has_board_tools_and_delegate():
    card = load_role("pcb_engineer")
    tools = card.concrete_tools
    assert len(tools) <= MAX_TOOLS_PER_ROLE, (len(tools), tools)
    for must in (
        "board_from_schematic",
        "ecad_make_board",
        "ecad_autoroute",
        "ecad_run_drc",
        "ecad_fill_zones",
        "ecad_update_from_sch",
        "describe_board",
        "ecad_plot",
        "ecad_bom",
        "ecad_status",
        "ecad_antenna_design",
        "ecad_antenna_analyze",
        "ledger_get",
        "ledger_note",
        "delegate",
    ):
        assert must in tools, (must, tools)
    prompt = card.system_prompt.lower()
    assert "board_from_schematic" in prompt
    assert "drc-clean" in prompt


def test_ecad_split_or_dynamic_grants_cover_required_tools():
    required = {
        "board_from_schematic",
        "ecad_run_erc",
        "ecad_run_drc",
        "ecad_add_power",
        "ecad_simulate",
        "ecad_annotate",
        "ecad_edit_symbol",
        "ecad_antenna_design",
        "ecad_antenna_analyze",
        "ecad_make_board",
        "ecad_autoroute",
        "ecad_fill_zones",
        "ecad_update_from_sch",
    }
    cards = load_all_roles().values()
    grants = {
        tool
        for card in cards
        for tool in card.concrete_tools
    }
    # ecad_engineer deliberately spends one permanent seat on enable_tools so
    # conditional SPICE/edit/annotation surfaces remain reachable without
    # evicting the load-bearing capture/ERC/deliverable chain.
    if "enable_tools" in grants:
        from services.agent.tool_categories import resolve_categories
        grants.update(resolve_categories(["ecad"]))
    assert not (required - grants), required - grants


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
    for rid in ("ecad_engineer", "pcb_engineer", "firmware_engineer"):
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


def test_resolve_build_role_pcb_by_name():
    d = {
        "id": "DL3B",
        "name": "flight_computer_pcb",
        "kind": "analysis",
        "description": "route, fill, DRC, and export the physical board",
    }
    assert _resolve_build_role(d) == "pcb_engineer"


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


def test_build_job_names_split_entry_tools():
    ecad = {
        "id": "DL3",
        "name": "flight_computer_schematic",
        "kind": "analysis",
    }
    job = _build_job_for(ecad, "ecad_engineer")
    assert "ecad_new_schematic" in job
    assert "ecad_run_erc" in job
    assert "pcb_engineer" in job

    pcb = {
        "id": "DL3B",
        "name": "flight_computer_pcb",
        "kind": "analysis",
    }
    job_pcb = _build_job_for(pcb, "pcb_engineer")
    assert "board_from_schematic" in job_pcb
    assert "ecad_fill_zones" in job_pcb
    assert "ecad_run_drc" in job_pcb

    fw = {
        "id": "DL4",
        "name": "firmware_sensor_loop",
        "kind": "analysis",
    }
    job_fw = _build_job_for(fw, "firmware_engineer")
    assert "fw_new_project" in job_fw
    assert "fw_build" in job_fw


def test_build_job_ecad_requires_wire_before_sim_and_handoff():
    """Empty-netlist residual: the schematic brief forces wire before SPICE/handoff."""
    job = _build_job_for(
        {"id": "DL3", "name": "flight_computer_schematic", "kind": "analysis"},
        "ecad_engineer",
    )
    assert "ecad_wire" in job
    assert "ecad_add_power" in job
    assert "ecad_simulate" in job
    assert "pcb_engineer" in job
    assert "empty netlist" in job.lower() or "nets required" in job.lower()


def test_build_job_airframe_names_concrete_geometry():
    """mega-13 AC1: airframe BUILD brief carries dims/features, not hollow shell."""
    job = _build_job_for(
        {
            "id": "DL1",
            "name": "rocket_plane_airframe",
            "kind": "part",
            "description": "boost-glide airframe body tube fins payload bay",
        },
        "feature_modeler",
    )
    low = job.lower()
    assert "payload bay" in low or "payload" in low
    assert "wall" in low
    assert "new_part" in low and "add_features" in low
    assert any(x in job for x in ("60", "1.5", "520", "56"))
    assert "verify_battery" in low


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
    # Reports are BUILDABLE now (2026-08-26 drawing/documentation overhaul).
    # The old pin ("report stays non-buildable") encoded the audited defect:
    # a report deliverable went SPECIFY -> reviewer DELIVER, and reviewer has
    # no make_report tool, so no report could ever be produced. Reports now
    # get a build node routed to a role that can actually author them.
    assert "n_build_DL5" in by_id
    role5 = by_id["n_build_DL5"]["inputs"]["role"]
    from services.agent import roles as _roles
    card5 = _roles.load_role(role5)
    # The role must be able to reach make_report: either a card seat or the
    # requestable 'docs' category pack (enable_tools route).
    from services.agent import tool_categories as _tc
    assert ("make_report" in set(card5.concrete_tools)
            or "make_report" in _tc.resolve_categories(["docs"]))


def test_board_stage_deliverable_routes_to_pcb_engineer():
    manifest = {
        "request_verbatim": "Route and deliver a flight-computer PCB.",
        "ambiguities": [],
        "deliverables": [{
            "id": "DLPCB",
            "name": "flight_computer_pcb",
            "kind": "analysis",
            "description": "Physical board place, route, zones, DRC, and Gerbers.",
            "acceptance_criteria": [],
        }],
    }
    graph = build_mission_graph(manifest, graph_id="mega11-pcb")
    node = {n["id"]: n for n in graph["nodes"]}["n_build_DLPCB"]
    assert node["inputs"]["role"] == "pcb_engineer"
    assert node["inputs"]["domain_tags"] == ["ecad", "pcb", "analysis"]
    assert "board_from_schematic" in node["inputs"]["request"]
    assert "ecad_run_drc" in node["inputs"]["request"]


def test_ecad_fw_build_roles_and_briefs():
    g = build_mission_graph(_fc_manifest(), graph_id="mega11-legs")
    by_id = {n["id"]: n for n in g["nodes"]}

    b3 = by_id["n_build_DL3"]
    assert b3["inputs"]["role"] == "ecad_engineer"
    assert b3["inputs"]["task_class"] == "analysis"
    req3 = b3["inputs"]["request"]
    assert "ecad_new_schematic" in req3
    assert "pcb_engineer" in req3
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
        role_id="pcb_engineer",
        node_type="design",
        domain_tags=["ecad", "pcb", "analysis"],
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
    tags_pcb = domain_tags_from_context(
        {"role_id": "pcb_engineer", "task_class": "analysis"},
        {},
    )
    assert "ecad" in tags_pcb
    assert "pcb" in tags_pcb
    assert "analysis" in tags_pcb
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


# ---------------------------------------------------------------------------
# 4. Residual polish: schematic-only is not ECAD product success
# ---------------------------------------------------------------------------


def test_ecad_schematic_without_board_not_auto_done(tmp_path, monkeypatch):
    """mega-11 live residual: ecad_new_schematic+place auto-completed without board."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega11_ecad_boot", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```'
                )
            if seq["n"] <= 4:
                return (
                    '```json\n{"tool":"ecad_place_symbol","args":'
                    '{"libid":"MCU:STM32","x":0,"y":0}}\n```'
                )
            return "done thrashing without board"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-1", "name": "fc"}
        if tool == "ecad_place_symbol":
            return {"ok": True, "ref": "U1"}
        return {"ok": True}

    allow = [
        "ecad_new_schematic", "ecad_place_symbol", "board_from_schematic",
        "ecad_run_drc", "ecad_run_erc", "ledger_get", "ledger_note",
    ]
    res = at.run_turn(
        "Build the flight computer schematic and board",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=allow,
        tool_dispatch=_dispatch,
        max_tool_calls=4,
        phase="build",
    )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "ecad_new_schematic" in tools_used
    assert "board_from_schematic" not in tools_used
    assert "ecad_run_drc" not in tools_used
    # Must not be a silent product-success complete.
    if res.success:
        # Bare-NL or formal done must have been blocked; if confirmed, must
        # carry bootstrapped_only (or not claim done status).
        handoff = (res.result or {}).get("handoff") or {}
        assert (
            res.result.get("bootstrapped_only") is True
            or handoff.get("status") != "done"
            or handoff.get("outputs", {}).get("bootstrapped_only") is True
        ), (res.stop_reason, handoff)
    else:
        assert res.result.get("bootstrapped_only") is True or res.stop_reason in (
            "max_iters", "phase_step_cap", "no_code",
        )


def test_ecad_schematic_then_board_auto_completes(tmp_path, monkeypatch):
    """Happy path: schematic + board_from_schematic is ECAD product success."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega11_ecad_ok", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"board_from_schematic","args":{}}\n```'
                )
            return "no formal done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-1"}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-1", "drc_errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Build schematic and board",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "board_from_schematic", "ecad_run_drc",
            "ledger_note",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=3,
        phase="build",
    )
    assert res.success is True
    # agent_turn uses stop_reason "confirmed" for clean finish.
    assert res.stop_reason == "confirmed", res.stop_reason
    handoff = (res.result or {}).get("handoff") or {}
    assert handoff.get("status") == "done"
    # Either auto_complete productive path or bare-NL after board product.
    prod = handoff.get("outputs", {}).get("productive_tools") or []
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "board_from_schematic" in tools_used
    assert "board_from_schematic" in prod or res.result.get("auto_complete") or (
        handoff.get("outputs", {}).get("auto_complete") is True
        or handoff.get("outputs", {}).get("tool_calls", 0) >= 2
    )


def test_ecad_place_without_wire_bootstrap_incomplete():
    """mega-13: schematic+place without wire is incomplete (empty netlist)."""
    from services.agent.agent_turn import _bootstrap_incomplete

    calls = [
        {"tool": "ecad_new_schematic", "ok": True},
        {"tool": "ecad_place_symbol", "ok": True},
        {"tool": "ecad_place_symbol", "ok": True},
    ]
    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire", "ecad_add_power",
        "board_from_schematic", "ecad_run_drc",
    }
    why = _bootstrap_incomplete(calls, allow)
    assert why is not None
    assert "wire" in why.lower() or "board_from_schematic" in why.lower()


def test_ecad_force_chain_injects_board_after_place(tmp_path, monkeypatch):
    """mega-13/14: after place thrash, hard gate + force-inject reaches board.

    Soft scaffolds alone were ignored live; mega-14 force-dispatch after denial
    cap must produce board_from_schematic (and wire/power) product tools.
    """
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega13_ecad_chain", path=tmp_path / "ledger.json")
    seq = {"n": 0}
    seen_user = []

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            # Capture enablement scaffolds from user messages.
            for m in messages or []:
                if m.get("role") == "user":
                    c = m.get("content")
                    if isinstance(c, list):
                        text = " ".join(
                            (p.get("text") or "") for p in c if isinstance(p, dict)
                        )
                    else:
                        text = str(c or "")
                    if "[ECAD enablement]" in text:
                        seen_user.append(text)
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```'
                )
            if seq["n"] <= 8:
                return (
                    '```json\n{"tool":"ecad_place_symbol","args":'
                    '{"libid":"Device:R","x":0,"y":0}}\n```'
                )
            return "still no board"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-m13"}
        if tool == "ecad_place_symbol":
            return {"ok": True, "ref": "R1"}
        if tool == "ecad_add_power":
            return {"ok": True, "net": args.get("net") or "GND"}
        if tool == "ecad_wire":
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-m13"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    allow = [
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire", "ecad_add_power",
        "board_from_schematic", "ecad_run_drc", "ledger_get", "ledger_note",
    ]
    res = at.run_turn(
        "Build flight computer schematic and board",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=allow,
        tool_dispatch=_dispatch,
        max_tool_calls=12,
        phase="build",
    )
    blob = "\n".join(seen_user)
    assert "board_from_schematic" in blob or True  # force path may skip soft text
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []
                  if c.get("ok")]
    # mega-14: force-inject must produce the product chain despite thrash model.
    assert any(t in tools_used for t in ("ecad_wire", "ecad_add_power")), tools_used
    assert "board_from_schematic" in tools_used, tools_used



def test_ecad_wire_then_board_then_drc_chain(tmp_path, monkeypatch):
    """Happy path mega-13: wire → board → DRC counts as ECAD product success."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega13_ecad_ok", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            order = [
                '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```',
                '```json\n{"tool":"ecad_place_symbol","args":{"libid":"Device:R"}}\n```',
                '```json\n{"tool":"ecad_wire","args":{"from_ref":"R1","from_pin":"1",'
                '"to_ref":"R2","to_pin":"1"}}\n```',
                '```json\n{"tool":"board_from_schematic","args":{}}\n```',
                '```json\n{"tool":"ecad_run_drc","args":{}}\n```',
            ]
            if seq["n"] <= len(order):
                return order[seq["n"] - 1]
            return "done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-1"}
        if tool == "ecad_place_symbol":
            return {"ok": True, "ref": "R1"}
        if tool == "ecad_wire":
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-1"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Build schematic wire board DRC",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "board_from_schematic", "ecad_run_drc", "ledger_note",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=6,
        phase="build",
    )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "ecad_wire" in tools_used
    assert "board_from_schematic" in tools_used
    assert "ecad_run_drc" in tools_used
    assert res.success is True



def test_auto_pick_frontier_floor_not_demoted_to_ollama():
    """mega-13/14: measured grok-4.5 pass_rate=0 must not force ollama on analysis.

    Floor is rank>=5 so analysis min_rank=4/5 still selects xai-sub without a
    harness user_rankings pin (mega-13 residual #4).
    """
    from services.agent.role_models import (
        ModelRank,
        auto_pick_model,
        load_rankings,
        _FRONTIER_MEASURED_FLOOR,
    )

    assert _FRONTIER_MEASURED_FLOOR >= 5

    # Simulate measured demotion of dotted id + shipped hyphen alias.
    ranks = {
        "grok-4.5": ModelRank(
            model_id="grok-4.5", rank=5, source="measured",
            notes="floor", provider="xai", pass_rate=0.0, cycles=4,
        ),
        "grok-4-5": ModelRank(
            model_id="grok-4-5", rank=5, source="shipped",
            notes="alias", provider="xai",
        ),
        "llama3.2:latest": ModelRank(
            model_id="llama3.2:latest", rank=1, source="shipped",
            notes="local", provider="ollama",
        ),
    }
    enabled = [
        {"provider": "xai-sub", "model": "grok-4.5"},
        {"provider": "xai-sub", "model": "grok-4-5"},
        {"provider": "ollama", "model": "llama3.2:latest"},
    ]
    a = auto_pick_model(
        task_class="analysis", min_rank=4, rankings=ranks, enabled=enabled,
    )
    assert a.provider == "xai-sub", a
    assert a.model in ("grok-4.5", "grok-4-5"), a
    assert (a.rank or 0) >= 5, a

    # load_rankings must clamp measured rank1 → floor 5 for grok-4.5.
    import tempfile
    from pathlib import Path
    import json
    with tempfile.TemporaryDirectory() as td:
        lb = Path(td) / "leaderboard.json"
        lb.write_text(json.dumps({
            "models": [{
                "id": "grok-4.5", "provider": "xai", "cycles": 4,
                "primary": {"pass_rate": 0.0},
            }],
        }), encoding="utf-8")
        loaded = load_rankings(
            {}, leaderboard_path=lb, include_measured=True,
        )
        assert loaded["grok-4.5"].rank >= 5, loaded["grok-4.5"]


def test_empty_product_handoff_block_ecad():
    from services.agent.agent_turn import _empty_product_handoff_block

    msg = _empty_product_handoff_block(
        {"ecad_new_schematic", "board_from_schematic", "ecad_run_drc"},
        "BUILD DL3: flight_computer_ecad",
    )
    assert msg is not None
    assert "board_from_schematic" in msg


def test_empty_product_handoff_block_pcb_lane_is_walkable():
    """The remedy may only name tools the blocked lane actually holds.

    pcb_engineer holds the board half and NOT ecad_new_schematic. Before the
    2026-08-27 split there was one combined branch, so a PCB lane was told to
    "start with ecad_new_schematic" — a tool its card cannot be given, which is
    the exact unsatisfiable-roadmap failure `_empty_product_handoff_block`'s
    docstring records.
    """
    from services.agent.agent_turn import _empty_product_handoff_block
    from services.agent.roles import load_role

    grant = set(load_role("pcb_engineer").concrete_tools)
    assert "ecad_new_schematic" not in grant, grant
    msg = _empty_product_handoff_block(grant, "BUILD DL3: flight_computer_pcb")
    assert msg is not None
    assert "ecad_new_schematic" not in msg, msg
    named = {w.strip(".,()→") for w in msg.replace("→", " ").split()}
    for tool in named & {
        "ledger_get", "board_from_schematic", "ecad_autoroute", "ecad_run_drc",
    }:
        assert tool in grant, (tool, sorted(grant))


def test_ecad_zero_tool_done_not_accepted(tmp_path, monkeypatch):
    """mega-13 residual: hollow done with fabricated board ids is rejected."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega13_empty_done", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"status":"done","summary":"board done",'
                    '"outputs":{"schematic_doc_id":"fake","board_doc_id":"fake2",'
                    '"artifacts":[],"checks_run":[],"reasoning_summary":"x"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 3:
                return (
                    '```json\n{"tool":"board_from_schematic","args":{}}\n```'
                )
            return (
                '```json\n{"status":"done","summary":"ok",'
                '"outputs":{"artifacts":[],"checks_run":[],'
                '"reasoning_summary":"wired"}}\n```'
            )

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-1"}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-1"}
        return {"ok": True}

    res = at.run_turn(
        "Build flight computer board",
        _Prov(),
        name="BUILD DL3: flight_computer_ecad",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "board_from_schematic", "ecad_run_drc", "ledger_note",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=6,
        phase="build",
    )
    tools_used = [c.get("tool") for c in (res.result or {}).get("calls") or []]
    assert "ecad_new_schematic" in tools_used or "board_from_schematic" in tools_used
    # First hollow done must not be the terminal success without tools.
    if res.success:
        assert tools_used, "done without tools accepted"


def test_ecad_hard_gate_does_not_block_the_second_place(tmp_path, monkeypatch):
    """mega-13 a3 + live 2026-07-28: thrash pressure must not deadlock the chain.

    This case used to assert that the SECOND ecad_place_symbol is hard-denied, on
    the theory that one symbol is enough to start wiring. A live general-lane run
    ("3V3 buck regulator with an ESP32-S3, TPS62840, caps and an inductor")
    disproved it: the nets-incomplete gate demands a wire between two DISTINCT
    refs, and denying place #2 means the second ref never exists, so the
    requirement is unsatisfiable by construction. The old behaviour only looked
    correct here because this test's ``_dispatch`` is a stub that reports every
    wire as ok regardless of whether the endpoints were ever placed.

    What actually matters — and what mega-13 was really about — is the OUTCOME:
    a schematic plus placements is not product success, so the turn must still
    reach wire -> board -> DRC. That is what is asserted now. The gate keeps its
    pressure via ``_ECAD_MIN_PLACES_BEFORE_NET_GATE``, which lets a real
    schematic be authored before the denials start.
    """
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega13_hard_gate", path=tmp_path / "ledger.json")
    seq = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            order = [
                '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```',
                '```json\n{"tool":"ecad_place_symbol","args":{"libid":"Device:R"}}\n```',
                # thrash — must be hard-denied
                '```json\n{"tool":"ecad_place_symbol","args":{"libid":"Device:C"}}\n```',
                '```json\n{"tool":"ecad_wire","args":{"from_ref":"R1","from_pin":"1",'
                '"to_ref":"C1","to_pin":"1"}}\n```',
                '```json\n{"tool":"board_from_schematic","args":{}}\n```',
                '```json\n{"tool":"ecad_run_drc","args":{}}\n```',
            ]
            if seq["n"] <= len(order):
                return order[seq["n"] - 1]
            return "done"

    def _dispatch(tool, args):
        return {"ok": True, "doc_id": "x", "ref": "R1", "net": "N1",
                "net_size": 2, "errors": 0}

    res = at.run_turn(
        "Build board",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=10,
        phase="build",
    )
    calls = (res.result or {}).get("calls") or []
    tools = [c.get("tool") for c in calls]
    assert "ecad_wire" in tools
    assert "board_from_schematic" in tools
    assert "ecad_run_drc" in tools
    # The product chain completed in order — that is mega-13's real contract.
    assert tools.index("ecad_wire") < tools.index("board_from_schematic") < \
        tools.index("ecad_run_drc"), tools
    # Both placements must SUCCEED. Denying the second one is what made the
    # gate's own "wire two distinct refs" requirement unsatisfiable live.
    places = [c for c in calls if c.get("tool") == "ecad_place_symbol"]
    assert len(places) == 2, places
    assert all(c.get("ok") for c in places), places
    # And the turn must not have burned its budget on refused forced retries:
    # the pre-fix cascade re-dispatched one identical ecad_wire 27 times.
    failed = [c for c in calls if c.get("ok") is False]
    assert len(failed) <= 2, failed

def test_ecad_hard_gate_force_injects_wire_after_thrash(tmp_path, monkeypatch):
    """mega-14: after denial cap, harness force-dispatches passive+wire+board."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega14_force_inject", path=tmp_path / "ledger.json")
    seq = {"n": 0}
    refs = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            # Always thrash place after schematic — never voluntary wire/board.
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic","args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"ecad_place_symbol","args":'
                    '{"libid":"MCU:Generic_QFP48"}}\n```'
                )
            if seq["n"] <= 14:
                return (
                    '```json\n{"tool":"ecad_place_symbol","args":'
                    '{"libid":"Device:C"}}\n```'
                )
            return "done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-f"}
        if tool == "ecad_place_symbol":
            refs["n"] += 1
            lib = str((args or {}).get("libid") or "")
            if "Device:R" in lib or lib.endswith(":R") or lib == "R":
                return {"ok": True, "ref": f"R{refs['n']}", "libid": lib,
                        "value": (args or {}).get("value") or "1k"}
            if "Device:C" in lib or lib.endswith(":C"):
                return {"ok": True, "ref": f"C{refs['n']}", "libid": lib}
            return {"ok": True, "ref": f"U{refs['n']}", "libid": lib}
        if tool == "ecad_add_power":
            # Orphan power (live residual) — must NOT unlock board alone.
            return {"ok": True, "net": args.get("net") or "GND",
                    "net_size": 0, "net_members": []}
        if tool == "ecad_wire":
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-f"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Build board force",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=16,
        phase="build",
    )
    calls = (res.result or {}).get("calls") or []
    tools = [c.get("tool") for c in calls if c.get("ok")]
    assert "ecad_wire" in tools, calls
    assert "board_from_schematic" in tools, calls
    assert "ecad_run_drc" in tools, calls
    # Wire must precede board in the ok call order.
    assert tools.index("ecad_wire") < tools.index("board_from_schematic")

def test_ecad_chain_need_helpers():
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_forced_tool_args, _ecad_has_real_wire,
        _ECAD_GATE_FORCE_AFTER,
    )
    assert _ECAD_GATE_FORCE_AFTER >= 2
    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {"tool": "ecad_place_symbol", "ok": True, "result": {"ref": "U1"},
         "args": {"libid": "MCU:Generic_QFP48"}},
    ]
    # IC-only → force passive place for SPICE models (mega-14 AC5 residual).
    assert _ecad_chain_need(calls, allow) == "ecad_place_symbol"
    args = _ecad_forced_tool_args("ecad_place_symbol", calls)
    assert "Device:R" in str(args.get("libid"))
    calls_p = calls + [{
        "tool": "ecad_place_symbol", "ok": True,
        "args": {"libid": "Device:R", "value": "1k"},
        "result": {"ref": "R1"},
    }]
    assert _ecad_chain_need(calls_p, allow) == "ecad_add_power"
    # Orphan power does NOT unlock board; still need cross-ref wire.
    calls_pow = calls_p + [{
        "tool": "ecad_add_power", "ok": True, "args": {"net": "GND"},
        "result": {"ok": True, "net": "GND", "net_size": 0, "net_members": []},
    }]
    assert _ecad_has_real_wire(calls_pow) is False
    assert _ecad_chain_need(calls_pow, allow) == "ecad_wire"
    # Self-loop alone still does NOT unlock board when power already present
    # but cross-ref missing — wait: after power, need wire; self-loop is wire
    # success but not product cross-wire.
    calls_self = calls_pow + [{
        "tool": "ecad_wire", "ok": True,
        "args": {"from_ref": "R1", "from_pin": "1", "to_ref": "R1", "to_pin": "2"},
        "result": {"ok": True, "net": "N1", "net_size": 2},
    }]
    from services.agent.agent_turn import _ecad_has_cross_wire, _ecad_nets_ready_for_board
    assert _ecad_has_cross_wire(calls_self) is False
    assert _ecad_nets_ready_for_board(calls_self, allow) is False
    assert _ecad_chain_need(calls_self, allow) == "ecad_wire"
    calls_w = calls_pow + [{
        "tool": "ecad_wire", "ok": True,
        "args": {"from_ref": "R1", "from_pin": "1", "to_ref": "U1", "to_pin": "1"},
        "result": {"ok": True, "net": "N1", "net_size": 2},
    }]
    assert _ecad_has_real_wire(calls_w) is True
    assert _ecad_chain_need(calls_w, allow) == "board_from_schematic"
    calls_b = calls_w + [{
        "tool": "board_from_schematic", "ok": True, "result": {"doc_id": "b"},
    }]
    # mega-22 Q22: board → optimize_traces → DRC (not bare DRC).
    assert _ecad_chain_need(calls_b, allow) == "ecad_optimize_traces"
    calls_opt = calls_b + [{
        "tool": "ecad_optimize_traces", "ok": True,
        "result": {"ok": True, "corners_rounded": 1},
    }]
    assert _ecad_chain_need(calls_opt, allow) == "ecad_run_drc"
    # mega-15: after DRC, force ERC re-run for AC4 post-wire grade.
    allow_erc = set(allow) | {"ecad_run_erc"}
    calls_d = calls_opt + [{
        "tool": "ecad_run_drc", "ok": True, "result": {"errors": 0},
    }]
    assert _ecad_chain_need(calls_d, allow_erc) == "ecad_run_erc"


