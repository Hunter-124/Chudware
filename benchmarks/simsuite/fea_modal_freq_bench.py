"""simsuite bench — modal FEA + frequency response (domain fea_modal_freq).

Cases (all on one steel beam, x = L 1000 x y = B 60 x z = H 20 mm, so the first
three cantilever modes are three DISTINCT Euler-Bernoulli values):

  cantilever_modal_f1/f2/f3  clamped-free natural frequencies vs Euler-Bernoulli
                             f = (beta L)^2/(2 pi L^2) sqrt(EI/(rho A))
                             (Rao, "Mechanical Vibrations" 5e Sec. 8.5; roots
                             beta1 L = 1.87510, beta2 L = 4.69409, Table 8.1).
                             One tet10 solve shared by the three cases (timing is
                             carried by f1; f2/f3 report ~0 s and say so).
  free_free_rigid_modes      unconstrained solve: exactly 6 rigid-body modes
                             (< 0.5 Hz) — 3 translations + 3 rotations of a free
                             solid. metric = rigid-mode count, ref = 6 (exact).
                             n_modes=14 is deliberate: it gives the shift-invert
                             eigensolver enough Krylov room to resolve the 6-fold-
                             degenerate 0 Hz cluster (small requests drop part of
                             it — see services/mcp/test_fea_modal_freq.py).
  free_free_first_elastic    first elastic free-free mode vs Euler-Bernoulli with
                             beta1 L = 4.73004 (free-free root, Rao Table 8.1).
  freq_response_peak         multiphysics worker path (engine_oracle/sim/
                             multiphysics_rpc.sim_freq_response): harmonic base-
                             excitation response must peak at the first natural
                             frequency (SDOF transmissibility maximum at r ~= 1,
                             Harris & Piersol 5e Ch. 2 Eq. 2.41). metric = peak
                             frequency (Hz), ref = Euler-Bernoulli f1.

Accuracy gate: rel_err < 5 % for frequency cases (coarse-tet10 discretization is
~0.1-0.5 % here; Euler-Bernoulli's neglected shear/rotary inertia adds ~1-3 % at
these slendernesses; a wrong mass matrix (10-20 %), tet4 stiffness bias (~80 %) or
mode mis-ordering (>= 2.1x) all blow far past 5 %). The peak-frequency case adds
the sweep-grid spacing (1.87 %) on top -> 5 % still holds.

Memory: largest solve here is < 0.5 GB peak (2-3 k nodes tet10) — no resguard
gating needed; resguard.check() runs between cases when available.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

DOMAIN = "fea_modal_freq"

# ---------------------------------------------------------------------------
# Geometry / material + analytic references (formulas cited above)
# ---------------------------------------------------------------------------
L_MM, B_MM, H_MM = 1000.0, 60.0, 20.0
STEEL = {"name": "bench-steel", "E_mpa": 200000.0, "nu": 0.3, "density_g_cm3": 7.85}

_E_PA = STEEL["E_mpa"] * 1e6
_RHO = STEEL["density_g_cm3"] * 1000.0
_L_M = L_MM * 1e-3
_A_M2 = (B_MM * 1e-3) * (H_MM * 1e-3)
_IZ_M4 = (B_MM * 1e-3) * (H_MM * 1e-3) ** 3 / 12.0   # thin-direction bending
_IY_M4 = (H_MM * 1e-3) * (B_MM * 1e-3) ** 3 / 12.0   # wide-direction bending


def _eb_hz(beta_l: float, i_m4: float) -> float:
    # Euler-Bernoulli: f = (beta L)^2 / (2 pi L^2) * sqrt(E I / (rho A))
    return (beta_l ** 2 / (2.0 * math.pi * _L_M ** 2)) * math.sqrt(
        _E_PA * i_m4 / (_RHO * _A_M2))


F1_REF = _eb_hz(1.87510, _IZ_M4)    # clamped-free mode 1 (~16.31 Hz)
F2_REF = _eb_hz(1.87510, _IY_M4)    # clamped-free mode 2 (~48.92 Hz)
F3_REF = _eb_hz(4.69409, _IZ_M4)    # clamped-free mode 3 (~102.20 Hz)
FF1_REF = _eb_hz(4.73004, _IZ_M4)   # free-free 1st elastic (~103.77 Hz)

# Exact box mass for the response leg: m = V_mm3 * 1e-3 cm3/mm3 * rho / 1000 kg/g.
MASS_KG = (L_MM * B_MM * H_MM * 1e-3) * STEEL["density_g_cm3"] / 1000.0

FREQ_TOL = 0.05
RIGID_HZ = 0.5


def _case(case: str, **kw) -> dict:
    out = {"domain": DOMAIN, "case": case, "setup_s": 0.0, "mesh_s": 0.0,
           "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
           "rel_err": None, "status": "ok", "detail": ""}
    out.update(kw)
    return out


def _freq_case(case: str, metric: float, ref: float, total_s: float,
               solve_s: float = 0.0, setup_s: float = 0.0, detail: str = "") -> dict:
    rel = abs(metric - ref) / abs(ref)
    return _case(case, metric=metric, ref=ref, rel_err=rel, total_s=total_s,
                 solve_s=solve_s, setup_s=setup_s,
                 status="ok" if rel < FREQ_TOL else "fail",
                 detail=detail or f"Hz; tol {FREQ_TOL:.0%}")


def run(quick: bool = False) -> list[dict]:
    try:
        import resguard
        _check = resguard.check
    except Exception:  # noqa: BLE001 — standalone runs outside run_all.py
        _check = lambda: None  # noqa: E731

    cases: list[dict] = []
    t0 = time.time()
    try:
        from services.mcp import core_bridge as cb
        from services.mcp.core_bridge import CoreError
    except Exception as e:  # noqa: BLE001
        return [_case("engine_load", status="skip", total_s=time.time() - t0,
                      detail=f"core_bridge unavailable: {e}")]
    try:
        cb.call("sim_run_modal", {"study": {}})
    except CoreError as exc:
        if "unknown method" in str(exc):
            return [_case("engine_load", status="skip", total_s=time.time() - t0,
                          detail="libchudware.so lacks sim_run_modal "
                                 "(Polyhedral-FEA not built)")]
        # anything else falls through — an empty study is expected to error

    fineness = 0.3 if quick else 0.5

    # ---------------------------------------------------------------- setup
    t_setup0 = time.time()
    doc = cb.call("doc_new", {"kind": "part", "name": "bench_modal_freq"})["doc_id"]
    step = tempfile.mktemp(suffix=".step", prefix="bench_modal_freq_")
    try:
        r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Beam",
             "params": {"length": {"value": L_MM}, "width": {"value": B_MM},
                        "height": {"value": H_MM}}}}]})
        fs = (r.get("rebuild") or {}).get("feature_status", [])
        if not fs or not all(f.get("ok") for f in fs):
            return [_case("setup", status="fail", total_s=time.time() - t0,
                          detail=f"beam rebuild failed: {fs}")]
        cb.call("doc_export_step", {"doc_id": doc, "path": step})
        q = cb.call("doc_query", {"doc_id": doc,
                                  "query": {"of": "face", "side": "left"}})
        faces = [m for m in q.get("matches", []) if m.get("kind") == "face"]
        fp = cb.call("doc_face_points", {"doc_id": doc, "selector": faces[0]})
        setup_s = time.time() - t_setup0

        # -------------------------------------------- clamped cantilever modal
        t1 = time.time()
        clamped = cb.call("sim_run_modal", {"study": {
            "step_path": step, "material": STEEL, "fixtures": [fp],
            "mesh_order": 2, "fineness": fineness}, "n_modes": 6})
        solve_s = time.time() - t1
        if not clamped.get("ok"):
            cases.append(_case("cantilever_modal_f1", status="fail",
                               total_s=time.time() - t1, solve_s=solve_s,
                               detail=f"solve failed: {clamped.get('error')}"))
        else:
            f = clamped["freqs_hz"]
            cases.append(_freq_case("cantilever_modal_f1", f[0], F1_REF,
                                    total_s=setup_s + solve_s, solve_s=solve_s,
                                    setup_s=setup_s,
                                    detail=f"Hz; tet10 fineness={fineness}; "
                                           f"n_nodes={clamped.get('n_nodes')}"))
            cases.append(_freq_case("cantilever_modal_f2", f[1], F2_REF, total_s=0.0,
                                    detail="Hz; shares solve with cantilever_modal_f1"))
            cases.append(_freq_case("cantilever_modal_f3", f[2], F3_REF, total_s=0.0,
                                    detail="Hz; shares solve with cantilever_modal_f1"))
        _check()

        # ------------------------------------------------------ free-free modal
        t2 = time.time()
        free = cb.call("sim_run_modal", {"study": {
            "step_path": step, "material": STEEL, "fixtures": [],
            "mesh_order": 2, "fineness": fineness}, "n_modes": 14})
        ff_s = time.time() - t2
        if not free.get("ok"):
            cases.append(_case("free_free_rigid_modes", status="fail", total_s=ff_s,
                               solve_s=ff_s,
                               detail=f"solve failed: {free.get('error')}"))
        else:
            freqs = free["freqs_hz"]
            n_rigid = sum(1 for x in freqs if x < RIGID_HZ)
            cases.append(_case(
                "free_free_rigid_modes", metric=float(n_rigid), ref=6.0,
                rel_err=abs(n_rigid - 6) / 6.0, total_s=ff_s, solve_s=ff_s,
                status="ok" if n_rigid == 6 else "fail",
                detail="count of modes < 0.5 Hz; 6 exact by rigid-body kinematics"))
            elastic = [x for x in freqs if x >= RIGID_HZ]
            if elastic:
                cases.append(_freq_case(
                    "free_free_first_elastic", elastic[0], FF1_REF, total_s=0.0,
                    detail="Hz; shares solve with free_free_rigid_modes"))
            else:
                cases.append(_case("free_free_first_elastic", status="fail",
                                   detail="no elastic modes returned"))
        _check()

        # -------------------------------------------------- frequency response
        t3 = time.time()
        if clamped.get("ok"):
            from engine_oracle.sim.multiphysics_rpc import WORKER_METHODS
            n_modes = len(clamped["freqs_hz"])
            n_nodes = int(clamped["n_nodes"])
            # 200 log points over 5-200 Hz: grid spacing (200/5)^(1/199)-1 = 1.87 %.
            sweep = [5.0 * (200.0 / 5.0) ** (i / 199.0) for i in range(200)]
            fr = WORKER_METHODS["sim_freq_response"]({
                "freqs_hz": clamped["freqs_hz"],
                "mode_shapes": clamped["mode_shapes"][:n_modes * n_nodes * 3],
                "n_nodes": n_nodes,
                "total_mass_kg": MASS_KG,
                "excitation_dir": [0.0, 0.0, 1.0],
                "sweep_hz": sweep,
                "zeta": 0.02,
            }, lambda *_: None)
            fr_s = time.time() - t3
            if not fr.get("ok"):
                cases.append(_case("freq_response_peak", status="fail", total_s=fr_s,
                                   solve_s=fr_s,
                                   detail=f"sim_freq_response failed: {fr.get('error')}"))
            else:
                peak = float(fr["harmonic"]["peak_freq_hz"])
                cases.append(_freq_case(
                    "freq_response_peak", peak, F1_REF, total_s=fr_s, solve_s=fr_s,
                    detail=f"Hz; peak_accel={fr['harmonic']['peak_accel_g']:.1f} g "
                           f"per 1 g base (zeta=0.02)"))
        else:
            cases.append(_case("freq_response_peak", status="skip",
                               detail="clamped modal solve failed upstream"))
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(step)
        except OSError:
            pass
    return cases


if __name__ == "__main__":
    for c in run(quick="--quick" in sys.argv):
        print({k: v for k, v in c.items() if v not in (None, "", 0.0)})
