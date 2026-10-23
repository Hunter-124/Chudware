"""EM simulation worker RPC surface with automatic solver fallback.

em_status / em_eigenmode / em_driven / em_timedomain.
Registered by ``services.worker_main`` alongside the Python simulation services.
The GUI uses the C++ ``services/worker`` routing seam; hostd forwards ``em_*``
requests to this Python table because libchudware does not implement them.

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

from .adapters import EmSolverError, _validate_result_finite, registry, select_adapter


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
                     "always work. See docs/reference/EM_SIMULATION.md for install recipes.")}


def _run(analysis: str, params: dict) -> dict:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"ok": False, "analysis": analysis, "error": "parameters must be an object"}

    def _invoke(ad) -> dict:
        result = getattr(ad, analysis)(params)
        if not isinstance(result, dict):
            raise EmSolverError(f"{ad.name} returned a non-object result")
        _validate_result_finite(result)
        return result

    requested = params.get("solver")
    automatic = requested in (None, "", "auto")
    if not automatic:
        try:
            ad = select_adapter(analysis, requested)
            return {"ok": True, "analysis": analysis, **_invoke(ad)}
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, EmSolverError) else (
                f"{type(exc).__name__}: {exc}")
            return {"ok": False, "analysis": analysis, "error": message,
                    "solvers": [candidate.info() for candidate in registry()]}

    failures = []
    for ad in registry():
        if analysis not in ad.capabilities or not ad.available().get("installed"):
            continue
        try:
            result = _invoke(ad)
            payload = {"ok": True, "analysis": analysis, **result}
            if failures:
                payload["solver_fallback"] = {
                    "selected": ad.name,
                    "failed_attempts": failures,
                }
            return payload
        except Exception as exc:  # noqa: BLE001
            message = str(exc) if isinstance(exc, EmSolverError) else (
                f"{type(exc).__name__}: {exc}")
            failures.append({"solver": ad.name, "error": message})

    detail = "; ".join(
        f"{failure['solver']}: {failure['error']}" for failure in failures)
    return {"ok": False, "analysis": analysis,
            "error": (f"all automatic {analysis} solvers failed: {detail}"
                      if failures else f"no installed solver offers {analysis!r} analysis"),
            "solvers": [ad.info() for ad in registry()]}


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
