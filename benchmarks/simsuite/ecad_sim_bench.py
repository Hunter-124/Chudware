"""simsuite bench — ecad_sim: circuit simulation runtime vs circuit size + accuracy gates.

Cases
  rc_step_tau        RC step transient through the FULL user path (ecad_new_schematic ->
                     place/wire -> netlist -> SPICE circuit -> tran); metric = measured
                     tau (63.2% crossing), ref = R*C (1st-order ODE: v = V*(1-e^-t/RC)).
  rlc_ac_peak        series-RLC band-pass AC sweep; metric = peak frequency,
                     ref = 1/(2*pi*sqrt(L*C)) (series resonance).
  divider_op_cpp     C++ core (core/sim/spice.cpp via `sim_spice`) op on the 1k/2k
                     divider; metric = V(OUT), ref = V*R2/(R1+R2) (divider closed form).
                     Skips cleanly when libaicad is unavailable.
  rc_ladder_N        N-section RC ladder transient (Python MNA solver) — the runtime-vs-
                     size scaling series. metric = final voltage of the LAST ladder node,
                     ref = source V (all series R, no DC load: every node settles to V;
                     tstop = 8 * Elmore delay of the last node, sum_k R_k*C_downstream ~
                     R*C*N*(N+1)/2, so the slowest mode has fully settled).

quick=True runs N in (10, 100); full mode adds N=500. Engine runs in-process
(AI_CAD_ENGINE_DAEMON=0); no daemon, no GUI, no LLM paths.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.environ.setdefault("AI_CAD_ENGINE_DAEMON", "0")

DOMAIN = "ecad_sim"


def _case(case: str, **kw) -> dict:
    out = {"domain": DOMAIN, "case": case, "setup_s": 0.0, "mesh_s": 0.0,
           "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
           "rel_err": None, "status": "ok", "detail": ""}
    out.update(kw)
    return out


def _rel(metric: float, ref: float) -> float:
    return abs(metric - ref) / abs(ref) if ref else abs(metric)


# --------------------------------------------------------------------------- MCP-path RC


def _bench_rc_step_tau() -> dict:
    t0 = time.perf_counter()
    try:
        from mcp_server.ecad_tools import register_ecad_tools
        from engine.ecad import netlist as netlist_mod
        from engine.ecad import session as ecad_session
        from engine.sim.spice import netlist_to_spice_circuit
        from engine.sim.circuit import simulate as sim_run

        class _Stub:
            def __init__(self):
                self.tools = {}

            def tool(self, *a, **k):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        stub = _Stub()
        register_ecad_tools(stub)

        def call(_name, **kw):
            return json.loads(stub.tools[_name](**kw))

        r = call("ecad_new_schematic", name="bench_rc_step")
        doc = r["doc_id"]
        call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20,
             value="5", doc_id=doc)
        call("ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k",
             angle=90, doc_id=doc)
        call("ecad_place_symbol", libid="Device:C", x=20, y=0, value="1u",
             angle=90, doc_id=doc)
        call("ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2", doc_id=doc)
        call("ecad_wire", from_ref="R1", from_pin="1", to_ref="C1", to_pin="1", doc_id=doc)
        call("ecad_wire", from_ref="C1", from_pin="2", to_ref="V1", to_pin="-", doc_id=doc)
        call("ecad_add_power", net="GND", x=0, y=14.92, doc_id=doc)
        setup_s = time.perf_counter() - t0

        ses = ecad_session.get_session(doc)
        circuit, _w = netlist_to_spice_circuit(netlist_mod.build_netlist(ses.doc))
        cnodes = next(d for d in circuit["devices"] if d["kind"] == "C")["nodes"]
        out = next(n for n in cnodes if n != "0")
        t1 = time.perf_counter()
        res = sim_run(circuit, {"analyses": [{"type": "tran", "tstep": 1e-5,
                                              "tstop": 5e-3, "uic": True}],
                                "probes": [f"V({out})"]})
        solve_s = time.perf_counter() - t1
        tr = res["tran"]
        t = tr["x_t"]
        v = tr["traces"][f"V({out})"]
        vf = v[-1]
        tgt = vf * (1.0 - math.exp(-1.0))
        tau = None
        for i in range(1, len(v)):
            if (v[i - 1] - tgt) * (v[i] - tgt) <= 0 and v[i] != v[i - 1]:
                tau = t[i - 1] + (tgt - v[i - 1]) * (t[i] - t[i - 1]) / (v[i] - v[i - 1])
                break
        ref = 1000.0 * 1e-6  # tau = R*C
        ok = tau is not None and res["convergence"].get("tran")
        return _case("rc_step_tau", setup_s=round(setup_s, 4), solve_s=round(solve_s, 4),
                     total_s=round(time.perf_counter() - t0, 4),
                     metric=tau, ref=ref, rel_err=_rel(tau, ref) if ok else None,
                     status="ok" if ok else "fail",
                     detail="tau (s), full MCP schematic path" if ok else "no tau crossing")
    except Exception as e:  # noqa: BLE001
        return _case("rc_step_tau", total_s=round(time.perf_counter() - t0, 4),
                     status="fail", detail=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- RLC AC


def _bench_rlc_ac_peak() -> dict:
    t0 = time.perf_counter()
    try:
        from engine.sim.circuit import simulate as sim_run
        L, C, R = 1e-3, 1e-6, 10.0
        circ = {"nodes": ["0", "IN", "A", "OUT"], "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 1.0,
             "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
            {"kind": "L", "name": "L1", "nodes": ["IN", "A"], "value": L, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["A", "OUT"], "value": C, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["OUT", "0"], "value": R, "params": {}}],
            "models": {}, "params": {"TEMP": 27}}
        setup_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        res = sim_run(circ, {"analyses": [{"type": "ac", "sweep": "dec", "points": 120,
                                           "fstart": 1e3, "fstop": 3e4}],
                             "probes": ["V(OUT)"]})
        solve_s = time.perf_counter() - t1
        f = res["ac"]["x_f"]
        mag = res["ac"]["mag"]["V(OUT)"]
        imax = max(range(len(mag)), key=lambda i: mag[i])
        ref = 1.0 / (2 * math.pi * math.sqrt(L * C))  # f0 = 1/(2*pi*sqrt(LC))
        return _case("rlc_ac_peak", setup_s=round(setup_s, 4), solve_s=round(solve_s, 4),
                     total_s=round(time.perf_counter() - t0, 4),
                     metric=f[imax], ref=ref, rel_err=_rel(f[imax], ref),
                     detail="peak frequency (Hz), grid-limited")
    except Exception as e:  # noqa: BLE001
        return _case("rlc_ac_peak", total_s=round(time.perf_counter() - t0, 4),
                     status="fail", detail=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- C++ core op


def _bench_divider_op_cpp() -> dict:
    t0 = time.perf_counter()
    circ = {"nodes": ["0", "IN", "OUT"], "devices": [
        {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
        {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
        {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}}],
        "models": {}, "params": {"TEMP": 27}}
    study = {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]}
    try:
        from services.mcp import core_bridge
        t1 = time.perf_counter()
        cpp = core_bridge.call("sim_spice", {"circuit": circ, "study": study})
        solve_s = time.perf_counter() - t1
    except Exception as e:  # noqa: BLE001
        return _case("divider_op_cpp", total_s=round(time.perf_counter() - t0, 4),
                     status="skip", detail=f"libaicad unavailable: {type(e).__name__}: {e}")
    got = (cpp.get("op") or {}).get("V(OUT)")
    ref = 10.0 * 2000.0 / 3000.0  # divider: V*R2/(R1+R2)
    ok = got is not None
    return _case("divider_op_cpp", solve_s=round(solve_s, 4),
                 total_s=round(time.perf_counter() - t0, 4),
                 metric=got, ref=ref, rel_err=_rel(got, ref) if ok else None,
                 status="ok" if ok else "fail",
                 detail="C++ core/sim/spice.cpp op via sim_spice")


# --------------------------------------------------------------------------- RC ladder scaling


def _ladder_circuit(n: int, r: float = 1000.0, c: float = 1e-9) -> dict:
    """V1 -> R1 -> node1 -> R2 -> node2 ... -> nodeN, a C from every node to ground."""
    devices = [{"kind": "V", "name": "V1", "nodes": ["N0", "0"], "value": 1.0, "params": {}}]
    nodes = ["0", "N0"]
    for k in range(1, n + 1):
        a, b = f"N{k-1}", f"N{k}"
        nodes.append(b)
        devices.append({"kind": "R", "name": f"R{k}", "nodes": [a, b], "value": r,
                        "params": {}})
        devices.append({"kind": "C", "name": f"C{k}", "nodes": [b, "0"], "value": c,
                        "params": {"ic": 0.0}})
    return {"nodes": nodes, "devices": devices, "models": {}, "params": {"TEMP": 27}}


def _bench_rc_ladder(n: int) -> dict:
    t0 = time.perf_counter()
    try:
        from engine.sim.circuit import simulate as sim_run
        r_ohm, c_f = 1000.0, 1e-9
        circ = _ladder_circuit(n, r_ohm, c_f)
        setup_s = time.perf_counter() - t0
        # Elmore delay of the last node of a uniform RC ladder: sum_k (k sections of R
        # upstream) -> tau_N = R*C*N*(N+1)/2. Run to 8*tau_N so the response has settled.
        tau = r_ohm * c_f * n * (n + 1) / 2.0
        tstop = 8.0 * tau
        tstep = tstop / 400.0
        t1 = time.perf_counter()
        res = sim_run(circ, {"analyses": [{"type": "tran", "tstep": tstep,
                                           "tstop": tstop, "uic": True}],
                             "probes": [f"V(N{n})"]})
        solve_s = time.perf_counter() - t1
        conv = bool(res["convergence"].get("tran"))
        v = res["tran"]["traces"].get(f"V(N{n})", [])
        got = v[-1] if v else None
        ref = 1.0  # DC: no load current -> zero drop across the series Rs -> V(N) = V1
        ok = conv and got is not None
        return _case(f"rc_ladder_{n}", setup_s=round(setup_s, 4),
                     solve_s=round(solve_s, 4),
                     total_s=round(time.perf_counter() - t0, 4),
                     metric=got, ref=ref, rel_err=_rel(got, ref) if ok else None,
                     status="ok" if ok else "fail",
                     detail=f"{n}-section ladder, 401 tran steps, {2*n+1} devices")
    except Exception as e:  # noqa: BLE001
        return _case(f"rc_ladder_{n}", total_s=round(time.perf_counter() - t0, 4),
                     status="fail", detail=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- contract


def run(quick: bool = False) -> list[dict]:
    cases = [
        _bench_rc_step_tau(),
        _bench_rlc_ac_peak(),
        _bench_divider_op_cpp(),
    ]
    for n in (10, 100) if quick else (10, 100, 500):
        cases.append(_bench_rc_ladder(n))
    return cases


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    for c in run(quick=args.quick):
        print(json.dumps(c))
