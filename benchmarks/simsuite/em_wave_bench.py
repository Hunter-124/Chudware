"""simsuite bench — EM wave stack (builtin FDFD eigensolver + analytic driven).

Cases: WR-90 TE10 cutoff via the 2-D FDFD eigensolver at increasing grid
resolutions (solve time vs grid size), plus one analytic uniform-guide driven
sweep as a near-zero-cost pipeline regression case.

Reference: TE10 cutoff of a hollow rectangular guide fc = c/(2a) (Pozar,
Microwave Engineering 4e, eq. 3.84 with m=1, n=0). No solver binaries needed:
pure numpy/scipy, in-process. quick=True runs only the smallest grid (<60 s
with enormous margin — the whole full sweep is ~1 s).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DOMAIN = "em_wave"

# WR-90 (EIA standard): a = 22.86 mm broad wall, b = 10.16 mm.
_A_M, _B_M = 0.02286, 0.01016


def _fdfd_case(resolution: int) -> dict:
    from engine_oracle.sim.em import analytic, fdfd

    t0 = time.perf_counter()
    ref = analytic.C0 / (2.0 * _A_M)          # TE10 fc = c/(2a), Pozar 4e eq. 3.84
    setup_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    try:
        res = fdfd.solve_eigenmodes("rectangular", _A_M, _B_M, n_modes=5,
                                    resolution=resolution)
        solve_s = time.perf_counter() - t1
        te10 = res["modes"][0]
        metric = float(te10["fc_hz"])
        rel_err = abs(metric - ref) / ref
        status = "ok" if (te10["name"] == "TE10" and rel_err < 0.01) else "fail"
        detail = (f"grid {res['solver_detail']['grid']}, "
                  f"{res['solver_detail']['cells']} cells, mode {te10['name']}; Hz")
    except Exception as exc:  # noqa: BLE001
        solve_s = time.perf_counter() - t1
        metric, rel_err, status, detail = None, None, "fail", f"{type(exc).__name__}: {exc}"
    return {"domain": DOMAIN, "case": f"fdfd_wr90_te10_res{resolution}",
            "setup_s": round(setup_s, 6), "mesh_s": 0.0,
            "solve_s": round(solve_s, 6),
            "total_s": round(setup_s + solve_s, 6),
            "metric": metric, "ref": ref, "rel_err": rel_err,
            "status": status, "detail": detail}


def _analytic_driven_case() -> dict:
    """Uniform WR-90 section S21 at 10 GHz must be lossless: |S21| = 1
    (S21 = exp(-gamma L), gamma purely imaginary above cutoff — Pozar 4e §3.3)."""
    import numpy as np
    from engine_oracle.sim.em.adapters import AnalyticAdapter

    t0 = time.perf_counter()
    try:
        res = AnalyticAdapter().driven({"standard": "WR-90", "length_mm": 100,
                                        "f_start_ghz": 5, "f_stop_ghz": 12,
                                        "n_freq": 201})
        total = time.perf_counter() - t0
        f = np.asarray(res["freq_ghz"])
        s21_db = np.asarray(res["s21_db"])
        metric = float(np.interp(10.0, f, s21_db))     # dB at 10 GHz
        ref = 0.0                                      # lossless propagating mode
        rel_err = abs(metric - ref)                    # dB error (ref is 0)
        status = "ok" if rel_err < 1e-3 else "fail"
        detail = "S21(10 GHz) dB vs 0 dB lossless; rel_err is |dB| here"
    except Exception as exc:  # noqa: BLE001
        total = time.perf_counter() - t0
        metric, ref, rel_err = None, 0.0, None
        status, detail = "fail", f"{type(exc).__name__}: {exc}"
    return {"domain": DOMAIN, "case": "analytic_wr90_driven_s21",
            "setup_s": 0.0, "mesh_s": 0.0, "solve_s": round(total, 6),
            "total_s": round(total, 6), "metric": metric, "ref": ref,
            "rel_err": rel_err, "status": status, "detail": detail}


def run(quick: bool = False) -> list:
    resolutions = (48,) if quick else (48, 96, 160)
    cases = [_fdfd_case(r) for r in resolutions]
    cases.append(_analytic_driven_case())
    return cases


if __name__ == "__main__":
    import json
    print(json.dumps(run(quick="--quick" in sys.argv), indent=2))
