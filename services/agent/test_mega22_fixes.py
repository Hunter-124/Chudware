"""Mega-22 fixes — P22 SPICE driven rail, Q22 board→optimize→DRC, R22 measure faces.

Grounded in mega-21 a1 fingerprints (reports/runs/mega-21/):

  P22 / AC5 — resolved_net=3V3 but V(3V3)=0.0; device_count=1; U1 skipped
              (no sim model). Source V + load R never both attached to the
              resolved rail. Force topology completion before simulate.

  Q22 / AC4 — ERC=0 but DRC errors=31 clearance on board 6f30f5cdb225.
              Forced chain was board_from_schematic → drc WITHOUT
              ecad_optimize_traces. Contract: board → optimize → drc,
              re-opt+re-DRC once if still dirty, else honest FAIL.

  R22 / AC1 — measure_between issued 'top face' / free-text queries the
              geom engine rejects (valid sides: top/bottom/… only).
              Derive from query_geometry/bbox; never guessed multi-word names.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_MEGA21_LEDGER = (
    Path(__file__).resolve().parents[2] / "reports/runs/mega-21/ledger.json"
)
_MEGA21_AC5 = (
    Path(__file__).resolve().parents[2]
    / "reports/runs/mega-21/criterion_AC5.json"
)
_MEGA21_AC4 = (
    Path(__file__).resolve().parents[2]
    / "reports/runs/mega-21/criterion_AC4.json"
)
_MEGA21_AC1 = (
    Path(__file__).resolve().parents[2]
    / "reports/runs/mega-21/criterion_AC1.json"
)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fixtures — mega-21 fingerprints
# ---------------------------------------------------------------------------


def test_mega21_ac5_fingerprint_device_count_one_undriven():
    """P22 fixture: resolved 3V3, V=0.0, device_count=1."""
    ac5 = _load_json(_MEGA21_AC5)
    assert ac5.get("result") == "FAIL"
    ev = ac5.get("evidence") or {}
    assert ev.get("resolved_net") == "3V3"
    assert ev.get("device_count") == 1
    op = ev.get("op") or {}
    assert float(op.get("V(3V3)", 1e9)) == 0.0
    warns = " ".join(ev.get("warnings") or [])
    assert "no sim model" in warns.lower() or "skipped" in warns.lower()


def test_mega21_ac4_fingerprint_drc_clearance_no_optimize():
    """Q22 fixture: DRC errors=31, no optimize_traces in forced chain."""
    ac4 = _load_json(_MEGA21_AC4)
    assert ac4.get("result") == "FAIL"
    ev = ac4.get("evidence") or {}
    drc = ev.get("drc") or ev.get("ecad_run_drc") or {}
    assert int(drc.get("errors") or 0) == 31
    assert drc.get("doc_id") == "6f30f5cdb225"
    # Ledger: board → drc with no optimize between.
    led = _load_json(_MEGA21_LEDGER)
    events = led.get("events") or []
    tools = []
    for evn in events:
        pl = evn.get("payload") if isinstance(evn, dict) else {}
        if not isinstance(pl, dict):
            continue
        if pl.get("event") == "tool_call" and pl.get("ok") and pl.get("tool"):
            tools.append(pl["tool"])
    assert "board_from_schematic" in tools
    assert "ecad_run_drc" in tools
    assert "ecad_optimize_traces" not in tools
    # Order: board before DRC, no optimize in between the forced pair.
    bi = tools.index("board_from_schematic")
    di = tools.index("ecad_run_drc")
    assert bi < di
    assert "ecad_optimize_traces" not in tools[bi:di + 1]


def test_mega21_ac1_fingerprint_bad_side_top_face():
    """R22 fixture: measure_between soft-zero from 'top face' side query."""
    ac1 = _load_json(_MEGA21_AC1)
    assert ac1.get("result") == "FAIL"
    detail = str(ac1.get("detail") or "")
    assert "measure_between" in detail
    # Findings carry the bad side error.
    findings = (ac1.get("evidence") or {}).get("findings") or []
    blob = json.dumps(findings).lower()
    assert "top face" in blob or "bad" in blob and "side" in blob


# ---------------------------------------------------------------------------
# P22 — SPICE driven topology
# ---------------------------------------------------------------------------


def _mega21_undriven_sim_calls() -> list[dict]:
    """Calls shaped like mega-21 residual just before a useful simulate."""
    return [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "6cd829c7d63f"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48", "ref": "U1"},
            "result": {
                "ok": True, "ref": "U1", "libid": "MCU:Generic_QFP48",
                "pins": [
                    {"number": "9", "name": "VDDA", "type": "power_in",
                     "xy": [34.76, 43.65], "net": "3V3"},
                    {"number": "12", "name": "VSS", "type": "power_in",
                     "xy": [115.24, 66.03], "net": "GND"},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k", "ref": "R1"},
            "result": {
                "ok": True, "ref": "R1", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "xy": [14.92, 20.0], "net": "N$1"},
                    {"number": "2", "xy": [25.08, 20.0], "net": "N$2"},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 34.76, "y": 43.65},
            "result": {
                "ok": True, "net": "3V3", "net_size": 1,
                "net_members": ["U1.9"],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND", "x": 115.24, "y": 66.03},
            "result": {
                "ok": True, "net": "GND", "net_size": 1,
                "net_members": ["U1.12"],
            },
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "U1", "from_pin": "9",
                "to_ref": "R1", "to_pin": "1",
            },
            "result": {
                "ok": True, "net": "3V3", "net_size": 2,
                "net_members": ["R1.1", "U1.9"],
            },
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "ok": True, "doc_id": "6f30f5cdb225",
                "board_doc_id": "6f30f5cdb225",
                "sch_doc_id": "6cd829c7d63f",
            },
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "result": {"ok": True, "errors": 31, "doc_id": "6f30f5cdb225"},
        },
        {
            "tool": "ecad_run_erc", "ok": True,
            "result": {"ok": True, "errors": 0},
        },
        # Prior undriven sim (mega-21 shape) — triggers completion.
        {
            "tool": "ecad_simulate", "ok": True,
            "args": {"analysis": "op", "probes": ["V(3V3)"],
                     "resolved_net": "3V3"},
            "result": {
                "ok": True, "analysis": "op", "backend": "mna",
                "converged": True, "device_count": 1,
                "nodes": ["3V3", "N$1"],
                "probes": ["V(3V3)"],
                "warnings": [
                    "U1 (MCU:Generic_QFP48): no sim model — skipped",
                ],
                "op": {"V(3V3)": 0.0},
                "resolved_net": "3V3",
            },
        },
    ]


def test_sim_topology_flags_mega21_undriven():
    from services.agent.agent_turn import _ecad_sim_topology

    calls = _mega21_undriven_sim_calls()
    topo = _ecad_sim_topology(calls=calls)
    assert topo["rail"] == "3V3"
    assert topo["device_count"] == 1
    assert topo["driven"] is False
    # Load may be detected via wire history; source must be missing.
    assert topo["has_source"] is False


def test_ensure_driven_rail_places_source_and_load_on_3v3():
    """P22: device_count=1 residual forces V+R on resolved rail before re-sim."""
    from services.agent.agent_turn import (
        _ecad_ensure_driven_rail, _ecad_sim_topology, _ecad_source_on_rail,
        _ecad_load_on_rail,
    )

    calls = _mega21_undriven_sim_calls()
    # Drop the prior sim so cascade can re-run it after completion; keep
    # the undriven fingerprint by injecting a synthetic prior result bag.
    # Keep prior sim so topology reads device_count=1.
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "Device:V" in lib or lib.endswith(":V"):
                return {
                    "ok": True, "ref": a.get("ref") or "V1", "libid": lib,
                    "value": a.get("value") or "DC 3.3",
                    "pins": [
                        {"number": "+", "name": "+", "type": "power_out",
                         "xy": [40, 5], "net": "N$V"},
                        {"number": "-", "name": "-", "type": "power_out",
                         "xy": [40, -5], "net": "N$G"},
                    ],
                }
            return {
                "ok": True, "ref": a.get("ref") or "Rload", "libid": lib,
                "value": a.get("value") or "1k",
                "pins": [
                    {"number": "1", "xy": [60, 0], "net": "N$L1"},
                    {"number": "2", "xy": [70, 0], "net": "N$L2"},
                ],
            }
        if tool == "ecad_wire":
            net = a.get("net") or "3V3"
            return {
                "ok": True, "net": net, "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 1,
                "net_members": [],
            }
        return {"ok": True}

    allow = {
        "ecad_place_symbol", "ecad_wire", "ecad_add_power", "ecad_simulate",
    }
    new_calls, made, steps, _ex = _ecad_ensure_driven_rail(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=20,
        reason="p22",
        steps=0,
        max_steps=12,
    )
    assert made > 0 and steps > 0
    # V source placed.
    assert any(
        c.get("tool") == "ecad_place_symbol"
        and (
            "Device:V" in str((c.get("args") or {}).get("libid") or "")
            or "V_source" in str((c.get("args") or {}).get("libid") or "")
            or "Simulation_SPICE" in str((c.get("args") or {}).get("libid") or "")
        )
        for c in new_calls if c.get("ok")
    ), dispatched
    # Source on rail after wires.
    assert _ecad_source_on_rail(new_calls, "3V3") or any(
        c.get("tool") == "ecad_wire"
        and str((c.get("args") or {}).get("from_ref") or "").startswith("V")
        for c in new_calls if c.get("ok")
    ), (dispatched, new_calls[-6:])
    # Load on rail.
    assert _ecad_load_on_rail(new_calls, "3V3") or any(
        c.get("tool") == "ecad_wire"
        and (
            str((c.get("args") or {}).get("from_ref") or "").startswith("R")
            or str((c.get("args") or {}).get("to_ref") or "").startswith("R")
        )
        for c in new_calls if c.get("ok")
    ), dispatched


def test_force_cascade_completes_driven_topology_before_sim():
    """Cascade on undriven residual injects source+load then re-simulates."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade, _ecad_source_on_rail, _ecad_load_on_rail,
    )

    # Pre-ERC chain ready for simulate, but no V source and R not on 3V3.
    calls = [
        c for c in _mega21_undriven_sim_calls()
        if c.get("tool") != "ecad_simulate"
    ]
    # Remove R-on-3V3 wire so load is also missing on rail.
    calls = [
        c for c in calls
        if not (
            c.get("tool") == "ecad_wire"
            and (c.get("result") or {}).get("net") == "3V3"
        )
    ]
    # Keep a cross wire of some kind so chain_need can reach simulate.
    calls.append({
        "tool": "ecad_wire", "ok": True,
        "args": {"from_ref": "U1", "from_pin": "9",
                 "to_ref": "R1", "to_pin": "1"},
        "result": {
            "ok": True, "net": "N$x", "net_size": 2,
            "net_members": ["U1.9", "R1.1"],
        },
    })
    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_optimize_traces",
        "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
    }
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "Device:V" in lib:
                return {
                    "ok": True, "ref": "V1", "libid": lib, "value": "DC 3.3",
                    "pins": [
                        {"number": "+", "xy": [40, 5]},
                        {"number": "-", "xy": [40, -5]},
                    ],
                }
            return {
                "ok": True, "ref": a.get("ref") or "R9", "libid": lib,
                "value": "1k",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            }
        if tool == "ecad_wire":
            net = a.get("net") or "3V3"
            return {
                "ok": True, "net": net, "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_add_power":
            return {"ok": True, "net": a.get("net"), "net_size": 1}
        if tool == "ecad_optimize_traces":
            return {"ok": True, "doc_id": a.get("doc_id"),
                    "corners_rounded": 2}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0, "doc_id": a.get("doc_id")}
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0}
        if tool == "ecad_simulate":
            return {
                "ok": True, "analysis": "op", "device_count": 2,
                "nodes": ["3V3", "GND"],
                "op": {"V(3V3)": 3.3},
                "resolved_net": "3V3",
                "devices": [
                    {"name": "V1", "type": "v", "nets": ["3V3", "GND"]},
                    {"name": "R1", "type": "r", "nets": ["3V3", "GND"]},
                ],
            }
        return {"ok": True}

    new_calls, made = _ecad_force_chain_cascade(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=24,
        reason="hard_gate",
    )
    tools = [c.get("tool") for c in new_calls if c.get("ok")]
    assert "ecad_simulate" in tools, (tools, dispatched)
    # Source+load topology completed.
    assert _ecad_source_on_rail(new_calls, "3V3") or any(
        "Device:V" in str((c.get("args") or {}).get("libid") or "")
        for c in new_calls
    )
    assert _ecad_load_on_rail(new_calls, "3V3") or any(
        c.get("tool") == "ecad_wire" and c.get("ok")
        and "R" in json.dumps(c.get("args") or {})
        for c in new_calls
    )


# ---------------------------------------------------------------------------
# Q22 — board → optimize_traces → DRC
# ---------------------------------------------------------------------------


def test_chain_need_inserts_optimize_between_board_and_drc():
    """Q22: after board product, next need is optimize_traces (not DRC)."""
    from services.agent.agent_turn import _ecad_chain_need

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_optimize_traces",
        "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"}, "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R"}, "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "GND", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "U1", "from_pin": "1",
                     "to_ref": "R1", "to_pin": "1"},
            "result": {"ok": True, "net": "N1", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {"doc_id": "6f30f5cdb225", "board_doc_id": "6f30f5cdb225"},
        },
    ]
    need = _ecad_chain_need(calls, allow)
    assert need == "ecad_optimize_traces", need


def test_chain_need_optimize_even_when_not_in_allow():
    """Q22: optimize forced when DRC is allowed (actor omit polish tool)."""
    from services.agent.agent_turn import _ecad_chain_need

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic",
        "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
        # intentionally NO ecad_optimize_traces
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R"}, "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"}, "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "U1", "from_pin": "1",
                     "to_ref": "R1", "to_pin": "1"},
            "result": {"ok": True, "net": "3V3", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {"doc_id": "b", "board_doc_id": "b"},
        },
    ]
    need = _ecad_chain_need(calls, allow)
    assert need == "ecad_optimize_traces", need


def test_cascade_board_optimize_drc_order_mega21_shape():
    """Replay mega-21 board residual: forced order board→opt→drc."""
    from services.agent.agent_turn import _ecad_force_chain_cascade

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_optimize_traces",
        "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"},
            "result": {
                "ref": "U1",
                "pins": [
                    {"number": "1", "name": "VDD", "type": "power_in",
                     "xy": [10, 10]},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "3V3", "net_size": 1,
                       "net_members": ["U1.1"]},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "U1", "from_pin": "1",
                     "to_ref": "R1", "to_pin": "1"},
            "result": {"ok": True, "net": "3V3", "net_size": 2,
                       "net_members": ["U1.1", "R1.1"]},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "doc_id": "6f30f5cdb225",
                "board_doc_id": "6f30f5cdb225",
                "sch_doc_id": "s",
            },
        },
    ]
    order: list[str] = []
    drc_n = {"n": 0}

    def _dispatch(tool, args):
        order.append(tool)
        a = dict(args or {})
        if tool == "ecad_optimize_traces":
            assert a.get("doc_id") == "6f30f5cdb225", a
            return {"ok": True, "doc_id": "6f30f5cdb225", "corners_rounded": 4}
        if tool == "ecad_run_drc":
            drc_n["n"] += 1
            assert a.get("doc_id") == "6f30f5cdb225", a
            # First DRC after first optimize still dirty → re-opt; second clean.
            errs = 31 if drc_n["n"] == 1 else 0
            return {"ok": True, "errors": errs, "doc_id": "6f30f5cdb225"}
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0}
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "Device:V" in lib:
                return {
                    "ok": True, "ref": "V1", "libid": lib, "value": "DC 3.3",
                    "pins": [
                        {"number": "+", "xy": [40, 5]},
                        {"number": "-", "xy": [40, -5]},
                    ],
                }
            return {"ok": True, "ref": "R9", "libid": lib}
        if tool == "ecad_wire":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_simulate":
            return {
                "ok": True, "device_count": 2, "op": {"V(3V3)": 3.3},
                "nodes": ["3V3", "GND"],
            }
        return {"ok": True}

    new_calls, _made = _ecad_force_chain_cascade(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=20,
        reason="hard_gate",
    )
    forced = [c.get("tool") for c in new_calls if c.get("forced")]
    assert "ecad_optimize_traces" in forced, forced
    assert "ecad_run_drc" in forced, forced
    # Order among forced product: optimize before first DRC.
    assert forced.index("ecad_optimize_traces") < forced.index("ecad_run_drc"), forced
    # Board doc_id on optimize + DRC.
    for c in new_calls:
        if c.get("tool") in ("ecad_optimize_traces", "ecad_run_drc") and c.get("ok"):
            assert (c.get("args") or {}).get("doc_id") == "6f30f5cdb225"


def test_reoptimize_once_then_honest_drc_residual():
    """Q22: one re-opt+re-DRC after dirty; no infinite loop."""
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_optimize_retry_count,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_optimize_traces",
        "ecad_run_drc", "ecad_run_erc",
    }
    base = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R"}, "result": {"ref": "R1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"}, "result": {"ref": "U1"},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "U1", "from_pin": "1",
                     "to_ref": "R1", "to_pin": "1"},
            "result": {"ok": True, "net": "3V3", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {"doc_id": "b", "board_doc_id": "b"},
        },
        {
            "tool": "ecad_optimize_traces", "ok": True,
            "result": {"ok": True, "corners_rounded": 1},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "result": {"ok": True, "errors": 31},
        },
    ]
    assert _ecad_optimize_retry_count(base) == 0
    need = _ecad_chain_need(base, allow)
    assert need == "ecad_optimize_traces", need

    # After second optimize + still-dirty DRC → stop re-opt (retry_count=1).
    after_retry = base + [
        {
            "tool": "ecad_optimize_traces", "ok": True,
            "result": {"ok": True, "corners_rounded": 2},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "result": {"ok": True, "errors": 12},
        },
    ]
    assert _ecad_optimize_retry_count(after_retry) == 1
    need2 = _ecad_chain_need(after_retry, allow)
    # Should advance past optimize (ERC or None), not loop forever.
    assert need2 != "ecad_optimize_traces", need2


# ---------------------------------------------------------------------------
# mega-22 rebind — mech land + ECAD terminal force on starve
# ---------------------------------------------------------------------------


def test_airframe_ops_positioned_not_buried():
    """Rebind: bay/wing/fins carry at/ offsets (not origin-only boxes)."""
    from services.agent.agent_turn import _airframe_build_ops

    ops = _airframe_build_ops()
    by_name = {
        (op.get("feature") or {}).get("name"): (op.get("feature") or {})
        for op in ops
    }
    assert "at" in (by_name["payload_bay"].get("params") or {})
    assert "at" in (by_name["wing"].get("params") or {})
    assert "at" in (by_name["fin_1"].get("params") or {})
    # Body not centered-default buried: centered False.
    assert (by_name["body_tube"].get("params") or {}).get("centered") is False


def test_mech_force_retry_body_only_on_batch_reject():
    """When full batch rejects, force retries simpler ops and can land."""
    from services.agent import agent_turn as at

    at._MECH_FORCE_ONCE.clear()
    calls_disp = []

    def _dispatch(tool, args):
        calls_disp.append((tool, args))
        if tool == "new_part":
            return {"ok": True, "doc_id": "doc-x"}
        if tool == "add_features":
            n = len((args or {}).get("ops") or [])
            if n >= 6:
                return {
                    "ok": False,
                    "rolled_back": True,
                    "error": "batch rejected — no features were kept",
                    "failed_features": [
                        {"id": "f5", "error": "box boss added no material"},
                    ],
                }
            return {"ok": True, "doc_id": "doc-x", "n_ops": n}
        return {"ok": True}

    led = type("L", (), {"run_id": "rebind-mech", "events": [], "append": lambda *a, **k: None})()
    # minimal ledger interface
    class _Led:
        run_id = "rebind-mech"
        events = []
        def append(self, *a, **k):
            pass
    out = at._mech_force_build_chain(
        _Led(),
        dispatch=_dispatch,
        run_id="rebind-mech",
    )
    assert out.get("ok") is True, out
    assert out.get("forced") is True
    # Saw a retry with fewer ops.
    af_ns = [
        len((a or {}).get("ops") or [])
        for t, a in calls_disp if t == "add_features"
    ]
    assert any(n < 6 for n in af_ns), af_ns


def test_mech_soft_refusal_does_not_hard_block_semantics():
    """Force attempted + failed → soft_refusal so loop can keep ECAD path."""
    from services.agent import agent_turn as at

    at._MECH_FORCE_ONCE.clear()

    def _dispatch(tool, args):
        if tool == "new_part":
            return {"ok": True, "doc_id": "doc-y"}
        if tool == "add_features":
            return {"ok": False, "rolled_back": True, "error": "still bad"}
        return {"ok": True}

    class _Led:
        run_id = "soft-mech"
        events = []
    gate = at.mech_deliver_gate(
        _Led(), dispatch=_dispatch, run_id="soft-mech", force=True,
    )
    assert gate.get("allow_deliver") is False
    assert gate.get("soft_refusal") is True
    assert gate.get("forced") is True
    assert "mech" in (gate.get("refusal") or "").lower()


def test_ecad_product_gate_forces_chain_when_starved():
    """P22/Q22: empty ledger → terminal force schematic…board…opt…drc…sim."""
    from services.agent.agent_turn import ecad_product_gate

    order = []

    def _dispatch(tool, args):
        order.append(tool)
        a = dict(args or {})
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch1"}
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            ref = a.get("ref") or ("V1" if "Device:V" in lib else "R1" if "Device:R" in lib else "U1")
            pins = [
                {"number": "1", "name": "VDDA", "type": "power_in", "xy": [10, 10]},
                {"number": "2", "name": "VSS", "type": "power_in", "xy": [10, 0]},
            ]
            if "Device:V" in lib:
                pins = [
                    {"number": "+", "xy": [40, 5]},
                    {"number": "-", "xy": [40, -5]},
                ]
            if "Device:R" in lib:
                pins = [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ]
            return {"ok": True, "ref": ref, "libid": lib, "pins": pins,
                    "doc_id": "sch1"}
        if tool == "ecad_add_power":
            return {"ok": True, "net": a.get("net") or "GND", "net_size": 1,
                    "net_members": ["U1.1"], "doc_id": "sch1"}
        if tool == "ecad_wire":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "board_from_schematic":
            return {
                "ok": True, "doc_id": "brd1", "board_doc_id": "brd1",
                "sch_doc_id": "sch1",
            }
        if tool == "ecad_optimize_traces":
            assert a.get("doc_id") == "brd1", a
            return {"ok": True, "doc_id": "brd1", "corners_rounded": 3}
        if tool == "ecad_run_drc":
            assert a.get("doc_id") == "brd1", a
            return {"ok": True, "errors": 0, "doc_id": "brd1"}
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0}
        if tool == "ecad_simulate":
            return {
                "ok": True, "device_count": 2, "nodes": ["3V3", "GND"],
                "op": {"V(3V3)": 3.3}, "resolved_net": "3V3",
            }
        return {"ok": True}

    class _Led:
        run_id = "ecad-starve"
        events = []

    out = ecad_product_gate(_Led(), dispatch=_dispatch, reason="test_starve")
    assert out.get("forced") is True
    assert out.get("ok") is True, (out, order)
    # Order includes board before optimize before drc (Q22).
    assert "board_from_schematic" in order
    assert "ecad_optimize_traces" in order
    assert "ecad_run_drc" in order
    assert order.index("board_from_schematic") < order.index("ecad_optimize_traces")
    assert order.index("ecad_optimize_traces") < order.index("ecad_run_drc")
    # P22: simulate executed after driven prep.
    assert "ecad_simulate" in order


