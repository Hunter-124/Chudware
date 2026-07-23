"""simsuite bench — fea_ccx_advanced: CalculiX-backed buckling + contact and the
engine fatigue (S-N) chain.

Cases
  buckling_euler_fixed_free   ccx *BUCKLE on the box-proxy column; first critical
                              load vs Euler Pcr = pi^2*E*I/(K*L)^2, K=2 fixed-free
                              (Timoshenko & Gere, Theory of Elastic Stability).
  contact_force_balance       ccx *STATIC,NLGEOM two-block *CONTACT PAIR; base
                              reaction vs applied load p*L*b (static equilibrium /
                              Newton III — a correct contact solve conserves it).
  fatigue_sn_basquin          host-mirror S-N life vs the Shigley hand calc
                              (Shigley 10th Eq 6-14..6-16, f=0.9):
                              Se=200, Sut=400, sa_eq=250 ->
                              a=(0.9*400)^2/200=648, b=-(1/3)log10(1.8)=-0.085085,
                              N=(250/648)^(1/b)=7.2627e4 cycles.
  fatigue_engine_cube         C++ engine sim_run_fatigue (core/sim/fea_fatigue.cpp)
                              on the inline 6-tet parity cube of
                              core/tests/test_fatigue.cpp; fully-reversed cycle has
                              zero mean stress, so every mean-stress theory's sa_eq
                              equals crit_sigma_a exactly (ratio ref = 1.0).

ccx absent -> the two ccx cases return status "skip" (honest degrade, GPL-2.0
subprocess isolation). Engine .so without the FEA plugin -> fatigue_engine_cube
skips. quick=True uses the coarser buckling mesh; the whole module runs in ~2 s.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

DOMAIN = "fea_ccx_advanced"


def _ccx_ready() -> bool:
    from services.mcp import fea_advanced as fa
    if fa.ccx_path() is None:
        default = Path.home() / ".local" / "bin" / "ccx"
        if default.exists():                     # documented install location
            os.environ.setdefault("CHUDWARE_CCX", str(default))
    return fa.ccx_path() is not None


def _case(case: str, **kw) -> dict:
    out = {"domain": DOMAIN, "case": case, "setup_s": 0.0, "mesh_s": 0.0,
           "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
           "rel_err": None, "status": "ok", "detail": ""}
    out.update(kw)
    return out


def _finish(c: dict, t0: float, metric, ref, tol: float, units: str) -> dict:
    c["total_s"] = time.time() - t0
    c["metric"], c["ref"] = metric, ref
    if metric is None:
        c["status"] = "fail"
        c["detail"] = c["detail"] or "no metric parsed"
        return c
    c["rel_err"] = abs(metric - ref) / abs(ref)
    c["status"] = "ok" if c["rel_err"] < tol else "fail"
    c["detail"] = f"{units}; tol {tol:g}"
    return c


# --------------------------------------------------------------------- buckling

def _bench_buckling(quick: bool) -> dict:
    from services.mcp import fea_advanced as fa
    t0 = time.time()
    c = _case("buckling_euler_fixed_free")
    # Slender steel column 400x10x10 mm (L/h=40): I=10*10^3/12=833.33 mm^4,
    # Pcr = pi^2*200000*833.33/(2*400)^2 = 2570.2 N (Euler fixed-free, K=2).
    L, b, h, E, nu, P = 400.0, 10.0, 10.0, 200000.0, 0.3, 1000.0
    nx = 24 if quick else 48
    ts = time.time()
    gen = fa.build_buckling_column_inp(L, b, h, E, nu, P, nx=nx, ny=4, nz=4)
    c["setup_s"] = time.time() - ts
    if not _ccx_ready():
        c.update(status="skip", detail="ccx not on PATH/CHUDWARE_CCX",
                 total_s=time.time() - t0)
        return c
    ts = time.time()
    run = fa.run_ccx(gen["inp"], job="bk", timeout=180.0)
    c["solve_s"] = time.time() - ts
    if not run.get("ok") or not run.get("dat_path"):
        c.update(status="fail", total_s=time.time() - t0,
                 detail=(run.get("stderr") or run.get("error") or "ccx failed")[-200:])
        return c
    factors = fa.parse_buckle_factors(run["dat_path"])
    metric = factors[0] * P if factors else None
    return _finish(c, t0, metric, gen["expected_Pcr"], 0.05,
                   f"N (first critical load, nx={nx}, C3D8I)")


# --------------------------------------------------------------------- contact

def _bench_contact(quick: bool) -> dict:
    del quick  # the shipped 6x6x4 two-block mesh is already the coarse case
    from services.mcp import fea_advanced as fa
    t0 = time.time()
    c = _case("contact_force_balance")
    L, b, h, E, nu, p = 50.0, 50.0, 20.0, 200000.0, 0.3, 1.0   # 2500 N applied
    ts = time.time()
    gen = fa.build_contact_block_inp(L, b, h, E, nu, p)
    c["setup_s"] = time.time() - ts
    if not _ccx_ready():
        c.update(status="skip", detail="ccx not on PATH/CHUDWARE_CCX",
                 total_s=time.time() - t0)
        return c
    ts = time.time()
    run = fa.run_ccx(gen["inp"], job="ct", timeout=300.0)
    c["solve_s"] = time.time() - ts
    if not run.get("ok") or not run.get("dat_path"):
        c.update(status="fail", total_s=time.time() - t0,
                 detail=(run.get("stderr") or run.get("error") or "ccx failed")[-200:])
        return c
    rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
    metric = abs(rf * len(gen["base_nodes"])) if rf is not None else None
    return _finish(c, t0, metric, gen["applied_load_n"], 0.02,
                   "N (base reaction vs applied, final increment)")


# --------------------------------------------------------------------- fatigue

def _bench_fatigue_sn() -> dict:
    from services.mcp import fea_advanced as fa
    t0 = time.time()
    c = _case("fatigue_sn_basquin")
    # Shigley 10th Eq 6-14..6-16 hand calc (see module docstring): 7.2627e4 cycles.
    metric = fa.sn_life(250.0, 200.0, 400.0)
    return _finish(c, t0, metric, 7.2627e4, 0.01, "cycles (S-N finite life)")


_CUBE_STUDY = {
    # Inline 6-tet unit cube — same fixture as core/tests/test_fatigue.cpp.
    "points": [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
               0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1],
    "tets": [0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
             0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6],
    "material": {"E_mpa": 200000.0, "nu": 0.29, "yield_mpa": 370.0,
                 "ultimate_mpa": 440.0, "endurance_mpa": 220.0},
    "fixtures": [{"points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]],
                  "tol_mm": 0.01}],
    "loads": [{"type": "force", "vector": [0.0, 0.0, 50000.0],
               "faces": {"points": [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]],
                         "tol_mm": 0.01}}],
    "fatigue": {"se_corrected_mpa": 200.0, "ultimate_mpa": 440.0,
                "yield_mpa": 370.0, "lmin": -1.0, "lmax": 1.0},
}


def _bench_fatigue_engine() -> dict:
    t0 = time.time()
    c = _case("fatigue_engine_cube")
    try:
        from services.mcp import core_bridge as cb
        from services.mcp.core_bridge import CoreError
    except Exception as exc:  # noqa: BLE001
        c.update(status="skip", detail=f"core_bridge unavailable: {exc}",
                 total_s=time.time() - t0)
        return c
    ts = time.time()
    try:
        res = cb.call("sim_run_fatigue", {"study": _CUBE_STUDY})
    except CoreError as exc:
        if "unknown method" in str(exc):
            c.update(status="skip", total_s=time.time() - t0,
                     detail="libchudware.so lacks sim_run_fatigue (no FEA plugin)")
            return c
        c.update(status="fail", detail=str(exc)[-200:], total_s=time.time() - t0)
        return c
    c["solve_s"] = time.time() - ts
    if not res.get("ok"):
        c.update(status="fail", detail=res.get("error", "solve failed")[-200:],
                 total_s=time.time() - t0)
        return c
    s = res["summary"]
    sa = float(s["crit_sigma_a"])
    # Fully-reversed cycle: sigma_m = 0, so every mean-stress theory's sa_eq
    # reduces to sa exactly — metric = worst theory ratio, ref = 1.0 (identity).
    ratios = [float(s["theories"][k]["sa_eq_mpa"]) / sa
              for k in ("goodman", "gerber", "soderberg", "asme", "morrow")]
    metric = max(ratios, key=lambda r: abs(r - 1.0)) if sa > 0 else None
    return _finish(c, t0, metric, 1.0, 1e-4,
                   "sa_eq/sa ratio (zero-mean identity, all theories)")


# --------------------------------------------------------------------- contract

def run(quick: bool = False) -> list[dict]:
    cases = [
        _bench_buckling(quick),
        _bench_contact(quick),
        _bench_fatigue_sn(),
        _bench_fatigue_engine(),
    ]
    return cases


if __name__ == "__main__":
    import json
    print(json.dumps(run(quick="--quick" in sys.argv), indent=2))
