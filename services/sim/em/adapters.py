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
import math
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



def _finite_float(value, name: str, *, positive: bool = False) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise EmSolverError(f"{name} must be a number") from exc
    if not math.isfinite(out):
        raise EmSolverError(f"{name} must be finite")
    if positive and out <= 0.0:
        raise EmSolverError(f"{name} must be greater than zero")
    return out


def _bounded_int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise EmSolverError(f"{name} must be an integer in [{lo}, {hi}]")
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmSolverError(f"{name} must be an integer in [{lo}, {hi}]") from exc
    try:
        exact = float(value)
    except (TypeError, ValueError, OverflowError):
        exact = float(out)
    if not math.isfinite(exact) or exact != out or not lo <= out <= hi:
        raise EmSolverError(f"{name} must be an integer in [{lo}, {hi}]")
    return out


def _positive_range(spec: dict, start_key: str, stop_key: str,
                    start_default: float, stop_default: float) -> tuple[float, float]:
    start = _finite_float(spec.get(start_key, start_default), start_key, positive=True)
    stop = _finite_float(spec.get(stop_key, stop_default), stop_key, positive=True)
    if stop <= start:
        raise EmSolverError(f"{stop_key} must exceed {start_key}")
    return start, stop


def _validate_result_finite(value, path: str = "result") -> None:
    """Reject NaN/Inf before a backend result is advertised as successful."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise EmSolverError(f"{path} contains a non-finite value")
        return
    if isinstance(value, complex):
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise EmSolverError(f"{path} contains a non-finite complex value")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_result_finite(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_result_finite(child, f"{path}[{index}]")
        return
    # numpy scalars are deliberately handled without importing numpy at module load.
    try:
        scalar = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(scalar):
        raise EmSolverError(f"{path} contains a non-finite value")

# ---------------------------------------------------------------- spec helpers

def resolve_cross_section(spec: dict) -> dict:
    """Normalise and validate a public waveguide cross-section specification."""
    if not isinstance(spec, dict):
        raise EmSolverError("EM analysis parameters must be an object")
    wg = spec.get("waveguide") or {}
    if not isinstance(wg, dict):
        raise EmSolverError("waveguide must be an object")
    eps_r = _finite_float(wg.get("eps_r", 1.0), "waveguide.eps_r", positive=True)
    std = spec.get("standard") or wg.get("standard")
    if std:
        if not isinstance(std, str):
            raise EmSolverError("waveguide standard must be a string such as 'WR-90'")
        try:
            a_mm, b_mm = analytic.lookup_standard(str(std))
        except ValueError as exc:
            raise EmSolverError(str(exc)) from exc
        return {"shape": "rectangular", "a_m": a_mm / 1e3, "b_m": b_mm / 1e3,
                "eps_r": eps_r, "label": f"WR-{str(std).upper().replace('WR', '').lstrip('-_ ')}"}
    shape = str(wg.get("shape") or "rectangular").lower()
    if shape not in ("rectangular", "circular"):
        raise EmSolverError(
            f"unsupported waveguide shape {shape!r}; expected 'rectangular' or 'circular'")
    if shape == "circular":
        raw_radius = wg.get("radius_mm", wg.get("a_mm"))
        if raw_radius is None:
            raise EmSolverError("circular waveguide needs radius_mm")
        radius_mm = _finite_float(raw_radius, "waveguide.radius_mm", positive=True)
        radius_m = radius_mm / 1e3
        if not math.isfinite(radius_m) or radius_m <= 0.0:
            raise EmSolverError("waveguide.radius_mm is outside the supported numeric range")
        return {"shape": "circular", "a_m": radius_m, "b_m": radius_m,
                "eps_r": eps_r, "label": f"circular r={radius_mm:g}mm"}
    if wg.get("a_mm") is None or wg.get("b_mm") is None:
        raise EmSolverError(
            "waveguide spec needs a_mm+b_mm (rectangular), radius_mm (circular), "
            "or standard='WR-xx'")
    a_mm = _finite_float(wg["a_mm"], "waveguide.a_mm", positive=True)
    b_mm = _finite_float(wg["b_mm"], "waveguide.b_mm", positive=True)
    if b_mm > a_mm:
        raise EmSolverError("waveguide.a_mm must be the broad-wall dimension (a_mm >= b_mm)")
    a_m, b_m = a_mm / 1e3, b_mm / 1e3
    if not math.isfinite(a_m) or not math.isfinite(b_m) or a_m <= 0.0 or b_m <= 0.0:
        raise EmSolverError("waveguide dimensions are outside the supported numeric range")
    return {"shape": "rectangular", "a_m": a_m, "b_m": b_m,
            "eps_r": eps_r, "label": f"{a_mm:g}x{b_mm:g}mm"}


def dominant_mode(cs: dict, mode_name: Optional[str] = None) -> dict:
    """Cutoff entry for the requested (or dominant) mode of a cross-section."""
    if mode_name is not None and not isinstance(mode_name, str):
        raise EmSolverError("mode must be a string such as 'TE10'")
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
    license = "Chudware (in-repo, numpy/scipy BSD)"
    linkage = "builtin"
    capabilities = ("eigenmode",)
    install_hint = "always available (numpy + scipy)"

    def eigenmode(self, spec: dict) -> dict:
        from . import fdfd
        cs = resolve_cross_section(spec)
        n_modes = _bounded_int(spec.get("n_modes", 6), "n_modes", 1, 40)
        resolution = _bounded_int(spec.get("resolution", 80), "resolution", 24, 512)
        res = fdfd.solve_eigenmodes(
            cs["shape"], cs["a_m"], cs["b_m"],
            n_modes=n_modes,
            resolution=resolution,
            eps_r=cs["eps_r"],
            want_fields=bool(spec.get("fields", False)))
        modes = [{**md, "fc_ghz": md["fc_hz"] / 1e9} for md in res["modes"]]
        result = {"solver": self.name, "cross_section": cs, "modes": modes,
                  "solver_detail": res["solver_detail"]}
        _validate_result_finite(result)
        return result


class AnalyticAdapter(EmAdapter):
    """Closed-form uniform-guide model — real physics for straight sections only.

    Driven/time-domain responses here assume an ideal uniform section between
    matched modal ports (S11 = 0, S21 = exp(-gamma L)).  Anything with actual
    3-D structure (irises, steps, transitions, STL geometry) must go to a
    full-wave solver; this adapter refuses those specs rather than fake them.
    """

    name = "analytic-uniform"
    kind = "analytic"
    license = "Chudware (in-repo)"
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
        n_modes = _bounded_int(spec.get("n_modes", 6), "n_modes", 1, 40)
        table = (analytic.rect_mode_table(cs["a_m"], cs["b_m"], n_modes, cs["eps_r"])
                 if cs["shape"] == "rectangular"
                 else analytic.circ_mode_table(cs["a_m"], n_modes, cs["eps_r"]))
        modes = [{**md, "fc_ghz": md["fc_hz"] / 1e9} for md in table]
        result = {"solver": self.name, "cross_section": cs, "modes": modes,
                  "solver_detail": {"method": "closed-form hollow-guide cutoffs"}}
        _validate_result_finite(result)
        return result

    def driven(self, spec: dict) -> dict:
        import numpy as np
        self._guard_uniform(spec)
        cs = resolve_cross_section(spec)
        md = dominant_mode(cs, spec.get("mode"))
        length_mm = _finite_float(spec.get("length_mm", 50.0), "length_mm", positive=True)
        length_m = length_mm / 1e3
        if not math.isfinite(length_m) or length_m <= 0.0:
            raise EmSolverError("length_mm is outside the supported numeric range")
        start_ghz, stop_ghz = _positive_range(
            spec, "f_start_ghz", "f_stop_ghz",
            md["fc_hz"] / 1e9 * 0.8, md["fc_hz"] / 1e9 * 2.0)
        n = _bounded_int(spec.get("n_freq", 201), "n_freq", 2, 100_001)
        f_start_hz, f_stop_hz = start_ghz * 1e9, stop_ghz * 1e9
        if not math.isfinite(f_start_hz) or not math.isfinite(f_stop_hz):
            raise EmSolverError("frequency range is outside the supported numeric range")
        f = np.linspace(f_start_hz, f_stop_hz, n)
        s11, s21 = analytic.uniform_section_sparams(
            f, md["fc_hz"], length_m, cs["eps_r"])
        result = {"solver": self.name, "cross_section": cs, "mode": md["name"],
                  "fc_ghz": md["fc_hz"] / 1e9, "length_mm": length_mm,
                  **sparam_payload(f, s11, s21),
                  "solver_detail": {"method": "S21=exp(-gamma L), matched modal ports"}}
        _validate_result_finite(result)
        return result

    def timedomain(self, spec: dict) -> dict:
        import numpy as np
        self._guard_uniform(spec)
        cs = resolve_cross_section(spec)
        md = dominant_mode(cs, spec.get("mode"))
        length_mm = _finite_float(spec.get("length_mm", 50.0), "length_mm", positive=True)
        length_m = length_mm / 1e3
        if not math.isfinite(length_m) or length_m <= 0.0:
            raise EmSolverError("length_mm is outside the supported numeric range")
        f0_ghz = _finite_float(
            spec.get("f0_ghz", md["fc_hz"] / 1e9 * 1.5), "f0_ghz", positive=True)
        bw_ghz = _finite_float(
            spec.get("bw_ghz", md["fc_hz"] / 1e9 * 0.5), "bw_ghz", positive=True)
        f0_hz, bw_hz = f0_ghz * 1e9, bw_ghz * 1e9
        if not math.isfinite(f0_hz) or not math.isfinite(bw_hz):
            raise EmSolverError("pulse frequencies are outside the supported numeric range")
        res = analytic.timedomain_response(
            md["fc_hz"], length_m, f0_hz, bw_hz)
        t_ns = res["t_s"] * 1e9
        t_max_ns = _finite_float(spec.get("t_max_ns", t_ns[-1]), "t_max_ns", positive=True)
        keep = int(np.searchsorted(t_ns, t_max_ns, side="right"))
        if keep < 2:
            raise EmSolverError("t_max_ns is too short to contain two time samples")
        result = {"solver": self.name, "cross_section": cs, "mode": md["name"],
                  "fc_ghz": md["fc_hz"] / 1e9, "length_mm": length_mm,
                  "t_ns": np.round(t_ns[:keep], 6).tolist(),
                  "ports": {"port1_v": np.round(res["v_in"][:keep], 6).tolist(),
                            "port2_v": np.round(res["v_out"][:keep], 6).tolist()},
                  "excitation": {"f0_ghz": f0_ghz, "bw_ghz": bw_ghz,
                                 "shape": "gaussian-modulated sine"},
                  "solver_detail": {"method": "dispersive IFFT through gamma(f)"}}
        _validate_result_finite(result)
        return result


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
    """Gmsh + GetDP FEM (frequency-domain / eigenmode) — scaffold."""

    name = "gmsh-getdp"
    kind = "fem"
    license = "Gmsh GPL-2+, GetDP GPL-2+ (external process only)"
    linkage = "external-process"
    capabilities = ()              # planned: ("eigenmode", "driven")
    install_hint = ("pip install gmsh; GetDP: sudo dnf install getdp "
                    "(or https://getdp.info binary) — adapter not yet implemented")
    _modules = ("gmsh",)
    _binaries = ("getdp", "gmsh")


class MeepAdapter(_ScaffoldAdapter):
    """MIT Meep FDTD — scaffold alternative to openEMS."""

    name = "meep"
    kind = "fdtd"
    license = "GPL-2+ (python module in separate worker process only)"
    linkage = "python-module"
    capabilities = ()              # planned: ("driven", "timedomain")
    install_hint = "conda install -c conda-forge pymeep — adapter not yet implemented"
    _modules = ("meep",)


class ElmerAdapter(_ScaffoldAdapter):
    """Elmer FEM (VectorHelmholtz) — scaffold."""

    name = "elmer"
    kind = "fem"
    license = "GPL-2+ w/ LGPL ElmerSolver library (external process only)"
    linkage = "external-process"
    capabilities = ()              # planned: ("driven", "eigenmode")
    install_hint = "sudo dnf install elmerfem — adapter not yet implemented"
    _binaries = ("ElmerSolver",)


# ---------------------------------------------------------------- result shaping

def sparam_payload(f_hz, s11, s21) -> dict:
    """Validate and shape complex S-parameter arrays for the public JSON payload."""
    import numpy as np

    freq = np.asarray(f_hz, dtype=float)
    s11_arr = np.asarray(s11, dtype=complex)
    s21_arr = np.asarray(s21, dtype=complex)
    if any(a.ndim != 1 for a in (freq, s11_arr, s21_arr)) or len(freq) < 2:
        raise EmSolverError("S-parameter results need one-dimensional arrays with at least two samples")
    if not (len(freq) == len(s11_arr) == len(s21_arr)):
        raise EmSolverError("S-parameter arrays have inconsistent sample counts")
    if not np.all(np.isfinite(freq)) or np.any(freq <= 0.0) or np.any(np.diff(freq) <= 0.0):
        raise EmSolverError("S-parameter frequency samples must be finite, positive, and increasing")
    if not all(np.all(np.isfinite(s.real)) and np.all(np.isfinite(s.imag))
               for s in (s11_arr, s21_arr)):
        raise EmSolverError("S-parameter arrays contain non-finite values")

    def _db(values):
        return np.round(20.0 * np.log10(np.maximum(np.abs(values), 1e-12)), 4).tolist()

    def _cplx(values):
        return [[round(float(v.real), 8), round(float(v.imag), 8)] for v in values]

    return {"freq_ghz": np.round(freq / 1e9, 6).tolist(),
            "s11_db": _db(s11_arr), "s21_db": _db(s21_arr),
            "s11": _cplx(s11_arr), "s21": _cplx(s21_arr)}


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
