"""Mega-23 fixes — AC5 SPICE probe readback + AC1 measure_between scoring.

Grounded in mega-22 a2 primary (reports/runs/mega-22/attempt-2-primary-graded-live7/):

  S23 / AC5 — circuit driven (device_count=2) but V(3V3) extracts null.
              Singular matrix from V- shorted to rail; also need case/format
              robust net→vector resolution (v(3v3)/V(3V3)/aliases).

  T23 / AC1 — measure_between issued geometrically valid query but score 0.0
              (feature id 'f1' / free-text treated as side words). Scorer must
              treat successful finite distance (incl. 0.0 coincident) as real.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_MEGA22_A2 = _ROOT / "reports/runs/mega-22/attempt-2-primary-graded-live7"
_MEGA22_AC5 = _MEGA22_A2 / "criterion_AC5.json"
_MEGA22_AC1 = _MEGA22_A2 / "criterion_AC1.json"
_MEGA22_BATTERY = _MEGA22_A2 / "battery_result.json"
# Root-level copies may be the primary after archive.
_MEGA22_AC5_ROOT = _ROOT / "reports/runs/mega-22/criterion_AC5.json"
_MEGA22_BATTERY_ROOT = _ROOT / "reports/runs/mega-22/battery_result.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ac5_fixture() -> dict:
    for p in (_MEGA22_AC5, _MEGA22_AC5_ROOT):
        if p.is_file():
            return _load_json(p)
    pytest.skip("mega-22 AC5 fixture missing")


def _battery_fixture() -> dict:
    for p in (_MEGA22_BATTERY, _MEGA22_BATTERY_ROOT):
        if p.is_file():
            return _load_json(p)
    pytest.skip("mega-22 battery fixture missing")


# ---------------------------------------------------------------------------
# Fixtures — mega-22 a2 fingerprints
# ---------------------------------------------------------------------------


def test_mega22_a2_ac5_fingerprint_null_v_driven():
    """S23 fixture: device_count=2, V(3V3)=null, singular matrix."""
    ac5 = _ac5_fixture()
    assert ac5.get("result") == "FAIL"
    ev = ac5.get("evidence") or {}
    assert int(ev.get("device_count") or 0) == 2
    op = ev.get("op") or {}
    assert "V(3V3)" in op or "v(3v3)" in {k.lower() for k in op}
    # Value is null / non-finite.
    val = op.get("V(3V3)", op.get("v(3v3)"))
    assert val is None or (
        isinstance(val, float) and not math.isfinite(val)
    )
    log = " ".join(ev.get("sim_log") or []).lower()
    assert "singular" in log or not ev.get("converged")


def test_mega22_a2_ac1_fingerprint_soft_zero_measure_between():
    """T23 fixture: measure_between score 0 from bad side 'f1'."""
    ac1 = _load_json(_MEGA22_AC1) if _MEGA22_AC1.is_file() else None
    bat = _battery_fixture()
    scores = (bat.get("evidence") or {}).get("scores") or {}
    if ac1:
        scores = (ac1.get("evidence") or {}).get("scores") or scores
    assert float(scores.get("measure_between", 1.0)) == 0.0
    findings = (bat.get("evidence") or {}).get("findings") or []
    if ac1:
        findings = (ac1.get("evidence") or {}).get("findings") or findings
    blob = json.dumps(findings).lower()
    assert "f1" in blob or "bad" in blob and "side" in blob


# ---------------------------------------------------------------------------
# S23 — SPICE probe readback robustness
# ---------------------------------------------------------------------------


def test_resolve_node_case_insensitive():
    """Engine maps V(3v3)/v(3V3)/+3V3 aliases onto schematic net 3V3.

    Retargeted 2026-07-22: engine.sim.circuit was deleted with the MNA stack
    (T-RF20); the C++ spice (core/sim/spice.cpp resolve_node_tolerant) now
    owns the behavior. Asserted at the RPC level; internals are covered by
    core/tests/test_spice_netlist.cpp + test_spice_sweep.cpp."""
    from services.mcp import core_bridge as cb

    circuit = {
        "nodes": ["0", "3V3"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["3V3", "0"], "value": 3.3},
            {"kind": "R", "name": "R1", "nodes": ["3V3", "0"], "value": 1000.0},
        ],
        "models": {},
        "params": {},
    }
    r = cb.call("sim_spice", {"circuit": circuit,
                              "study": {"analyses": [{"type": "op"}],
                                        "probes": ["V(3v3)", "V(3V3)", "V(+3V3)"]}})
    op = r.get("op") or {}
    assert op.get("V(3v3)") == 3.3 and op.get("V(3V3)") == 3.3 and op.get("V(+3V3)") == 3.3, r


def test_probe_eval_accepts_lower_case_v3v3():
    """OP probe V(3v3) returns finite voltage when node is 3V3 (C++ spice, T-RF20)."""
    from services.mcp import core_bridge as cb

    circuit = {
        "nodes": ["0", "3V3"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["3V3", "0"], "value": 3.3},
            {"kind": "R", "name": "R1", "nodes": ["3V3", "0"], "value": 1000.0},
        ],
        "models": {},
        "params": {},
    }
    for probe in ("V(3V3)", "V(3v3)", "v(3v3)", "v(3V3)"):
        res = cb.call("sim_spice", {
            "circuit": circuit,
            "study": {"analyses": [{"type": "op"}], "probes": [probe]},
        })
        assert res["convergence"]["op"], (probe, res.get("log"))
        op = res["op"]
        vals = [v for k, v in op.items() if v is not None and math.isfinite(float(v))]
        assert vals, (probe, op)
        assert abs(float(vals[0]) - 3.3) < 1e-6, (probe, vals)


def test_ac5_grader_on_mega22_a2_null_payload():
    """Grader still honest-FAIL on a2 null (not NOT-CHECKED); band intact."""
    from services.agent.reviewer_exec import FAIL, evaluate_pass_rule

    ac5 = _ac5_fixture()
    payload = dict(ac5.get("evidence") or {})
    v, d = evaluate_pass_rule("V(3V3) in [3.135, 3.465]", payload)
    assert v == FAIL, (v, d)
    assert "null" in d.lower() or "not finite" in d.lower() or "3v3" in d.lower()


def test_ac5_grader_accepts_in_band_after_driven_fix():
    """When V is finite in-band, PASS regardless of prior null residual."""
    from services.agent.reviewer_exec import PASS, evaluate_pass_rule

    payload = {
        "ok": True,
        "analysis": "op",
        "backend": "mna",
        "converged": True,
        "device_count": 2,
        "nodes": ["3V3", "0"],
        "probes": ["V(3V3)"],
        "resolved_net": "3V3",
        "op": {"V(3V3)": 3.30, "v(3v3)": 3.30},
        "reading": "V(3V3) = 3.3 V",
    }
    v, d = evaluate_pass_rule("V(3V3) in [3.135, 3.465]", payload)
    assert v == PASS, (v, d)


def test_ensure_driven_wires_v_minus_to_gnd():
    """Even when topology looks driven, force V- → GND return wire."""
    from services.agent.agent_turn import _ecad_ensure_driven_rail

    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"doc_id": "sch-a2"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Simulation_SPICE:V_source", "ref": "V1",
                     "value": "DC 3.3"},
            "result": {
                "ok": True, "ref": "V1",
                "libid": "Simulation_SPICE:V_source",
                "pins": [
                    {"number": "+", "name": "+", "net": "3V3"},
                    {"number": "-", "name": "-", "net": "3V3"},  # a2 short
                ],
            },
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "value": "1k"},
            "result": {
                "ok": True, "ref": "R1", "libid": "Device:R",
                "pins": [
                    {"number": "1", "net": "3V3"},
                    {"number": "2", "net": "N$2"},
                ],
            },
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "V1", "from_pin": "+",
                     "to_ref": "R1", "to_pin": "1", "net": "3V3"},
            "result": {
                "ok": True, "net": "3V3",
                "net_members": ["V1.+", "R1.1"],
            },
        },
        {
            "tool": "ecad_simulate", "ok": True,
            "result": {
                "ok": True, "device_count": 2, "nodes": ["3V3", "N$1", "N$2"],
                "op": {"V(3V3)": None}, "resolved_net": "3V3",
                "converged": False,
                "sim_log": ["op: linear solve failed — dense solve singular"],
            },
        },
    ]
    dispatched = []

    def _dispatch(tool, args):
        dispatched.append((tool, dict(args or {})))
        if tool == "ecad_add_power":
            return {
                "ok": True, "net": args.get("net"),
                "net_members": ["#PWR.1"], "net_size": 1,
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": args.get("net"),
                "net_members": [
                    f"{args.get('from_ref')}.{args.get('from_pin')}",
                    f"{args.get('to_ref')}.{args.get('to_pin')}",
                ],
            }
        return {"ok": True}

    allow = {"ecad_place_symbol", "ecad_wire", "ecad_add_power", "ecad_simulate"}
    new_calls, made, steps, _ex = _ecad_ensure_driven_rail(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=20,
        reason="mega23_s23",
        steps=0,
        max_steps=20,
    )
    # Must issue a GND return for V- (mega-24: net stays on call_rec, stripped
    # from MCP dispatch — ecad_wire has no net kwarg).
    gnd_wires = [
        c.get("args") or {}
        for c in new_calls
        if c.get("tool") == "ecad_wire"
        and c.get("forced")
        and str((c.get("args") or {}).get("net", "")).upper() == "GND"
    ]
    assert gnd_wires, (
        f"expected V-/load → GND wire on call_rec; dispatched={dispatched} "
        f"new_calls={[c for c in new_calls if c.get('forced')]}"
    )
    # Prefer explicit V- pin on a GND net wire.
    assert any(
        str(a.get("from_ref", "")).upper().startswith("V")
        or str(a.get("to_ref", "")).upper().startswith("V")
        for a in gnd_wires
    ), gnd_wires
    # Dispatch path must not TypeError on net= (stripped).
    for t, a in dispatched:
        if t == "ecad_wire":
            assert "net" not in a, a
    assert made >= 1
    assert steps >= 1
    assert isinstance(new_calls, list)


def test_has_sim_source_recognizes_simulation_spice_v():
    from services.agent.agent_turn import _ecad_has_sim_source

    calls = [{
        "tool": "ecad_place_symbol", "ok": True,
        "args": {"libid": "Simulation_SPICE:V_source", "ref": "Vrail"},
        "result": {"ok": True, "ref": "Vrail",
                   "libid": "Simulation_SPICE:V_source"},
    }]
    assert _ecad_has_sim_source(calls) is True


# ---------------------------------------------------------------------------
# T23 — measure_between real nonzero-scored measurement
# ---------------------------------------------------------------------------


def test_measure_between_score_finite_distance():
    """Successful finite distance → score 1.0 (not silent zero)."""
    from services.verify.battery import _extract_score

    score, findings = _extract_score(
        "measure_between",
        "scalar",
        json.dumps({"ok": True, "distance": 500.0, "summary": "distance 500 mm"}),
    )
    assert score == 1.0, (score, findings)


def test_measure_between_score_zero_distance_is_honest_coincidence():
    """0.0 distance with valid refs = coincidence finding, score 1.0."""
    from services.verify.battery import _extract_score

    score, findings = _extract_score(
        "measure_between",
        "scalar",
        json.dumps({"ok": True, "distance": 0.0, "summary": "distance 0 mm"}),
    )
    assert score == 1.0, (score, findings)
    blob = json.dumps(findings).lower()
    assert "coincident" in blob or "0.0" in blob


