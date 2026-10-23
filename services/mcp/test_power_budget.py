"""Tests for the system power/energy budget (services/mcp/power_budget.py).

Groups:
  1. Sensor-node fixture — sleep/wake duty cycle via an aicad.powerprofile/1 artifact;
     battery life must match the hand calc within tolerance.
  2. Converter overload — a seeded over-rating fixture is flagged (verdict=fail,
     negative margin, worst rail).
  3. Degraded mode — no firmware profile → datasheet-typ, always-active, LABELLED;
     unknown currents stay null and land in coverage.
  4. Efficiency curves, PDN cross-check, sequencing note, verify_battery integration.

Pure Python — no engine kernel, no FastMCP server required.

Run: pytest services/mcp/test_power_budget.py -q
"""
from __future__ import annotations

import json

import pytest

from services.mcp.power_budget import solve_power_budget, SCHEMA, DEGRADED_LABEL


# ── fixtures ──────────────────────────────────────────────────────────────────

def _battery(cap_mah=2400.0, v=3.6, i_max=None):
    d = {"kind": "battery", "name": "VBAT", "voltage_v": v, "capacity_mah": cap_mah}
    if i_max is not None:
        d["i_max_a"] = i_max
    return d


def _buck(eta=0.90, i_max=0.5):
    return {"name": "U_BUCK", "input": "VBAT", "output": "3V3",
            "voltage_out_v": 3.3, "topology": "buck",
            "efficiency": eta, "i_out_max_a": i_max}


def _sensor_board(mcu_active=0.020, mcu_sleep=5e-6, sensor_active=0.005,
                  sensor_sleep=None, sequencing=None):
    """Minimal aicad.boardphys/1 sidecar: MCU + sensor on rail 3V3."""
    return {
        "schema": "aicad.boardphys/1",
        "board": {"name": "sensor-node"},
        "components": [
            {"ref": "U1", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": mcu_active,
                           "current_a_sleep": mcu_sleep}],
                "voltage_domains": ["3V3"],
                "sequencing": [] if sequencing is None else sequencing}},
            {"ref": "U2", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": sensor_active,
                           "current_a_sleep": sensor_sleep}],
                "voltage_domains": ["3V3"], "sequencing": []}},
        ],
    }


def _profile(i_avg, i_peak, duty=0.01):
    """Hand-built aicad.powerprofile/1 rail entry (contracts/firmware_rpc.md §11)."""
    return {
        "schema": "aicad.powerprofile/1",
        "model": "duty-cycle bookkeeping",
        "trace_ref": "/tmp/fw.trace.json",
        "cpu": {"active_frac": duty, "sleep_frac": 1 - duty},
        "pins": {},
        "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                   "i_avg_a": i_avg, "i_peak_a": i_peak,
                   "residency": {"active_frac": duty, "sleep_frac": 1 - duty,
                                 "active_s": None, "sleep_s": None},
                   "schedule": {"duty": duty, "period_s": 1.0},
                   "provenance": [f"U1: duty {duty} (cpu_markers), "
                                  "i_active=0.02 A, i_sleep=5e-06 A"]}],
        "coverage": {"components_total": 2, "components_with_rails": 2,
                     "unknown_active_current": [], "notes": []},
    }


# ── 1. sensor node: battery life vs hand calc ─────────────────────────────────

def test_sensor_node_battery_life_matches_hand_calc():
    # Duty cycle 1%: MCU 20 mA active / 5 uA sleep, sensor 5 mA active / 0 sleep.
    duty = 0.01
    i_avg = duty * (0.020 + 0.005) + (1 - duty) * 5e-6        # rail avg, hand calc
    i_peak = 0.025
    res = solve_power_budget(
        _battery(), [_buck(eta=0.90)], board=_sensor_board(),
        power_profile=_profile(i_avg, i_peak, duty), scenario="field deployment")

    assert res["ok"] and res["schema"] == SCHEMA
    assert res["degraded"] is False

    # Hand calc: i_in = P_out / (eta * V_in); life = capacity_ah / i_in.
    i_in = (3.3 * i_avg) / (0.90 * 3.6)
    life_h = 2.4 / i_in
    assert res["battery"]["life_h"] == pytest.approx(life_h, rel=0.01)
    assert res["battery"]["i_source_avg_a"] == pytest.approx(i_in, rel=1e-6)
    assert "aicad.powerprofile/1" in res["battery"]["note"]

    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["mode"] == "profile"
    assert rail["i_avg_a"] == pytest.approx(i_avg)
    assert rail["converter"]["overload"] is False
    assert rail["converter"]["margin_frac"] == pytest.approx(1 - i_peak / 0.5, abs=1e-4)
    # every number traceable
    assert any("aicad.powerprofile/1" in p for p in rail["provenance"])
    assert res["verdict"] == "ok"


# ── 2. converter overload seeded fixture ──────────────────────────────────────

def test_converter_overload_flagged():
    # Peak 0.8 A through a 0.5 A buck → fail, negative margin, worst rail named.
    res = solve_power_budget(
        _battery(), [_buck(eta=0.90, i_max=0.5)], board=_sensor_board(),
        power_profile=_profile(0.4, 0.8))
    assert res["verdict"] == "fail" and res["ok"] is False
    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["converter"]["overload"] is True
    assert rail["converter"]["margin_frac"] < 0
    assert res["worst_rail"] == {"rail": "3V3",
                                 "margin_frac": rail["converter"]["margin_frac"]}
    assert any("exceeds" in w and "U_BUCK" in w for w in res["warnings"])
    assert any(f["severity"] == "fail" for f in res["findings"])


def test_source_rating_exceeded_is_fail():
    res = solve_power_budget(
        _battery(i_max=0.1), [_buck(i_max=2.0)], board=_sensor_board(),
        power_profile=_profile(0.4, 0.8))
    assert res["verdict"] == "fail"
    assert any("source VBAT" in w for w in res["warnings"])


# ── 3. degraded mode (no firmware profile) ────────────────────────────────────

def test_degraded_mode_labelled_and_conservative():
    res = solve_power_budget(_battery(), [_buck()], board=_sensor_board())
    assert res["degraded"] is True
    assert any("DEGRADED MODE" in n for n in res["coverage"]["notes"])
    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["mode"] == "degraded"
    # Always-active sum: 20 mA + 5 mA, and the label is on every contributor line.
    assert rail["i_avg_a"] == pytest.approx(0.025)
    assert sum(DEGRADED_LABEL in p for p in rail["provenance"]) == 2
    assert "conservative" in res["battery"]["note"]


def test_degraded_unknown_current_stays_null_and_floor_labelled():
    board = _sensor_board(sensor_active=None)  # U2 draw unknown
    res = solve_power_budget(_battery(), [_buck()], board=board)
    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["i_avg_a"] == pytest.approx(0.020)  # floor, U2 omitted — not 0-invented
    assert "U2:3V3" in res["coverage"]["unknown_active_current"]
    assert any("FLOOR" in p for p in rail["provenance"])
    assert "UPPER BOUND" in res["battery"]["note"]


def test_all_unknown_rail_currents_are_null_never_zero():
    board = _sensor_board(mcu_active=None, sensor_active=None)
    res = solve_power_budget(_battery(), [_buck()], board=board)
    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["i_avg_a"] is None and rail["i_peak_a"] is None
    assert res["battery"]["life_h"] is None
    # unknown demand against a rated converter is called out
    assert any("cannot prove" in w for w in res["warnings"])


# ── 4. efficiency curves / LDO / chained tree ────────────────────────────────

def test_efficiency_curve_interpolation():
    cv = _buck(eta=[[0.1, 0.80], [0.5, 0.90], [1.0, 0.85]], i_max=0.5)
    # i_avg 0.15 A → load_frac 0.3 → eta = 0.85 (midpoint of 0.80..0.90)
    res = solve_power_budget(_battery(), [cv], power_profile=_profile(0.15, 0.15))
    i_in = (3.3 * 0.15) / (0.85 * 3.6)
    assert res["source"]["i_avg_a"] == pytest.approx(i_in, rel=1e-6)


def test_ldo_input_equals_output_plus_iq():
    cv = {"name": "U_LDO", "input": "VBAT", "output": "3V3", "voltage_out_v": 3.3,
          "topology": "ldo", "i_out_max_a": 0.3, "i_q_a": 50e-6}
    res = solve_power_budget(_battery(), [cv], power_profile=_profile(0.010, 0.010))
    assert res["source"]["i_avg_a"] == pytest.approx(0.010 + 50e-6)


def test_chained_converters_accumulate_to_source():
    # VBAT → buck 3V3 → ldo 1V8; load only on 1V8.
    buck = _buck(eta=0.9, i_max=1.0)
    ldo = {"name": "U_LDO", "input": "3V3", "output": "1V8", "voltage_out_v": 1.8,
           "topology": "ldo", "i_out_max_a": 0.2}
    profile = _profile(0.0, 0.0)
    profile["rails"] = [{"name": "1V8", "voltage_v_nom": 1.8, "i_avg_a": 0.05,
                         "i_peak_a": 0.05, "residency": {}, "schedule": None,
                         "provenance": ["U3: assumed always active"]}]
    res = solve_power_budget(_battery(), [buck, ldo], power_profile=profile)
    # LDO passes 0.05 A through; buck input = 3.3*0.05/(0.9*3.6)
    assert res["source"]["i_avg_a"] == pytest.approx(3.3 * 0.05 / (0.9 * 3.6), rel=1e-6)
    names = {r["name"] for r in res["rails"]}
    assert {"3V3", "1V8"} <= names


def test_converter_cycle_rejected():
    a = {"name": "A", "input": "R2", "output": "R1", "voltage_out_v": 1.0,
         "topology": "ldo"}
    b = {"name": "B", "input": "R1", "output": "R2", "voltage_out_v": 1.0,
         "topology": "ldo"}
    res = solve_power_budget(_battery(), [a, b])
    assert res["ok"] is False and "cycle" in res["error"]


# ── 5. PDN cross-check + sequencing ───────────────────────────────────────────

def _pdn_block(modelled_i=0.025, within=True, hotspots=0):
    return {"schema": "aicad.pdn/1", "generated_by": "engine.ecad.pdn.solve_pdn",
            "nets": [{"net": "3V3", "vsrc": 3.3, "sources": ["U_BUCK"],
                      "drops": [{"ref": "U1", "i_a": modelled_i, "v_pad": 3.29,
                                 "drop_v": 0.01, "v_min": 3.0,
                                 "within_tolerance": within, "connected": True}],
                      "worst_drop": {"ref": "U1", "i_a": modelled_i, "v_pad": 3.29,
                                     "within_tolerance": within},
                      "hotspots": [{"layer": "F.Cu", "j_a_mm2": 40.0}] * hotspots,
                      "violations": [], "unknown_sinks": []}]}


def test_pdn_cross_check_stale_analysis_warns():
    board = _sensor_board()
    board["pdn"] = _pdn_block(modelled_i=0.005)  # PDN modelled far less than budget peak
    res = solve_power_budget(_battery(), [_buck()], board=board,
                             power_profile=_profile(0.0003, 0.025))
    rail = next(r for r in res["rails"] if r["name"] == "3V3")
    assert rail["pdn_check"]["modelled_i_a"] == pytest.approx(0.005)
    assert any("re-run ecad_run_pdn" in w for w in res["warnings"])


def test_pdn_drop_and_hotspots_warn():
    board = _sensor_board()
    board["pdn"] = _pdn_block(modelled_i=0.025, within=False, hotspots=2)
    res = solve_power_budget(_battery(), [_buck()], board=board,
                             power_profile=_profile(0.0003, 0.025))
    assert any("copper drop violation" in w for w in res["warnings"])
    assert any("hotspot" in w for w in res["warnings"])
    assert res["verdict"] == "warn"


def test_sequencing_notes():
    ok = solve_power_budget(_battery(), [_buck()], board=_sensor_board())
    assert "known-none" in ok["sequencing_note"]
    seq = solve_power_budget(_battery(), [_buck()],
                             board=_sensor_board(sequencing=["VCORE before VIO"]))
    assert "VCORE before VIO" in seq["sequencing_note"]
    board = _sensor_board()
    board["components"][0]["supply"]["sequencing"] = None
    unk = solve_power_budget(_battery(), [_buck()], board=board)
    assert "UNKNOWN" in unk["sequencing_note"]


# ── 6. verify_battery integration ─────────────────────────────────────────────

def test_verify_battery_power_budget_check():
    from services.verify.battery import verify, CHECKS
    assert "power_budget" in CHECKS
    res = verify(doc_id=None, checks=["power_budget"],
                 power_source=_battery(),
                 power_converters=[_buck(i_max=0.5)],
                 power_board=_sensor_board(),
                 power_profile=_profile(0.4, 0.8))   # seeded overload
    assert res["scores"]["power_budget"] < 1.0
    assert any(f["check"] == "power_budget" for f in res["findings"])


def test_verify_battery_power_budget_skips_without_source():
    from services.verify.battery import verify
    res = verify(doc_id=None, checks=["power_budget"])
    assert res["scores"]["power_budget"] == 1.0
    assert res["findings"] == []


def test_mcp_tool_roundtrip(tmp_path):
    """The registered MCP tool loads artifacts from disk and returns JSON."""
    from mcp.server.fastmcp import FastMCP
    from services.mcp.power_budget import register_power_budget_tools
    mcp = FastMCP("t")
    register_power_budget_tools(mcp)
    fn = mcp._tool_manager._tools["power_budget"].fn
    bp = tmp_path / "board.boardphys.json"
    pp = tmp_path / "fw.power.json"
    bp.write_text(json.dumps(_sensor_board()))
    pp.write_text(json.dumps(_profile(0.000255, 0.025)))
    out = json.loads(fn(source=_battery(), converters=[_buck()],
                        board_path=str(bp), power_profile_path=str(pp),
                        scenario="sensor node"))
    assert out["ok"] and out["schema"] == SCHEMA
    assert out["battery"]["life_h"] is not None
