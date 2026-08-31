"""simsuite bench — fea_thermal (steady + transient heat conduction).

Cases (engine in-process via services.mcp.core_bridge, sim_run_thermal RPC —
the seam behind cad_tools.run_fea_thermal's transient branch):

  steady_slab_mid_temp      inline slab, ends fixed  → mid-plane (Th+Tc)/2
                            (1-D Fourier conduction: T(x)=Th+(Tc-Th)x/L)
  steady_fin_tip_temp       pin fin, lateral films   → θ(L)/θb = 1/cosh(mL),
                            m=√(hP/kA) (Incropera Table 3.4 case B, adiabatic tip)
  transient_slab_quench     slab quench, mid-plane at Fo=0.3 vs the odd-term
                            Fourier series Σ 4T0/(nπ)·sin(nπx/L)·exp(-n²π²Fo)
  transient_lumped_cooling  cube, films all faces, Bi<<0.1 → θ/θ0=exp(-t/τ),
                            τ=ρcpV/(hA) (lumped capacitance, Incropera §5.1)
  transient_oracle_parity   duty-scheduled flux, engine vs the Python oracle
                            (engine_oracle/sim/fea_thermal_transient.py) on the
                            SAME mesh/steps — differential, ref = oracle value
  steady_ccx_bar            CalculiX *HEAT TRANSFER STEADY STATE box bar (the
                            run_fea_thermal steady backend) → mid-plane mean
  transient_realmesh_cool   (full mode only) doc box → STEP → Polyhedral-FEA mesh →
                            lumped cooling on the real mesh path. target_size_mm=5 keeps
                            element size above the 1.5 mm face-point tolerance; finer
                            meshes currently admit subsurface nodes and inflate film area.

Inline cases use the oracle's deterministic Kuhn box mesh (no mesher), with
facesets passed as exact node coordinates, one boundary plane per faceset (a
merged multi-plane set makes interior diagonal faces count as surface).
Meshes are tiny (<4k nodes); no resguard gating needed beyond install().
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

DOMAIN = "fea_thermal"


def _case(case: str, **kw) -> dict:
    base = {"domain": DOMAIN, "case": case, "setup_s": 0.0, "mesh_s": 0.0,
            "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
            "rel_err": None, "status": "ok", "detail": ""}
    base.update(kw)
    return base


def _finish(c: dict, t0: float, metric: float, ref, tol: float, detail: str = "") -> dict:
    c["total_s"] = time.time() - t0
    c["metric"] = float(metric)
    if ref is not None:
        c["ref"] = float(ref)
        c["rel_err"] = abs(metric - ref) / (abs(ref) or 1.0)
        if c["rel_err"] > tol:
            c["status"] = "fail"
            detail = (detail + " " if detail else "") + f"rel_err {c['rel_err']:.3g} > tol {tol}"
    c["detail"] = detail
    return c


def _faceset(pts, ids) -> dict:
    return {"points": [[float(v) for v in pts[i]] for i in ids], "tol_mm": 1e-6}


def _plane(pts, axis: int, value: float) -> list:
    return [i for i in range(len(pts)) if abs(pts[i, axis] - value) < 1e-9]


def _inline_study(pts, tets, material, fixtures, loads, transient=None) -> dict:
    st = {"points": pts.flatten().tolist(), "tets": tets.flatten().tolist(),
          "nodes_per_elem": 4, "material": material, "fixtures": fixtures,
          "loads": loads}
    if transient is not None:
        st["transient"] = transient
    return st


def run(quick: bool = False) -> list:
    try:
        import resguard
        resguard.install()
    except Exception:  # noqa: BLE001 — standalone runs without the harness
        resguard = None

    import numpy as np  # noqa: F401 (used below)
    from engine.sim.fea_thermal_transient import (FluxLoad, Schedule, box_mesh,
                                                  solve_transient)

    cases: list = []

    # ── engine availability ──────────────────────────────────────────────────
    cb = None
    engine_skip = ""
    try:
        from services.mcp import core_bridge as _cb
        from services.mcp.core_bridge import CoreError
        try:
            _cb.call("sim_run_thermal", {"study": {}})
            cb = _cb
        except CoreError as exc:
            if "unknown method" in str(exc):
                engine_skip = f"libchudware lacks sim_run_thermal: {exc}"
            else:
                cb = _cb  # RPC exists (it rejected the empty study)
    except Exception as exc:  # noqa: BLE001
        engine_skip = f"core_bridge unavailable: {exc}"

    def engine_cases():
        import numpy as np

        # 1. steady slab ------------------------------------------------------
        t0 = time.time()
        L, Th, Tc = 20.0, 100.0, 0.0
        nx = 8 if quick else 24
        pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, nx, 2, 2)
        c = _case("steady_slab_mid_temp", setup_s=time.time() - t0)
        ts = time.time()
        res = cb.call("sim_run_thermal", {"study": _inline_study(
            pts, tets, {"k_w_mk": 50.0},
            [dict(_faceset(pts, x0), temp_c=Th), dict(_faceset(pts, xL), temp_c=Tc)],
            [])})
        c["solve_s"] = time.time() - ts
        if not res.get("ok"):
            c.update(status="fail", detail=res.get("error", ""), total_s=time.time() - t0)
        else:
            T = np.array(res["temperature"])
            mid = float(np.mean(T[_plane(pts, 0, L / 2.0)]))
            # linear profile ⇒ mid-plane = (Th+Tc)/2 (1-D Fourier conduction)
            _finish(c, t0, mid, (Th + Tc) / 2.0, 1e-6, "degC")
        cases.append(c)

        # 2. steady fin --------------------------------------------------------
        t0 = time.time()
        Lf, bf, Tb, Tamb = 100.0, 10.0, 100.0, 25.0
        nxf = 25 if quick else 50
        pts, tets, x0, xL = box_mesh(Lf, bf, bf, nxf, 2 if quick else 4, 2 if quick else 4)
        loads = [{"faces": _faceset(pts, _plane(pts, ax, v)), "type": "convection",
                  "h": 100.0, "t_amb_c": Tamb}
                 for ax in (1, 2) for v in (0.0, bf)]
        c = _case("steady_fin_tip_temp", setup_s=time.time() - t0)
        ts = time.time()
        res = cb.call("sim_run_thermal", {"study": _inline_study(
            pts, tets, {"k_w_mk": 200.0},
            [dict(_faceset(pts, x0), temp_c=Tb)], loads)})
        c["solve_s"] = time.time() - ts
        if not res.get("ok"):
            c.update(status="fail", detail=res.get("error", ""), total_s=time.time() - t0)
        else:
            T = np.array(res["temperature"])
            tip = float(np.mean(T[xL]))
            # θ(L)/θb = 1/cosh(mL), m=√(hP/kA) — Incropera Table 3.4 case B
            m = math.sqrt(1e-4 * 4.0 * bf / (0.2 * bf * bf))
            ref = Tamb + (Tb - Tamb) / math.cosh(m * Lf)
            _finish(c, t0, tip, ref, 0.02, "degC (tip)")
        cases.append(c)

        # 3. transient slab quench ----------------------------------------------
        t0 = time.time()
        L, T0v = 20.0, 100.0
        k_w_mk, cp, rho = 50.0, 500.0, 8.0            # α = 12.5 mm²/s
        alpha = (k_w_mk / 1000.0) / (rho * 1000.0 * cp / 1e9)
        t_star = 0.3 * L * L / alpha                  # Fo = 0.3
        nxq, nsteps = (20, 400) if quick else (40, 800)
        pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, nxq, 2, 2)
        c = _case("transient_slab_quench", setup_s=time.time() - t0)
        ts = time.time()
        res = cb.call("sim_run_thermal", {"study": _inline_study(
            pts, tets, {"k_w_mk": k_w_mk, "cp_j_kgk": cp, "density_g_cm3": rho},
            [dict(_faceset(pts, x0), temp_c=0.0), dict(_faceset(pts, xL), temp_c=0.0)],
            [], transient={"t_end_s": t_star, "dt_s": t_star / nsteps,
                           "t_init_c": T0v})})
        c["solve_s"] = time.time() - ts
        if not res.get("ok"):
            c.update(status="fail", detail=res.get("error", ""), total_s=time.time() - t0)
        else:
            T = np.array(res["frames"][-1]["temperature"])
            got = float(np.mean(T[_plane(pts, 0, L / 2.0)]))
            # odd-term Fourier series for the quenched slab at x=L/2, Fo=0.3
            ref = sum(4.0 * T0v / (n * math.pi) * math.sin(n * math.pi / 2.0)
                      * math.exp(-n * n * math.pi * math.pi * 0.3)
                      for n in range(1, 200, 2))
            _finish(c, t0, got, ref, 0.05, "degC (mid-plane)")
        cases.append(c)

        # 4. transient lumped cooling -------------------------------------------
        t0 = time.time()
        Lb, h, Tamb, T0v = 10.0, 200.0, 20.0, 100.0
        rho_kg, cpv = 2700.0, 900.0
        nc, nsteps = (3, 400) if quick else (5, 800)
        pts, tets, _, _ = box_mesh(Lb, Lb, Lb, nc, nc, nc)
        loads = [{"faces": _faceset(pts, _plane(pts, ax, v)), "type": "convection",
                  "h": h, "t_amb_c": Tamb}
                 for ax in (0, 1, 2) for v in (0.0, Lb)]
        V, A = Lb ** 3 * 1e-9, 6.0 * Lb * Lb * 1e-6
        tau = rho_kg * cpv * V / (h * A)              # lumped time constant
        c = _case("transient_lumped_cooling", setup_s=time.time() - t0)
        ts = time.time()
        res = cb.call("sim_run_thermal", {"study": _inline_study(
            pts, tets,
            {"k_w_mk": 200.0, "cp_j_kgk": cpv, "density_g_cm3": rho_kg / 1000.0},
            [], loads, transient={"t_end_s": tau, "dt_s": tau / nsteps,
                                  "t_init_c": T0v})})
        c["solve_s"] = time.time() - ts
        if not res.get("ok"):
            c.update(status="fail", detail=res.get("error", ""), total_s=time.time() - t0)
        else:
            got = max(res["frames"][-1]["temperature"])
            # θ/θ0 = exp(-t/τ) at t=τ (lumped capacitance, Bi≈1.7e-3)
            ref = Tamb + (T0v - Tamb) * math.exp(-1.0)
            _finish(c, t0, got, ref, 0.02, "degC")
        cases.append(c)

        # 5. duty-schedule differential parity vs the Python oracle --------------
        t0 = time.time()
        L, Tc, q_mm2, duty = 30.0, 40.0, 0.01, 0.5
        k_w_mk, cpv, rho = 20.0, 500.0, 8.0
        k_mm, rho_cp = k_w_mk / 1000.0, rho * 1000.0 * cpv / 1e9
        nxp = 12 if quick else 20
        pts, tets, x0, xL = box_mesh(L, 8.0, 8.0, nxp, 2, 2)
        tau_d = rho_cp * L * L / k_mm
        period = tau_d / 5.0
        t_end = (3.0 if quick else 6.0) * tau_d
        dt = tau_d / 200.0
        c = _case("transient_oracle_parity", setup_s=time.time() - t0)
        ts = time.time()
        r_o = solve_transient(
            pts, tets, k_mm, xL, [Tc] * len(xL),
            [FluxLoad(nodes=x0, q=q_mm2,
                      schedule=Schedule(times=[0.0, duty * period],
                                        scales=[1.0, 0.0], period=period))],
            [], rho_cp, t_end=t_end, dt=dt, t_init=Tc)
        oracle_s = time.time() - ts
        ts = time.time()
        res = cb.call("sim_run_thermal", {"study": _inline_study(
            pts, tets, {"k_w_mk": k_w_mk, "cp_j_kgk": cpv, "density_g_cm3": rho},
            [dict(_faceset(pts, xL), temp_c=Tc)],
            [{"faces": _faceset(pts, x0), "type": "flux", "magnitude": q_mm2 * 1e6,
              "schedule": {"duty": duty, "period_s": period}}],
            transient={"t_end_s": t_end, "dt_s": dt, "t_init_c": Tc})})
        c["solve_s"] = time.time() - ts
        if not (res.get("ok") and r_o.ok):
            c.update(status="fail", total_s=time.time() - t0,
                     detail=res.get("error", "") or r_o.error)
        else:
            got = float(max(res["frames"][-1]["temperature"]))
            ref = float(max(r_o.frames[-1][1]))     # differential: oracle IS the ref
            _finish(c, t0, got, ref, 1e-9,
                    f"degC (peak); oracle solve {oracle_s:.2f}s")
        cases.append(c)

        # 7. (full) real-mesh transient cooling -----------------------------------
        if not quick:
            t0 = time.time()
            c = _case("transient_realmesh_cool")
            step = None
            doc = None
            try:
                Lb2, h2, Tamb2, T02 = 20.0, 200.0, 20.0, 100.0
                doc = cb.call("doc_new", {"kind": "part",
                                          "name": "fea_thermal_bench_cube"})["doc_id"]
                cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
                    {"op": "add_feature", "feature": {
                        "type": "box", "name": "Cube", "params": {
                            "length": {"value": Lb2}, "width": {"value": Lb2},
                            "height": {"value": Lb2}}}}]})
                step = tempfile.mktemp(suffix=".step", prefix="fea_thermal_bench_")
                cb.call("doc_export_step", {"doc_id": doc, "path": step})
                fps = []
                for side in ("left", "right", "top", "bottom", "front", "back"):
                    q = cb.call("doc_query", {"doc_id": doc,
                                              "query": {"of": "face", "side": side}})
                    m = [x for x in q.get("matches", []) if x.get("kind") == "face"]
                    fps.append(cb.call("doc_face_points", {"doc_id": doc,
                                                           "selector": m[0]}))
                c["setup_s"] = time.time() - t0
                V2 = (Lb2 * 1e-3) ** 3
                A2 = 6.0 * (Lb2 * 1e-3) ** 2
                tau2 = 2700.0 * 900.0 * V2 / (h2 * A2)
                ts = time.time()
                # target_size_mm=5 keeps element size above the doc_face_points
                # perp_tol (1.5 mm) — finer meshes admit subsurface nodes into the
                # facesets and inflate the film area (see module docstring).
                res = cb.call("sim_run_thermal", {"study": {
                    "step_path": step, "target_size_mm": 5.0, "fineness": 0.5,
                    "material": {"k_w_mk": 200.0, "cp_j_kgk": 900.0,
                                 "density_g_cm3": 2.7},
                    "fixtures": [],
                    "loads": [{"faces": fp, "type": "convection", "h": h2,
                               "t_amb_c": Tamb2} for fp in fps],
                    "transient": {"t_end_s": tau2, "dt_s": tau2 / 400.0,
                                  "t_init_c": T02}}})
                c["solve_s"] = time.time() - ts   # includes meshing
                if not res.get("ok"):
                    c.update(status="fail", detail=res.get("error", ""),
                             total_s=time.time() - t0)
                else:
                    got = max(res["frames"][-1]["temperature"])
                    ref = Tamb2 + (T02 - Tamb2) * math.exp(-1.0)
                    _finish(c, t0, got, ref, 0.05,
                            f"degC; n_nodes={res.get('n_nodes')} (mesh in solve_s)")
            except Exception as exc:  # noqa: BLE001
                c.update(status="fail", detail=str(exc)[:300], total_s=time.time() - t0)
            finally:
                if doc is not None:
                    try:
                        cb.call("doc_close", {"doc_id": doc})
                    except Exception:  # noqa: BLE001
                        pass
                if step and os.path.exists(step):
                    os.unlink(step)
            cases.append(c)

    if cb is None:
        for name in ("steady_slab_mid_temp", "steady_fin_tip_temp",
                     "transient_slab_quench", "transient_lumped_cooling",
                     "transient_oracle_parity"):
            cases.append(_case(name, status="skip", detail=engine_skip))
        if not quick:
            cases.append(_case("transient_realmesh_cool", status="skip",
                               detail=engine_skip))
    else:
        engine_cases()

    # 6. CalculiX steady bar ----------------------------------------------------
    t0 = time.time()
    c = _case("steady_ccx_bar")
    try:
        from services.mcp import fea_advanced as fa
        if fa.ccx_path() is None:
            c.update(status="skip", detail="ccx (CalculiX) not on PATH/CHUDWARE_CCX",
                     total_s=time.time() - t0)
        else:
            nx = 8 if quick else 40
            gen = fa.build_thermal_bar_inp(200.0, 20.0, 20.0, 50.0, 100.0, 0.0, nx=nx)
            c["setup_s"] = time.time() - t0
            ts = time.time()
            run_r = fa.run_ccx(gen["inp"], job="simsuite_thermal", timeout=240.0)
            c["solve_s"] = time.time() - ts
            if run_r.get("skipped") or not run_r.get("ok") or not run_r.get("dat_path"):
                c.update(status="fail" if not run_r.get("skipped") else "skip",
                         detail=(run_r.get("stderr", "") or str(run_r))[-300:],
                         total_s=time.time() - t0)
            else:
                mid = fa.parse_dat_nodal(run_r["dat_path"], "NT", gen["mid_nodes"],
                                         component=1)
                # 1-D linear conduction ⇒ mid-plane = (Th+Tc)/2 = 50
                _finish(c, t0, float(mid), gen["expected_mid_T"], 1e-3, "degC")
    except Exception as exc:  # noqa: BLE001
        c.update(status="fail", detail=str(exc)[:300], total_s=time.time() - t0)
    cases.append(c)

    return cases


if __name__ == "__main__":
    import json
    quick = "--quick" in sys.argv
    print(json.dumps(run(quick=quick), indent=2))
