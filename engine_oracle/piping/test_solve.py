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
    else:
        print("test_solve: SKIP fluids-dependent cases (fluids not installed)")
    test_pump_affinity()
    test_softfails()
    print("test_solve OK")


if __name__ == "__main__":
    main()
