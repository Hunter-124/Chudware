"""mega-2 live harness fixes — unit coverage.

Defects (live mega-2-fc-rocket ledger + AestheticGrouse fingerprints):
1. tools_slot lists fw_*/em_*/ecad_antenna_* but dispatch could not resolve
2. ledger_get(section="all"|"events") unknown section
3. intake_role no_code after fixture (auto-complete productive tools)
4. manifest not in ledger sections / empty requirements after intake
5. ledger_note decision/dimension KeyError on missing id
6. C1 AC2-AC4 grading path: fit_bore / bolt_circle / battery mirror
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_fw_em_antenna_tools_resolve():
    """mega-2 defect 1: family:fw/em tools must resolve at dispatch."""
    from services.agent.reviewer_exec import _resolve_fn, _EXTRA_MCP
    _EXTRA_MCP.clear()
    for name in (
        "fw_status", "fw_new_project", "fw_build",
        "em_status", "em_driven", "em_eigenmode",
        "ecad_antenna_design", "ecad_antenna_analyze",
    ):
        fn = _resolve_fn(name)
        assert fn is not None, f"{name} unresolved"


def test_mirror_fit_calc_writes_fit_bore_clearance(tmp_path):
    """C1 AC2: fit_calc success mirrors decision fit_bore.clearance."""
    from services.agent.agent_turn import _mirror_c1_grading_evidence
    from services.agent.ledger import Ledger

    led = Ledger("c1_fit", tmp_path / "l.json")
    _mirror_c1_grading_evidence(
        "fit_calc",
        {"nominal": 20, "fit": "H7/h7"},
        {"ok": True, "answer": {"clearance": [0.0, 0.042], "fit_character": "clearance"}},
        led, "spec",
    )
    decs = [e for e in led.events if e["kind"] == "decision"]
    assert any(
        e["payload"].get("id") == "fit_bore"
        and e["payload"].get("clearance") is not None
        for e in decs
    ), decs


def test_mirror_bolt_circle_writes_holes(tmp_path):
    """C1 AC3: bolt_circle tool mirrors decision bolt_circle.holes."""
    from services.agent.agent_turn import _mirror_c1_grading_evidence
    from services.agent.ledger import Ledger

    led = Ledger("c1_bc", tmp_path / "l.json")
    _mirror_c1_grading_evidence(
        "bolt_circle",
        {"diameter": 4.5, "count": 4, "pcd": 34.0},
        {"ok": True},
        led, "plan",
    )
    decs = [e for e in led.events if e["kind"] == "decision"]
    assert any(
        e["payload"].get("id") == "bolt_circle"
        and e["payload"].get("holes") == 4
        for e in decs
    ), decs


def test_mirror_verify_battery_writes_artifact(tmp_path):
    """C1 AC4: verify_battery success mirrors battery artifact with verdict."""
    from services.agent.agent_turn import _mirror_c1_grading_evidence
    from services.agent.ledger import Ledger

    led = Ledger("c1_bat", tmp_path / "l.json")
    _mirror_c1_grading_evidence(
        "verify_battery", {},
        {"scores": {"check_sketch": 1.0}, "findings": []},
        led, "reviewer",
    )
    arts = [e for e in led.events if e["kind"] == "artifact"]
    assert any(
        e["payload"].get("kind") == "battery"
        and str(e["payload"].get("verdict", "")).upper() == "PASS"
        for e in arts
    ), arts


def test_mirror_verify_battery_never_turns_failed_findings_into_pass(tmp_path):
    """Live run-88954488724871 had three ok:false findings and three zero scores, yet
    the turn mirror emitted battery_result PASS.  Mirror must use the canonical grader."""
    from services.agent.agent_turn import _mirror_c1_grading_evidence
    from services.agent.ledger import Ledger

    led = Ledger("c1_bat_fail", tmp_path / "l.json")
    _mirror_c1_grading_evidence(
        "verify_battery", {"doc_id": "doc-1"},
        {
            "scores": {
                "dfm_check": 0.0,
                "self_intersection": 0.0,
                "wall_thickness": 1.0,
                "watertight": 0.0,
            },
            "findings": [
                {"check": "dfm_check", "ok": False, "detail": "unknown check"},
                {"check": "self_intersection", "ok": False, "detail": "unknown check"},
                {"check": "watertight", "ok": False, "detail": "unknown check"},
            ],
        },
        led, "reviewer",
    )

    artifact = next(e for e in led.events if e["kind"] == "artifact")
    battery = next(e for e in led.events if e["kind"] == "battery_result")
    assert artifact["payload"]["verdict"] == "FAIL", artifact
    assert battery["payload"]["verdict"] == "FAIL", battery
    assert battery["payload"]["ok"] is False
    assert "failing findings" in battery["payload"]["detail"]


def test_plan_build_productive_auto_complete():
    """plan/build handoff class: productive tools + no formal done → confirmed."""
    from services.agent import agent_turn
    from services.agent.test_agent_turn import _ScriptedProvider

    provider = _ScriptedProvider([
        '```json\n{"tool": "buildgraph_plan", "args": {"plan_json": {"parts": []}}}\n```',
        '```json\n{"tool": "ledger_note", "args": {"kind": "decision", '
        '"payload": {"id": "plan_ok", "stage": "plan", "text": "planned", '
        '"rationale": "x"}}}\n```',
        # junk finish (no tool/done/status) → auto-complete path
        '```json\n{"note": "plan looks good"}\n```',
    ])
    res = agent_turn.run_turn(
        "plan the mission build graph",
        provider,
        allowed_tools=["buildgraph_plan", "ledger_note"],
        tool_dispatch=lambda t, a: {"ok": True},
    )
    assert res.success is True, res.stop_reason
    assert res.stop_reason == "confirmed"
    assert res.result.get("auto_complete") is True or (
        isinstance(res.result.get("handoff"), dict)
        and res.result["handoff"].get("status") == "done"
    )


def test_bare_text_finish_after_tools_is_role_handoff():
    """Bare prose finish after tools is accepted as role handoff (intake/plan)."""
    from services.agent import agent_turn
    from services.agent.test_agent_turn import _ScriptedProvider

    provider = _ScriptedProvider([
        '```json\n{"tool": "ledger_get", "args": {}}\n```',
        "Manifest looks complete with 5 deliverables; proceeding.",
    ])
    res = agent_turn.run_turn(
        "confirm the intake manifest",
        provider,
        allowed_tools=["ledger_get"],
        tool_dispatch=lambda t, a: {"ok": True, "rendered": "ok"},
    )
    assert res.success is True, res.stop_reason
    assert res.stop_reason == "confirmed"
    handoff = res.result.get("handoff") or {}
    assert handoff.get("status") == "done"


def test_decision_id_keyerror_regression(tmp_path):
    """Replay mega-2 seq 39→41 pattern: id-less decision then named decision."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    led = Ledger("mega2_dec", tmp_path / "l.json")
    # Corrupt pre-fix shape: append a decision with no id via raw append
    # (simulates first successful write before id synthesis existed).
    led.append("agent", "decision", {
        "name": "fc_process", "value": "pcb", "driver": "x", "provenance": "y",
    })
    md = MissionDispatch(run_id="mega2_dec", ledger=led)
    # Second write with explicit id must not KeyError on state upsert.
    out = md.ledger_note(
        kind="decision",
        payload={"id": "fc_3v3_regulator", "name": "fc_3v3_regulator",
                 "value": "Buck_3V3", "driver": "x", "provenance": "ecad"},
    )
    assert out["ok"] is True, out


def test_ledger_note_kind_verdict_aliases_to_decision(tmp_path):
    """mega-2 defect 8: DELIVER kind=verdict must not unknown-kind."""
    from services.agent.ledger import Ledger
    from services.agent.mission_dispatch import MissionDispatch

    led = Ledger("mega2_verdict", tmp_path / "l.json")
    md = MissionDispatch(run_id="mega2_verdict", ledger=led)
    out = md.ledger_note(
        kind="verdict",
        actor="reviewer",
        payload={
            "deliverable": "DL5",
            "AC9": "PASS",
            "measured": "10/10 legs",
            "evidence": "ledger_get",
        },
    )
    assert out["ok"] is True, out
    assert out.get("kind") == "decision"
    assert out.get("aliased_from") == "verdict"
    decs = md.ledger_get(section="decisions")
    assert decs["ok"] and any(d.get("id") == "DL5" for d in decs["data"])
