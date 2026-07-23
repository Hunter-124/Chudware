"""Pluggable EM-solver adapter layer.

Every solver backend (openEMS FDTD, the built-in FDFD eigensolver, the analytic
uniform-guide model, and the scaffolded Gmsh+GetDP / Meep / Elmer adapters)
implements :class:`EmAdapter`.  The registry resolves "which solver runs this
analysis" with an explicit ``solver=`` override or capability-ranked auto pick.

License containment (contracts/license_debt.md): GPL solvers (openEMS GPLv3,
Meep GPLv2, GetDP GPLv2) are reached ONLY as external processes or as Python
modules imported lazily inside their adapter's methods — never linked into the
C++ core, never imported at package import time.
"""

from __future__ import annotations

import importlib
import shutil
from abc import ABC
from typing import Dict, List, Optional

from . import analytic
from .analytic import C0  # noqa: F401  (re-export convenience)


class EmSolverError(RuntimeError):
    """A solver backend failed or is unavailable for the requested analysis."""


class EmAdapter(ABC):
    """One EM solver backend. Subclasses fill the class metadata and analysis methods."""

    name: str = "?"
    kind: str = "?"                # "fdtd" | "fem" | "fdfd" | "analytic" | "mom"
    license: str = "?"
    linkage: str = "builtin"       # "builtin" | "python-module" | "external-process"
    capabilities: tuple = ()       # subset of ("eigenmode", "driven", "timedomain")
    install_hint: str = ""

    # -- availability -------------------------------------------------------
    def available(self) -> Dict:
        """{"installed": bool, "version": str|None, "detail": str}."""
        return {"installed": True, "version": None, "detail": "built in"}

    def info(self) -> Dict:
        avail = self.available()
        return {"name": self.name, "kind": self.kind, "license": self.license,
                "linkage": self.linkage, "capabilities": list(self.capabilities),
                "installed": bool(avail.get("installed")),
                "version": avail.get("version"),
                "detail": avail.get("detail", ""),
                "install": self.install_hint}

    # -- analyses (override the ones in `capabilities`) ----------------------
    def eigenmode(self, spec: dict) -> dict:
        raise EmSolverError(f"{self.name} does not implement eigenmode analysis")

    def driven(self, spec: dict) -> dict:
        raise EmSolverError(f"{self.name} does not implement driven (S-parameter) analysis")

    def timedomain(self, spec: dict) -> dict:
        raise EmSolverError(f"{self.name} does not implement time-domain analysis")


# ---------------------------------------------------------------- spec helpers

def resolve_cross_section(spec: dict) -> dict:
    """Normalise the waveguide cross-section out of an RPC spec.

    Accepts either spec["standard"] = "WR-90" or spec["waveguide"] =
    {"shape": "rectangular"|"circular", "a_mm", "b_mm" | "radius_mm", "eps_r"}.
    Returns {"shape", "a_m", "b_m", "eps_r", "label"}.
    """
    eps_r = 1.0
    wg = spec.get("waveguide") or {}
    if isinstance(wg, dict) and wg.get("eps_r"):
        eps_r = float(wg["eps_r"])
    std = spec.get("standard") or (wg.get("standard") if isinstance(wg, dict) else None)
    if std:
        a_mm, b_mm = analytic.lookup_standard(str(std))
        return {"shape": "rectangular", "a_m": a_mm / 1e3, "b_m": b_mm / 1e3,
                "eps_r": eps_r, "label": str(std).upper()}
    shape = (wg.get("shape") or "rectangular").lower()
    if shape == "circular":
        r_mm = wg.get("radius_mm") or wg.get("a_mm")
        if not r_mm:
            raise EmSolverError("circular waveguide needs radius_mm")
        return {"shape": "circular", "a_m": float(r_mm) / 1e3, "b_m": float(r_mm) / 1e3,
                "eps_r": eps_r, "label": f"circular r={r_mm}mm"}
    a_mm, b_mm = wg.get("a_mm"), wg.get("b_mm")
    if not a_mm or not b_mm:
        raise EmSolverError(
            "waveguide spec needs a_mm+b_mm (rectangular), radius_mm (circular), "
            "or standard='WR-xx'")
    return {"shape": "rectangular", "a_m": float(a_mm) / 1e3, "b_m": float(b_mm) / 1e3,
            "eps_r": eps_r, "label": f"{a_mm}x{b_mm}mm"}


def dominant_mode(cs: dict, mode_name: Optional[str] = None) -> dict:
    """Cutoff entry for the requested (or dominant) mode of a cross-section."""
    table = (analytic.rect_mode_table(cs["a_m"], cs["b_m"], 20, cs["eps_r"])
             if cs["shape"] == "rectangular"
             else analytic.circ_mode_table(cs["a_m"], 20, cs["eps_r"]))
    if mode_name:
        want = mode_name.strip().upper()
        for md in table:
            if md["name"] == want:
                return md
        raise EmSolverError(f"mode {mode_name!r} not found for {cs['label']}")
    return table[0]


# ---------------------------------------------------------------- builtin adapters

class BuiltinFdfdAdapter(EmAdapter):
    """Real 2-D finite-difference eigenmode solver — no external install needed."""

    name = "builtin-fdfd"
    kind = "fdfd"
    license = "AI-CAD (in-repo, numpy/scipy BSD)"
    linkage = "builtin"
    capabilities = ("eigenmode",)
    install_hint = "always available (numpy + scipy)"

    def eigenmode(self, spec: dict) -> dict:
        from . import fdfd
        cs = resolve_cross_section(spec)
        res = fdfd.solve_eigenmodes(
            cs["shape"], cs["a_m"], cs["b_m"],
            n_modes=int(spec.get("n_modes", 6)),
            resolution=int(spec.get("resolution", 80)),
            eps_r=cs["eps_r"],
            want_fields=bool(spec.get("fields", False)))
        modes = [{**md, "fc_ghz": md["fc_hz"] / 1e9} for md in res["modes"]]
        return {"solver": self.name, "cross_section": cs, "modes": modes,
                "solver_detail": res["solver_detail"]}


class AnalyticAdapter(EmAdapter):
    """Closed-form uniform-guide model — real physics for straight sections only.

    Driven/time-domain responses here assume an ideal uniform section between
    matched modal ports (S11 = 0, S21 = exp(-gamma L)).  Anything with actual
    3-D structure (irises, steps, transitions, STL geometry) must go to a
    full-wave solver; this adapter refuses those specs rather than fake them.
    """

    name = "analytic-uniform"
    kind = "analytic"
    license = "AI-CAD (in-repo)"
    linkage = "builtin"
    capabilities = ("eigenmode", "driven", "timedomain")
    install_hint = "always available"

    def _guard_uniform(self, spec: dict) -> None:
        if spec.get("geometry"):
            raise EmSolverError(
                "analytic-uniform only models straight uniform sections; "
                "geometry (STL/doc) specs need a full-wave solver (openEMS)")

    def eigenmode(self, spec: dict) -> dict:
        cs = resolve_cross_section(spec)
        table = (analytic.rect_mode_table(cs["a_m"], cs["b_m"],
                                          int(spec.get("n_modes", 6)), cs["eps_r"])
                 if cs["shape"] == "rectangular"
                 else analytic.circ_mode_table(cs["a_m"], int(spec.get("n_modes", 6)),
                                               cs["eps_r"]))
        modes = [{**md, "fc_ghz": md["fc_hz"] / 1e9} for md in table]
        return {"solver": self.name, "cross_section": cs, "modes": modes,
                "solver_detail": {"method": "closed-form hollow-guide cutoffs"}}

    def driven(self, spec: dict) -> dict:
        import numpy as np
        self._guard_uniform(spec)
        cs = resolve_cross_section(spec)
        md = dominant_mode(cs, spec.get("mode"))
        length_m = float(spec.get("length_mm", 50.0)) / 1e3
        f0 = float(spec.get("f_start_ghz", md["fc_hz"] / 1e9 * 0.8)) * 1e9
        f1 = float(spec.get("f_stop_ghz", md["fc_hz"] / 1e9 * 2.0)) * 1e9
        n = int(spec.get("n_freq", 201))
        f = np.linspace(f0, f1, n)
        s11, s21 = analytic.uniform_section_sparams(f, md["fc_hz"], length_m, cs["eps_r"])
        return {"solver": self.name, "cross_section": cs, "mode": md["name"],
                "fc_ghz": md["fc_hz"] / 1e9, "length_mm": length_m * 1e3,
                **sparam_payload(f, s11, s21),
                "solver_detail": {"method": "S21=exp(-gamma L), matched modal ports"}}

    def timedomain(self, spec: dict) -> dict:
        import numpy as np
        self._guard_uniform(spec)
        cs = resolve_cross_section(spec)
        md = dominant_mode(cs, spec.get("mode"))
        length_m = float(spec.get("length_mm", 50.0)) / 1e3
        f0 = float(spec.get("f0_ghz", md["fc_hz"] / 1e9 * 1.5)) * 1e9
        bw = float(spec.get("bw_ghz", md["fc_hz"] / 1e9 * 0.5)) * 1e9
        res = analytic.timedomain_response(md["fc_hz"], length_m, f0, bw)
        t_ns = (res["t_s"] * 1e9)
        keep = int(np.searchsorted(t_ns, float(spec.get("t_max_ns", t_ns[-1]))))
        return {"solver": self.name, "cross_section": cs, "mode": md["name"],
                "fc_ghz": md["fc_hz"] / 1e9, "length_mm": length_m * 1e3,
                "t_ns": np.round(t_ns[:keep], 6).tolist(),
                "ports": {"port1_v": np.round(res["v_in"][:keep], 6).tolist(),
                          "port2_v": np.round(res["v_out"][:keep], 6).tolist()},
                "excitation": {"f0_ghz": f0 / 1e9, "bw_ghz": bw / 1e9,
                               "shape": "gaussian-modulated sine"},
                "solver_detail": {"method": "dispersive IFFT through gamma(f)"}}


# ---------------------------------------------------------------- scaffold stubs

class _ScaffoldAdapter(EmAdapter):
    """Base for planned-but-not-implemented backends: detected, never run."""

    _modules: tuple = ()
    _binaries: tuple = ()

    def available(self) -> Dict:
        for mod in self._modules:
            try:
                m = importlib.import_module(mod)
                ver = getattr(m, "__version__", None)
                return {"installed": True, "version": ver,
                        "detail": f"python module {mod} present (adapter not implemented yet)"}
            except Exception:  # noqa: BLE001
                continue
        for exe in self._binaries:
            path = shutil.which(exe)
            if path:
                return {"installed": True, "version": None,
                        "detail": f"{exe} on PATH at {path} (adapter not implemented yet)"}
        return {"installed": False, "version": None, "detail": "not installed"}


class GetdpAdapter(_ScaffoldAdapter):
    """Gmsh + GetDP FEM (frequency-domain / eigenmode) — scaffold, T-SIM11."""

    name = "gmsh-getdp"
    kind = "fem"
    license = "Gmsh GPL-2+, GetDP GPL-2+ (external process only)"
    linkage = "external-process"
    capabilities = ()              # planned: ("eigenmode", "driven")
    install_hint = ("pip install gmsh; GetDP: sudo dnf install getdp "
                    "(or https://getdp.info binary) — adapter tracked in T-SIM11")
    _modules = ("gmsh",)
    _binaries = ("getdp", "gmsh")


class MeepAdapter(_ScaffoldAdapter):
    """MIT Meep FDTD — scaffold alternative to openEMS, T-SIM11."""

    name = "meep"
    kind = "fdtd"
    license = "GPL-2+ (python module in separate worker process only)"
    linkage = "python-module"
    capabilities = ()              # planned: ("driven", "timedomain")
    install_hint = "conda install -c conda-forge pymeep — adapter tracked in T-SIM11"
    _modules = ("meep",)


class ElmerAdapter(_ScaffoldAdapter):
    """Elmer FEM (VectorHelmholtz) — scaffold, T-SIM11."""

    name = "elmer"
    kind = "fem"
    license = "GPL-2+ w/ LGPL ElmerSolver library (external process only)"
    linkage = "external-process"
    capabilities = ()              # planned: ("driven", "eigenmode")
    install_hint = "sudo dnf install elmerfem — adapter tracked in T-SIM11"
    _binaries = ("ElmerSolver",)


# ---------------------------------------------------------------- result shaping

def sparam_payload(f_hz, s11, s21) -> dict:
    """Shape complex S-parameter arrays the way the GUI results plumbing expects.

    freq_ghz + per-parameter dB magnitude + [re, im] complex pairs (JSON-safe).
    """
    import numpy as np

    def _db(s):
        return np.round(20.0 * np.log10(np.maximum(np.abs(s), 1e-12)), 4).tolist()

    def _cplx(s):
        return [[round(float(v.real), 8), round(float(v.imag), 8)] for v in s]

    return {"freq_ghz": np.round(np.asarray(f_hz) / 1e9, 6).tolist(),
            "s11_db": _db(s11), "s21_db": _db(s21),
            "s11": _cplx(s11), "s21": _cplx(s21)}


# ---------------------------------------------------------------- registry

def _build_registry() -> List[EmAdapter]:
    from .openems_adapter import OpenEmsAdapter
    return [OpenEmsAdapter(), BuiltinFdfdAdapter(), AnalyticAdapter(),
            GetdpAdapter(), MeepAdapter(), ElmerAdapter()]


_REGISTRY: Optional[List[EmAdapter]] = None


def registry() -> List[EmAdapter]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def select_adapter(analysis: str, solver: Optional[str] = None) -> EmAdapter:
    """Pick the adapter for an analysis: explicit ``solver=`` name, else the first
    installed adapter (registry order = preference order) that has the capability."""
    adapters = registry()
    if solver and solver not in ("auto", ""):
        for ad in adapters:
            if ad.name == solver:
                if analysis not in ad.capabilities:
                    raise EmSolverError(f"solver {solver!r} cannot run {analysis!r} "
                                        f"(capabilities: {list(ad.capabilities)})")
                if not ad.available().get("installed"):
                    raise EmSolverError(
                        f"solver {solver!r} is not installed — {ad.install_hint}")
                return ad
        raise EmSolverError(
            f"unknown solver {solver!r}; known: {[a.name for a in adapters]}")
    for ad in adapters:
        if analysis in ad.capabilities and ad.available().get("installed"):
            return ad
    raise EmSolverError(f"no installed solver offers {analysis!r} analysis")
