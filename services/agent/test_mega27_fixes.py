"""Mega-27 fixes — C27 board outline room + D27 clearance retry grow ladder.

Grounded in mega-26 a1 fingerprints:

  C27 / AC4 residual room — board_from_schematic AUTO-OUTLINE packs parts so
    the router cannot meet 0.2mm clearance even after B26 autoroute(pitch=0.3).
    Cure: at force time, size board_size so outline_area >= 3x component bbox
    area (or outline_margin >= 15 engine-margin equivalent); record both areas
    on call records for grade axes.

  D27 / AC4 — retry ladder becomes outline-grow (x1.5) + autoroute pitch 0.3
    + re-opt + re-DRC once, then honest FAIL (errors==0 pass rule unchanged).
    mega-26 residual: 23 clearance errors after retry_n>=1 without grow.

Schemas checked (no invented knobs):
  * MCP board_from_schematic: previously only sch_doc_id/optimize/doc_id —
    now plumbs board_size / outline_margin / outline_area_scale / outline /
    force_outline into board_flow.
  * board_flow._ensure_outline: was hard-coded margin=5; now accepts knobs.
  * place_board place_opts.board_size: already existed.
  * ecad_autoroute: pitch / ripup_passes / diff_pairs only (no clearance).
  * ecad_optimize_traces: targets / doc_id only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MEGA26 = _ROOT / "reports/runs/mega-26"
_MEGA26_AC4 = _MEGA26 / "criterion_AC4.json"
_MEGA26_LEDGER = _MEGA26 / "ledger.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_ecad_calls() -> list[dict]:
    data = _load_json(_MEGA26_LEDGER)
    events = data if isinstance(data, list) else data.get("events") or []
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") not in (None, "tool_call") and "tool" not in ev:
            # ledger rows may be bare tool_call records
            if "tool" not in ev:
                continue
        tool = ev.get("tool")
        if not tool:
            continue
        rec = {
            "tool": tool,
            "ok": bool(ev.get("ok", True)),
            "args": ev.get("args") if isinstance(ev.get("args"), dict) else {},
            "result": ev.get("result"),
            "forced": bool(ev.get("forced")),
            "force_reason": ev.get("force_reason"),
        }
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Live fingerprints (mega-26 residual)
# ---------------------------------------------------------------------------


def test_mega26_ac4_fingerprint_clearance_still_dirty():
    """D27 fixture: DRC clearance residual after B26 retry (errors≈23)."""
    ac4 = _load_json(_MEGA26_AC4)
    assert ac4.get("result") == "FAIL"
    ev = ac4.get("evidence") or {}
    drc = ev.get("drc") or ev.get("ecad_run_drc") or {}
    errors = int(drc.get("errors") or 0)
    assert errors >= 20, errors
    assert "clearance" in str(drc.get("verdict") or "").lower() or errors > 0


def test_mega26_ledger_board_had_no_outline_size_args():
    """C27 fixture: mega-26 forced board_from_schematic lacked board_size."""
    calls = _ledger_ecad_calls()
    boards = [
        c for c in calls
        if c.get("tool") == "board_from_schematic" and c.get("ok")
    ]
    if not boards:
        pytest.skip("no board_from_schematic in mega-26 ledger")
    for b in boards:
        args = b.get("args") or {}
        # Pre-fix: no outline room knobs on the call record.
        assert "board_size" not in args or not args.get("board_size")
        assert not args.get("outline_area_mm2")


# ---------------------------------------------------------------------------
# C27 — outline sizing >= 3x component bbox
# ---------------------------------------------------------------------------


def test_c27_outline_sizing_ratio_at_least_three():
    from services.agent.agent_turn import _ecad_outline_sizing

    # 1 part, 5 parts, 12 parts — always >=3x.
    for n in (1, 5, 12, 24):
        calls = [
            {
                "tool": "ecad_place_symbol",
                "ok": True,
                "args": {"libid": "Device:R", "ref": f"R{i}"},
                "result": {"ok": True, "ref": f"R{i}"},
            }
            for i in range(n)
        ]
        s = _ecad_outline_sizing(calls, area_mult=3.0, grow=1.0)
        assert s["outline_area_ratio"] >= 3.0 - 1e-6, s
        assert s["outline_area_mm2"] >= 3.0 * s["component_bbox_area_mm2"] - 1e-3
        assert s["outline_margin"] >= 15.0
        assert len(s["board_size"]) == 2
        assert s["board_size"][0] >= 80.0
        assert s["board_size"][1] >= 60.0
        assert s["n_components"] == n


def test_c27_forced_board_args_carry_outline_room():
    from services.agent.agent_turn import _ecad_forced_tool_args

    sch = "sch_c27"
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48", "ref": "U1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "U1", "doc_id": sch,
                "pins": [
                    {"ref": "U1", "pin": "1", "name": "VDD", "type": "power_in"},
                    {"ref": "U1", "pin": "2", "name": "GND", "type": "power_in"},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "R1", "doc_id": sch,
                "pins": [
                    {"ref": "R1", "pin": "1", "name": "1", "type": "passive"},
                    {"ref": "R1", "pin": "2", "name": "2", "type": "passive"},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "U1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "1", "doc_id": sch,
            },
            "result": {
                "ok": True, "net": "3V3", "net_size": 2,
                "net_members": ["U1.1", "R1.1"],
            },
        },
    ]
    args = _ecad_forced_tool_args("board_from_schematic", calls)
    assert args.get("doc_id") == sch or args.get("sch_doc_id") == sch
    assert isinstance(args.get("board_size"), list) and len(args["board_size"]) == 2
    assert float(args.get("outline_margin") or 0) >= 15.0
    assert float(args.get("outline_area_mm2") or 0) > 0
    assert float(args.get("component_bbox_area_mm2") or 0) > 0
    ratio = float(args.get("outline_area_ratio") or 0)
    assert ratio >= 3.0 - 1e-6, args
    assert float(args.get("outline_grow") or 0) == 1.0
    assert float(args.get("min_clearance_mm") or 0) == 0.2


def test_c27_mega26_ledger_replay_gets_outline_args():
    """Replay mega-26-style pre-board snap: forced board carries outline knobs."""
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = _ledger_ecad_calls()
    # Snap just before first board_from_schematic if present; else full ledger.
    idx = next(
        (i for i, c in enumerate(calls) if c.get("tool") == "board_from_schematic"),
        None,
    )
    snap = calls[:idx] if idx is not None else calls
    if not any(c.get("tool") == "ecad_place_symbol" and c.get("ok") for c in snap):
        # Fabricate minimal place if ledger truncated places away.
        snap = list(snap) + [
            {
                "tool": "ecad_place_symbol", "ok": True,
                "args": {"libid": "Device:R", "ref": "R1"},
                "result": {"ok": True, "ref": "R1"},
            },
            {
                "tool": "ecad_place_symbol", "ok": True,
                "args": {"libid": "MCU:Generic_QFP48", "ref": "U1"},
                "result": {"ok": True, "ref": "U1"},
            },
        ]
    args = _ecad_forced_tool_args("board_from_schematic", snap)
    assert args.get("board_size"), args
    assert float(args.get("outline_area_ratio") or 0) >= 3.0 - 1e-6, args
    assert float(args.get("outline_area_mm2") or 0) >= 3.0 * float(
        args.get("component_bbox_area_mm2") or 1
    ) - 1.0


# ---------------------------------------------------------------------------
# D27 — outline-grow + autoroute pitch 0.3 retry ladder
# ---------------------------------------------------------------------------


def _dirty_board_calls(brd: str = "brd_d27", sch: str = "sch_d27",
                       errors: int = 23) -> list[dict]:
    """Board → opt → dirty DRC with clearance residual (mega-26 shape)."""
    return [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "R1",
                "pins": [
                    {"ref": "R1", "pin": "1", "name": "1", "type": "passive"},
                    {"ref": "R1", "pin": "2", "name": "2", "type": "passive"},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48", "ref": "U1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "U1",
                "pins": [
                    {"ref": "U1", "pin": "1", "name": "VDD", "type": "power_in"},
                    {"ref": "U1", "pin": "2", "name": "GND", "type": "power_in"},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "U1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "1", "doc_id": sch,
            },
            "result": {
                "ok": True, "net": "3V3", "net_size": 2,
                "net_members": ["U1.1", "R1.1"],
            },
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "args": {
                "doc_id": sch, "sch_doc_id": sch,
                "board_size": [80.0, 60.0],
                "outline_margin": 15.0,
                "outline_area_mm2": 4800.0,
                "component_bbox_area_mm2": 600.0,
                "outline_area_ratio": 8.0,
                "outline_grow": 1.0,
            },
            "result": {
                "ok": True, "doc_id": brd, "board_doc_id": brd,
                "sch_doc_id": sch, "placed": 2, "routed": 1,
                "outline_area_mm2": 4800.0,
                "component_bbox_area_mm2": 600.0,
            },
            "forced": True,
        },
        {
            "tool": "ecad_optimize_traces", "ok": True,
            "args": {"doc_id": brd, "min_clearance_mm": 0.2},
            "result": {"ok": True, "doc_id": brd, "corners_rounded": 2},
            "forced": True,
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "args": {"doc_id": brd},
            "result": {
                "ok": True, "doc_id": brd, "errors": errors,
                "verdict": (
                    f"{errors} error(s), 0 warning(s). worst: [ERROR] clearance: "
                    "clearance 0.08mm < required 0.2mm"
                ),
                "violations": [
                    {
                        "severity": "error", "rule": "clearance",
                        "message": "clearance 0.08mm < required 0.2mm",
                    }
                ] * min(errors, 5),
            },
            "forced": True,
        },
    ]


def test_d27_clearance_retry_fires_outline_grow_and_autoroute():
    """Dirty DRC fixture: grow board + autoroute(pitch=0.3) + re-DRC."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade,
        _ecad_optimize_retry_count,
        _ecad_last_drc_errors,
    )

    brd, sch = "brd_d27", "sch_d27"
    base = _dirty_board_calls(brd=brd, sch=sch, errors=23)
    allow = {
        "ecad_add_power", "ecad_new_schematic", "ecad_place_symbol",
        "ecad_wire", "ecad_run_erc", "ecad_simulate",
        "board_from_schematic", "ecad_run_drc", "ecad_optimize_traces",
        "ecad_autoroute",
    }
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "board_from_schematic":
            # Grow path must carry larger board_size / outline_grow>=1.5.
            assert a.get("board_size"), a
            return {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": sch,
                "placed": 2,
                "routed": 2,
                "outline_area_mm2": a.get("outline_area_mm2") or 7200.0,
                "component_bbox_area_mm2": a.get("component_bbox_area_mm2") or 600.0,
            }
        if tool == "ecad_autoroute":
            assert float(a.get("pitch") or 0) >= 0.3, a
            return {"ok": True, "doc_id": brd, "routed": 2}
        if tool == "ecad_optimize_traces":
            return {"ok": True, "doc_id": brd, "corners_rounded": 1}
        if tool == "ecad_run_drc":
            # Clean after grow+reroute — D27 PASS path.
            return {
                "ok": True, "doc_id": brd, "errors": 0,
                "violations": [], "verdict": "0 error(s)",
            }
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0, "doc_id": sch}
        if tool == "ecad_simulate":
            return {
                "ok": True, "device_count": 2, "op": {"V(3V3)": 3.3},
                "nodes": ["3V3", "GND"], "doc_id": sch,
                "resolved_net": "3V3",
            }
        return {"ok": True}

    new_calls, made = _ecad_force_chain_cascade(
        calls=list(base),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=24,
        reason="end_of_turn_ecad_force",
        max_steps=12,
    )
    tools = [t for t, _ in dispatched]
    assert "board_from_schematic" in tools, tools
    assert "ecad_autoroute" in tools, tools
    grow_args = next(a for t, a in dispatched if t == "board_from_schematic")
    # Call record (not necessarily dispatch) should show grow; dispatch keeps
    # board_size. Check call records for outline_grow diagnostic.
    board_forced = [
        c for c in new_calls
        if c.get("tool") == "board_from_schematic" and c.get("forced")
    ]
    assert board_forced, new_calls
    last_board = board_forced[-1]
    last_args = last_board.get("args") or {}
    assert float(last_args.get("outline_grow") or 0) >= 1.5, last_args
    assert float(last_args.get("outline_area_ratio") or 0) >= 3.0, last_args
    assert grow_args.get("board_size"), grow_args
    assert any(t == "ecad_run_drc" for t in tools), tools
    assert _ecad_optimize_retry_count(new_calls) >= 1
    assert int(_ecad_last_drc_errors(new_calls) or 0) == 0
    assert made > 0


def test_d27_grow_path_honest_fail_when_still_dirty():
    """If re-DRC still dirty after one grow+reroute, errors stay >0 (no lie)."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade,
        _ecad_optimize_retry_count,
        _ecad_last_drc_errors,
        _ecad_chain_need,
    )

    brd, sch = "brd_d27b", "sch_d27b"
    base = _dirty_board_calls(brd=brd, sch=sch, errors=23)
    allow = {
        "board_from_schematic", "ecad_run_drc", "ecad_optimize_traces",
        "ecad_autoroute", "ecad_run_erc", "ecad_simulate",
        "ecad_wire", "ecad_add_power", "ecad_place_symbol",
    }
    re_drc_n = [0]

    def _dispatch(tool, args):
        if tool == "board_from_schematic":
            return {
                "ok": True, "doc_id": brd, "board_doc_id": brd,
                "sch_doc_id": sch, "placed": 2,
            }
        if tool == "ecad_autoroute":
            return {"ok": True, "doc_id": brd}
        if tool == "ecad_optimize_traces":
            return {"ok": True, "doc_id": brd}
        if tool == "ecad_run_drc":
            re_drc_n[0] += 1
            # Still residual — honest FAIL (reduced count is fine).
            return {
                "ok": True, "doc_id": brd, "errors": 10,
                "verdict": "10 error(s). worst: [ERROR] clearance: 0.15mm",
                "violations": [
                    {"severity": "error", "rule": "clearance",
                     "message": "clearance 0.15mm < required 0.2mm"},
                ],
            }
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0, "doc_id": sch}
        if tool == "ecad_simulate":
            return {
                "ok": True, "op": {"V(3V3)": 3.3}, "doc_id": sch,
                "device_count": 2,
            }
        return {"ok": True}

    new_calls, _made = _ecad_force_chain_cascade(
        calls=list(base),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=24,
        reason="end_of_turn_ecad_force",
        max_steps=12,
    )
    assert _ecad_optimize_retry_count(new_calls) >= 1
    # Bounded once: no infinite re-opt.
    assert _ecad_chain_need(new_calls, allow) != "ecad_optimize_traces"
    errs = int(_ecad_last_drc_errors(new_calls) or 0)
    assert errs > 0  # honest FAIL
    assert errs < 23  # reduced vs mega-26 residual (or equal if fixture fixed)
    assert re_drc_n[0] >= 1


def test_d27_outline_grow_sizing_is_one_point_five():
    from services.agent.agent_turn import (
        _ecad_outline_sizing,
        _ecad_forced_tool_args,
    )

    base = _dirty_board_calls(errors=23)
    # With clearance residual present, forced board args grow ×1.5.
    args = _ecad_forced_tool_args("board_from_schematic", base)
    assert float(args.get("outline_grow") or 0) >= 1.5, args
    s1 = _ecad_outline_sizing(base, area_mult=3.0, grow=1.0)
    s15 = _ecad_outline_sizing(base, area_mult=3.0, grow=1.5)
    assert s15["outline_area_mm2"] >= s1["outline_area_mm2"] * 1.5 - 1.0
    assert s15["outline_area_ratio"] >= 3.0 * 1.5 - 0.1


# ---------------------------------------------------------------------------
# Regression holds (mega-26 green axes)
# ---------------------------------------------------------------------------


def test_b26_y25_s23_hold_sim_and_clearance_intent():
    from services.agent.agent_turn import _ecad_forced_tool_args

    sch, brd = "sch_hold", "brd_hold"
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "args": {"doc_id": sch},
            "result": {"ok": True, "doc_id": brd, "board_doc_id": brd},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "args": {"doc_id": brd},
            "result": {
                "ok": True, "doc_id": brd, "errors": 3,
                "verdict": "3 error(s). [ERROR] clearance: 0.1mm",
            },
        },
    ]
    ar = _ecad_forced_tool_args("ecad_autoroute", calls)
    assert float(ar.get("pitch") or 0) >= 0.3
    assert ar.get("doc_id") == brd
    sim = _ecad_forced_tool_args("ecad_simulate", calls)
    assert sim.get("doc_id") == sch  # Y25: sim on schematic
    assert sim.get("u1_stubbed_ideal_regulator") is True
    drc = _ecad_forced_tool_args("ecad_run_drc", calls)
    assert drc.get("doc_id") == brd


def test_board_flow_outline_knobs_accepted():
    """Engine-side: board_flow._ensure_outline honors board_size / margin."""
    # board_flow drives the C++-backed facade now (services.mcp.ecad_common).
    from services.mcp.ecad_common import ecad_session, schema
    from services.mcp import board_flow as bf

    pcb = ecad_session.new_session("pcb", "m27_outline")
    # Valid fp ids required by schema validation on apply_ops.
    pcb.doc["footprints"] = [
        {
            "id": "fp1", "ref": "R1", "fpid": "R_0603",
            "pos": [10.0, 10.0], "angle": 0.0,
            "pads": [{"id": "p1", "number": "1", "pos": [0.0, 0.0],
                      "size": [0.8, 0.8], "type": "smd", "layers": ["F.Cu"]}],
        },
        {
            "id": "fp2", "ref": "R2", "fpid": "R_0603",
            "pos": [20.0, 12.0], "angle": 0.0,
            "pads": [{"id": "p2", "number": "1", "pos": [0.0, 0.0],
                      "size": [0.8, 0.8], "type": "smd", "layers": ["F.Cu"]}],
        },
    ]
    ecad_session.swap_doc(pcb, pcb.doc)  # pcb.doc is a client copy — write back
    report: dict = {"steps": []}
    warnings: list = []
    bf._ensure_outline(
        pcb, report, warnings,
        margin=15.0,
        board_size=[120.0, 90.0],
        area_scale=1.0,
        force=True,
    )
    assert not warnings, warnings
    assert (pcb.doc.get("outline") or {}).get("shapes")
    assert report.get("outline_area_mm2") == pytest.approx(120.0 * 90.0, rel=1e-3)
    assert report.get("outline_size_mm") == [120.0, 90.0]
    # Grow: force replace with scale 1.5 on parts+margin path.
    report2: dict = {"steps": []}
    bf._ensure_outline(
        pcb, report2, warnings,
        margin=15.0,
        board_size=None,
        area_scale=1.5,
        force=True,
    )
    assert report2.get("outline_area_mm2", 0) > 0
    assert schema.is_board(pcb.doc)
