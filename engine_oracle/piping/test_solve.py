"""Solver tests: native 1D pipe-network hydraulic solve + pump curves. House style: main() + asserts.

The hydraulic-solve tests need the optional ``fluids`` dependency; they SKIP cleanly when it is
not installed. The pump-curve CSV math and the soft-fail contract cases are pure-Python.

Run (from repo root):  python -m engine.piping.test_solve
"""

from __future__ import annotations

try:
    import pytest
except Exception:  # noqa: BLE001
    pytest = None

from engine.piping import schema, solve

HAVE_FLUIDS = solve.have_fluids()
_needs_fluids = (pytest.mark.skipif(not HAVE_FLUIDS, reason="fluids not installed")
                 if pytest else (lambda f: f))


def _single_pipe_doc():
    doc = schema.new_piping("case1")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [10000, 0, 0]], nps=2, schedule="40"))
    return doc


@_needs_fluids
def test_single_pipe_darcy():
    doc = _single_pipe_doc()
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 300},
                                     {"node": "n1", "pressure": 100}])
    assert res["ok"] and res["converged"], res
    assert abs(res["links"][0]["dp_kpa"] - 200.0) < 1.0, res["links"][0]
    assert res["links"][0]["flow_m3h"] > 0, res["links"][0]
    assert abs(res["max_imbalance_m3h"]) < 1e-3, res


@_needs_fluids
def test_series_mass_conservation_and_elbow_K():
    doc = schema.new_piping("series")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [5000, 0, 0]], nps=2, schedule="40"))
    doc["runs"].append(schema.new_pipe_run("r2", [[5000, 0, 0], [5000, 5000, 0]], nps=2, schedule="40"))
    doc["fittings"].append(schema.new_fitting("ft1", "elbow", nps=2, origin=(5000, 0, 0),
                                              schedule="40", params={"angle": 90, "radius": "LR"}))
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 400},
                                     {"node": "n2", "pressure": 100}])
    assert res["ok"] and res["converged"], res
    links = {ln["id"]: ln for ln in res["links"]}
    assert abs(links["r1"]["flow_m3h"] - links["r2"]["flow_m3h"]) < 1e-2, res["links"]
    # Exactly one run carries the elbow K.
    ks = sorted([links["r1"]["k_total"], links["r2"]["k_total"]])
    assert ks[0] == 0 and ks[1] > 0, res["links"]


@_needs_fluids
def test_demand_driven_flow():
    doc = schema.new_piping("demand")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [100000, 0, 0]], nps=4, schedule="40"))
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 500},
                                     {"node": "n1", "demand": 50.0}])
    assert res["ok"] and res["converged"], res
    assert abs(res["links"][0]["flow_m3h"] - 50.0) < 0.1, res["links"][0]


@_needs_fluids
def test_branch_split():
    doc = schema.new_piping("branch")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [10000, 0, 0]], nps=4, schedule="40"))
    doc["runs"].append(schema.new_pipe_run("r2", [[10000, 0, 0], [20000, 0, 0]], nps=3, schedule="40"))
    doc["runs"].append(schema.new_pipe_run("r3", [[10000, 0, 0], [10000, 10000, 0]], nps=3, schedule="40"))
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 600},
                                     {"node": "n2", "demand": 30},
                                     {"node": "n3", "demand": 20}])
    assert res["ok"] and res["converged"], res
    flows = {ln["id"]: ln["flow_m3h"] for ln in res["links"]}
    assert abs(flows["r1"] - 50.0) < 0.1, flows
    assert abs((flows["r2"] + flows["r3"]) - 50.0) < 0.1, flows


@_needs_fluids
def test_parallel_loop():
    doc = schema.new_piping("parallel")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [5000, 0, 0], [10000, 0, 0]], nps=2, schedule="40"))
    doc["runs"].append(schema.new_pipe_run("r2", [[0, 0, 0], [5000, -3000, 0], [10000, 0, 0]], nps=3, schedule="40"))
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 300},
                                     {"node": "n1", "demand": 60}])
    assert res["ok"] and res["converged"], res
    assert len(res["nodes"]) == 2, res["nodes"]
    flows = {ln["id"]: ln["flow_m3h"] for ln in res["links"]}
    assert abs((flows["r1"] + flows["r2"]) - 60.0) < 0.1, flows
    # The larger (nps3) run carries more flow.
    assert flows["r2"] > flows["r1"], flows


@_needs_fluids
def test_gravity_riser():
    doc = schema.new_piping("riser")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [0, 0, 20000]], nps=4, schedule="40"))
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 300},
                                     {"node": "n1", "demand": 0.001}])
    assert res["ok"] and res["converged"], res
    # Top node (z=20000) pressure ~ 300 - rho*g*20 ~ 104 kPa.
    assert abs(res["nodes"][1]["pressure_kpa"] - 104.0) < 3.0, res["nodes"]


def test_pump_affinity():
    curve = "flow,head\n0,50\n50,45\n100,30\n150,5\n"
    p1 = solve.load_pump_curve(curve)
    assert p1["ok"], p1
    assert p1["shutoff_head"] == 50, p1
    assert p1["max_flow"] == 150, p1
    p2 = solve.load_pump_curve(curve, speed_ratio=0.5)
    assert p2["ok"], p2
    assert abs(p2["shutoff_head"] - 12.5) < 1e-6, p2   # H' = H * 0.5^2
    assert abs(p2["max_flow"] - 75.0) < 1e-6, p2       # Q' = Q * 0.5
    # Soft-fail: empty and missing flow/head columns.
    assert solve.load_pump_curve("")["ok"] is False
    assert solve.load_pump_curve("x,y\n1,2\n3,4\n")["ok"] is False


@_needs_fluids
def test_pump_in_network():
    doc = schema.new_piping("pumpnet")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [50000, 0, 0]], nps=3, schedule="40"))
    res = solve.solve(doc,
                      boundary=[{"node": "n0", "pressure": 100},
                                {"node": "n1", "demand": 40.0}],
                      pumps=[{"run": "r1",
                              "curve": "flow,head\n0,50\n50,45\n100,30\n150,5\n",
                              "direction": "a_to_b"}])
    assert res["ok"] and res["converged"], res
    assert abs(res["links"][0]["flow_m3h"] - 40.0) < 0.5, res["links"][0]
    assert res["links"][0]["pump_head_m"] > 0, res["links"][0]


@_needs_fluids
def test_pressure_drop_report():
    doc = _single_pipe_doc()
    res = solve.solve(doc, boundary=[{"node": "n0", "pressure": 300},
                                     {"node": "n1", "pressure": 100}])
    assert res["ok"], res
    rep = solve.pressure_drop_report(doc, res)
    assert rep["ok"], rep
    assert rep["total_dp_kpa"] > 0, rep
    assert len(rep["segments"]) == 1, rep
    assert rep["segments"][0]["breakdown"][0]["item"] == "pipe friction", rep["segments"][0]


def test_softfails():
    # (a) No fixed node -> under-constrained.
    doc = _single_pipe_doc()
    a = solve.solve(doc, boundary=[{"node": "n0", "demand": 10}])
    assert a["ok"] is False, a
    # (b) Bad node reference.
    doc = _single_pipe_doc()
    b = solve.solve(doc, boundary=[{"node": "nX", "pressure": 100}])
    assert b["ok"] is False, b
    # (c) Schema-invalid doc (run with a single path point) -> errors, before fluids check.
    bad = schema.new_piping("bad")
    bad["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0]], nps=2, schedule="40"))
    c = solve.solve(bad, boundary=[{"node": "n0", "pressure": 100},
                                   {"node": "n1", "pressure": 50}])
    assert c["ok"] is False and "errors" in c, c


# ── gas (compressible) network solve ──────────────────────────────────────────

@_needs_fluids
def test_gas_single_pipe_weymouth():
    """Isothermal gas single-pipe mass flow matches the Weymouth closed form (hand calc)."""
    import math
    from engine.piping import catalog
    from engine.piping.solve import R_UNIVERSAL
    doc = schema.new_piping("gas1")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [1000000, 0, 0]], nps=8, schedule="40"))
    res = solve.solve(doc, network_type="gas",
                      fluid={"name": "methane", "T": 288.15, "P": 3.0e6},
                      boundary=[{"node": "n0", "pressure": 3000},
                                {"node": "n1", "pressure": 2900}])
    assert res["ok"] and res["converged"], res
    assert res["network_type"] == "gas", res
    lk = res["links"][0]
    assert abs(res["max_imbalance_kgh"]) < 1e-3, res
    # Hand calc: ṁ² = A²·M/(R·T)·(P1²−P2²)/(f·L/D), using the solver's own converged fd.
    spec = catalog.nearest_pipe(nps=8, schedule="40")
    D = spec.inner_dia / 1000.0
    A = math.pi * D ** 2 / 4.0
    M, T, Lm = 0.0160425, 288.15, 1000.0
    P1, P2 = 3000 * 1000 + 101325, 2900 * 1000 + 101325
    fd = lk["friction_factor"]
    mdot = math.sqrt(A ** 2 * M / (R_UNIVERSAL * T) * (P1 ** 2 - P2 ** 2) / (fd * Lm / D))
    assert abs(lk["mass_flow_kgh"] - mdot * 3600.0) / (mdot * 3600.0) < 0.01, (lk, mdot * 3600.0)
    assert lk["std_flow_nm3h"] > 0 and lk["mass_flow_kgh"] > 0, lk


@_needs_fluids
def test_gas_series_mass_conservation():
    doc = schema.new_piping("gas2")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [500000, 0, 0]], nps=6, schedule="40"))
    doc["runs"].append(schema.new_pipe_run("r2", [[500000, 0, 0], [1000000, 0, 0]], nps=6, schedule="40"))
    res = solve.solve(doc, network_type="gas",
                      fluid={"name": "natural_gas", "T": 288.15, "P": 2.0e6},
                      boundary=[{"node": "n0", "pressure": 2000},
                                {"node": "n2", "pressure": 1800}])
    assert res["ok"] and res["converged"], res
    flows = {ln["id"]: ln["mass_flow_kgh"] for ln in res["links"]}
    assert abs(flows["r1"] - flows["r2"]) < 1e-3, res["links"]     # mass conserved
    mid = res["nodes"][1]["pressure_abs_kpa"]
    assert 1800 + 101.325 < mid < 2000 + 101.325, res["nodes"]     # monotone pressure fall


def test_gas_needs_named_fluid():
    doc = schema.new_piping("gas3")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [1000, 0, 0]], nps=2, schedule="40"))
    # {rho,mu} has no molar mass -> gas solve cannot proceed, contract-shaped.
    r = solve.solve(doc, network_type="gas", fluid={"rho": 1.2, "mu": 1.8e-5},
                    boundary=[{"node": "n0", "pressure": 100}, {"node": "n1", "pressure": 50}])
    assert r["ok"] is False and "compressible" in r["reason"], r


# ── operating point + NPSH margin (pure-python; no fluids dependency) ──────────

_PUMP_CSV = "flow,head\n0,50\n50,45\n100,30\n150,5\n"


def test_operating_point_duty():
    # System: H_sys = 20 + 0.002·Q². Intersection on the 50–100 pump segment
    # (linear 45->30: H=45-0.3(Q-50)) solves to Q*=85.078 m³/h analytically.
    op = solve.operating_point(_PUMP_CSV, system={"static_head_m": 20.0, "k": 0.002})
    assert op["ok"], op
    assert abs(op["duty_point"]["flow_m3h"] - 85.078) < 0.05, op
    assert abs(op["duty_point"]["head_m"] - (20.0 + 0.002 * 85.078 ** 2)) < 0.05, op


def test_operating_point_npsh_margin():
    op = solve.operating_point(
        _PUMP_CSV, system={"static_head_m": 20.0, "k": 0.002},
        fluid={"name": "water", "T": 293.15},
        suction={"pressure_kpa": 50.0, "elevation_m": 2.0, "friction_m": 0.5},
        npshr_curve="flow,npshr\n0,2\n50,2.5\n100,4\n150,7\n")
    npsh = op["npsh"]
    # NPSHa = (101325 + 50000 − 2339)/(998.21·9.80665) + 2.0 − 0.5 ≈ 16.72 m.
    hand_npsha = (101325.0 + 50000.0 - 2339.0) / (998.21 * 9.80665) + 2.0 - 0.5
    assert abs(npsh["npsha_m"] - hand_npsha) < 0.05, npsh
    # NPSHr interp @ Q*≈85.08: 2.5 + (35.08/50)·1.5 ≈ 3.55 m.
    assert abs(npsh["npshr_m"] - 3.55) < 0.1, npsh
    assert abs(npsh["margin_m"] - (npsh["npsha_m"] - npsh["npshr_m"])) < 2e-3, npsh
    assert "NIST" in (npsh["pvap_source"] or ""), npsh


def test_operating_point_no_intersection():
    # Static lift (60 m) above the pump shutoff head (50 m) -> no duty point.
    op = solve.operating_point(_PUMP_CSV, system={"static_head_m": 60.0, "k": 0.001})
    assert op["ok"] is False and "shutoff" in op["reason"], op


@_needs_fluids
def test_operating_point_derived_system_curve():
    doc = schema.new_piping("opnet")
    doc["runs"].append(schema.new_pipe_run("r1", [[0, 0, 0], [30000, 0, 0]], nps=3, schedule="40"))
    op = solve.operating_point(
        _PUMP_CSV, doc=doc,
        boundary=[{"node": "n0", "head": 5.0}, {"node": "n1", "demand": 40.0}],
        q_ref_m3h=40.0)
    assert op["ok"], op
    assert op["duty_point"]["flow_m3h"] > 0, op
    assert "derived_from" in op["system_curve"], op


def main():
    if HAVE_FLUIDS:
        test_single_pipe_darcy()
        test_series_mass_conservation_and_elbow_K()
        test_demand_driven_flow()
        test_branch_split()
        test_parallel_loop()
        test_gravity_riser()
        test_pump_in_network()
        test_pressure_drop_report()
        test_gas_single_pipe_weymouth()
        test_gas_series_mass_conservation()
        test_operating_point_derived_system_curve()
    else:
        print("test_solve: SKIP fluids-dependent cases (fluids not installed)")
    test_pump_affinity()
    test_softfails()
    test_gas_needs_named_fluid()
    test_operating_point_duty()
    test_operating_point_npsh_margin()
    test_operating_point_no_intersection()
    print("test_solve OK")


if __name__ == "__main__":
    main()
