"""Mega-18 fixes — Z2r force-EXECUTE add_power + net-aware wire; Z4 AC5 null FAIL.

Axes:
  Z2r — parts placed but no power symbols force-EXECUTES ecad_add_power (cascade,
        not schedule-only); returned refs feed ecad_wire; self-loop guard rejects
        same-ref pairs when ≥2 live refs. Replay mega-17 ledger fixture.
  Z4  — AC5 grader reads real ecad_simulate payload; null V(3V3) → FAIL naming
        missing net, never NOT-CHECKED when the tool ran.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_MEGA17_LEDGER = (
    Path(__file__).resolve().parents[2]
    / "reports/runs/mega-17/attempt-2-primary-graded-z2fail/ledger.json"
)


def _mega17_ecad_calls() -> list[dict]:
    """Reconstruct ECAD call records from mega-17 a2 ledger."""
    if not _MEGA17_LEDGER.is_file():
        pytest.skip(f"mega-17 ledger fixture missing: {_MEGA17_LEDGER}")
    raw = json.loads(_MEGA17_LEDGER.read_text(encoding="utf-8"))
    events = raw if isinstance(raw, list) else raw.get("events", [])
    calls: list[dict] = []
    for e in events:
        pl = e.get("payload") or e
        if pl.get("event") != "tool_call":
            continue
        tool = pl.get("tool")
        if not tool:
            continue
        if not (
            str(tool).startswith("ecad_") or tool in ("board_from_schematic",)
        ):
            continue
        if "ok" not in pl:
            continue
        rec = {
            "tool": tool,
            "args": pl.get("args") or {},
            "ok": bool(pl.get("ok")),
            "result": pl.get("result"),
        }
        if isinstance(rec["result"], str):
            try:
                rec["result"] = json.loads(rec["result"])
            except Exception:  # noqa: BLE001
                pass
        calls.append(rec)
    return calls


def _synthetic_mega17_residual_calls() -> list[dict]:
    """Canonical mega-17 residual: U1 + R1 self-loop, no add_power."""
    return [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "args": {"name": "RocketFlightComputer"},
            "result": {"ok": True, "doc_id": "fb6e6e4b586d"},
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {
                "libid": "Sensor_Pressure:BMP280",
                "x": 0, "y": 0, "value": "BMP280",
            },
            "result": {
                "ok": True,
                "doc_id": "fb6e6e4b586d",
                "ref": "U1",
                "libid": "Sensor_Pressure:BMP280",
                "pins": [
                    {
                        "number": "1", "name": "VDDIO", "type": "power_in",
                        "xy": [-8.89, 5.08],
                    },
                    {
                        "number": "2", "name": "GND", "type": "power_in",
                        "xy": [-8.89, 2.54],
                    },
                    {
                        "number": "3", "name": "SDA", "type": "bidirectional",
                        "xy": [8.89, 5.08],
                    },
                ],
            },
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {"libid": "Device:R", "value": "1k", "x": 20.0, "y": 20.0},
            "result": {
                "ok": True,
                "doc_id": "fb6e6e4b586d",
                "ref": "R1",
                "libid": "Device:R",
                "value": "1k",
                "pins": [
                    {"number": "1", "name": "~", "type": "passive",
                     "xy": [14.92, 20.0]},
                    {"number": "2", "name": "~", "type": "passive",
                     "xy": [25.08, 20.0]},
                ],
            },
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {
                "from_ref": "R1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "2",
            },
            "result": {
                "ok": True, "net": "N$9", "net_size": 2,
                "net_members": ["R1.1", "R1.2"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Z2r — force-EXECUTE add_power + net-aware wire (no self-loop)
# ---------------------------------------------------------------------------


def test_mega17_residual_chain_need_is_add_power():
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_has_cross_wire, _ecad_nets_ready_for_board,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    calls = _synthetic_mega17_residual_calls()
    assert _ecad_has_cross_wire(calls) is False
    assert _ecad_nets_ready_for_board(calls, allow) is False
    assert _ecad_chain_need(calls, allow) == "ecad_add_power"


def test_force_cascade_executes_add_power_then_cross_wire():
    """Cascade must EXECUTE add_power (not schedule-only) then valid wire."""
    from services.agent.agent_turn import _ecad_force_chain_cascade

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = _synthetic_mega17_residual_calls()
    dispatched: list[tuple[str, dict]] = []
    power_n = {"n": 0}

    def _dispatch(tool, args):
        dispatched.append((tool, dict(args or {})))
        if tool == "ecad_add_power":
            power_n["n"] += 1
            net = (args or {}).get("net") or "GND"
            # Bond when forced onto a pin xy (mega-18 residual fix).
            out = {
                "ok": True, "net": net, "net_size": 2, "net_members": ["U1.1"],
                "pos": [(args or {}).get("x", 0), (args or {}).get("y", 0)],
                "ref": f"#PWR{power_n['n']:02d}",
            }
            return out
        if tool == "ecad_place_symbol":
            return {
                "ok": True, "ref": "R1", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            }
        if tool == "ecad_wire":
            a = args or {}
            assert a.get("from_ref") != a.get("to_ref"), (
                f"self-loop not allowed in cascade: {a}"
            )
            return {
                "ok": True, "net": "N_PWR", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb-z2r", "board_doc_id": "pcb-z2r"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    new_calls, made = _ecad_force_chain_cascade(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=12,
        reason="z2r_replay",
    )
    forced = [c for c in new_calls if c.get("forced")]
    forced_ok = [c.get("tool") for c in forced if c.get("ok")]
    assert "ecad_add_power" in forced_ok, forced
    assert any(t == "ecad_add_power" for t, _ in dispatched), dispatched
    # At least one successful cross-ref wire (not residual self-loop).
    wires = [
        c for c in new_calls
        if c.get("tool") == "ecad_wire" and c.get("ok")
        and (c.get("args") or {}).get("from_ref")
        != (c.get("args") or {}).get("to_ref")
    ]
    assert wires, new_calls
    assert made >= 2


def test_wire_self_loop_guard_rederives():
    from services.agent.agent_turn import (
        _ecad_reject_self_loop_args, _ecad_sanitize_wire_args,
    )

    calls = _synthetic_mega17_residual_calls()[:3]  # sch + U1 + R1
    bad = {
        "from_ref": "R1", "from_pin": "1",
        "to_ref": "R1", "to_pin": "2",
    }
    fixed = _ecad_reject_self_loop_args(bad, calls)
    assert fixed["from_ref"] != fixed["to_ref"], fixed
    assert {fixed["from_ref"], fixed["to_ref"]} <= {"U1", "R1"}

    fixed2 = _ecad_sanitize_wire_args(bad, calls)
    assert fixed2["from_ref"] != fixed2["to_ref"], fixed2


def test_forced_wire_after_power_prefers_power_pin():
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = _synthetic_mega17_residual_calls()[:3] + [{
        "tool": "ecad_add_power",
        "ok": True,
        "args": {"net": "GND", "x": -8.89, "y": 2.54},
        "result": {
            "ok": True, "net": "GND", "ref": "#PWR01",
            "net_size": 0, "pin": "1",
        },
    }]
    args = _ecad_forced_tool_args("ecad_wire", calls)
    assert args["from_ref"] != args["to_ref"], args
    assert "#PWR01" in {args["from_ref"], args["to_ref"]}, args


def test_mega17_ledger_replay_produces_add_power_and_valid_wire():
    """Replay ledger residual: cascade yields add_power + non-self wire."""
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_force_chain_cascade, _ecad_forced_tool_args,
        _ecad_last_place_refs,
    )

    try:
        ledger_calls = _mega17_ecad_calls()
    except Exception:  # noqa: BLE001
        ledger_calls = []
    residual: list[dict] = []
    for c in ledger_calls:
        if c.get("tool") in (
            "board_from_schematic", "ecad_run_drc",
            "ecad_run_erc", "ecad_simulate",
        ):
            break
        residual.append(c)
    if not residual or not any(
        c.get("tool") == "ecad_place_symbol" and c.get("ok") for c in residual
    ):
        residual = _synthetic_mega17_residual_calls()

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    residual = [c for c in residual if c.get("tool") != "ecad_add_power"]
    # Self-loop residual must not unlock board; need add_power.
    assert _ecad_chain_need(residual, allow) == "ecad_add_power"
    # Truncated U1 result should still yield ≥2 refs after harvest fix.
    assert len(_ecad_last_place_refs(residual)) >= 2 or sum(
        1 for c in residual
        if c.get("ok") and c.get("tool") == "ecad_place_symbol"
    ) >= 2

    def _dispatch(tool, args):
        if tool == "ecad_add_power":
            return {
                "ok": True,
                "net": (args or {}).get("net") or "GND",
                "ref": "#PWRGND",
                "net_size": 2,
                "net_members": ["U1.2"],
                "pos": [
                    (args or {}).get("x", 0),
                    (args or {}).get("y", 0),
                ],
            }
        if tool == "ecad_wire":
            a = args or {}
            if a.get("from_ref") == a.get("to_ref"):
                return {
                    "ok": False,
                    "error": "self-loop rejected by test harness",
                }
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {
                "ok": True, "doc_id": "pcb-replay",
                "board_doc_id": "pcb-replay",
            }
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        if tool == "ecad_place_symbol":
            return {
                "ok": True, "ref": "R99", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            }
        return {"ok": True}

    new_calls, _made = _ecad_force_chain_cascade(
        calls=list(residual),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="replay",
        messages=[],
        tool_calls_made=0,
        budget=10,
        reason="z2r_ledger_replay",
    )
    ok_tools = [c.get("tool") for c in new_calls if c.get("ok")]
    assert "ecad_add_power" in ok_tools, ok_tools
    cross_wires = [
        c for c in new_calls
        if c.get("tool") == "ecad_wire" and c.get("ok")
        and (c.get("args") or {}).get("from_ref")
        != (c.get("args") or {}).get("to_ref")
    ]
    assert cross_wires, [
        (c.get("tool"), c.get("ok"), c.get("args"), c.get("forced"))
        for c in new_calls
    ]

    after_power = residual + [{
        "tool": "ecad_add_power", "ok": True,
        "args": {"net": "GND"},
        "result": {"ok": True, "net": "GND", "ref": "#PWRGND"},
    }]
    wargs = _ecad_forced_tool_args("ecad_wire", after_power)
    assert wargs.get("from_ref") != wargs.get("to_ref"), wargs


def test_live_force_inject_turn_hits_add_power(tmp_path, monkeypatch):
    """End-to-end turn: thrash place → hard gate cascade executes add_power."""
    from services.agent.ledger import Ledger
    from services.agent import agent_turn as at

    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    ledger = Ledger("mega18_z2r", path=tmp_path / "ledger.json")
    seq = {"n": 0}
    refs = {"n": 0}

    class _Prov:
        name = "mock"
        model = "mock"

        def complete(self, system, messages, max_tokens=8000):
            seq["n"] += 1
            if seq["n"] == 1:
                return (
                    '```json\n{"tool":"ecad_new_schematic",'
                    '"args":{"name":"fc"}}\n```'
                )
            if seq["n"] == 2:
                return (
                    '```json\n{"tool":"ecad_place_symbol",'
                    '"args":{"libid":"Sensor_Pressure:BMP280"}}\n```'
                )
            if seq["n"] <= 10:
                return (
                    '```json\n{"tool":"ecad_place_symbol",'
                    '"args":{"libid":"Device:C"}}\n```'
                )
            return "done"

    def _dispatch(tool, args):
        if tool == "ecad_new_schematic":
            return {"ok": True, "doc_id": "sch-m18"}
        if tool == "ecad_place_symbol":
            refs["n"] += 1
            lib = str((args or {}).get("libid") or "")
            if "Device:R" in lib:
                return {
                    "ok": True, "ref": f"R{refs['n']}", "libid": lib,
                    "value": "1k",
                    "pins": [
                        {"number": "1", "xy": [20, 20]},
                        {"number": "2", "xy": [30, 20]},
                    ],
                }
            if "Device:C" in lib:
                return {
                    "ok": True, "ref": f"C{refs['n']}", "libid": lib,
                    "pins": [
                        {"number": "1", "xy": [40, 20]},
                        {"number": "2", "xy": [50, 20]},
                    ],
                }
            return {
                "ok": True, "ref": f"U{refs['n']}", "libid": lib,
                "pins": [
                    {"number": "1", "name": "VDD", "type": "power_in",
                     "xy": [0, 5]},
                    {"number": "2", "name": "GND", "type": "power_in",
                     "xy": [0, 0]},
                ],
            }
        if tool == "ecad_add_power":
            # Bond when xy is near a pin (forced path); orphan if arbitrary.
            x = float((args or {}).get("x") or 0)
            y = float((args or {}).get("y") or 0)
            bonded = abs(x) < 1.0 and abs(y) < 6.0  # U1 pins at (0,5)/(0,0)
            return {
                "ok": True, "net": (args or {}).get("net") or "GND",
                "ref": "#PWR01",
                "net_size": 2 if bonded else 0,
                "net_members": ["U1.2"] if bonded else [],
                "pos": [x, y],
            }
        if tool == "ecad_wire":
            a = args or {}
            if a.get("from_ref") == a.get("to_ref"):
                return {"ok": True, "net": "Nself", "net_size": 2}
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {
                "ok": True, "doc_id": "pcb-m18", "board_doc_id": "pcb-m18",
            }
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    res = at.run_turn(
        "Power then wire cascade",
        _Prov(),
        name="BUILD DL3",
        ledger=ledger,
        allowed_tools=[
            "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
            "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        ],
        tool_dispatch=_dispatch,
        max_tool_calls=20,
        phase="build",
    )
    calls = (res.result or {}).get("calls") or []
    tools_ok = [c.get("tool") for c in calls if c.get("ok")]
    assert "ecad_add_power" in tools_ok, tools_ok
    assert "ecad_wire" in tools_ok, tools_ok
    if "board_from_schematic" in tools_ok:
        assert tools_ok.index("ecad_add_power") < tools_ok.index(
            "board_from_schematic"
        )
    cross = [
        c for c in calls
        if c.get("ok") and c.get("tool") == "ecad_wire"
        and (c.get("args") or {}).get("from_ref")
        != (c.get("args") or {}).get("to_ref")
    ]
    assert cross, calls


# ---------------------------------------------------------------------------
# Z4 — AC5 null V(3V3) → FAIL (never NOT-CHECKED when simulate ran)
# ---------------------------------------------------------------------------

_MEGA17_AC5_PAYLOAD = {
    "ok": True,
    "doc_id": "fb6e6e4b586d",
    "analysis": "op",
    "backend": "mna",
    "converged": True,
    "device_count": 1,
    "nodes": ["N$9"],
    "probes": ["V(3V3)"],
    "warnings": [
        "U1 (Sensor_Pressure:BMP280): no sim model — skipped",
    ],
    "sim_log": ["op: converged in 1 iters"],
    "op": {"V(3V3)": None},
    "reading": "no finite probe values — check connectivity / source values",
    "hint": "OP solved: no finite probe values — check connectivity / source values.",
    "source": "live",
}


def test_ac5_null_v3v3_is_fail_not_not_checked():
    from services.agent.reviewer_exec import FAIL, NOT_CHECKED, evaluate_pass_rule

    v, d = evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        dict(_MEGA17_AC5_PAYLOAD),
    )
    assert v == FAIL, (v, d)
    assert v != NOT_CHECKED
    assert "3V3" in d or "3v3" in d.lower(), d
    assert (
        "null" in d.lower()
        or "not finite" in d.lower()
        or "missing" in d.lower()
        or "not driven" in d.lower()
    ), d


def test_ac5_numeric_v3v3_still_pass():
    from services.agent.reviewer_exec import PASS, evaluate_pass_rule

    v, d = evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        {"ok": True, "analysis": "op", "op": {"V(3V3)": 3.30}},
    )
    assert v == PASS, (v, d)


def test_ac5_out_of_range_still_fail():
    from services.agent.reviewer_exec import FAIL, evaluate_pass_rule

    v, d = evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        {"ok": True, "V(3V3)": 2.5, "analysis": "op"},
    )
    assert v == FAIL, (v, d)


def test_ac5_tool_never_ran_still_not_checked():
    from services.agent.reviewer_exec import NOT_CHECKED, evaluate_pass_rule

    v, d = evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        {"unrelated": True},
    )
    assert v == NOT_CHECKED, (v, d)


def test_ac5_missing_probe_with_converged_is_fail():
    from services.agent.reviewer_exec import FAIL, evaluate_pass_rule

    v, d = evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        {
            "ok": True,
            "converged": True,
            "backend": "mna",
            "device_count": 1,
            "nodes": ["N$9"],
            "analysis": "op",
        },
    )
    assert v == FAIL, (v, d)
    assert "3V3" in d or "3v3" in d.lower() or "missing" in d.lower(), d


# ---------------------------------------------------------------------------
# mega-18 a1 residuals — orphan power + AC4 board doc
# ---------------------------------------------------------------------------


def test_orphan_power_does_not_unlock_board():
    """mega-18 a1: net_size=0 ports must not unlock board; re-force pin xy."""
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_forced_tool_args, _ecad_has_bonded_power,
        _ecad_nets_ready_for_board,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X", "ref": "U1"},
            "result": {
                "ref": "U1",
                "pins": [
                    {"number": "1", "name": "VBAT", "type": "power_in",
                     "xy": [34.76, 53.97]},
                    {"number": "2", "name": "GND", "type": "power_in",
                     "xy": [34.76, 40.0]},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND", "x": 10.0, "y": 80.0},
            "result": {
                "ok": True, "net": "GND", "net_size": 0, "pos": [10.0, 80.0],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 10.0, "y": 10.0},
            "result": {
                "ok": True, "net": "3V3", "net_size": 0, "pos": [10.0, 10.0],
            },
        },
    ]
    assert _ecad_has_bonded_power(calls) is False
    assert _ecad_nets_ready_for_board(calls, allow) is False
    # Must re-force add_power at pin xy — not place Device:R.
    assert _ecad_chain_need(calls, allow) == "ecad_add_power"
    args = _ecad_forced_tool_args("ecad_add_power", calls)
    # Land on a U1 power pin xy, not the orphan (10,80).
    assert (round(float(args["x"]), 2), round(float(args["y"]), 2)) in {
        (34.76, 53.97), (34.76, 40.0),
    }, args


def test_cascade_after_orphan_power_rebonds_then_wire():
    """After orphan GND+3V3, cascade rebinds pin then wires power_in→rail."""
    from services.agent.agent_turn import _ecad_force_chain_cascade

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    calls = [
        {"tool": "ecad_new_schematic", "ok": True, "result": {"doc_id": "s"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:X"},
            "result": {
                "ref": "U1",
                "pins": [
                    {"number": "1", "name": "VBAT", "type": "power_in",
                     "xy": [34.76, 53.97]},
                    {"number": "2", "name": "GND", "type": "power_in",
                     "xy": [34.76, 40.0]},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND", "x": 10.0, "y": 80.0},
            "result": {"ok": True, "net": "GND", "net_size": 0, "pos": [10, 80]},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 10.0, "y": 10.0},
            "result": {"ok": True, "net": "3V3", "net_size": 0, "pos": [10, 10]},
        },
    ]
    dispatched = []

    def _dispatch(tool, args):
        dispatched.append((tool, dict(args or {})))
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": (args or {}).get("net") or "GND",
                "net_size": 2, "ref": "#PWRX",
                "pos": [(args or {}).get("x"), (args or {}).get("y")],
            }
        if tool == "ecad_place_symbol":
            return {
                "ok": True, "ref": "R1", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            }
        if tool == "ecad_wire":
            a = args or {}
            assert a.get("from_ref") != a.get("to_ref"), a
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": "pcb1", "board_doc_id": "pcb1"}
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0}
        return {"ok": True}

    new, _ = _ecad_force_chain_cascade(
        calls=list(calls), allow=allow, dispatch=_dispatch, ledger=None,
        actor="t", messages=[], tool_calls_made=0, budget=10,
        reason="z2r_orphan_rebond",
    )
    # First cascade step must re-add_power (not place R).
    assert dispatched[0][0] == "ecad_add_power", dispatched
    # Wire must include power rail ref when available.
    wires = [
        c for c in new
        if c.get("tool") == "ecad_wire" and c.get("ok") and c.get("forced")
    ]
    assert wires, new
    wargs = wires[0].get("args") or {}
    assert "#PWRX" in {wargs.get("from_ref"), wargs.get("to_ref")} or (
        wargs.get("from_ref") != wargs.get("to_ref")
    ), wargs


def test_ac4_drc_resolves_board_doc_from_truncated_result():
    """mega-18 a1: DRC must use board_doc_id 08cf… not sch 0ce…."""
    from services.agent.reviewer_exec import _resolve_domain_doc_id

    class _Led:
        events = []

    led = _Led()
    board_res = (
        '{"ok":true,"doc_id":"08cfb42d9b98","board_doc_id":"08cfb42d9b98",'
        '"sch_doc_id":"0ce20163ee10","revision":5,"placed":2,"routed":1,'
        '"routed_nets":["N$1"],"failed_nets":[],"residual_drc":[],'
        '"drc_errors":0,"report":{"ratsnest_before":1,"ratsnest_after":0,'
        '"corners_rounded":0,"teardrops":1,"nets_tuned":0,"drc_errors":0,'
        '"seed":0,"steps":[{"step":"update_from_sch","ok":true,"added":2,'
        '"updated":0,"unresolved":0},{"step":"place_board","ok":true,'
        '"moved":2,"n_footprints":2,"ratsnest_le'
    )
    led.events = [
        {
            "kind": "agent_turn",
            "payload": {
                "event": "tool_call", "tool": "ecad_new_schematic",
                "ok": True,
                "result": {"ok": True, "doc_id": "0ce20163ee10"},
            },
        },
        {
            "kind": "agent_turn",
            "payload": {
                "event": "tool_call", "tool": "ecad_place_symbol",
                "ok": True, "args": {"doc_id": "0ce20163ee10"},
                "result": '{"ok":true,"doc_id":"0ce20163ee10","ref":"U1"}',
            },
        },
        {
            "kind": "agent_turn",
            "payload": {
                "event": "tool_call", "tool": "board_from_schematic",
                "ok": True, "args": {}, "result": board_res,
            },
        },
    ]
    assert _resolve_domain_doc_id("ecad_run_drc", led) == "08cfb42d9b98"
    # ERC/simulate still prefer schematic.
    assert _resolve_domain_doc_id("ecad_run_erc", led) == "0ce20163ee10"

