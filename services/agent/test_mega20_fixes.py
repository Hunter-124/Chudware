"""Mega-20 fixes — A20 rail rebond (no #PWR ref), B20 board doc_id on DRC.

Grounded in the mega-19 a2 live ledger:

  A20 — ecad_add_power returns no symbol ref; forced wires cannot target rails.
        power_not_driven persists until ports sit on power_in pin xy.
        Fix: rebond unbonded power-class pins at pin world xy before wire;
        cascade must cover every power_in.

  B20 — AC4 live: DRC against sch doc_id cd7516e36580
        ("is a sch, not a board"). Unit fixtures with clean board result pass,
        but live/forced path still needs board id forced onto DRC args.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


_MEGA19_A2_LEDGER = (
    Path(__file__).resolve().parents[2]
    / "reports/runs/mega-19/attempt-2-primary-graded-wire-zero/ledger.json"
)


def _a2_ecad_calls() -> list[dict]:
    """Reconstruct ECAD call records from mega-19 a2 ledger (live residual)."""
    if not _MEGA19_A2_LEDGER.is_file():
        pytest.skip(f"missing a2 ledger: {_MEGA19_A2_LEDGER}")
    data = json.loads(_MEGA19_A2_LEDGER.read_text())
    events = data.get("events") if isinstance(data, dict) else data
    calls: list[dict] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        pl = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if pl.get("event") not in (None, "tool_call") and pl.get("tool") is None:
            # force inject events carry args but no ok/result
            if pl.get("event") == "ecad_chain_force_inject":
                continue
        tool = pl.get("tool")
        if not tool:
            continue
        if not (
            str(tool).startswith("ecad_")
            or tool in ("board_from_schematic",)
        ):
            continue
        if pl.get("event") and pl.get("event") not in ("tool_call",):
            continue
        rec = {
            "tool": tool,
            "ok": pl.get("ok"),
            "args": pl.get("args") if isinstance(pl.get("args"), dict) else {},
            "result": pl.get("result"),
        }
        if pl.get("forced"):
            rec["forced"] = True
        if pl.get("force_reason"):
            rec["force_reason"] = pl.get("force_reason")
        # Only keep successful product calls + denials that carry shape.
        if rec["ok"] is True or (
            rec["ok"] is False and tool in ("ecad_place_symbol",)
        ):
            calls.append(rec)
    return calls


def _a2_residual_pre_wire() -> list[dict]:
    """a2 residual up to (not including) any wire/board/drc — live stop point."""
    calls = _a2_ecad_calls()
    residual: list[dict] = []
    for c in calls:
        if c.get("tool") in (
            "ecad_wire", "board_from_schematic", "ecad_make_board",
            "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
        ):
            break
        if c.get("ok") is True:
            residual.append(c)
    # Ensure full U1 pin list for power_in harvest (ledger place result is truncated).
    # Live a2 place only kept partial pins in the truncated digest — inject the
    # engine-known power_in set from the same residual place args/doc.
    enriched: list[dict] = []
    for c in residual:
        if c.get("tool") == "ecad_place_symbol" and c.get("ok"):
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            res = c.get("result")
            if isinstance(res, str):
                try:
                    res_obj = json.loads(res)
                except Exception:
                    res_obj = {}
            else:
                res_obj = res if isinstance(res, dict) else {}
            ref = str(
                (res_obj or {}).get("ref") or args.get("ref") or ""
            )
            lib = str(
                (res_obj or {}).get("libid") or args.get("libid") or ""
            )
            # Only expand the truncated MCU place (U1) — never rewrite Device:R.
            is_mcu = (
                ref == "U1"
                or "MCU:" in lib
                or "Generic_QFP" in lib
            )
            pins = res_obj.get("pins") if isinstance(res_obj, dict) else None
            if is_mcu and (
                not isinstance(pins, list) or len(pins) < 4
            ):
                from services.agent.agent_turn import _ecad_place_pin_records
                harvested = _ecad_place_pin_records([c])
                by_name = {
                    (p.get("name") or "").upper(): p
                    for p in harvested if p.get("name")
                }
                # a2 place pos was [100,80]; VBAT tip survived at [84.76, 93.97].
                # Prefer harvested xy when present; stable defaults otherwise.
                base = {
                    "VBAT": by_name.get("VBAT") or {
                        "ref": "U1", "pin": "1", "name": "VBAT",
                        "type": "power_in", "x": 84.76, "y": 93.97,
                    },
                    "VSSA": by_name.get("VSSA") or {
                        "ref": "U1", "pin": "8", "name": "VSSA",
                        "type": "power_in", "x": 84.76, "y": 76.19,
                    },
                    "VDDA": by_name.get("VDDA") or {
                        "ref": "U1", "pin": "9", "name": "VDDA",
                        "type": "power_in", "x": 84.76, "y": 73.65,
                    },
                    "VSS": by_name.get("VSS") or {
                        "ref": "U1", "pin": "12", "name": "VSS",
                        "type": "power_in", "x": 115.24, "y": 66.03,
                    },
                }
                pin_list = []
                for _nm, p in base.items():
                    pin_list.append({
                        "number": p["pin"],
                        "name": p["name"],
                        "type": p["type"],
                        "xy": [p["x"], p["y"]],
                    })
                for p in harvested:
                    if (p.get("name") or "").upper() in base:
                        continue
                    if (p.get("type") or "").lower() in (
                        "power_in", "power_out", "power",
                    ):
                        pin_list.append({
                            "number": p["pin"],
                            "name": p.get("name") or "",
                            "type": p.get("type") or "passive",
                            "xy": [p["x"], p["y"]],
                        })
                c = dict(c)
                c["result"] = {
                    "ok": True,
                    "doc_id": "cd7516e36580",
                    "ref": "U1",
                    "libid": lib or "MCU:Generic_QFP48",
                    "pins": pin_list,
                }
        enriched.append(c)
    return enriched

def test_a2_residual_chain_need_rebonds_before_wire():
    """Live a2: orphan add_power → next need is rebond add_power, not wire."""
    from services.agent.agent_turn import (
        _ecad_chain_need, _ecad_unbonded_power_pins, _ecad_power_refs,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    calls = _a2_residual_pre_wire()
    assert any(c.get("tool") == "ecad_add_power" for c in calls), calls
    # Live engine shape: no #PWR refs.
    refs = [p.get("ref") for p in _ecad_power_refs(calls) if p.get("ref")]
    assert not refs, f"a2 residual unexpectedly has power refs: {refs}"
    unbonded = _ecad_unbonded_power_pins(calls)
    assert unbonded, "expected unbonded power_in pins on a2 residual"
    need = _ecad_chain_need(calls, allow)
    assert need == "ecad_add_power", (need, unbonded[:3])


def test_a2_forced_add_power_lands_on_power_pin_xy():
    """Forced rebond args must sit on a live power_in pin, not (0,10)."""
    from services.agent.agent_turn import (
        _ecad_forced_tool_args, _ecad_unbonded_power_pins,
    )

    calls = _a2_residual_pre_wire()
    unbonded = _ecad_unbonded_power_pins(calls)
    assert unbonded
    args = _ecad_forced_tool_args("ecad_add_power", calls)
    xy = (round(float(args["x"]), 2), round(float(args["y"]), 2))
    pin_xy = {
        (round(float(p["x"]), 2), round(float(p["y"]), 2)) for p in unbonded
    }
    assert xy in pin_xy, (args, pin_xy)
    # Not the live a2 orphan coordinate.
    assert xy != (0.0, 10.0), args


def test_a2_cascade_bonds_every_power_class_pin():
    """Replay a2 residual: cascade rebond covers every power-class pin xy."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade, _ecad_unbonded_power_pins,
        _ecad_place_pin_records,
    )

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
        "ecad_run_erc", "ecad_simulate",
    }
    calls = _a2_residual_pre_wire()
    power_pins = [
        p for p in _ecad_place_pin_records(calls)
        if (p.get("type") or "").lower() in ("power_in", "power_out", "power")
        or any(
            tok in (p.get("name") or "").upper()
            for tok in (
                "VDD", "VCC", "3V3", "GND", "VSS", "VIN", "VBAT",
                "VDDIO", "VDDA", "VSSA",
            )
        )
    ]
    assert power_pins, "fixture needs power-class pins"

    dispatched: list[tuple[str, dict]] = []
    board_id = "board_a2_mega20"

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "ecad_add_power":
            return {
                "ok": True,
                "net": a.get("net") or "GND",
                # Live shape: NO ref field.
                "net_size": 1,
                "pos": [a.get("x"), a.get("y")],
                "net_members": ["U1.1"],
                "doc_id": "cd7516e36580",
            }
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "Device:V" in lib:
                return {
                    "ok": True, "ref": "V1", "libid": lib, "value": "DC 3.3",
                    "pins": [
                        {"number": "+", "name": "+", "type": "power_out",
                         "xy": [40, 5]},
                        {"number": "-", "name": "-", "type": "power_out",
                         "xy": [40, -5]},
                    ],
                }
            return {
                "ok": True, "ref": "R1", "libid": "Device:R", "value": "1k",
                "pins": [
                    {"number": "1", "xy": [20, 20]},
                    {"number": "2", "xy": [30, 20]},
                ],
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": "N1", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "board_from_schematic":
            return {
                "ok": True,
                "doc_id": board_id,
                "board_doc_id": board_id,
                "sch_doc_id": "cd7516e36580",
            }
        if tool == "ecad_run_drc":
            return {"ok": True, "errors": 0, "doc_id": a.get("doc_id")}
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0}
        if tool == "ecad_simulate":
            return {
                "ok": True, "analysis": "op", "device_count": 2,
                "op": {"V(3V3)": 3.3},
            }
        return {"ok": True}

    new_calls, made = _ecad_force_chain_cascade(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="t",
        messages=[],
        tool_calls_made=13,  # a2 thrash residual
        budget=16,
        reason="hard_gate_cap",
    )
    # Every power-class pin must be covered by a co-located add_power.
    remaining = _ecad_unbonded_power_pins(new_calls)
    assert remaining == [], (
        f"unbonded power pins remain: {remaining}; "
        f"add_power args={[a for t,a in dispatched if t=='ecad_add_power']}"
    )
    # Wire + board + DRC still complete after rebond.
    tools = [t for t, _ in dispatched]
    assert "ecad_wire" in tools, dispatched
    assert "board_from_schematic" in tools, dispatched
    assert "ecad_run_drc" in tools, dispatched
    assert made > 16


# ---------------------------------------------------------------------------
# B20 — forced DRC carries board doc_id (a2/live sch-not-board residual)
# ---------------------------------------------------------------------------


def test_forced_drc_rejects_sch_doc_id():
    """Board harvest must never return the schematic id."""
    from services.agent.agent_turn import (
        _ecad_board_doc_id, _ecad_forced_tool_args,
    )

    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "cd7516e36580", "kind": "sch"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "result": {"doc_id": "cd7516e36580", "ref": "U1"},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "ok": True,
                "doc_id": "board_a2_mega20",
                "board_doc_id": "board_a2_mega20",
                "sch_doc_id": "cd7516e36580",
            },
        },
    ]
    assert _ecad_board_doc_id(calls) == "board_a2_mega20"
    drc = _ecad_forced_tool_args("ecad_run_drc", calls)
    assert drc.get("doc_id") == "board_a2_mega20", drc
    assert drc.get("doc_id") != "cd7516e36580"


def test_forced_drc_from_nested_board_result():
    """Live-ish nested/truncated board envelopes still yield board id."""
    from services.agent.agent_turn import (
        _ecad_board_doc_id, _ecad_forced_tool_args,
    )

    # Nested envelope (some dispatch paths wrap result).
    nested = {
        "ok": True,
        "result": {
            "doc_id": "board_nested",
            "board_doc_id": "board_nested",
            "sch_doc_id": "cd7516e36580",
        },
    }
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "cd7516e36580"},
        },
        {"tool": "board_from_schematic", "ok": True, "result": nested},
    ]
    assert _ecad_board_doc_id(calls) == "board_nested"
    assert _ecad_forced_tool_args("ecad_run_drc", calls).get("doc_id") == (
        "board_nested"
    )

    # Truncated string (mega-18/a2 ledger digest).
    truncated = (
        '{"ok":true,"doc_id":"board_trunc","board_doc_id":"board_trunc",'
        '"sch_doc_id":"cd7516e36580","revision":5,"placed":2,"ro'
    )
    calls2 = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "cd7516e36580"},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": truncated,
        },
    ]
    assert _ecad_board_doc_id(calls2) == "board_trunc"
    assert _ecad_forced_tool_args("ecad_run_drc", calls2).get("doc_id") == (
        "board_trunc"
    )


def test_a2_cascade_drc_args_carry_board_doc_id():
    """Forced DRC after board_from_schematic must use board id, not sch."""
    from services.agent.agent_turn import _ecad_force_chain_cascade

    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire",
        "ecad_add_power", "board_from_schematic", "ecad_run_drc",
    }
    sch = "cd7516e36580"
    board = "board_a2_mega20"
    calls = [
        {"tool": "ecad_new_schematic", "ok": True,
         "result": {"doc_id": sch, "kind": "sch"}},
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48"},
            "result": {
                "ref": "U1", "doc_id": sch,
                "pins": [
                    {"number": "1", "name": "VBAT", "type": "power_in",
                     "xy": [84.76, 93.97]},
                    {"number": "8", "name": "VSSA", "type": "power_in",
                     "xy": [84.76, 60.0]},
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
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
            "args": {"net": "GND", "x": 84.76, "y": 60.0, "doc_id": sch},
            "result": {
                "ok": True, "net": "GND", "net_size": 1,
                "pos": [84.76, 60.0], "doc_id": sch,
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 84.76, "y": 93.97, "doc_id": sch},
            "result": {
                "ok": True, "net": "3V3", "net_size": 1,
                "pos": [84.76, 93.97], "doc_id": sch,
            },
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "U1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "1", "doc_id": sch,
            },
            "result": {"ok": True, "net": "N1", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "ok": True, "doc_id": board, "board_doc_id": board,
                "sch_doc_id": sch,
            },
        },
    ]
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "ecad_run_drc":
            assert a.get("doc_id") == board, a
            return {"ok": True, "errors": 0}
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": a.get("net") or "GND",
                "net_size": 1, "pos": [a.get("x"), a.get("y")],
            }
        if tool == "ecad_wire":
            return {"ok": True, "net": "N1", "net_size": 2}
        if tool == "board_from_schematic":
            return {
                "ok": True, "doc_id": board, "board_doc_id": board,
                "sch_doc_id": sch,
            }
        if tool == "ecad_place_symbol":
            return {
                "ok": True, "ref": "R2", "libid": "Device:R",
                "pins": [{"number": "1", "xy": [1, 1]},
                         {"number": "2", "xy": [2, 2]}],
            }
        return {"ok": True}

    _ecad_force_chain_cascade(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="t",
        messages=[],
        tool_calls_made=5,
        budget=20,
        reason="hard_gate_cap",
    )
    drcs = [a for t, a in dispatched if t == "ecad_run_drc"]
    assert drcs, dispatched
    assert all(a.get("doc_id") == board for a in drcs), drcs
    assert all(a.get("doc_id") != sch for a in drcs), drcs

# ---------------------------------------------------------------------------
# A20 a3 residual — wire onto rail-bonded pin (not unbonded power_in)
# ---------------------------------------------------------------------------


_MEGA19_A3_LEDGER = (
    Path(__file__).resolve().parents[2] / "reports/runs/mega-19/ledger.json"
)


def _a3_ecad_calls() -> list[dict]:
    """ECAD calls from mega-19 a3 primary ledger (root ledger.json)."""
    if not _MEGA19_A3_LEDGER.is_file():
        pytest.skip(f"missing a3 ledger: {_MEGA19_A3_LEDGER}")
    data = json.loads(_MEGA19_A3_LEDGER.read_text())
    events = data.get("events") if isinstance(data, dict) else data
    calls: list[dict] = []
    for ev in events or []:
        pl = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if pl.get("event") not in (None, "tool_call"):
            continue
        tool = pl.get("tool")
        if not tool:
            continue
        if not (
            str(tool).startswith("ecad_")
            or tool in ("board_from_schematic",)
        ):
            continue
        if pl.get("ok") is not True:
            continue
        calls.append({
            "tool": tool,
            "ok": True,
            "args": pl.get("args") if isinstance(pl.get("args"), dict) else {},
            "result": pl.get("result"),
            **({"forced": True} if pl.get("forced") else {}),
        })
    return calls


def test_a3_forced_wire_joins_rail_member_not_unbonded_power_in():
    """a3 live: force wire was U1.3/GND↔R1; must be U1.6 (rail member)↔R1."""
    from services.agent.agent_turn import (
        _ecad_forced_tool_args, _ecad_rail_member_pins,
    )

    # Synthetic residual matching a3 shape (truncated place may lack full pins).
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "596497ba8ad5"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Regulator_Switching:Buck_3V3", "ref": "U1"},
            "result": {
                "ref": "U1",
                "libid": "Regulator_Switching:Buck_3V3",
                "pins": [
                    {"number": "1", "name": "VBST", "type": "passive",
                     "xy": [9.84, 45.08]},
                    {"number": "2", "name": "SW", "type": "output",
                     "xy": [9.84, 42.54]},
                    {"number": "3", "name": "GND", "type": "power_in",
                     "xy": [9.84, 40.0]},
                    {"number": "4", "name": "FB", "type": "input",
                     "xy": [9.84, 37.46]},
                    {"number": "5", "name": "EN", "type": "input",
                     "xy": [30.16, 37.46]},
                    {"number": "6", "name": "VIN", "type": "power_in",
                     "xy": [30.16, 40.0]},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND", "x": 0.0, "y": 70.0},
            "result": {
                "ok": True, "net": "GND", "pos": [0.0, 70.0],
                "net_size": 0, "net_members": [],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "x": 30.16, "y": 40.0},
            "result": {
                "ok": True, "net": "3V3", "pos": [30.16, 40.0],
                "net_size": 1, "net_members": ["U1.6"],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "value": "1k"},
            "result": {
                "ref": "R1",
                "pins": [
                    {"number": "1", "name": "~", "type": "passive",
                     "xy": [14.92, 20.0]},
                    {"number": "2", "name": "~", "type": "passive",
                     "xy": [25.08, 20.0]},
                ],
            },
        },
    ]
    members = _ecad_rail_member_pins(calls)
    assert any(m.get("ref") == "U1" and m.get("pin") == "6" for m in members), members
    args = _ecad_forced_tool_args("ecad_wire", calls)
    ends = {(args.get("from_ref"), str(args.get("from_pin"))),
            (args.get("to_ref"), str(args.get("to_pin")))}
    assert ("U1", "6") in ends, args
    assert any(r == "R1" for r, _ in ends), args
    # Not the a3 residual undriven power_in pair.
    assert ("U1", "3") not in ends, args


def test_a3_ledger_wire_after_bonded_power_hits_rail_member():
    """Replay a3 ledger calls: after bonded add_power, wire hits net_member."""
    from services.agent.agent_turn import (
        _ecad_forced_tool_args, _ecad_rail_member_pins,
    )

    calls = _a3_ecad_calls()
    # Residual: stop before first wire.
    residual = []
    for c in calls:
        if c.get("tool") == "ecad_wire":
            break
        residual.append(c)
    members = _ecad_rail_member_pins(residual)
    if not members:
        # Ledger place result may be truncated; ensure synthetic member path
        # still covered by test_a3_forced_wire_joins_rail_member_not_unbonded_power_in.
        pytest.skip("a3 ledger residual has no net_members (truncated?)")
    args = _ecad_forced_tool_args("ecad_wire", residual)
    ends = {(args.get("from_ref"), str(args.get("from_pin"))),
            (args.get("to_ref"), str(args.get("to_pin")))}
    member_ends = {(m["ref"], str(m["pin"])) for m in members}
    assert ends & member_ends, (args, members)
    # Must not be pure unbonded power_in (a3 U1.3).
    assert not (ends == {("U1", "3"), ("R1", "1")}), args
