"""fea_static simsuite bench — diverse-geometry mesh + static-solve accuracy/timing.

Contract: benchmarks/simsuite/README.md (``run(quick=False) -> list[dict]``).

quick=True  : 4 coarse cases (<60 s total) — cantilever tet10, plate-with-hole
              tet10, Lamé annulus tet10, L-bracket tet4 regression.
quick=False : the full frontier —
  * cantilever  : tet4 / tet10 / structured-hex8 at 2-3 fineness levels each
                  (error-vs-time for the element-order comparison),
  * plate hole  : tet4 + tet10 at two curvature refinements,
  * Lamé annulus: tet4 + tet10 at two fineness levels,
  * native-pressure witness case (KNOWN core bug, emitted honestly as fail),
  * 6 non-analytic parts (fillet bracket / shell / revolve / multi-feature /
    loft / sweep) as mesh+solve regressions; bracket + loft carry a finer-mesh
    self-convergence reference.

All analytic references cite their formula at the point of use in
fea_static_cases.py (Roark/Euler-Bernoulli, Peterson-Kirsch, Lamé).
"""
from __future__ import annotations

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fea_static_cases as fsc  # noqa: E402


def _skip(case_id: str, reason: str) -> dict:
    return {"domain": "fea_static", "case": case_id, "setup_s": 0.0, "mesh_s": 0.0,
            "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
            "rel_err": None, "status": "skip", "detail": reason}


def _guard(fn, case_id: str) -> dict:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a crashing case must not kill the module
        tb = traceback.format_exc(limit=4)
        return {"domain": "fea_static", "case": case_id, "setup_s": 0.0, "mesh_s": 0.0,
                "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
                "rel_err": None, "status": "fail", "detail": f"exception: {tb[-500:]}"}


def run(quick: bool = False) -> list[dict]:
    ok, reason = fsc.solver_available()
    quick_ids = ["cantilever_tet10_f0.25", "plate_hole_tet10_f0.25",
                 "annulus_lame_tet10_f0.25", "lbracket_fillet_tet4_f0.25"]
    if not ok:
        ids = quick_ids if quick else quick_ids + ["(full set)"]
        return [_skip(cid, reason) for cid in ids]

    cases: list[dict] = []

    if quick:
        cases.append(_guard(lambda: fsc.case_cantilever(
            "cantilever_tet10_f0.25", order=2, fineness=0.25), quick_ids[0]))
        cases.append(_guard(lambda: fsc.case_plate_hole(
            "plate_hole_tet10_f0.25", order=2, fineness=0.25), quick_ids[1]))
        cases.append(_guard(lambda: fsc.case_annulus_lame(
            "annulus_lame_tet10_f0.25", order=2, fineness=0.25), quick_ids[2]))
        cases.append(_guard(lambda: fsc.case_regression(
            "lbracket_fillet_tet4_f0.25", "lbracket_fillet", order=1,
            fineness=0.25), quick_ids[3]))
        return cases

    # ---- full mode -------------------------------------------------------
    # Element-order / fineness frontier on the analytic cantilever.
    for order, fin in [(1, 0.3), (1, 0.5), (2, 0.25), (2, 0.4), (2, 0.55)]:
        kind = "tet10" if order == 2 else "tet4"
        cid = f"cantilever_{kind}_f{fin}"
        cases.append(_guard(lambda o=order, f=fin, c=cid:
                            fsc.case_cantilever(c, order=o, fineness=f), cid))

    # Plate with hole: element order + fineness.
    for order, fin in [(1, 0.3), (2, 0.25), (2, 0.4)]:
        kind = "tet10" if order == 2 else "tet4"
        cid = f"plate_hole_{kind}_f{fin}"
        cases.append(_guard(lambda o=order, f=fin, c=cid:
                            fsc.case_plate_hole(c, order=o, fineness=f), cid))

    # Lamé annulus: order + fineness (keep <=0.4 — solve grows ~cubically).
    for order, fin in [(1, 0.3), (2, 0.25), (2, 0.4)]:
        kind = "tet10" if order == 2 else "tet4"
        cid = f"annulus_lame_{kind}_f{fin}"
        cases.append(_guard(lambda o=order, f=fin, c=cid:
                            fsc.case_annulus_lame(c, order=o, fineness=f), cid))

    # Native-pressure witnesses (regression guards for the 2026-07-12 fixes):
    #   flat face  -> C1 oriented fan normals (was 6.9x off);
    #   curved bore -> C1 + C2 analytic curved-face facesets (was mostly-missed nodes).
    cases.append(_guard(lambda: fsc.case_pressure_native_bar(
        "pressure_native_bar_tet10"), "pressure_native_bar_tet10"))
    cases.append(_guard(lambda: fsc.case_annulus_lame_native(
        "annulus_lame_native_tet10"), "annulus_lame_native_tet10"))

    # Non-analytic regressions; bracket + loft get self-convergence references.
    # Ref fineness per geometry: the loft solves ~cubically with fineness (f0.5
    # measured 308 s solve on this box), so its ref stays at f0.35 (~1 min max).
    for geom, ref_fin in (("lbracket_fillet", 0.5), ("loft_tower", 0.35)):
        cid_ref = f"{geom}_tet10_f{ref_fin}_ref"
        ref_case = _guard(lambda g=geom, c=cid_ref, rf=ref_fin:
                          fsc.case_regression(c, g, order=2, fineness=rf), cid_ref)
        cases.append(ref_case)
        ref_disp = ref_case.get("metric") if ref_case.get("status") == "ok" else None
        cid = f"{geom}_tet10_f0.25"
        cases.append(_guard(lambda g=geom, c=cid, rd=ref_disp:
                            fsc.case_regression(c, g, order=2, fineness=0.25,
                                                ref_disp=rd), cid))
    for geom in ("shelled_box", "revolved_shaft", "multi_feature_plate", "swept_rod"):
        cid = f"{geom}_tet10_f0.25"
        cases.append(_guard(lambda g=geom, c=cid:
                            fsc.case_regression(c, g, order=2, fineness=0.25), cid))

    return cases


if __name__ == "__main__":
    import json
    out = run(quick="--quick" in sys.argv)
    print(json.dumps(out, indent=2, default=str))
