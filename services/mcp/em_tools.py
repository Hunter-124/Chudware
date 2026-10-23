"""Full-wave EM MCP tools — thin FastMCP wrappers over engine_oracle.sim.em worker RPCs.

Exposes ``em_status`` / ``em_eigenmode`` / ``em_driven`` / ``em_timedomain`` that call the
validated builtin-fdfd + analytic-uniform adapters (and openEMS when installed). Results are
summary-first: resonant frequencies, key S-param points, and error-vs-analytic scalars ride in
the envelope by default; raw field meshes / full S-parameter arrays only appear when
``detail=True`` / ``verbose=True``.

openEMS-absent is an honest skip envelope (never a traceback). Registration is deferred behind
:func:`register_em_tools` so ``server.py`` wires us in with a one-line call.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from services.mcp._envelopes import analysis_envelope
from services.sim.em import analytic
from services.sim.em.worker_rpc import WORKER_METHODS

_dumps, _err, _ok = analysis_envelope()


def _call_worker(method: str, params: dict) -> dict:
    """Invoke a worker RPC handler; never raise — wrap unexpected errors."""
    fn = WORKER_METHODS.get(method)
    if fn is None:
        return {"ok": False, "error": f"unknown EM method {method!r}"}
    try:
        return fn(params or {}, None)
    except Exception as exc:  # noqa: BLE001 — MCP tools never traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "analysis": method.replace("em_", "")}



def _finite_arg(value: Any, name: str, *, allow_default_zero: bool = False) -> Optional[str]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return f"{name} must be a number"
    if not math.isfinite(number):
        return f"{name} must be finite"
    if number <= 0.0 and not (allow_default_zero and number == 0.0):
        return f"{name} must be greater than zero"
    return None

def _int_arg(value: Any, name: str, lo: int, hi: int) -> tuple[Optional[int], Optional[str]]:
    if isinstance(value, bool):
        return None, f"{name} must be an integer in [{lo}, {hi}]"
    try:
        number = float(value)
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        return None, f"{name} must be an integer in [{lo}, {hi}]"
    if not math.isfinite(number) or number != integer or not lo <= integer <= hi:
        return None, f"{name} must be an integer in [{lo}, {hi}]"
    return integer, None

def _standard_te10_ref_hz(standard: Optional[str], waveguide: Optional[dict]) -> Optional[float]:
    """Closed-form dominant-mode cutoff for error-vs-analytic reporting."""
    try:
        if standard:
            a_mm, b_mm = analytic.lookup_standard(standard)
            return analytic.rect_cutoff_hz(a_mm * 1e-3, b_mm * 1e-3, 1, 0)
        if waveguide and waveguide.get("shape", "rectangular") == "rectangular":
            a = float(waveguide.get("a_mm") or waveguide.get("a_m", 0) * 1e3)
            b = float(waveguide.get("b_mm") or waveguide.get("b_m", 0) * 1e3)
            if a > 0 and b > 0:
                return analytic.rect_cutoff_hz(a * 1e-3, b * 1e-3, 1, 0)
    except Exception:  # noqa: BLE001
        return None
    return None


def _compact_modes(modes: list, *, detail: bool, ref_hz: Optional[float]) -> dict:
    """Mode table → summary scalars; drop field meshes unless detail."""
    summary = []
    te10 = None
    for md in modes or []:
        entry = {
            "name": md.get("name"),
            "fc_ghz": md.get("fc_ghz", (md.get("fc_hz") or 0) / 1e9),
            "family": md.get("family") or md.get("type"),
            "m": md.get("m"),
            "n": md.get("n"),
        }
        if "kc" in md:
            entry["kc"] = md["kc"]
        if detail:
            for k in ("field", "fields", "preview", "Ez", "Hz", "psi"):
                if k in md:
                    entry[k] = md[k]
        summary.append(entry)
        if str(md.get("name", "")).upper() in ("TE10", "TE1,0") and te10 is None:
            te10 = entry
    out: dict[str, Any] = {
        "n_modes": len(summary),
        "modes": summary if detail else summary,  # always list cutoffs (small)
        "dominant": te10 or (summary[0] if summary else None),
    }
    if te10 is not None and ref_hz is not None and ref_hz > 0:
        fc = float(te10["fc_ghz"]) * 1e9
        out["error_vs_analytic_pct"] = abs(fc - ref_hz) / ref_hz * 100.0
        out["analytic_fc_ghz"] = ref_hz / 1e9
    if not detail:
        # Strip any residual field arrays that rode in from the adapter.
        for md in out["modes"]:
            for k in list(md.keys()):
                if k in ("field", "fields", "preview", "Ez", "Hz", "psi"):
                    md.pop(k, None)
    return out


def _compact_sparams(raw: dict, *, detail: bool) -> dict:
    """S-param payload → key-point scalars; full arrays only when detail."""
    freq = list(raw.get("freq_ghz") or [])
    s21_db = list(raw.get("s21_db") or [])
    s11_db = list(raw.get("s11_db") or [])
    out: dict[str, Any] = {
        "n_freq": len(freq),
        "f_start_ghz": freq[0] if freq else None,
        "f_stop_ghz": freq[-1] if freq else None,
        "solver": raw.get("solver"),
        "mode": raw.get("mode"),
        "fc_ghz": raw.get("fc_ghz"),
        "length_mm": raw.get("length_mm"),
    }
    if freq and s21_db:
        # Key points: mid-band S21, min/max S21, and S21 near 1.5*fc when available.
        mid = len(freq) // 2
        out["s21_db_mid"] = s21_db[mid]
        out["s11_db_mid"] = s11_db[mid] if s11_db else None
        out["s21_db_min"] = min(s21_db)
        out["s21_db_max"] = max(s21_db)
        fc = raw.get("fc_ghz")
        if fc is not None:
            target = 1.5 * float(fc)
            best_i = min(range(len(freq)), key=lambda i: abs(freq[i] - target))
            out["s21_db_at_1p5_fc"] = s21_db[best_i]
            out["f_at_1p5_fc_ghz"] = freq[best_i]
    if detail:
        out["freq_ghz"] = freq
        out["s21_db"] = s21_db
        out["s11_db"] = s11_db
        for k in ("s11", "s21", "s11_mag", "s21_mag", "s11_phase_deg", "s21_phase_deg"):
            if k in raw:
                out[k] = raw[k]
    return out


def _compact_timedomain(raw: dict, *, detail: bool) -> dict:
    ports = raw.get("ports") or {}
    t_ns = list(raw.get("t_ns") or [])
    p1 = list(ports.get("port1_v") or [])
    p2 = list(ports.get("port2_v") or [])
    out: dict[str, Any] = {
        "solver": raw.get("solver"),
        "mode": raw.get("mode"),
        "fc_ghz": raw.get("fc_ghz"),
        "length_mm": raw.get("length_mm"),
        "n_samples": len(t_ns),
        "t_max_ns": t_ns[-1] if t_ns else None,
        "excitation": raw.get("excitation"),
    }
    if p1:
        out["port1_peak"] = max(abs(v) for v in p1)
    if p2:
        out["port2_peak"] = max(abs(v) for v in p2)
        # Crude delay: argmax |port2| - argmax |port1|
        if p1 and t_ns:
            i1 = max(range(len(p1)), key=lambda i: abs(p1[i]))
            i2 = max(range(len(p2)), key=lambda i: abs(p2[i]))
            out["delay_ns"] = t_ns[i2] - t_ns[i1]
    if detail:
        out["t_ns"] = t_ns
        out["ports"] = {"port1_v": p1, "port2_v": p2}
    return out


def _is_openems_missing(raw: dict, *, requested: bool = False) -> bool:
    if raw.get("ok"):
        return False
    err = str(raw.get("error") or "").lower()
    if "openems" in err and any(token in err for token in (
            "not installed", "import failed", "no module", "cannot import",
            "not available", "backend unavailable")):
        return True
    if requested:
        for solver in raw.get("solvers") or []:
            if (isinstance(solver, dict) and solver.get("name") == "openems"
                    and not solver.get("installed")):
                return True
    return False


def register_em_tools(mcp) -> None:
    """Register em_status / em_eigenmode / em_driven / em_timedomain on ``mcp``."""

    @mcp.tool()
    def em_status() -> str:
        """EM solver matrix — which backends are installed and the default per analysis.

        Returns ``{ok, solvers, defaults, analyses, warnings, hint, next_actions}``. openEMS
        may be ``installed=false`` with an install hint; builtin-fdfd and analytic-uniform
        always work. Never fails.
        """
        raw = _call_worker("em_status", {})
        if not raw.get("ok"):
            return _err(raw.get("error", "em_status failed"),
                        hint="builtin-fdfd and analytic-uniform should always be available.",
                        next_actions=["em_eigenmode(standard='WR-90')",
                                      "em_driven(standard='WR-90', length_mm=50)"])
        solvers = raw.get("solvers") or []
        openems = next((s for s in solvers if s.get("name") == "openems"), None)
        warnings = []
        if openems is not None and not openems.get("installed"):
            warnings.append(
                "openEMS not installed — full-wave FDTD unavailable; "
                "driven/timedomain fall back to analytic-uniform for straight sections. "
                + str(openems.get("install") or openems.get("install_hint") or ""))
        return _ok({
            "solvers": solvers,
            "defaults": raw.get("defaults"),
            "analyses": raw.get("analyses"),
            "note": raw.get("note"),
        }, hint=("Call em_eigenmode(standard='WR-90') for a validated TE10 cutoff, "
                 "or em_driven(standard='WR-90', length_mm=50) for analytic S-params."),
           next_actions=[
               "em_eigenmode(standard='WR-90', n_modes=3)",
               "em_driven(standard='WR-90', length_mm=50, n_freq=21)",
               "em_timedomain(standard='WR-90', length_mm=80)",
           ], warnings=warnings)

    @mcp.tool()
    def em_eigenmode(standard: str = "", waveguide: str = "", n_modes: int = 6,
                     resolution: int = 80, fields: bool = False, solver: str = "",
                     detail: bool = False, verbose: bool = False) -> str:
        """Waveguide eigenmode cutoffs (builtin-fdfd by default).

        Pass ``standard='WR-90'`` (or another EIA name) or a JSON ``waveguide`` dict
        ``{shape, a_mm, b_mm, eps_r?}``. Returns compact mode table with ``fc_ghz`` and
        ``error_vs_analytic_pct`` for TE10 when a rectangular standard is used. Field meshes
        are omitted unless ``detail``/``verbose`` is True (or ``fields=True``).
        """
        n_modes_i, error = _int_arg(n_modes, "n_modes", 1, 40)
        if error:
            return _err(error, next_actions=["em_eigenmode(standard='WR-90', n_modes=3)"])
        resolution_i, error = _int_arg(resolution, "resolution", 24, 512)
        if error:
            return _err(error, next_actions=["em_eigenmode(standard='WR-90', resolution=80)"])
        params: dict[str, Any] = {
            "n_modes": n_modes_i,
            "resolution": resolution_i,
            "fields": bool(fields or detail or verbose),
        }
        if standard:
            params["standard"] = standard
        if waveguide:
            try:
                params["waveguide"] = json.loads(waveguide) if isinstance(waveguide, str) else waveguide
            except json.JSONDecodeError as exc:
                return _err(f"waveguide JSON invalid: {exc}",
                            hint='pass waveguide=\'{"shape":"rectangular","a_mm":22.86,"b_mm":10.16}\'',
                            next_actions=["em_eigenmode(standard='WR-90')"])
        if solver:
            params["solver"] = solver
        if not standard and not waveguide:
            return _err("pass standard= (e.g. 'WR-90') or waveguide= JSON cross-section",
                        hint="em_eigenmode needs a guide cross-section.",
                        next_actions=["em_eigenmode(standard='WR-90', n_modes=3)"])

        raw = _call_worker("em_eigenmode", params)
        if not raw.get("ok"):
            return _err(raw.get("error", "em_eigenmode failed"),
                        hint="Check standard name (WR-90) or waveguide dimensions.",
                        next_actions=["em_status()", "em_eigenmode(standard='WR-90')"],
                        solvers=raw.get("solvers"))

        ref_hz = _standard_te10_ref_hz(standard or None, params.get("waveguide"))
        want_detail = bool(detail or verbose or fields)
        compact = _compact_modes(raw.get("modes") or [], detail=want_detail, ref_hz=ref_hz)
        payload = {
            "analysis": "eigenmode",
            "solver": raw.get("solver"),
            "cross_section": raw.get("cross_section"),
            **compact,
        }
        if want_detail and "solver_detail" in raw:
            payload["solver_detail"] = raw["solver_detail"]
        hint = "Dominant-mode cutoff ready."
        if compact.get("error_vs_analytic_pct") is not None:
            hint = (f"TE10 fc={compact['dominant']['fc_ghz']:.4f} GHz; "
                    f"error vs analytic {compact['error_vs_analytic_pct']:.3f}%.")
        return _ok(payload, hint=hint,
                   next_actions=["em_driven(standard='WR-90', length_mm=50)",
                                 "em_eigenmode(..., detail=True)  # include field previews"])

    @mcp.tool()
    def em_driven(standard: str = "", waveguide: str = "", length_mm: float = 50.0,
                  f_start_ghz: float = 0.0, f_stop_ghz: float = 0.0, n_freq: int = 21,
                  mode: str = "", solver: str = "", geometry: str = "",
                  detail: bool = False, verbose: bool = False) -> str:
        """Driven S-parameter sweep of a uniform guide section (analytic-uniform default).

        Full-wave openEMS is used when installed and selected; otherwise analytic-uniform
        returns ideal matched-port S21=exp(-gamma L). When openEMS is requested but absent
        the result is an honest skip envelope (never a traceback). Default payload is
        summary scalars (mid-band S21/S11, extrema); pass ``detail=True`` for full arrays.
        """
        for value, name, default_zero in (
                (length_mm, "length_mm", False),
                (f_start_ghz, "f_start_ghz", True),
                (f_stop_ghz, "f_stop_ghz", True)):
            error = _finite_arg(value, name, allow_default_zero=default_zero)
            if error:
                return _err(error, next_actions=["em_driven(standard='WR-90', length_mm=50)"])
        length_value = float(length_mm)
        start_value = float(f_start_ghz)
        stop_value = float(f_stop_ghz)
        n_freq_i, error = _int_arg(n_freq, "n_freq", 2, 100_001)
        if error:
            return _err(error, next_actions=["em_driven(standard='WR-90', n_freq=21)"])
        params: dict[str, Any] = {"length_mm": length_value, "n_freq": n_freq_i}
        if standard:
            params["standard"] = standard
        if waveguide:
            try:
                params["waveguide"] = json.loads(waveguide) if isinstance(waveguide, str) else waveguide
            except json.JSONDecodeError as exc:
                return _err(f"waveguide JSON invalid: {exc}",
                            next_actions=["em_driven(standard='WR-90', length_mm=50)"])
        if start_value > 0:
            params["f_start_ghz"] = start_value
        if stop_value > 0:
            params["f_stop_ghz"] = stop_value
        if mode:
            params["mode"] = mode
        if solver:
            params["solver"] = solver
        if geometry:
            try:
                params["geometry"] = (
                    json.loads(geometry) if geometry.startswith("{") else {"stl_path": geometry})
            except json.JSONDecodeError as exc:
                return _err(f"geometry JSON invalid: {exc}",
                            next_actions=["em_driven(standard='WR-90', length_mm=50)"])
        if not standard and not waveguide and not geometry:
            return _err("pass standard= (e.g. 'WR-90') or waveguide=/geometry=",
                        hint="em_driven needs a guide cross-section or STL geometry.",
                        next_actions=["em_driven(standard='WR-90', length_mm=50, n_freq=21)"])

        raw = _call_worker("em_driven", params)
        if not raw.get("ok"):
            err = str(raw.get("error") or "em_driven failed")
            if _is_openems_missing(raw, requested=solver == "openems"):
                solvers = raw.get("solvers") or []
                openems_info = next((s for s in solvers if s.get("name") == "openems"), None)
                return _dumps({
                        "ok": False,
                        "skipped": True,
                        "skip_reason": "openems_not_installed",
                        "error": err,
                        "warnings": [
                            "openEMS FDTD backend is not installed on this machine; "
                            "full-wave driven analysis is unavailable."
                        ],
                        "hint": (openems_info or {}).get("install")
                                or (openems_info or {}).get("install_hint")
                                or "See docs/reference/EM_SIMULATION.md for openEMS install recipes.",
                        "next_actions": [
                            "em_driven(standard='WR-90', length_mm=50, solver='analytic-uniform')",
                            "em_status()  # inspect installed solvers",
                        ],
                        "solvers": solvers,
                        "analysis": "driven",
                })
            return _err(err, hint="Use analytic-uniform for straight sections, or install openEMS.",
                        next_actions=["em_driven(standard='WR-90', solver='analytic-uniform')",
                                      "em_status()"],
                        solvers=raw.get("solvers"))

        want_detail = bool(detail or verbose)
        compact = _compact_sparams(raw, detail=want_detail)
        payload = {"analysis": "driven", **compact}
        if want_detail and "solver_detail" in raw:
            payload["solver_detail"] = raw["solver_detail"]
        # Analytic cross-check: for analytic-uniform, S21 mid-band should be ~0 dB above cutoff.
        if raw.get("solver") == "analytic-uniform" and compact.get("s21_db_mid") is not None:
            payload["analytic_s21_db_mid"] = compact["s21_db_mid"]
        return _ok(payload,
                   hint=(f"S21 mid-band {compact.get('s21_db_mid')} dB via {compact.get('solver')}."
                         if compact.get("s21_db_mid") is not None else "Driven S-params ready."),
                   next_actions=["em_driven(..., detail=True)  # full S-param arrays",
                                 "em_timedomain(standard='WR-90', length_mm=80)"])

    @mcp.tool()
    def em_timedomain(standard: str = "", waveguide: str = "", length_mm: float = 80.0,
                      f0_ghz: float = 0.0, bw_ghz: float = 0.0, mode: str = "",
                      solver: str = "", detail: bool = False, verbose: bool = False) -> str:
        """Time-domain pulse through a uniform guide (analytic dispersive IFFT default).

        Returns peak/delay summary scalars; raw port waveforms only when ``detail=True``.
        openEMS-absent with ``solver='openems'`` returns an honest skip envelope.
        """
        for value, name, default_zero in (
                (length_mm, "length_mm", False),
                (f0_ghz, "f0_ghz", True),
                (bw_ghz, "bw_ghz", True)):
            error = _finite_arg(value, name, allow_default_zero=default_zero)
            if error:
                return _err(error, next_actions=["em_timedomain(standard='WR-90', length_mm=80)"])
        length_value = float(length_mm)
        f0_value = float(f0_ghz)
        bw_value = float(bw_ghz)
        params: dict[str, Any] = {"length_mm": length_value}
        if standard:
            params["standard"] = standard
        if waveguide:
            try:
                params["waveguide"] = json.loads(waveguide) if isinstance(waveguide, str) else waveguide
            except json.JSONDecodeError as exc:
                return _err(f"waveguide JSON invalid: {exc}",
                            next_actions=["em_timedomain(standard='WR-90', length_mm=80)"])
        if f0_value > 0:
            params["f0_ghz"] = f0_value
        if bw_value > 0:
            params["bw_ghz"] = bw_value
        if mode:
            params["mode"] = mode
        if solver:
            params["solver"] = solver
        if not standard and not waveguide:
            return _err("pass standard= (e.g. 'WR-90') or waveguide=",
                        next_actions=["em_timedomain(standard='WR-90', length_mm=80)"])

        raw = _call_worker("em_timedomain", params)
        if not raw.get("ok"):
            err = str(raw.get("error") or "em_timedomain failed")
            if _is_openems_missing(raw, requested=solver == "openems"):
                solvers = raw.get("solvers") or []
                openems_info = next((s for s in solvers if s.get("name") == "openems"), None)
                return _dumps({
                        "ok": False,
                        "skipped": True,
                        "skip_reason": "openems_not_installed",
                        "error": err,
                        "warnings": [
                            "openEMS FDTD backend is not installed; time-domain full-wave "
                            "unavailable."
                        ],
                        "hint": (openems_info or {}).get("install")
                                or (openems_info or {}).get("install_hint")
                                or "See docs/reference/EM_SIMULATION.md.",
                        "next_actions": [
                            "em_timedomain(standard='WR-90', solver='analytic-uniform')",
                            "em_status()",
                        ],
                        "solvers": solvers,
                        "analysis": "timedomain",
                })
            return _err(err, next_actions=["em_timedomain(standard='WR-90', solver='analytic-uniform')",
                                           "em_status()"],
                        solvers=raw.get("solvers"))

        want_detail = bool(detail or verbose)
        compact = _compact_timedomain(raw, detail=want_detail)
        payload = {"analysis": "timedomain", **compact}
        return _ok(payload,
                   hint=(f"port2 peak={compact.get('port2_peak')}, delay={compact.get('delay_ns')} ns"
                         if compact.get("port2_peak") is not None else "Time-domain response ready."),
                   next_actions=["em_timedomain(..., detail=True)  # full waveforms"])
