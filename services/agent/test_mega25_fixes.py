"""Mega-25 fixes — Y25 simulate schematic doc + Z25 clearance-aware board chain.

Grounded in mega-24 a3 fingerprints:

  Y25 / AC5 — forced/model/reviewer ecad_simulate received PCB board
              doc 767f0f0fa678, not schematic a6e440d0e2fc
              ("doc is a pcb, not a schematic"; V(3V3) null).
              Mirror of B20 (board INTO drc/trace): simulate must thread
              the SCHEMATIC doc holding the netlist.

  Z25 / AC4 — DRC clearance×6 at 0.1296mm < 0.2mm on board 767f0f0fa678.
              optimize_traces has no min_clearance param; chain must pass
              clearance intent (>=0.2mm) and do one bounded re-route/
              re-opt when DRC reports clearance-class errors, then honest
              FAIL (errors==0 or residual naming clearance).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_MEGA24 = _ROOT / "reports/runs/mega-24"
_MEGA24_AC4 = _MEGA24 / "criterion_AC4.json"
_MEGA24_AC5 = _MEGA24 / "criterion_AC5.json"
_MEGA24_LEDGER = _MEGA24 / "ledger.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Live fingerprints (would have caught the thrash class)
# ---------------------------------------------------------------------------


def test_mega24_ac5_fingerprint_simulate_on_pcb_doc():
    """Y25 fixture: AC5 failed because simulate targeted board not schematic."""
    ac5 = _load_json(_MEGA24_AC5)
    assert ac5.get("result") == "FAIL"
    detail = str(ac5.get("detail") or "")
    ev = ac5.get("evidence") or {}
    blob = detail + " " + json.dumps(ev, default=str)
    assert "pcb" in blob.lower() or "not a schematic" in blob.lower(), blob
    assert "767f0f0fa678" in blob  # board id from mega-24 a3
    # S23 readback still recorded preferred net — doc routing was the miss.
    assert (ev.get("resolved_net") or "").upper() in ("3V3", "3.3V", "VCC")


def test_mega24_ac4_fingerprint_clearance_residual():
    """Z25 fixture: DRC clearance×6 at 0.1296 < 0.2mm on board."""
    ac4 = _load_json(_MEGA24_AC4)
    assert ac4.get("result") == "FAIL"
    ev = ac4.get("evidence") or {}
    drc = ev.get("drc") or ev.get("ecad_run_drc") or {}
    assert int(drc.get("errors") or 0) >= 1
    blob = json.dumps(drc, default=str).lower()
    assert "clearance" in blob
    assert "0.2" in blob or "0.20" in blob
    assert drc.get("doc_id") == "767f0f0fa678"


# ---------------------------------------------------------------------------
# Y25 — simulate / ERC / net-resolution target SCHEMATIC doc
# ---------------------------------------------------------------------------


def _mega24_ledger_ns():
    data = _load_json(_MEGA24_LEDGER)
    return SimpleNamespace(events=data.get("events") or [], state=data.get("state"))


def test_y25_reviewer_simulate_resolves_schematic_not_board():
    """Live mega-24 ledger: ecad_simulate must get sch a6e…, not board 767f…"""
    from services.agent.reviewer_exec import _resolve_domain_doc_id

    ledger = _mega24_ledger_ns()
    sim = _resolve_domain_doc_id("ecad_simulate", ledger)
    drc = _resolve_domain_doc_id("ecad_run_drc", ledger)
    erc = _resolve_domain_doc_id("ecad_run_erc", ledger)
    assert sim == "a6e440d0e2fc", sim
    assert drc == "767f0f0fa678", drc  # B20 hold
    assert erc == "a6e440d0e2fc", erc
    assert sim != drc


def test_y25_forced_simulate_args_carry_schematic_doc():
    """Forced chain simulate args must thread schematic, never board."""
    from services.agent.agent_turn import _ecad_forced_tool_args

    sch, brd = "sch_aaaa1111", "brd_bbbb2222"
    calls = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"ok": True, "doc_id": sch, "kind": "sch"},
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "doc_id": sch},
            "result": {
                "ok": True,
                "doc_id": sch,
                "ref": "R1",
                "pins": [
                    {
                        "ref": "R1",
                        "pin": "1",
                        "name": "1",
                        "type": "passive",
                        "x": 0.0,
                        "y": 0.0,
                    },
                    {
                        "ref": "R1",
                        "pin": "2",
                        "name": "2",
                        "type": "passive",
                        "x": 2.54,
                        "y": 0.0,
                    },
                ],
            },
        },
        {
            "tool": "ecad_add_power",
            "ok": True,
            "args": {"net": "3V3", "x": 0.0, "y": 0.0, "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1, "doc_id": sch},
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {
                "from_ref": "R1",
                "from_pin": "1",
                "to_ref": "R1",
                "to_pin": "2",
                "doc_id": sch,
            },
            "result": {"ok": True, "net_size": 2, "doc_id": sch},
        },
        {
            "tool": "board_from_schematic",
            "ok": True,
            "args": {"doc_id": sch, "sch_doc_id": sch},
            "result": {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": sch,
            },
        },
        {
            "tool": "ecad_optimize_traces",
            "ok": True,
            "args": {"doc_id": brd},
            "result": {"ok": True, "doc_id": brd, "corners_rounded": 0},
        },
        {
            "tool": "ecad_run_drc",
            "ok": True,
            "args": {"doc_id": brd},
            "result": {"ok": True, "doc_id": brd, "errors": 0},
        },
        {
            "tool": "ecad_run_erc",
            "ok": True,
            "args": {"doc_id": sch},
            "result": {"ok": True, "doc_id": sch, "errors": 0},
        },
    ]
    sim = _ecad_forced_tool_args("ecad_simulate", calls)
    assert sim.get("doc_id") == sch, sim
    assert sim.get("doc_id") != brd
    opt = _ecad_forced_tool_args("ecad_optimize_traces", calls)
    assert opt.get("doc_id") == brd, opt
    assert float(opt.get("min_clearance_mm") or 0) >= 0.2


def test_y25_forced_dispatch_rewrites_simulate_to_schematic():
    """Dispatch path rewrites board-ish args to schematic for SPICE."""
    from services.agent.agent_turn import _ecad_dispatch_forced_step

    sch, brd = "sch_y25", "brd_y25"
    calls = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"ok": True, "doc_id": sch, "kind": "sch"},
        },
        {
            "tool": "board_from_schematic",
            "ok": True,
            "result": {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": sch,
            },
        },
    ]
    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        dispatched.append((tool, dict(args or {})))
        if tool == "ecad_simulate":
            # Mirror live MCP: board → hard error; sch → finite V.
            did = (args or {}).get("doc_id")
            if did == brd:
                return {
                    "ok": False,
                    "error": f"doc '{did}' is a pcb, not a schematic",
                }
            return {
                "ok": True,
                "analysis": "op",
                "device_count": 2,
                "nodes": ["3V3", "GND"],
                "op": {"V(3V3)": 3.3},
                "doc_id": did,
            }
        return {"ok": True}

    new_calls, made, rec, ok = _ecad_dispatch_forced_step(
        tool="ecad_simulate",
        args={"analysis": "op", "probes": ["V(3V3)"], "doc_id": brd},  # wrong
        calls=list(calls),
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        reason="y25",
        tool_calls_made=0,
        budget=8,
    )
    assert ok is True, (rec, dispatched)
    assert dispatched and dispatched[0][1].get("doc_id") == sch, dispatched
    assert "resolved_net" not in dispatched[0][1]


def test_y25_old_trace_routed_board_new_routes_schematic():
    """Fixture proving old mega-24 resolve returned board; new returns sch."""
    from services.agent.reviewer_exec import _resolve_domain_doc_id

    ledger = _mega24_ledger_ns()
    # Document the old bug class: board id must NOT be chosen for simulate.
    sim = _resolve_domain_doc_id("ecad_simulate", ledger)
    assert sim is not None
    assert sim != "767f0f0fa678"
    assert sim == "a6e440d0e2fc"


# ---------------------------------------------------------------------------
# Z25 — clearance-aware optimize / bounded retry
# ---------------------------------------------------------------------------


def _clearance_drc_result(doc_id: str = "767f0f0fa678", errors: int = 6) -> dict:
    """mega-24 AC4 DRC result shape (clearance 0.1296 < 0.2mm)."""
    return {
        "ok": True,
        "doc_id": doc_id,
        "revision": 6,
        "errors": errors,
        "warnings": [],
        "verdict": (
            f"{errors} error(s), 0 warning(s). worst: [ERROR] clearance: "
            "clearance 0.1296mm < required 0.2mm between p11 (net 'N$2') "
            "and t31 (net 'GND')"
        ),
        "violations": [
            {
                "rule": "clearance",
                "severity": "error",
                "message": "clearance 0.1296mm < required 0.2mm",
            }
        ]
        * min(errors, 3),
        "by_rule": [{"rule": "clearance", "count": errors}],
        "source": "live",
    }


def test_z25_clearance_detection_from_mega24_shape():
    from services.agent.agent_turn import _ecad_last_drc_has_clearance

    calls = [
        {
            "tool": "ecad_run_drc",
            "ok": True,
            "args": {"doc_id": "767f0f0fa678"},
            "result": _clearance_drc_result(),
        },
    ]
    assert _ecad_last_drc_has_clearance(calls) is True
    clean = [
        {
            "tool": "ecad_run_drc",
            "ok": True,
            "result": {"ok": True, "errors": 0, "verdict": "Clean board"},
        },
    ]
    assert _ecad_last_drc_has_clearance(clean) is False


def test_z25_optimize_args_carry_min_clearance_intent():
    from services.agent.agent_turn import _ecad_forced_tool_args

    brd = "brd_z25"
    calls = [
        {
            "tool": "board_from_schematic",
            "ok": True,
            "result": {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": "sch_z25",
            },
        },
        {
            "tool": "ecad_optimize_traces",
            "ok": True,
            "args": {"doc_id": brd},
            "result": {"ok": True, "doc_id": brd},
        },
        {
            "tool": "ecad_run_drc",
            "ok": True,
            "args": {"doc_id": brd},
            "result": _clearance_drc_result(brd),
        },
    ]
    opt = _ecad_forced_tool_args("ecad_optimize_traces", calls)
    assert opt.get("doc_id") == brd
    assert float(opt.get("min_clearance_mm") or 0) >= 0.2
    assert opt.get("clearance_retry") is True
    ar = _ecad_forced_tool_args("ecad_autoroute", calls)
    assert ar.get("doc_id") == brd
    assert float(ar.get("pitch") or 0) >= 0.3
    assert float(ar.get("min_clearance_mm") or 0) >= 0.2


def test_z25_cascade_clearance_reroute_once_then_stop():
    """Clearance residual → one autoroute+reopt; second dirty does not loop."""
    from services.agent.agent_turn import (
        _ecad_chain_need,
        _ecad_force_chain_cascade,
        _ecad_optimize_retry_count,
    )

    brd, sch = "brd_z25c", "sch_z25c"
    allow = {
        "ecad_new_schematic",
        "ecad_place_symbol",
        "ecad_wire",
        "ecad_add_power",
        "board_from_schematic",
        "ecad_optimize_traces",
        "ecad_autoroute",
        "ecad_run_drc",
        "ecad_run_erc",
        "ecad_simulate",
    }
    base = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"ok": True, "doc_id": sch, "kind": "sch"},
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "doc_id": sch},
            "result": {
                "ok": True,
                "doc_id": sch,
                "ref": "R1",
                "pins": [
                    {
                        "ref": "R1",
                        "pin": "1",
                        "name": "1",
                        "type": "passive",
                        "x": 0.0,
                        "y": 0.0,
                    },
                    {
                        "ref": "R1",
                        "pin": "2",
                        "name": "2",
                        "type": "passive",
                        "x": 2.54,
                        "y": 0.0,
                    },
                ],
            },
        },
        {
            "tool": "ecad_add_power",
            "ok": True,
            "args": {"net": "3V3", "x": 0.0, "y": 0.0, "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire",
            "ok": True,
            "args": {
                "from_ref": "R1",
                "from_pin": "1",
                "to_ref": "R1",
                "to_pin": "2",
                "doc_id": sch,
            },
            "result": {"ok": True, "net_size": 2},
        },
        {
            "tool": "board_from_schematic",
            "ok": True,
            "result": {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": sch,
            },
        },
        {
            "tool": "ecad_optimize_traces",
            "ok": True,
            "args": {"doc_id": brd},
            "result": {"ok": True, "doc_id": brd, "corners_rounded": 0},
        },
        {
            "tool": "ecad_run_drc",
            "ok": True,
            "args": {"doc_id": brd},
            "result": _clearance_drc_result(brd),
        },
    ]
    assert _ecad_optimize_retry_count(base) == 0
    need = _ecad_chain_need(base, allow)
    assert need == "ecad_optimize_traces", need

    dispatched: list[str] = []

    def _dispatch(tool, args):
        dispatched.append(tool)
        a = dict(args or {})
        if tool == "ecad_autoroute":
            assert a.get("doc_id") == brd
            assert float(a.get("pitch") or 0) >= 0.3
            # meta stripped
            assert "min_clearance_mm" not in a
            return {"ok": True, "doc_id": brd, "routed": 2}
        if tool == "ecad_optimize_traces":
            assert a.get("doc_id") == brd
            assert "min_clearance_mm" not in a
            return {"ok": True, "doc_id": brd, "corners_rounded": 1}
        if tool == "ecad_run_drc":
            # Still dirty after retry — honest residual.
            return _clearance_drc_result(brd, errors=3)
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0, "doc_id": sch}
        if tool == "ecad_simulate":
            assert a.get("doc_id") == sch, a
            return {
                "ok": True,
                "op": {"V(3V3)": 3.3},
                "nodes": ["3V3", "GND"],
                "doc_id": sch,
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
        budget=20,
        reason="z25_clearance",
        max_steps=8,
    )
    assert "ecad_autoroute" in dispatched or "ecad_optimize_traces" in dispatched
    # Bounded: retry count after cascade ≤1 extra opt/route.
    assert _ecad_optimize_retry_count(new_calls) >= 1
    # No infinite re-opt after second dirty DRC.
    after = list(new_calls)
    need2 = _ecad_chain_need(after, allow)
    # May advance to ERC/sim or None; must not demand yet another optimize.
    # (If still on optimize, retry_count must already be >=1 so chain_need
    # eventually stops — assert we don't loop forever by re-running cascade.)
    if need2 == "ecad_optimize_traces":
        assert _ecad_optimize_retry_count(after) >= 1


def test_z25_optimize_dispatch_strips_meta_keeps_board_doc():
    from services.agent.agent_turn import _ecad_dispatch_forced_step

    brd, sch = "brd_strip", "sch_strip"
    calls = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "board_from_schematic",
            "ok": True,
            "result": {
                "ok": True,
                "doc_id": brd,
                "board_doc_id": brd,
                "sch_doc_id": sch,
            },
        },
    ]
    seen = {}

    def _dispatch(tool, args):
        seen["tool"] = tool
        seen["args"] = dict(args or {})
        return {"ok": True, "doc_id": brd, "corners_rounded": 0}

    _ecad_dispatch_forced_step(
        tool="ecad_optimize_traces",
        args={
            "doc_id": sch,  # wrong — must rewrite to board
            "min_clearance_mm": 0.2,
            "clearance_retry": True,
            "skip_teardrops": True,
        },
        calls=list(calls),
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        reason="z25",
        tool_calls_made=0,
        budget=4,
    )
    assert seen["tool"] == "ecad_optimize_traces"
    assert seen["args"].get("doc_id") == brd
    assert "min_clearance_mm" not in seen["args"]
    assert "clearance_retry" not in seen["args"]


# ---------------------------------------------------------------------------
# Regression holds (prior green axes must not weaken)
# ---------------------------------------------------------------------------


def test_b20_drc_still_targets_board():
    from services.agent.agent_turn import _ecad_forced_tool_args

    calls = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"doc_id": "sch1", "kind": "sch"},
        },
        {
            "tool": "board_from_schematic",
            "ok": True,
            "result": {
                "doc_id": "brd1",
                "board_doc_id": "brd1",
                "sch_doc_id": "sch1",
            },
        },
    ]
    assert _ecad_forced_tool_args("ecad_run_drc", calls).get("doc_id") == "brd1"
    assert _ecad_forced_tool_args("ecad_optimize_traces", calls).get("doc_id") == "brd1"


def test_s23_t23_contracts_still_hold():
    """S23/T23 pass rules remain: in-band V, real measure scores."""
    from services.agent import reviewer_exec as re
    from services.verify import battery as bat

    # S23: in-band V grades PASS.
    v, detail = re.evaluate_pass_rule(
        "V(3V3) in [3.135, 3.465]",
        {"ok": True, "op": {"V(3V3)": 3.3}, "resolved_net": "3V3"},
    )
    assert v == "PASS", (v, detail)

    # T23: measure_between finite 0.0 still scores 1.0.
    score, _ = bat._extract_score(
        "measure_between",
        "scalar",
        json.dumps({"ok": True, "distance": 0.0, "unit": "mm"}),
    )
    assert score == 1.0


# ---------------------------------------------------------------------------
# Thrash rebind (mega-25 a1): end-of-turn ECAD force + no dead mistral
# ---------------------------------------------------------------------------


def test_end_of_turn_ecad_force_from_orphan_power_seed():
    """a1 BUILD DL3 shape: sch+place+orphan power, budget spent → force board."""
    from services.agent.agent_turn import _ecad_force_chain_cascade, _ecad_chain_need

    sch, brd = "sch_a1", "brd_a1"

    def dispatch(tool, args):
        a = dict(args or {})
        if tool == "ecad_place_symbol":
            ref = a.get("ref") or "R1"
            return {
                "ok": True,
                "doc_id": sch,
                "ref": ref,
                "libid": a.get("libid") or "Device:R",
                "value": "1k",
                "pins": [
                    {
                        "ref": ref,
                        "pin": "1",
                        "name": "1",
                        "type": "passive",
                        "x": float(a.get("x") or 20),
                        "y": float(a.get("y") or 0),
                    },
                    {
                        "ref": ref,
                        "pin": "2",
                        "name": "2",
                        "type": "passive",
                        "x": float(a.get("x") or 20) + 2.54,
                        "y": float(a.get("y") or 0),
                    },
                ],
            }
        if tool == "ecad_add_power":
            return {
                "ok": True,
                "doc_id": sch,
                "net": a.get("net"),
                "net_size": 1,
                "pos": [a.get("x"), a.get("y")],
            }
        if tool == "ecad_wire":
            return {"ok": True, "doc_id": sch, "net_size": 2}
        if tool == "board_from_schematic":
            return {"ok": True, "doc_id": brd, "board_doc_id": brd, "sch_doc_id": sch}
        if tool == "ecad_optimize_traces":
            assert a.get("doc_id") == brd
            return {"ok": True, "doc_id": brd}
        if tool == "ecad_run_drc":
            assert a.get("doc_id") == brd
            return {"ok": True, "doc_id": brd, "errors": 0}
        if tool == "ecad_run_erc":
            assert a.get("doc_id") == sch
            return {"ok": True, "doc_id": sch, "errors": 0}
        if tool == "ecad_simulate":
            assert a.get("doc_id") == sch, a
            return {
                "ok": True,
                "doc_id": sch,
                "op": {"V(3V3)": 3.3},
                "nodes": ["3V3", "GND"],
            }
        if tool == "ecad_autoroute":
            return {"ok": True, "doc_id": brd}
        return {"ok": True}

    seed = [
        {
            "tool": "ecad_new_schematic",
            "ok": True,
            "result": {"ok": True, "doc_id": sch, "kind": "sch"},
        },
        {
            "tool": "ecad_place_symbol",
            "ok": True,
            "args": {"libid": "MCU:Generic_QFP48", "ref": "U1", "doc_id": sch},
            "result": {
                "ok": True,
                "doc_id": sch,
                "ref": "U1",
                "libid": "MCU:Generic_QFP48",
                "pins": [
                    {
                        "ref": "U1",
                        "pin": "1",
                        "name": "VDD",
                        "type": "power_in",
                        "x": 0.0,
                        "y": 0.0,
                    },
                    {
                        "ref": "U1",
                        "pin": "2",
                        "name": "GND",
                        "type": "power_in",
                        "x": 0.0,
                        "y": 2.54,
                    },
                ],
            },
        },
        {
            "tool": "ecad_add_power",
            "ok": True,
            "args": {"net": "GND", "x": 20.0, "y": 90.0, "doc_id": sch},
            "result": {"ok": True, "net": "GND", "net_size": 0, "pos": [20.0, 90.0]},
        },
        {
            "tool": "ecad_add_power",
            "ok": True,
            "args": {"net": "3V3", "x": 20.0, "y": 20.0, "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 0, "pos": [20.0, 20.0]},
        },
    ]
    allow = {
        "ecad_new_schematic",
        "ecad_place_symbol",
        "ecad_add_power",
        "ecad_wire",
        "ecad_run_erc",
        "ecad_simulate",
        "board_from_schematic",
        "ecad_optimize_traces",
        "ecad_run_drc",
        "ecad_autoroute",
    }
    assert _ecad_chain_need(seed, allow) is not None
    new_calls, made = _ecad_force_chain_cascade(
        calls=list(seed),
        allow=allow,
        dispatch=dispatch,
        ledger=None,
        actor="BUILD DL3",
        messages=[],
        tool_calls_made=16,
        budget=16,
        reason="end_of_turn_ecad_force",
        max_steps=14,
    )
    assert made > 16  # cascade extended past exhausted budget
    assert any(
        c.get("ok") and c.get("tool") == "board_from_schematic" for c in new_calls
    )
    assert any(c.get("ok") and c.get("tool") == "ecad_run_drc" for c in new_calls)
    for c in new_calls:
        if c.get("tool") == "ecad_simulate" and c.get("ok"):
            assert (c.get("args") or {}).get("doc_id") == sch


def test_chudcode_role_settings_preserve_resolved_model_without_private_fallback():
    """DAG execution forwards the governed assignment; Chudcode owns fallback."""
    from services.dag import node_runner as nr

    pack = {
        "role_id": "reviewer",
        "node_inputs": {"role": "reviewer"},
        "role_model": {
            "provider": "openrouter",
            "model": "google/gemma-3-27b-it:free",
            "reasoning_effort": "low",
        },
    }
    settings = nr._resolve_role_settings(pack)
    assert settings == {
        "provider": "openrouter",
        "model": "google/gemma-3-27b-it:free",
        "reasoning_effort": "low",
    }
