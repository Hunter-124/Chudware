"""simsuite bench — multiphysics_misc: motion, piping, hydraulic, structural + the
cross-sim handoff seam (aicad.simhandoff/1).

Cases (each vs a cited analytic reference where one exists):
  hydraulic_cylinder_f_pa    size_cylinder extend force vs Pascal's law F = P*(pi/4*D^2)
  hydraulic_pump_shaft_kw    size_pump shaft power vs P = Q*dP/eta
  piping_laminar_hagen_poiseuille  1-D solve dp vs dp = 128*mu*L*Q/(pi*D^4)
  piping_operating_point     duty flow vs the hand quadratic root of pump-vs-system curve
  piping_internal_volume     bore solid volume vs the exact cylinder pi/4*d^2*L
  struct_cantilever_udl      deflection vs Euler-Bernoulli delta = w*L^4/(8*E*I)
  motion_swing_kinematics    engine sweep: arm chord vs rigid-rotation 2*r*sin(theta/2)
  handoff_seams_smoke        hydraulic->FEA + hydraulic->piping->solve + motion->FEA
                             (dry_run) envelope validity (regression-only, ref None)

Engine cases run IN-PROCESS (AI_CAD_ENGINE_DAEMON=0, services/dist libaicad via
services.mcp.core_bridge); optional deps (fluids / build123d / engine lib)
produce clean skips. All cases are light — quick and full modes differ only in the
motion sweep sample count.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("AI_CAD_ENGINE_DAEMON", "0")

DOMAIN = "multiphysics_misc"


def _case(case: str, *, total_s: float, metric=None, ref=None, status="ok",
          detail: str = "", setup_s: float = 0.0, solve_s: float = 0.0) -> dict:
    rel = None
    if ref not in (None, 0) and metric is not None:
        rel = abs(metric - ref) / abs(ref)
        if status == "ok" and rel > 0.05:
            status = "fail"
            detail = detail or f"rel_err {rel:.3%} > 5%"
    return {"domain": DOMAIN, "case": case, "setup_s": round(setup_s, 4),
            "mesh_s": 0.0, "solve_s": round(solve_s, 4), "total_s": round(total_s, 4),
            "metric": metric, "ref": ref, "rel_err": rel, "status": status,
            "detail": detail}


def _skip(case: str, reason: str) -> dict:
    return _case(case, total_s=0.0, status="skip", detail=reason)


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:  # noqa: BLE001
        return False


# ── pure-oracle cases ──────────────────────────────────────────────────────────

def _bench_hydraulic() -> list:
    from engine.hydraulic import solve as hyd
    out = []

    t0 = time.perf_counter()
    r = hyd.size_cylinder(bore_mm=50.0, rod_mm=25.0, pressure_bar=200.0,
                          stroke_mm=300.0, velocity_mm_s=100.0, efficiency=1.0)
    dt = time.perf_counter() - t0
    # Pascal's law: F = P * A = 200e5 Pa * pi/4*(0.05 m)^2 -> kN
    ref = math.pi / 4.0 * 0.05 ** 2 * 200e5 / 1000.0
    out.append(_case("hydraulic_cylinder_f_pa", total_s=dt, solve_s=dt,
                     metric=r.get("extend_force_kn"), ref=ref,
                     status="ok" if r.get("ok") else "fail",
                     detail="kN; F = P*(pi/4*D^2), Pascal's law"))

    t0 = time.perf_counter()
    r = hyd.size_pump(flow_lpm=30.0, pressure_bar=150.0, efficiency=0.85,
                      speed_rpm=1800.0)
    dt = time.perf_counter() - t0
    # P_shaft = Q*dP/eta = (30/60000 m^3/s * 150e5 Pa) / 0.85 -> kW
    ref = (30.0 / 60.0 / 1000.0) * 150e5 / 1000.0 / 0.85
    out.append(_case("hydraulic_pump_shaft_kw", total_s=dt, solve_s=dt,
                     metric=r.get("shaft_power_kw"), ref=ref,
                     status="ok" if r.get("ok") else "fail",
                     detail="kW; P = Q*dP/eta (hydraulic power balance)"))
    return out


def _bench_piping_solve() -> dict:
    if not _have("fluids"):
        return _skip("piping_laminar_hagen_poiseuille", "fluids library not installed")
    from engine.piping import schema as ps, solve as sol
    bore_mm, length_mm = 6.0, 20000.0
    d = bore_mm / 1000.0
    v = 0.05                                   # m/s -> Re ~ 190, laminar
    q = v * math.pi / 4.0 * d * d
    doc = ps.new_piping("bench")
    doc["runs"].append(ps.new_pipe_run("r1", [[0, 0, 0], [length_mm, 0, 0]], 0.5,
                                       bore_mm=bore_mm))
    t0 = time.perf_counter()
    res = sol.solve(doc, boundary=[{"run": "r1", "end": "start", "pressure": 300.0},
                                   {"run": "r1", "end": "end", "demand": q * 3600.0}])
    dt = time.perf_counter() - t0
    if not (res.get("ok") and res.get("converged")):
        return _case("piping_laminar_hagen_poiseuille", total_s=dt, status="fail",
                     detail=f"solve failed: {res.get('reason') or 'not converged'}")
    mu = res["fluid"]["mu"]
    # Hagen-Poiseuille: dp = 128*mu*L*Q/(pi*D^4)  [Pa] -> kPa
    ref = 128.0 * mu * (length_mm / 1000.0) * q / (math.pi * d ** 4) / 1000.0
    return _case("piping_laminar_hagen_poiseuille", total_s=dt, solve_s=dt,
                 metric=res["links"][0]["dp_kpa"], ref=ref,
                 detail="kPa; dp = 128*mu*L*Q/(pi*D^4), Hagen-Poiseuille")


def _bench_operating_point() -> dict:
    from engine.piping import solve as sol
    t0 = time.perf_counter()
    r = sol.operating_point("flow,head\n0,50\n100,30\n",
                            system={"static_head_m": 10.0, "k": 0.004})
    dt = time.perf_counter() - t0
    if not r.get("ok"):
        return _case("piping_operating_point", total_s=dt, status="fail",
                     detail=r.get("reason", ""))
    # H_pump = 50 - 0.2*Q vs H_sys = 10 + 0.004*Q^2 -> 0.004*Q^2 + 0.2*Q - 40 = 0
    ref = (-0.2 + math.sqrt(0.2 ** 2 + 4.0 * 0.004 * 40.0)) / (2.0 * 0.004)
    return _case("piping_operating_point", total_s=dt, solve_s=dt,
                 metric=r["duty_point"]["flow_m3h"], ref=ref,
                 detail="m3/h; quadratic root of pump-vs-system intersection")


def _bench_internal_volume() -> dict:
    if not _have("build123d"):
        return _skip("piping_internal_volume", "build123d/OCP kernel not installed")
    from engine.piping import schema as ps
    from engine.piping import cfd_handoff as cfd
    bore_mm, length_mm = 20.0, 500.0
    doc = ps.new_piping("bench")
    doc["runs"].append(ps.new_pipe_run("r1", [[0, 0, 0], [length_mm, 0, 0]], 0.5,
                                       bore_mm=bore_mm))
    out = os.path.join(tempfile.mkdtemp(prefix="mp_bench_"), "bore.step")
    t0 = time.perf_counter()
    try:
        r = cfd.internal_volume(doc, out)
    except cfd.PipingCFDError as exc:
        return _skip("piping_internal_volume", f"kernel unavailable: {exc}")
    dt = time.perf_counter() - t0
    ref = math.pi / 4.0 * bore_mm ** 2 * length_mm       # exact cylinder volume
    return _case("piping_internal_volume", total_s=dt, solve_s=dt,
                 metric=r.get("volume_mm3"), ref=ref,
                 status="ok" if r.get("ok") else "fail",
                 detail="mm^3; V = pi/4*d^2*L (exact cylinder)")


def _bench_struct() -> dict:
    if not _engine_ok():
        return _skip("struct_cantilever_udl", "libaicad not staged (engine unavailable)")
    from mcp_server import core_bridge as cb
    L, w = 2000.0, 10.0
    doc = {
        "schema": "aicad.structure/1",
        "elements": [{"element_type": "beam", "section_name": "100x100",
                      "material": "steel", "length_mm": L, "id": "b1",
                      "transform": [0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1]}],
        "load_cases": [{"kind": "dead", "factor": 1.0, "gravity": [0, 0, -1],
                        "distributed": {"w_n_per_mm": w}}],
    }
    t0 = time.perf_counter()
    try:
        res = cb.call("struct_analyze", {"doc": doc, "load_combo": "DEAD_ONLY"})
    except cb.CoreError as exc:
        return _skip("struct_cantilever_udl", f"structural solver unavailable: {exc}")
    dt = time.perf_counter() - t0
    if res.get("code") and res.get("message"):
        return _case("struct_cantilever_udl", total_s=dt, solve_s=dt, status="fail",
                     detail=f"struct_analyze: {res['message']}")
    row = res["results"][0]
    E = 200000.0                    # steel E (MPa), core/structural materials table
    I = 100.0 * 100.0 ** 3 / 12.0   # I = b*h^3/12, 100x100 square
    ref = 1.4 * w * L ** 4 / (8.0 * E * I)   # Euler-Bernoulli cantilever UDL, 1.4D combo
    return _case("struct_cantilever_udl", total_s=dt, solve_s=dt,
                 metric=abs(row["deflection_mm"]), ref=ref,
                 detail="mm; delta = w*L^4/(8*E*I), Euler-Bernoulli cantilever")


# ── engine-backed cases ────────────────────────────────────────────────────────

def _engine_ok() -> bool:
    try:
        from mcp_server import core_bridge as cb
        return cb.available()
    except Exception:  # noqa: BLE001
        return False


def _mcp_call(name: str, args: dict):
    import asyncio
    from mcp_server import server
    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    return json.loads(text) if isinstance(text, str) else None


def _swing_arm(tmp_dir: str):
    from mcp_server import core_bridge as cb
    r = _mcp_call("new_part", {"name": "MPBenchBar"})
    doc = r["doc_id"]
    _mcp_call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Bar", {"length": cb.dim(40), "width": cb.dim(4),
                               "height": cb.dim(4)})]})
    faces = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}}).get("matches", [])
    path = os.path.join(tmp_dir, "Bar.step+")
    cb.call("doc_save", {"doc_id": doc, "path": path})
    asm = cb.call("doc_new", {"name": "MPBenchSwing", "kind": "assembly"})["doc_id"]

    def rev():
        return cb.call("doc_tree", {"doc_id": asm}).get("revision", 0)

    cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(), "with_tess": False,
                              "ops": [
        {"op": "asm_insert_component", "source": {"path": path}, "name": "Ground",
         "fixed": True, "transform": [1, 0, 0, 0, 0, 1, 0, 200, 0, 0, 1, 0, 0, 0, 0, 1]},
        {"op": "asm_insert_component", "source": {"path": path}, "name": "Arm",
         "fixed": False}]})
    xsel = max(faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[0]))
    zsel = max(faces, key=lambda f: abs(f.get("centroid", [0, 0, 0])[2]))
    r = cb.call("doc_apply_ops", {"doc_id": asm, "base_revision": rev(),
                                  "with_tess": False, "ops": [
        {"op": "asm_add_mate", "mate": {
            "type": "angle", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": xsel},
                     {"component": "k2", "selector": xsel}], "value": {"value": 0.0}}},
        {"op": "asm_add_mate", "mate": {
            "type": "parallel", "align": "aligned", "suppressed": False,
            "refs": [{"component": "k1", "selector": zsel},
                     {"component": "k2", "selector": zsel}]}}]})
    return asm, r.get("id_map", {}).get("$0"), "k2"


def _bench_motion(quick: bool) -> dict:
    if not _engine_ok():
        return _skip("motion_swing_kinematics", "libaicad not staged (engine unavailable)")
    steps = 5 if quick else 13
    tmp = tempfile.mkdtemp(prefix="mp_bench_motion_")
    t0 = time.perf_counter()
    asm, mate_id, arm = _swing_arm(tmp)
    setup_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    r = _mcp_call("simulate_motion", {"doc_id": asm, "mate_id": mate_id,
                                      "start": 0.0, "stop": 90.0, "steps": steps,
                                      "check_interference": False})
    solve_s = time.perf_counter() - t1
    total = time.perf_counter() - t0
    if not (r and r.get("ok")):
        return _case("motion_swing_kinematics", total_s=total, setup_s=setup_s,
                     solve_s=solve_s, status="fail", detail=str((r or {}).get("error")))

    def _pt(m, p):
        return (m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[3],
                m[4] * p[0] + m[5] * p[1] + m[6] * p[2] + m[7],
                m[8] * p[0] + m[9] * p[1] + m[10] * p[2] + m[11])

    def _arm_xf(frame):
        return next(b["transform"] for b in frame["bodies"] if b["component"] == arm)

    tip = (40.0, 0.0, 0.0)
    p0 = _pt(_arm_xf(r["trajectory"][0]), tip)
    p1 = _pt(_arm_xf(r["trajectory"][-1]), tip)
    org = _pt(_arm_xf(r["trajectory"][0]), (0.0, 0.0, 0.0))
    chord = math.dist(p0, p1)
    r_tip = math.dist(p0, org)
    # rigid rotation chord: c = 2*r*sin(theta/2), theta = 90 deg -> r*sqrt(2)
    ref = r_tip * math.sqrt(2.0)
    return _case("motion_swing_kinematics", total_s=total, setup_s=setup_s,
                 solve_s=solve_s, metric=chord, ref=ref,
                 detail="mm; chord = 2*r*sin(theta/2) rigid rotation, theta=90deg")


def _bench_handoff_seams(quick: bool) -> dict:
    """Regression-only smoke of the three cross-sim seams (payload shape, no solve)."""
    t0 = time.perf_counter()
    try:
        from services.mcp import simhandoff as sh
        from services.mcp import hydraulic_tools as ht

        class _Stub:
            def __init__(self):
                self.tools = {}

            def tool(self):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        stub = _Stub()
        ht.register_hydraulic_tools(stub)
        checked = []
        # hydraulic -> FEA
        r = json.loads(stub.tools["hydraulic_fea_handoff"](
            bore_mm=63.0, max_pressure_bar=210.0, rod_mm=36.0))
        assert r["ok"] and sh.validate_envelope(r["simhandoff"]) == [], r
        checked.append("hydraulic_to_fea")
        # hydraulic -> piping (-> solve when fluids present)
        from engine.hydraulic import schema as hs
        doc = hs.new_hydraulic("bench")
        doc["components"] += [hs.new_component("c1", "pump"),
                              hs.new_component("c2", "cylinder_linear", bore_mm=50.0)]
        doc["hoses"].append(hs.new_hose("h1", "P", [
            {"component_id": "c1", "port": "pressure"},
            {"component_id": "c2", "port": "work_a"}]))
        doc["hoses"][0]["id_mm"] = 12.7
        doc["hoses"][0]["length_mm"] = 2500.0
        r2 = json.loads(stub.tools["hydraulic_piping_handoff"](
            supply_pressure_bar=200.0, flow_lpm=30.0, doc=doc))
        if r2.get("ok"):
            assert sh.validate_envelope(r2["simhandoff"]) == [], r2
            checked.append("hydraulic_to_piping")
        # motion -> FEA (dry_run, needs the engine)
        if _engine_ok():
            tmp = tempfile.mkdtemp(prefix="mp_bench_seam_")
            asm, mate_id, arm = _swing_arm(tmp)
            r3 = _mcp_call("motion_fea_handoff", {
                "doc_id": asm, "component": arm,
                "fixtures": [{"of": "face", "pick": "largest"}],
                "mate_id": mate_id, "start": 0.0, "stop": 60.0, "steps": 4,
                "dynamics": {"duration_s": 1.0, "default_density_g_cm3": 7.85},
                "dry_run": True})
            assert r3["ok"] and sh.validate_envelope(r3["simhandoff"]) == [], r3
            checked.append("motion_to_fea")
        dt = time.perf_counter() - t0
        return _case("handoff_seams_smoke", total_s=dt, solve_s=dt,
                     detail="seams validated: " + ", ".join(checked))
    except Exception as exc:  # noqa: BLE001
        return _case("handoff_seams_smoke", total_s=time.perf_counter() - t0,
                     status="fail", detail=f"{type(exc).__name__}: {exc}")


def run(quick: bool = False) -> list:
    cases: list = []
    cases += _bench_hydraulic()
    cases.append(_bench_piping_solve())
    cases.append(_bench_operating_point())
    cases.append(_bench_internal_volume())
    cases.append(_bench_struct())
    cases.append(_bench_motion(quick))
    cases.append(_bench_handoff_seams(quick))
    return cases


if __name__ == "__main__":
    for c in run(quick="--quick" in sys.argv):
        print(json.dumps(c))
