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
from typing import Any, Optional

from services.sim.em import analytic
from services.sim.em.worker_rpc import WORKER_METHODS


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=_json_default)


def _json_default(obj: Any):
    # numpy scalars / arrays may leak through worker payloads; keep JSON clean.
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:  # noqa: BLE001
        pass
    return str(obj)


def _err(reason: str, *, hint: str = "", next_actions: Optional[list] = None,
         **extra: Any) -> str:
    out: dict[str, Any] = {
        "ok": False,
        "error": reason,
        "warnings": [],
        "hint": hint or reason,
        "next_actions": next_actions or [],
    }
    out.update(extra)
    return _dumps(out)


def _ok(payload: dict, *, hint: str = "", next_actions: Optional[list] = None,
        warnings: Optional[list] = None) -> str:
    out = {
        "ok": True,
        "warnings": list(warnings or []),
        "hint": hint,
        "next_actions": list(next_actions or []),
        **payload,
    }
    return _dumps(out)


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


def _is_openems_missing(raw: dict) -> bool:
    if raw.get("ok"):
        return False
    err = str(raw.get("error") or "").lower()
    if "openems" in err and any(tok in err for tok in (
            "not installed", "import failed", "no module", "unavailable", "not available")):
        return True
    # Explicit solver=openems with adapter unavailable surfaces via solvers list.
    if raw.get("solver") == "openems" and not raw.get("ok"):
        return True
    solvers = raw.get("solvers") or []
    if any(isinstance(s, dict) and s.get("name") == "openems"
           and not s.get("installed") for s in solvers):
        if "openems" in err or raw.get("analysis") in ("driven", "timedomain"):
            # Only treat as openEMS-skip when the request targeted it or the error names it.
            if "openems" in err or (isinstance(raw.get("params"), dict)
                                    and raw["params"].get("solver") == "openems"):
                return True
    return "openems" in err and ("install" in err or "import" in err or "not" in err)


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
        params: dict[str, Any] = {
            "n_modes": int(n_modes),
            "resolution": int(resolution),
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
        params: dict[str, Any] = {
            "length_mm": float(length_mm),
            "n_freq": int(n_freq),
        }
        if standard:
            params["standard"] = standard
        if waveguide:
            try:
                params["waveguide"] = json.loads(waveguide) if isinstance(waveguide, str) else waveguide
            except json.JSONDecodeError as exc:
                return _err(f"waveguide JSON invalid: {exc}",
                            next_actions=["em_driven(standard='WR-90', length_mm=50)"])
        if f_start_ghz > 0:
            params["f_start_ghz"] = float(f_start_ghz)
        if f_stop_ghz > 0:
            params["f_stop_ghz"] = float(f_stop_ghz)
        if mode:
            params["mode"] = mode
        if solver:
            params["solver"] = solver
        if geometry:
            params["geometry"] = json.loads(geometry) if geometry.startswith("{") else {"stl_path": geometry}
        if not standard and not waveguide and not geometry:
            return _err("pass standard= (e.g. 'WR-90') or waveguide=/geometry=",
                        hint="em_driven needs a guide cross-section or STL geometry.",
                        next_actions=["em_driven(standard='WR-90', length_mm=50, n_freq=21)"])

        raw = _call_worker("em_driven", params)
        if not raw.get("ok"):
            err = str(raw.get("error") or "em_driven failed")
            # Honest openEMS skip when the binary/module is absent.
            if solver == "openems" or "openems" in err.lower():
                solvers = raw.get("solvers") or []
                openems_info = next((s for s in solvers if s.get("name") == "openems"), None)
                installed = bool(openems_info and openems_info.get("installed"))
                if not installed or "openems" in err.lower():
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
                                or "See docs/EM_SIMULATION.md for openEMS install recipes.",
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
        params: dict[str, Any] = {"length_mm": float(length_mm)}
        if standard:
            params["standard"] = standard
        if waveguide:
            try:
                params["waveguide"] = json.loads(waveguide) if isinstance(waveguide, str) else waveguide
            except json.JSONDecodeError as exc:
                return _err(f"waveguide JSON invalid: {exc}",
                            next_actions=["em_timedomain(standard='WR-90', length_mm=80)"])
        if f0_ghz > 0:
            params["f0_ghz"] = float(f0_ghz)
        if bw_ghz > 0:
            params["bw_ghz"] = float(bw_ghz)
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
            if solver == "openems" or "openems" in err.lower():
                solvers = raw.get("solvers") or []
                openems_info = next((s for s in solvers if s.get("name") == "openems"), None)
                installed = bool(openems_info and openems_info.get("installed"))
                if not installed or "openems" in err.lower():
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
                                or "See docs/EM_SIMULATION.md.",
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
