"""openEMS FDTD adapter — the primary full-wave EM backend.

openEMS (https://openems.de, GPLv3) is driven through its official Python
interface (``openEMS`` + ``CSXCAD`` modules), imported LAZILY inside this
adapter only — the modules run in the Python sim-worker process and are never
linked into the C++ core (license containment, contracts/license_debt.md).

Structure — three layers so the pipeline is testable without the solver binary:

* :meth:`OpenEmsAdapter.build_plan` — PURE: spec -> normalised simulation plan
  (drawing unit, mesh lines, ports, boundaries, excitation, geometry items).
* :meth:`OpenEmsAdapter._execute` — the ONLY method that imports/runs openEMS.
  Returns raw per-port modal voltage spectra (and time signals on request).
* :meth:`driven` / :meth:`timedomain` — plan -> execute -> JSON result shaping.

Tests (test_em.py) drive the full driven/timedomain pipeline with ``_execute``
replaced by an analytic synthesiser (mock-proven, not all-skip): the plan
builder, S-parameter math and result shaping are all exercised for real.

Not installed on this machine?  ``available()`` says so and points at the
install recipe (docs/reference/EM_SIMULATION.md).  openEMS has no PyPI wheel — it is a
source build (CSXCAD + openEMS C++ libs + python bindings) or a distro package.
"""

from __future__ import annotations

import math
import os
import tempfile
from typing import Dict, Optional

from .adapters import (
    EmAdapter, EmSolverError, _bounded_int, _finite_float, _positive_range,
    _validate_result_finite, dominant_mode, resolve_cross_section, sparam_payload,
)
from .analytic import C0

def _lines(lo: float, hi: float, step: float) -> list:
    """Uniform mesh lines covering [lo, hi] inclusive with spacing <= step."""
    n = max(2, int(math.ceil((hi - lo) / step)) + 1)
    return [round(lo + (hi - lo) * i / (n - 1), 6) for i in range(n)]


_INSTALL = ("no PyPI wheel — build from source: "
            "https://docs.openems.de/install (Fedora: dnf install cmake gcc-c++ "
            "tinyxml-devel hdf5-devel vtk-devel boost-devel CGAL-devel, then "
            "`pip install .` in CSXCAD/python and openEMS/python), "
            "or use the openSUSE/Ubuntu packages; see docs/reference/EM_SIMULATION.md")


class OpenEmsAdapter(EmAdapter):
    name = "openems"
    kind = "fdtd"
    license = "GPLv3 (python module in separate sim-worker process; never linked into core)"
    linkage = "python-module"
    capabilities = ("driven", "timedomain")
    install_hint = _INSTALL

    # ------------------------------------------------------------ availability
    def available(self) -> Dict:
        try:
            import openEMS  # noqa: F401  (lazy, GPL containment)
            import CSXCAD   # noqa: F401
            ver = getattr(openEMS, "__version__", None)
            return {"installed": True, "version": ver,
                    "detail": "openEMS + CSXCAD python modules importable"}
        except Exception as exc:  # noqa: BLE001
            return {"installed": False, "version": None,
                    "detail": f"import failed: {exc}"}

    # ------------------------------------------------------------ plan (pure)
    def build_plan(self, spec: dict, broadband: bool = False) -> dict:
        """Normalise an RPC spec into an openEMS simulation plan (no solver calls).

        The plan is plain data: mm-unit mesh lines, two rectangular-waveguide
        modal ports along +z, PEC side walls, PML_8 ends, Gaussian excitation.

        Time-domain f0_ghz/bw_ghz map directly to SetGaussExcite's centre and
        Gaussian bandwidth parameter (not a full-width conversion), matching
        the analytic backend. Missing pulse fields retain the sweep-derived
        defaults. Explicit spectrum boundaries are never widened; absent ones
        sample the requested nominal pulse band. The mesh resolves f0 + 4*bw,
        matching the analytic backend's time-domain frequency margin.
        """
        cs = resolve_cross_section(spec)
        if cs["shape"] != "rectangular":
            raise EmSolverError("openems adapter v1 handles rectangular guides "
                                "(circular ports are not yet supported)")
        md = dominant_mode(cs, spec.get("mode"))
        fc_hz = md["fc_hz"]
        length_mm = _finite_float(spec.get("length_mm", 50.0), "length_mm", positive=True)
        explicit_pulse = broadband and ("f0_ghz" in spec or "bw_ghz" in spec)
        if explicit_pulse:
            # Validate ordering after absent boundaries have been derived from
            # the pulse; a high explicit start may exceed the old default stop.
            start_ghz = _finite_float(
                spec.get("f_start_ghz", fc_hz / 1e9 * 0.8),
                "f_start_ghz", positive=True)
            stop_ghz = _finite_float(
                spec.get("f_stop_ghz", fc_hz / 1e9 * 2.0),
                "f_stop_ghz", positive=True)
        else:
            start_ghz, stop_ghz = _positive_range(
                spec, "f_start_ghz", "f_stop_ghz",
                fc_hz / 1e9 * 0.8, fc_hz / 1e9 * 2.0)
        f_start, f_stop = start_ghz * 1e9, stop_ghz * 1e9
        if not math.isfinite(f_start) or not math.isfinite(f_stop):
            raise EmSolverError("frequency range is outside the supported numeric range")
        if broadband and "f_start_ghz" not in spec:
            f_start = min(f_start, fc_hz * 0.5)
        excitation_start = min(f_start, fc_hz * 0.5) if broadband else f_start
        fbw = 0.5 * (f_stop - excitation_start)
        f0 = excitation_start + fbw
        if not explicit_pulse and (
                not math.isfinite(f0) or not math.isfinite(fbw) or fbw <= 0.0):
            raise EmSolverError("openEMS excitation bandwidth is degenerate")
        mesh_frequency = f_stop
        if explicit_pulse:
            pulse = {}
            for key, default_hz in (("f0_ghz", f0), ("bw_ghz", fbw)):
                if key not in spec:
                    pulse[key] = default_hz
                    continue
                if isinstance(spec[key], bool):
                    raise EmSolverError(f"{key} must be a number, not a boolean")
                try:
                    value = _finite_float(spec[key], key, positive=True) * 1e9
                except OverflowError as exc:
                    raise EmSolverError(
                        f"{key} is outside the supported numeric range") from exc
                if not math.isfinite(value) or value <= 0.0:
                    raise EmSolverError(f"{key} is outside the supported numeric range")
                pulse[key] = value
            f0, fbw = pulse["f0_ghz"], pulse["bw_ghz"]
            if not math.isfinite(f0) or f0 <= 0.0:
                raise EmSolverError("f0_ghz produces a degenerate excitation centre")
            if not math.isfinite(fbw) or fbw <= 0.0:
                raise EmSolverError("bw_ghz produces a degenerate excitation bandwidth")
            upper_band = f0 + 4.0 * fbw
            if not math.isfinite(upper_band) or upper_band <= f0:
                raise EmSolverError(
                    "f0_ghz/bw_ghz produce a non-finite or degenerate excitation range")
            # Positive bandwidth alone is insufficient at floating-point extremes:
            # the envelope duration must also be representable, as in gaussian_pulse.
            envelope_time = (9.0 / (2.0 * math.pi)) / fbw
            if not math.isfinite(envelope_time) or envelope_time <= 0.0:
                raise EmSolverError("bw_ghz produces a degenerate Gaussian envelope")
            if "f_start_ghz" not in spec:
                f_start = f0 - fbw if f0 > fbw else min(f_start, f0)
            if "f_stop_ghz" not in spec:
                f_stop = f0 + fbw
            if f_stop <= f_start:
                raise EmSolverError(
                    "f_start_ghz/f_stop_ghz do not define a positive range for f0_ghz/bw_ghz")
            mesh_frequency = max(f_stop, upper_band)

        a_mm, b_mm = cs["a_m"] * 1e3, cs["b_m"] * 1e3
        # Resolve both the shortest wavelength and the narrow wall; the old a-only
        # limit left fewer than 15 cells across b in every canonical WR guide.
        cells_per_wavelength = _bounded_int(
            spec.get("cells_per_wavelength", 20), "cells_per_wavelength", 10, 200)
        lam_min_mm = C0 / (mesh_frequency * math.sqrt(cs["eps_r"])) * 1e3
        res_mm = min(lam_min_mm / cells_per_wavelength, a_mm / 15.0, b_mm / 15.0)
        if not math.isfinite(res_mm) or res_mm <= 0.0:
            raise EmSolverError("openEMS mesh resolution is degenerate")
        spans = {"x": a_mm, "y": b_mm, "z": length_mm}
        ratios = {axis: span / res_mm for axis, span in spans.items()}
        if any(not math.isfinite(ratio) for ratio in ratios.values()):
            raise EmSolverError("openEMS mesh dimensions are outside the supported numeric range")
        line_counts = {
            axis: max(2, int(math.ceil(ratio)) + 1)
            for axis, ratio in ratios.items()
        }
        mesh_cells = line_counts["x"] * line_counts["y"] * line_counts["z"]
        if max(line_counts.values()) > 200_000 or mesh_cells > 5_000_000:
            raise EmSolverError(
                "openEMS mesh exceeds the adapter capability limit "
                f"({line_counts}, {mesh_cells} cells)")
        mesh = {
            "x": _lines(0.0, a_mm, res_mm),
            "y": _lines(0.0, b_mm, res_mm),
            "z": _lines(0.0, length_mm, res_mm),
        }
        # Ports occupy a finite propagation-direction box. Their measurement planes
        # (the box stops in openEMS) remain at 15% and 85% of the guide, while the
        # excitation boxes extend five target cells toward their respective PMLs.
        z1 = round(length_mm * 0.15, 6)
        z2 = round(length_mm * 0.85, 6)
        port_depth = min(5.0 * res_mm, length_mm * 0.1)
        if z2 <= z1 or not math.isfinite(port_depth) or port_depth <= 0.0:
            raise EmSolverError("waveguide length is too short to separate the modal ports")
        plan = {
            "unit_m": 1e-3,
            "cross_section": cs,
            "mode": md["name"],
            "fc_hz": fc_hz,
            "length_mm": length_mm,
            "boundaries": ["PEC", "PEC", "PEC", "PEC", "PML_8", "PML_8"],
            "excitation": {"type": "gaussian", "f0_hz": f0, "fc_hz": fbw},
            "freq": {"start_hz": f_start, "stop_hz": f_stop,
                     "n": _bounded_int(spec.get("n_freq", 201), "n_freq", 2, 100_001)},
            "ports": [
                {"nr": 0, "start_z_mm": z1 - port_depth, "stop_z_mm": z1,
                 "excite": 1, "mode": md["name"], "a_mm": a_mm, "b_mm": b_mm},
                {"nr": 1, "start_z_mm": z2 + port_depth, "stop_z_mm": z2,
                 "excite": 0, "mode": md["name"], "a_mm": a_mm, "b_mm": b_mm},
            ],
            "port_separation_mm": z2 - z1,
            "geometry": [],
            "nr_ts": _bounded_int(spec.get("max_timesteps", 200_000),
                                   "max_timesteps", 1, 10_000_000),
            "end_criteria": _finite_float(
                spec.get("end_criteria", 1e-4), "end_criteria", positive=True),
            "mesh": mesh,
        }
        if plan["end_criteria"] >= 1.0:
            raise EmSolverError("end_criteria must be less than 1")
        geom = spec.get("geometry") or {}
        if isinstance(geom, dict) and geom.get("stl_path"):
            path = str(geom["stl_path"])
            if not os.path.isfile(path):
                raise EmSolverError(f"geometry stl_path not found: {path}")
            plan["geometry"].append({
                "kind": "stl-metal", "path": path,
                "priority": _bounded_int(geom.get("priority", 10), "geometry.priority", 0, 1000),
            })
        elif geom:
            raise EmSolverError(
                "unsupported geometry spec — v1 supports {'stl_path': ...} (export the "
                "doc body via export_part first); direct doc tess intake is not yet supported")
        return plan

    # ------------------------------------------------------------ solver seam
    def _execute(self, plan: dict, want_time: bool = False) -> dict:
        """Run openEMS on a plan. Returns raw port spectra (+ time signals).

        {"freq_hz": [...], "p1_inc": [complex...], "p1_ref": [...], "p2_ref": [...],
         "time": {"t_s": [...], "port1_v": [...], "port2_v": [...]}?}
        THE ONLY method that touches the GPL modules; tests replace it.
        """
        import numpy as np
        try:
            from CSXCAD import ContinuousStructure
            from openEMS import openEMS
        except Exception as exc:  # noqa: BLE001
            raise EmSolverError(f"openEMS not installed — {_INSTALL} ({exc})")

        fdtd = openEMS(NrTS=plan["nr_ts"], EndCriteria=plan["end_criteria"])
        exc_ = plan["excitation"]
        fdtd.SetGaussExcite(exc_["f0_hz"], exc_["fc_hz"])
        fdtd.SetBoundaryCond(plan["boundaries"])
        csx = ContinuousStructure()
        fdtd.SetCSX(csx)
        mesh = csx.GetGrid()
        mesh.SetDeltaUnit(plan["unit_m"])
        for axis in ("x", "y", "z"):
            mesh.AddLine(axis, plan["mesh"][axis])
        for g in plan["geometry"]:
            if g["kind"] == "stl-metal":
                metal = csx.AddMetal("stl_geom")
                reader = metal.AddPolyhedronReader(g["path"], priority=g["priority"])
                reader.ReadFile()
        ports = []
        for p in plan["ports"]:
            start = [0.0, 0.0, p["start_z_mm"]]
            stop = [p["a_mm"], p["b_mm"], p["stop_z_mm"]]
            mesh.AddLine("z", [p["start_z_mm"], p["stop_z_mm"]])
            ports.append(fdtd.AddRectWaveGuidePort(
                p["nr"], start, stop, "z", p["a_mm"] * plan["unit_m"],
                p["b_mm"] * plan["unit_m"], p["mode"], excite=p["excite"]))
        fr = plan["freq"]
        # RectWGPort.CalcPort in openEMS 0.37 evaluates beta with a real sqrt and
        # therefore returns NaNs at/below modal cutoff. Driven sweeps must fall back
        # to the analytic uniform-guide model for that band. A time-domain run still
        # uses its broadband excitation, but its reported modal spectrum is sampled
        # only where the port impedance is defined by this binding.
        if not want_time and fr["start_hz"] <= plan["fc_hz"]:
            raise EmSolverError(
                "openEMS RectWGPort post-processing requires f_start_ghz above "
                f"the {plan['mode']} cutoff ({plan['fc_hz'] / 1e9:.6g} GHz); "
                "use solver='analytic-uniform' for a sweep through cutoff")
        spectrum_start = (max(fr["start_hz"], plan["fc_hz"] * (1.0 + 1e-9))
                          if want_time else fr["start_hz"])
        if spectrum_start >= fr["stop_hz"]:
            raise EmSolverError(
                "openEMS time-domain spectrum has no samples above modal cutoff")
        freq = np.linspace(spectrum_start, fr["stop_hz"], fr["n"])
        sim_path = tempfile.mkdtemp(prefix="chudware_em_")
        fdtd.Run(sim_path, cleanup=True)
        for p in ports:
            p.CalcPort(sim_path, freq)
        out = {
            "freq_hz": freq.tolist(),
            "p1_inc": np.asarray(ports[0].uf_inc, dtype=complex),
            "p1_ref": np.asarray(ports[0].uf_ref, dtype=complex),
            "p2_ref": np.asarray(ports[1].uf_ref, dtype=complex),
        }
        if want_time:
            # openEMS keeps per-port time-domain voltages after CalcPort (u_data)
            try:
                t = np.asarray(ports[0].u_data.ui_time[0])
                out["time"] = {
                    "t_s": t.tolist(),
                    "port1_v": np.real(ports[0].u_data.ui_val[0]).tolist(),
                    "port2_v": np.real(ports[1].u_data.ui_val[0]).tolist(),
                }
            except Exception as exc:  # noqa: BLE001
                out["time_error"] = f"time signals unavailable: {exc}"
        return out

    # ------------------------------------------------------------ analyses
    def driven(self, spec: dict) -> dict:
        import numpy as np
        plan = self.build_plan(spec)
        raw = self._execute(plan, want_time=False)
        freq, inc, reflected, transmitted = self._validated_spectra(raw)
        s11 = reflected / inc
        s21 = transmitted / inc
        result = {"solver": self.name, "cross_section": plan["cross_section"],
                  "mode": plan["mode"], "fc_ghz": plan["fc_hz"] / 1e9,
                  "length_mm": plan["length_mm"],
                  "port_separation_mm": plan["port_separation_mm"],
                  **sparam_payload(freq, s11, s21),
                  "solver_detail": {"method": "openEMS FDTD, modal waveguide ports",
                                    "mesh_cells": [len(plan["mesh"][ax]) for ax in "xyz"],
                                    "boundaries": plan["boundaries"]}}
        _validate_result_finite(result)
        return result

    @staticmethod
    def _validated_spectra(raw: dict):
        import numpy as np
        try:
            freq = np.asarray(raw["freq_hz"], dtype=float)
            inc = np.asarray(raw["p1_inc"], dtype=complex)
            reflected = np.asarray(raw["p1_ref"], dtype=complex)
            transmitted = np.asarray(raw["p2_ref"], dtype=complex)
        except (KeyError, TypeError, ValueError) as exc:
            raise EmSolverError(f"openEMS returned malformed port spectra: {exc}") from exc
        arrays = (freq, inc, reflected, transmitted)
        if any(a.ndim != 1 for a in arrays) or len(freq) < 2:
            raise EmSolverError("openEMS port spectra must be one-dimensional with at least two samples")
        if not (len(freq) == len(inc) == len(reflected) == len(transmitted)):
            raise EmSolverError("openEMS port spectra have inconsistent sample counts")
        if not np.all(np.isfinite(freq)) or np.any(freq <= 0.0) or np.any(np.diff(freq) <= 0.0):
            raise EmSolverError("openEMS returned invalid frequency samples")
        if not all(np.all(np.isfinite(a.real)) and np.all(np.isfinite(a.imag))
                   for a in (inc, reflected, transmitted)):
            raise EmSolverError("openEMS returned non-finite port spectra")
        peak = float(np.max(np.abs(inc)))
        if not math.isfinite(peak) or peak <= 0.0 or np.any(np.abs(inc) <= peak * 1e-15):
            raise EmSolverError("openEMS incident-port spectrum is zero or numerically unusable")
        return freq, inc, reflected, transmitted

    def timedomain(self, spec: dict) -> dict:
        import numpy as np
        plan = self.build_plan(spec, broadband=True)
        raw = self._execute(plan, want_time=True)
        time = raw.get("time")
        if not isinstance(time, dict):
            raise EmSolverError(raw.get("time_error", "openEMS returned no time-domain signals"))
        try:
            t_s = np.asarray(time["t_s"], dtype=float)
            port1 = np.asarray(time["port1_v"], dtype=float)
            port2 = np.asarray(time["port2_v"], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise EmSolverError(f"openEMS returned malformed time-domain signals: {exc}") from exc
        if (t_s.ndim != 1 or len(t_s) < 2 or len(t_s) != len(port1) or len(t_s) != len(port2)
                or not np.all(np.isfinite(t_s)) or not np.all(np.diff(t_s) > 0.0)
                or not np.all(np.isfinite(port1)) or not np.all(np.isfinite(port2))):
            raise EmSolverError("openEMS returned non-finite or inconsistent time-domain signals")
        result = {"solver": self.name, "cross_section": plan["cross_section"],
                  "mode": plan["mode"], "fc_ghz": plan["fc_hz"] / 1e9,
                  "length_mm": plan["length_mm"],
                  "excitation": {"f0_ghz": plan["excitation"]["f0_hz"] / 1e9,
                                 "bw_ghz": plan["excitation"]["fc_hz"] / 1e9,
                                 "shape": "gaussian-modulated sine"},
                  "solver_detail": {"method": "openEMS FDTD broadband run"},
                  "t_ns": np.round(t_s * 1e9, 6).tolist(),
                  "ports": {"port1_v": port1.tolist(), "port2_v": port2.tolist()}}
        freq, inc, reflected, transmitted = self._validated_spectra(raw)
        result["spectrum"] = sparam_payload(freq, reflected / inc, transmitted / inc)
        _validate_result_finite(result)
        return result
