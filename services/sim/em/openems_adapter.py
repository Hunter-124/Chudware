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
install recipe (docs/EM_SIMULATION.md).  openEMS has no PyPI wheel — it is a
source build (CSXCAD + openEMS C++ libs + python bindings) or a distro package.
"""

from __future__ import annotations

import math
import os
import tempfile
from typing import Dict, Optional

from .adapters import EmAdapter, EmSolverError, resolve_cross_section, dominant_mode, \
    sparam_payload
from .analytic import C0

def _lines(lo: float, hi: float, step: float) -> list:
    """Uniform mesh lines covering [lo, hi] inclusive with spacing <= step."""
    n = max(2, int(math.ceil((hi - lo) / step)) + 1)
    return [round(lo + (hi - lo) * i / (n - 1), 6) for i in range(n)]


_INSTALL = ("no PyPI wheel — build from source: "
            "https://docs.openems.de/install (Fedora: dnf install cmake gcc-c++ "
            "tinyxml-devel hdf5-devel vtk-devel boost-devel CGAL-devel, then "
            "`pip install .` in CSXCAD/python and openEMS/python), "
            "or use the openSUSE/Ubuntu packages; see docs/EM_SIMULATION.md")


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
        """
        cs = resolve_cross_section(spec)
        if cs["shape"] != "rectangular":
            raise EmSolverError("openems adapter v1 handles rectangular guides "
                                "(circular ports tracked in T-SIM11)")
        md = dominant_mode(cs, spec.get("mode"))
        fc_hz = md["fc_hz"]
        length_mm = float(spec.get("length_mm", 50.0))
        f_start = float(spec.get("f_start_ghz", fc_hz / 1e9 * 0.8)) * 1e9
        f_stop = float(spec.get("f_stop_ghz", fc_hz / 1e9 * 2.0)) * 1e9
        if broadband:
            f_start = min(f_start, fc_hz * 0.5)
        f0 = 0.5 * (f_start + f_stop)
        fbw = 0.5 * (f_stop - f_start)
        if fbw <= 0:
            raise EmSolverError("f_stop_ghz must exceed f_start_ghz")

        a_mm, b_mm = cs["a_m"] * 1e3, cs["b_m"] * 1e3
        # mesh: >= 20 cells per shortest wavelength, and >= 15 across the section
        lam_min_mm = C0 / (f_stop * math.sqrt(cs["eps_r"])) * 1e3
        res_mm = min(lam_min_mm / float(spec.get("cells_per_wavelength", 20)),
                     a_mm / 15.0)
        mesh = {
            "x": _lines(0.0, a_mm, res_mm),
            "y": _lines(0.0, b_mm, res_mm),
            "z": _lines(0.0, length_mm, res_mm),
        }
        # ports sit inboard of the PMLs; S21 reference length is their separation
        z1 = round(length_mm * 0.15, 4)
        z2 = round(length_mm * 0.85, 4)
        plan = {
            "unit_m": 1e-3,
            "cross_section": cs,
            "mode": md["name"],
            "fc_hz": fc_hz,
            "length_mm": length_mm,
            "boundaries": ["PEC", "PEC", "PEC", "PEC", "PML_8", "PML_8"],
            "excitation": {"type": "gaussian", "f0_hz": f0, "fc_hz": fbw},
            "freq": {"start_hz": f_start, "stop_hz": f_stop,
                     "n": int(spec.get("n_freq", 201))},
            "ports": [
                {"nr": 0, "z_mm": z1, "excite": 1, "mode": md["name"],
                 "a_mm": a_mm, "b_mm": b_mm},
                {"nr": 1, "z_mm": z2, "excite": 0, "mode": md["name"],
                 "a_mm": a_mm, "b_mm": b_mm},
            ],
            "port_separation_mm": z2 - z1,
            "geometry": [],
            "nr_ts": int(spec.get("max_timesteps", 200_000)),
            "end_criteria": float(spec.get("end_criteria", 1e-4)),
            "mesh": mesh,
        }
        geom = spec.get("geometry") or {}
        if isinstance(geom, dict) and geom.get("stl_path"):
            path = str(geom["stl_path"])
            if not os.path.isfile(path):
                raise EmSolverError(f"geometry stl_path not found: {path}")
            plan["geometry"].append({"kind": "stl-metal", "path": path,
                                     "priority": int(geom.get("priority", 10))})
        elif geom:
            raise EmSolverError(
                "unsupported geometry spec — v1 supports {'stl_path': ...} (export the "
                "doc body via export_part first); direct doc tess intake tracked in T-SIM11")
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
            start = [0.0, 0.0, p["z_mm"]]
            stop = [p["a_mm"], p["b_mm"], p["z_mm"]]
            ports.append(fdtd.AddRectWaveGuidePort(
                p["nr"], start, stop, "z", p["a_mm"] * plan["unit_m"],
                p["b_mm"] * plan["unit_m"], p["mode"], excite=p["excite"]))
        sim_path = tempfile.mkdtemp(prefix="chudware_em_")
        fdtd.Run(sim_path, cleanup=True)

        fr = plan["freq"]
        freq = np.linspace(fr["start_hz"], fr["stop_hz"], fr["n"])
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
        freq = np.asarray(raw["freq_hz"], dtype=float)
        inc = np.asarray(raw["p1_inc"], dtype=complex)
        inc = np.where(np.abs(inc) < 1e-30, 1e-30, inc)
        s11 = np.asarray(raw["p1_ref"], dtype=complex) / inc
        s21 = np.asarray(raw["p2_ref"], dtype=complex) / inc
        return {"solver": self.name, "cross_section": plan["cross_section"],
                "mode": plan["mode"], "fc_ghz": plan["fc_hz"] / 1e9,
                "length_mm": plan["length_mm"],
                "port_separation_mm": plan["port_separation_mm"],
                **sparam_payload(freq, s11, s21),
                "solver_detail": {"method": "openEMS FDTD, modal waveguide ports",
                                  "mesh_cells": [len(plan["mesh"][ax]) for ax in "xyz"],
                                  "boundaries": plan["boundaries"]}}

    def timedomain(self, spec: dict) -> dict:
        import numpy as np
        plan = self.build_plan(spec, broadband=True)
        raw = self._execute(plan, want_time=True)
        result = {"solver": self.name, "cross_section": plan["cross_section"],
                  "mode": plan["mode"], "fc_ghz": plan["fc_hz"] / 1e9,
                  "length_mm": plan["length_mm"],
                  "excitation": {"f0_ghz": plan["excitation"]["f0_hz"] / 1e9,
                                 "bw_ghz": plan["excitation"]["fc_hz"] / 1e9,
                                 "shape": "gaussian-modulated sine"},
                  "solver_detail": {"method": "openEMS FDTD broadband run"}}
        time = raw.get("time")
        if time:
            t_ns = np.asarray(time["t_s"], dtype=float) * 1e9
            result["t_ns"] = np.round(t_ns, 6).tolist()
            result["ports"] = {"port1_v": time["port1_v"], "port2_v": time["port2_v"]}
        else:
            result["warning"] = raw.get("time_error", "no time signals captured")
        # one broadband run also yields the spectrum — include it for free
        freq = np.asarray(raw["freq_hz"], dtype=float)
        inc = np.asarray(raw["p1_inc"], dtype=complex)
        inc = np.where(np.abs(inc) < 1e-30, 1e-30, inc)
        s11 = np.asarray(raw["p1_ref"], dtype=complex) / inc
        s21 = np.asarray(raw["p2_ref"], dtype=complex) / inc
        result["spectrum"] = sparam_payload(freq, s11, s21)
        return result
