"""EM simulation worker RPC surface — em_status / em_eigenmode / em_driven / em_timedomain.

Registered in engine_oracle/worker.py alongside the other Python-side sim
surfaces (the OpenFOAM CFD bridge pattern).  The host GUI reaches these over
the standard worker seam (services/worker WorkerClient stdio JSON-RPC); the
C++ core does not implement em_* so routing falls through to Python untouched.

Method contract (all return {"ok": bool, ...}; failures return
{"ok": False, "error": str, "solvers"?: [...]}):

em_status {}                       -> installed solver matrix + per-analysis default
em_eigenmode {waveguide|standard, n_modes?, resolution?, fields?, solver?}
                                   -> mode table (fc_ghz, kc, TE/TM labels, previews)
em_driven {waveguide|standard, length_mm?, f_start_ghz?, f_stop_ghz?, n_freq?,
           mode?, geometry?, solver?}
                                   -> S-parameters (freq_ghz, s11/s21 dB + complex)
em_timedomain {waveguide|standard, length_mm?, f0_ghz?, bw_ghz?, mode?, solver?}
                                   -> port waveforms (t_ns, port1_v/port2_v) [+ spectrum]
"""

from __future__ import annotations

from typing import Callable, Optional

from .adapters import EmSolverError, registry, select_adapter


def _status(params: dict, _emit: Optional[Callable] = None) -> dict:
    solvers = [ad.info() for ad in registry()]
    defaults = {}
    for analysis in ("eigenmode", "driven", "timedomain"):
        try:
            defaults[analysis] = select_adapter(analysis).name
        except EmSolverError:
            defaults[analysis] = None
    return {"ok": True, "solvers": solvers, "defaults": defaults,
            "analyses": ["eigenmode", "driven", "timedomain"],
            "note": ("openEMS is the full-wave FDTD engine; builtin-fdfd (real 2-D "
                     "eigensolver) and analytic-uniform (closed-form straight sections) "
                     "always work. See docs/EM_SIMULATION.md for install recipes.")}


def _run(analysis: str, params: dict) -> dict:
    params = params or {}
    try:
        ad = select_adapter(analysis, params.get("solver"))
        result = getattr(ad, analysis)(params)
        return {"ok": True, "analysis": analysis, **result}
    except EmSolverError as exc:
        return {"ok": False, "analysis": analysis, "error": str(exc),
                "solvers": [ad.info() for ad in registry()]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "analysis": analysis,
                "error": f"{type(exc).__name__}: {exc}"}


def _eigenmode(params: dict, _emit: Optional[Callable] = None) -> dict:
    return _run("eigenmode", params)


def _driven(params: dict, _emit: Optional[Callable] = None) -> dict:
    return _run("driven", params)


def _timedomain(params: dict, _emit: Optional[Callable] = None) -> dict:
    return _run("timedomain", params)


WORKER_METHODS = {
    "em_status": _status,
    "em_eigenmode": _eigenmode,
    "em_driven": _driven,
    "em_timedomain": _timedomain,
}
