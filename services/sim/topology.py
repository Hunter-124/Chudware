"""Topology-optimization service helpers shared by the model tool and the job registry.

No numerical solver lives here. The native ``sim_run_topology`` RPC owns meshing, the
seven physics evaluators and the four optimizer families; ``topology_capabilities`` is the
only method x physics support matrix. This module owns the service boundary around them:

* :func:`normalize_study` — the canonical, GUI-readable PERSISTED study. Faces stay
  selectors (never machine node ids); every unknown/model-authored key and every retained
  run is kept verbatim.
* :func:`build_envelope` — the resolved RUN envelope the native adapter consumes: one
  merged faceset per boundary entry (:func:`merge_facesets`, the same merge rule the GUI
  uses), a strict physical material dict, the capability-filtered ``topology`` object and
  a server-owned ``result_dir``.
* :func:`validate_run_envelope` — the topology-specific payload check of the closed
  ``core_rpc`` job stage (services/sim/jobs.py), run before any job row exists.
* :func:`allocate_run_dir` — the per-run artifact directory: inside the project jail,
  outside the job spool (which is pruned), created exclusively so nothing is overwritten.
* Collection: :func:`verify_artifacts`, :func:`externalize_bulk`, :func:`dfam_report`,
  :func:`run_record`, :func:`apply_run_record` — hashes/provenance, density fields moved
  out of the study, printability from services.dfam on the real exported surface, and the
  compact RunRecord (host/src/ui/sim_topology.h) appended to the SAME study.
* Unfinished runs: :func:`unfinished_result`, :func:`recover_native_run` — a failed or
  cancelled job retained with only what the solver wrote (reply or durable native files).

Units follow the public study contract: mm, N, MPa, W, C, W/(m K), W/(m^2 K), m/s, Pa,
Pa*s, g/cm^3. Conversion to SI happens exactly once, in the native preparation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

# ---------------------------------------------------------------- vocabulary (contract)
# Identifiers of the public study schema. They are NOT a support matrix: whether a
# method/physics pair runs, which objectives/limits/parameters it offers, and its
# formulation label come only from the native topology_capabilities() reply.

PHYSICS = ("structural", "thermal", "modal", "buckling", "flow", "thermomechanical", "cht")
METHODS = ("simp", "ramp", "beso", "level_set")

#: core/sim/topopt_model.cpp uses_structural_boundary / uses_thermal_boundary /
#: uses_flow_boundary. The native preparation REFUSES a section its physics does not use.
STRUCTURAL_PHYSICS = frozenset({"structural", "modal", "buckling", "thermomechanical"})
THERMAL_PHYSICS = frozenset({"thermal", "thermomechanical", "cht"})
FLOW_PHYSICS = frozenset({"flow", "cht"})

#: Physics whose response is identically zero without a non-zero mechanical load.
LOAD_DRIVEN_PHYSICS = frozenset({"structural", "buckling"})

#: Physical material keys the native parse_material accepts (plus ``name``).
MATERIAL_KEYS = ("E_mpa", "nu", "yield_mpa", "density_g_cm3", "k_w_mk", "alpha_per_k",
                 "cp_j_kgk")

#: core/sim/topopt_model.cpp require_material, per physics.
REQUIRED_MATERIAL = {
    "structural": ("E_mpa", "nu"),
    "buckling": ("E_mpa", "nu"),
    "modal": ("E_mpa", "nu", "density_g_cm3"),
    "thermal": ("k_w_mk",),
    "cht": ("k_w_mk",),
    "thermomechanical": ("E_mpa", "nu", "k_w_mk", "alpha_per_k"),
    "flow": (),
}

#: Physics whose material elasticity is consumed (an anisotropic library row cannot be
#: replaced by isotropic defaults for them).
_ELASTIC_PHYSICS = frozenset({"structural", "buckling", "modal", "thermomechanical"})

#: CHT advects heat through the optimized fluid, so the fluid's own properties are inputs.
_CHT_FLUID_KEYS = ("fluid_k_w_mk", "fluid_cp_j_kgk")

#: Surface-mesh artifact formats the DFAM check and the export panel can use.
SURFACE_FORMATS = ("stl", "3mf", "obj", "ply")

#: Result keys that carry per-element/per-node arrays. They are written to the run
#: directory and referenced as artifacts; a study record stays compact.
BULK_RESULT_KEYS = ("density", "fields")

#: Descriptive keys tolerated on any entry; never consulted as physics.
_LABEL_KEYS = frozenset({"label", "name", "comment", "note", "notes", "description", "title"})

_STUDY_TYPE = "topology"
_ABS_ZERO_C = -273.15


class TopologyInputError(ValueError):
    """A setup refusal with a stable machine code and the offending field.

    Codes: ``invalid_input``, ``missing_selection``, ``missing_material``,
    ``missing_geometry``, ``unsupported_combination``, ``capabilities_unavailable``,
    ``project_root_unavailable``, ``path_outside_project``.
    """

    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.field = field

    def payload(self) -> dict:
        out = {"ok": False, "code": self.code, "error": str(self)}
        if self.field:
            out["field"] = self.field
        return out


_MISSING = object()


def _invalid(where: str, message: str) -> TopologyInputError:
    return TopologyInputError("invalid_input", f"{where}: {message}", where)


def _norm(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "").replace(" ", "")


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(where, f"must be a finite number, got {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise _invalid(where, f"must be a finite number, got {value!r}")
    return out


def _positive(value: Any, where: str) -> float:
    out = _finite(value, where)
    if out <= 0.0:
        raise _invalid(where, f"must be > 0, got {value!r}")
    return out


def _temperature(value: Any, where: str) -> float:
    out = _finite(value, where)
    if out <= _ABS_ZERO_C:
        raise _invalid(where, f"must be above absolute zero ({_ABS_ZERO_C} C), got {value!r}")
    return out


def _vec3(value: Any, where: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _invalid(where, f"must be exactly [x, y, z], got {value!r}")
    return [_finite(v, f"{where}[{i}]") for i, v in enumerate(value)]


def _tolerated(key: Any) -> bool:
    return key in _LABEL_KEYS or str(key).startswith("_")


def _reject_unknown(entry: Mapping, accepted: Iterable[str], where: str) -> None:
    accepted = set(accepted)
    unknown = sorted(str(k) for k in entry if k not in accepted and not _tolerated(k))
    if unknown:
        raise _invalid(where, f"unsupported fields {unknown}; refusing to ignore them. "
                              f"Accepted: {sorted(accepted)} plus label/name or _-prefixed "
                              "UI keys.")


def _take(entry: dict, names: tuple, where: str) -> Any:
    """Pop every spelling of one field (case/underscore-insensitive); conflicting values refuse."""
    wanted = {_norm(n) for n in names}
    found = [(k, entry[k]) for k in list(entry) if _norm(k) in wanted]
    if not found:
        return _MISSING
    first = found[0][1]
    if any(v != first for _, v in found[1:]):
        raise _invalid(where, f"conflicting values for {[k for k, _ in found]}")
    for key, _ in found:
        del entry[key]
    return first


def _label_of(entry: Mapping) -> Optional[str]:
    for key in ("label", "name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@lru_cache(maxsize=1)
def print_processes() -> list[dict]:
    """Shared GUI/agent print-process presets."""
    path = Path(__file__).with_name("topology_print_processes.json")
    return json.loads(path.read_text(encoding="utf-8"))["processes"]


def print_process(process_id: Any) -> dict:
    for process in print_processes():
        if process["id"] == process_id:
            return process
    valid = [p["id"] for p in print_processes()]
    raise TopologyInputError(
        "unknown_print_process",
        f"Unknown print process {process_id!r}; valid ids: {valid}.", "study.print_process")


# ---------------------------------------------------------------- selectors

_FACE_KEYS = ("faces", "face")


def _is_selector(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())) or (
        isinstance(value, dict) and bool(value))


def _has_any(entry: Mapping, names: Iterable[str]) -> bool:
    wanted = {_norm(n) for n in names}
    return any(_norm(k) in wanted for k in entry)


def _faces(entry: dict, where: str) -> list:
    value = _take(entry, _FACE_KEYS, where)
    if value is _MISSING:
        raise TopologyInputError(
            "missing_selection",
            f"{where} names no face. Pass faces:[<selector>] — a face query such as "
            '"top" or {"of":"face","side":"bottom"}, a face sid from describe_part, or a '
            "GUI face selector.", where)
    selectors = value if isinstance(value, list) else [value]
    if not selectors:
        raise TopologyInputError("missing_selection", f"{where}.faces is empty", where)
    for i, selector in enumerate(selectors):
        if not _is_selector(selector):
            raise _invalid(f"{where}.faces[{i}]",
                           f"must be a face query string or selector object, got {selector!r}")
    return list(selectors)


def selector_text(selector: Any) -> str:
    """A short quotable label for one selector, used to name refusals and facesets."""
    if isinstance(selector, str):
        return selector.strip()[:120]
    if isinstance(selector, dict):
        if "selector" in selector and isinstance(selector["selector"], str):
            return selector["selector"][:120]
        visible = {k: v for k, v in selector.items() if not str(k).startswith("_")
                   and k not in ("points", "tol_mm", "face_normal", "perp_tol_mm", "surface")}
        return json.dumps(visible, sort_keys=True, default=str)[:120]
    return repr(selector)[:120]


def merge_facesets(facesets: list, label: str) -> dict:
    """Merge resolved doc_face_points sets into ONE faceset for a single boundary entry.

    Points concatenate and ``tol_mm`` takes the maximum. ``face_normal``,
    ``perp_tol_mm`` and the analytic ``surface`` survive only when every merged face
    shares one identical normal (all absent counts as identical): one face's normal would
    otherwise make the distance-to-plane test reject every node of the other faces. The
    surface descriptor additionally needs to be identical on every face. This is the rule
    the GUI applies to its own run envelope, so the native adapter sees one dialect.
    """
    merged: dict = {"points": []}
    for faceset in facesets:
        merged["points"].extend(faceset.get("points") or [])
    if facesets:
        tols = [f["tol_mm"] for f in facesets if "tol_mm" in f]
        if tols:
            merged["tol_mm"] = max(tols)
        normals = [f.get("face_normal") for f in facesets]
        if all(n == normals[0] for n in normals):
            if normals[0] is not None:
                merged["face_normal"] = normals[0]
            perps = [f["perp_tol_mm"] for f in facesets if "perp_tol_mm" in f]
            if perps:
                merged["perp_tol_mm"] = max(perps)
            surfaces = [f.get("surface") for f in facesets]
            if surfaces[0] is not None and all(s == surfaces[0] for s in surfaces):
                merged["surface"] = surfaces[0]
    merged["selector"] = label
    return merged


# ---------------------------------------------------------------- persisted study

def _norm_dofs(value: Any, where: str) -> list:
    axes = {"x": 0, "y": 1, "z": 2}
    items = value if isinstance(value, list) else [value]
    out: list = []
    for item in items:
        if isinstance(item, str) and item.strip().lower() in axes:
            dof = axes[item.strip().lower()]
        elif isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 2:
            dof = item
        else:
            raise _invalid(where, f"dofs entries must be 0|1|2 (or x|y|z), got {item!r}")
        if dof in out:
            raise _invalid(where, f"dofs repeats {item!r}")
        out.append(dof)
    if not out:
        raise _invalid(where, "dofs is empty; a fixture must fix at least one DOF "
                              "(omit dofs to fix all three)")
    return sorted(out)


def _bare(value: Any, where: str) -> dict:
    if not _is_selector(value):
        raise _invalid(where, f"must be a face selector or an object with faces, got {value!r}")
    return {"faces": [value]}


def _norm_fixture(raw: Any, where: str) -> dict:
    """Structural fixture. Per-axis ``dofs`` are kept exactly; omitted means all three."""
    wrapper_keys = _FACE_KEYS + ("dofs", "dof", "fixed_dofs", "axes", "type")
    if not isinstance(raw, dict) or not _has_any(raw, wrapper_keys):
        return _bare(raw, where)
    e = dict(raw)
    out: dict = {"faces": _faces(e, where)}
    dofs = _take(e, ("dofs", "dof", "fixed_dofs", "axes"), where)
    if dofs is not _MISSING:
        out["dofs"] = _norm_dofs(dofs, f"{where}.dofs")
    kind = _take(e, ("type",), where)
    if kind is not _MISSING and str(kind).strip().lower() not in (
            "fixed", "clamped", "encastre", "fixture"):
        raise _invalid(f"{where}.type", f"{kind!r} is not a topology fixture type. Fixtures "
                       "fix DOFs: use dofs:[0,1,2] (x,y,z), e.g. dofs:[2] for a roller "
                       "normal to Z.")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_load(raw: Any, where: str, notes: list) -> dict:
    """Mechanical load: force vector (N, world XYZ) or inward pressure magnitude (MPa)."""
    if not isinstance(raw, dict):
        raise _invalid(where, 'each load must be {faces, type:"force", vec:[fx,fy,fz]} (N) '
                              'or {faces, type:"pressure", magnitude:<MPa>}')
    e = dict(raw)
    faces = _faces(e, where)
    kind = _take(e, ("type", "kind"), where)
    vec = _take(e, ("vec", "vector", "force", "force_n", "vector_n"), where)
    magnitude = _take(e, ("magnitude", "pressure", "pressure_mpa", "magnitude_mpa",
                          "magnitude_n"), where)
    direction = _take(e, ("direction", "dir"), where)
    if kind is _MISSING:
        kind = "force" if vec is not _MISSING or direction is not _MISSING else (
            "pressure" if magnitude is not _MISSING else None)
    kind = str(kind).strip().lower() if kind is not None else None
    out: dict = {"faces": faces}
    if kind == "force":
        if vec is not _MISSING and direction is not _MISSING:
            raise _invalid(where, "give either vec:[fx,fy,fz] or magnitude+direction, not both")
        if vec is not _MISSING:
            if magnitude is not _MISSING:
                raise _invalid(where, "a force vec already carries its magnitude; remove "
                                      "magnitude")
            out["type"] = "force"
            out["vec"] = _vec3(vec, f"{where}.vec")
        elif direction is not _MISSING and magnitude is not _MISSING:
            d = _vec3(direction, f"{where}.direction")
            length = math.sqrt(sum(c * c for c in d))
            if length <= 0.0:
                raise _invalid(f"{where}.direction", "must be non-zero")
            m = _finite(magnitude, f"{where}.magnitude")
            out["type"] = "force"
            out["vec"] = [m * c / length for c in d]
            notes.append(f"{where}: polar force (magnitude {m:g} N along {d}) stored as vec "
                         f"{out['vec']}")
        else:
            raise _invalid(where, "force load needs vec:[fx,fy,fz] in newtons "
                                  "(or magnitude + direction)")
    elif kind == "pressure":
        if vec is not _MISSING or direction is not _MISSING:
            raise _invalid(where, "a pressure load takes magnitude (MPa, inward) only")
        if magnitude is _MISSING:
            raise _invalid(where, "pressure load needs magnitude:<MPa>")
        out["type"] = "pressure"
        out["magnitude"] = _finite(magnitude, f"{where}.magnitude")
    else:
        raise _invalid(where, f"load type must be force or pressure, got {kind!r}")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_case(raw: Any, where: str, notes: list) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "each load case must be {name, weight, fixtures?, loads}")
    e = dict(raw)
    out: dict = {}
    name = _take(e, ("name",), where)
    if name is not _MISSING:
        if not isinstance(name, str) or not name.strip():
            raise _invalid(f"{where}.name", "must be a non-empty string")
        out["name"] = name
    weight = _take(e, ("weight",), where)
    if weight is not _MISSING:
        out["weight"] = _positive(weight, f"{where}.weight")
    fixtures = _take(e, ("fixtures",), where)
    if fixtures is not _MISSING:
        if not isinstance(fixtures, list):
            raise _invalid(f"{where}.fixtures", "must be a list")
        out["fixtures"] = [_norm_fixture(f, f"{where}.fixtures[{i}]")
                           for i, f in enumerate(fixtures)]
    loads = _take(e, ("loads",), where)
    if loads is _MISSING or not isinstance(loads, list) or not loads:
        raise TopologyInputError("missing_selection",
                                 f"{where} needs a non-empty loads list", f"{where}.loads")
    out["loads"] = [_norm_load(ld, f"{where}.loads[{i}]", notes) for i, ld in enumerate(loads)]
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_region(raw: Any, where: str) -> dict:
    """Preserve/exclude: faceset band (+ thickness_mm) or an explicit min/max box (mm)."""
    box_keys = ("box_min_mm", "box_max_mm", "min_mm", "max_mm")
    if not isinstance(raw, dict) or not _has_any(raw, _FACE_KEYS + box_keys
                                                 + ("thickness_mm", "thickness")):
        return _bare(raw, where)
    e = dict(raw)
    lo = _take(e, ("box_min_mm", "min_mm"), where)
    hi = _take(e, ("box_max_mm", "max_mm"), where)
    out: dict = {}
    if lo is not _MISSING or hi is not _MISSING:
        if _has_any(e, _FACE_KEYS):
            raise _invalid(where, "a region is EITHER faces (+thickness_mm) OR a box, not both")
        if lo is _MISSING or hi is _MISSING:
            raise _invalid(where, "a box region needs both box_min_mm and box_max_mm")
        lo3, hi3 = _vec3(lo, f"{where}.box_min_mm"), _vec3(hi, f"{where}.box_max_mm")
        if any(a >= b for a, b in zip(lo3, hi3)):
            raise _invalid(where, f"box_min_mm {lo3} must be below box_max_mm {hi3} on "
                                  "every axis")
        out["box_min_mm"], out["box_max_mm"] = lo3, hi3
    else:
        out["faces"] = _faces(e, where)
        thickness = _take(e, ("thickness_mm", "thickness"), where)
        if thickness is not _MISSING:
            t = _finite(thickness, f"{where}.thickness_mm")
            if t < 0.0:
                raise _invalid(f"{where}.thickness_mm", "must be >= 0")
            out["thickness_mm"] = t
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_thermal_fixture(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "each thermal fixture must be {faces, temperature_c}")
    e = dict(raw)
    out: dict = {"faces": _faces(e, where)}
    temp = _take(e, ("temperature_c", "temperature", "temp_c"), where)
    if temp is _MISSING:
        raise _invalid(where, "thermal fixture needs temperature_c")
    out["temperature_c"] = _temperature(temp, f"{where}.temperature_c")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_thermal_load(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, 'each thermal load must be {faces, type:"flux", magnitude_w_m2}'
                              ' | {faces, type:"power", power_w} | {faces, type:"convection",'
                              " h_w_m2k, ambient_c}")
    e = dict(raw)
    faces = _faces(e, where)
    kind = _take(e, ("type", "kind"), where)
    flux = _take(e, ("magnitude_w_m2", "flux_w_m2", "flux"), where)
    power = _take(e, ("power_w", "power"), where)
    h = _take(e, ("h_w_m2k", "h"), where)
    ambient = _take(e, ("ambient_c", "t_amb_c", "ambient_temperature_c"), where)
    if kind is _MISSING:
        present = [k for k, v in (("flux", flux), ("power", power), ("convection", h))
                   if v is not _MISSING]
        kind = present[0] if len(present) == 1 else None
    kind = str(kind).strip().lower() if kind is not None else None
    given = {"flux": flux, "power": power, "h": h, "ambient": ambient}
    out: dict = {"faces": faces}
    if kind == "flux":
        stray = [k for k in ("power", "h", "ambient") if given[k] is not _MISSING]
        if flux is _MISSING or stray:
            raise _invalid(where, "flux load takes magnitude_w_m2 only")
        out.update(type="flux", magnitude_w_m2=_finite(flux, f"{where}.magnitude_w_m2"))
    elif kind == "power":
        stray = [k for k in ("flux", "h", "ambient") if given[k] is not _MISSING]
        if power is _MISSING or stray:
            raise _invalid(where, "power load takes power_w only")
        out.update(type="power", power_w=_finite(power, f"{where}.power_w"))
    elif kind == "convection":
        if h is _MISSING or ambient is _MISSING or flux is not _MISSING or power is not _MISSING:
            raise _invalid(where, "convection load takes h_w_m2k and ambient_c")
        out.update(type="convection", h_w_m2k=_positive(h, f"{where}.h_w_m2k"),
                   ambient_c=_temperature(ambient, f"{where}.ambient_c"))
    else:
        raise _invalid(where, f"thermal load type must be flux, power or convection, got "
                              f"{kind!r}")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_thermal(raw: Any, where: str = "thermal") -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "must be an object {reference_temperature_c?, fixtures, loads}")
    e = dict(raw)
    out: dict = {}
    ref = _take(e, ("reference_temperature_c", "reference_temp_c"), where)
    if ref is not _MISSING:
        out["reference_temperature_c"] = _temperature(ref, f"{where}.reference_temperature_c")
    fixtures = _take(e, ("fixtures",), where)
    if fixtures is not _MISSING:
        if not isinstance(fixtures, list):
            raise _invalid(f"{where}.fixtures", "must be a list")
        out["fixtures"] = [_norm_thermal_fixture(f, f"{where}.fixtures[{i}]")
                           for i, f in enumerate(fixtures)]
    loads = _take(e, ("loads",), where)
    if loads is not _MISSING:
        if not isinstance(loads, list):
            raise _invalid(f"{where}.loads", "must be a list")
        out["loads"] = [_norm_thermal_load(ld, f"{where}.loads[{i}]")
                        for i, ld in enumerate(loads)]
    _reject_unknown(e, (), where)
    out.update(e)
    return out


_FLOW_SCALARS = {
    "viscosity_pa_s": ("viscosity_pa_s", "viscosity"),
    "density_kg_m3": ("density_kg_m3",),
    "fluid_k_w_mk": ("fluid_k_w_mk",),
    "fluid_cp_j_kgk": ("fluid_cp_j_kgk",),
    "brinkman_max": ("brinkman_max",),
    "reference_length_mm": ("reference_length_mm",),
}


def _norm_inlet(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "each inlet must be {faces, velocity_m_s:[vx,vy,vz], "
                              "temperature_c?}")
    e = dict(raw)
    out: dict = {"faces": _faces(e, where)}
    velocity = _take(e, ("velocity_m_s", "velocity"), where)
    if velocity is _MISSING:
        raise _invalid(where, "inlet needs velocity_m_s:[vx,vy,vz] (m/s)")
    out["velocity_m_s"] = _vec3(velocity, f"{where}.velocity_m_s")
    if not any(out["velocity_m_s"]):
        raise _invalid(f"{where}.velocity_m_s", "must be non-zero")
    temp = _take(e, ("temperature_c", "temperature"), where)
    if temp is not _MISSING:
        out["temperature_c"] = _temperature(temp, f"{where}.temperature_c")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_outlet(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "each outlet must be {faces, pressure_pa}")
    e = dict(raw)
    out: dict = {"faces": _faces(e, where)}
    pressure = _take(e, ("pressure_pa", "pressure"), where)
    if pressure is _MISSING:
        raise _invalid(where, "outlet needs pressure_pa (Pa, gauge)")
    out["pressure_pa"] = _finite(pressure, f"{where}.pressure_pa")
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_wall(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict) or not _has_any(raw, _FACE_KEYS):
        return _bare(raw, where)
    e = dict(raw)
    out: dict = {"faces": _faces(e, where)}
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_flow(raw: Any, where: str = "flow") -> dict:
    if not isinstance(raw, dict):
        raise _invalid(where, "must be an object {viscosity_pa_s, density_kg_m3, inlets, "
                              "outlets, walls?}")
    e = dict(raw)
    out: dict = {}
    for canon, names in _FLOW_SCALARS.items():
        value = _take(e, names, where)
        if value is not _MISSING:
            out[canon] = _positive(value, f"{where}.{canon}")
    for key, fn in (("inlets", _norm_inlet), ("outlets", _norm_outlet), ("walls", _norm_wall)):
        items = _take(e, (key,), where)
        if items is _MISSING:
            continue
        if not isinstance(items, list):
            raise _invalid(f"{where}.{key}", "must be a list")
        out[key] = [fn(item, f"{where}.{key}[{i}]") for i, item in enumerate(items)]
    _reject_unknown(e, (), where)
    out.update(e)
    return out


def _norm_list(study: dict, key: str, fn: Callable) -> None:
    if key not in study:
        return
    items = study[key]
    if items is None:
        del study[key]
        return
    if not isinstance(items, list):
        raise _invalid(key, f"must be a list, got {type(items).__name__}")
    study[key] = [fn(item, f"{key}[{i}]") for i, item in enumerate(items)]


def normalize_study(raw: Any, notes: list) -> dict:
    """The canonical persisted topology study (selectors, GUI-readable).

    Validates every boundary entry's shape and units, folds documented spellings onto the
    canonical keys, wraps bare selectors as ``{faces:[selector]}``, and keeps every other
    key verbatim — unknown top-level keys, ``runs`` and ``last_job`` included. Nothing is
    resolved against the document here.
    """
    if not isinstance(raw, dict):
        raise _invalid("study", f"must be an object, got {type(raw).__name__}")
    study = dict(raw)
    kind = study.get("type")
    if kind not in (None, _STUDY_TYPE):
        raise _invalid("study.type", f"must be {_STUDY_TYPE!r}, got {kind!r}. A topology "
                                     "study is its own type; it never converts another study.")
    study["type"] = _STUDY_TYPE
    name = study.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _invalid("study.name", "a topology study needs a non-empty name")
    order = study.get("mesh_order", 1)
    if isinstance(order, bool) or order != 1:
        raise _invalid("study.mesh_order", f"the topology kernel meshes linear tet4 only; got "
                                           f"{order!r}. Refusing to change it silently: set 1.")
    study["mesh_order"] = 1
    for key in ("mesh_target_mm", "mesh_min_mm"):
        if study.get(key) is not None:
            study[key] = _positive(study[key], f"study.{key}")
    if study.get("mesh_fineness") is not None:
        fineness = _finite(study["mesh_fineness"], "study.mesh_fineness")
        if not 0.0 <= fineness <= 1.0:
            raise _invalid("study.mesh_fineness", "must be within [0, 1]")
        study["mesh_fineness"] = fineness
    topology = study.get("topology", {})
    if not isinstance(topology, dict):
        raise _invalid("study.topology", "must be an object")
    for key, vocab in (("physics", PHYSICS), ("method", METHODS)):
        if key in topology and topology[key] not in vocab:
            raise _invalid(f"topology.{key}", f"must be one of {list(vocab)}, got "
                                              f"{topology[key]!r}")
    study["topology"] = dict(topology)
    if "print_process" in study:
        print_process(study["print_process"])
    if study.get("build_plate") is not None:
        plate = study["build_plate"]
        if not isinstance(plate, dict):
            raise _invalid("study.build_plate", "must be an object with faces:[selector]")
        plate = dict(plate)
        faces = _faces(plate, "study.build_plate")
        _reject_unknown(plate, (), "study.build_plate")
        study["build_plate"] = {"faces": faces, **plate}
    material = study.get("material")
    if material is not None and not (
            (isinstance(material, str) and material.strip())
            or (isinstance(material, dict) and material)):
        raise _invalid("study.material", "must be a material name or a property object")
    _norm_list(study, "fixtures", _norm_fixture)
    _norm_list(study, "loads", lambda r, w: _norm_load(r, w, notes))
    _norm_list(study, "load_cases", lambda r, w: _norm_case(r, w, notes))
    _norm_list(study, "preserve", _norm_region)
    _norm_list(study, "exclude", _norm_region)
    if study.get("thermal") is not None:
        study["thermal"] = _norm_thermal(study["thermal"])
    if study.get("flow") is not None:
        study["flow"] = _norm_flow(study["flow"])
    runs = study.get("runs")
    if runs is not None and not isinstance(runs, list):
        raise _invalid("study.runs", "retained runs must be a list of run records")
    return study


def merge_study(existing: dict, update: dict) -> dict:
    """Overlay a caller's study onto the persisted one without dropping anything it omits.

    Top-level keys from ``update`` replace the stored ones, except ``topology`` (merged
    key by key, so a model changing ``volume_fraction`` keeps the GUI's other settings)
    and ``runs``/``id``/``type`` which only the service writes.
    """
    out = dict(existing)
    for key, value in update.items():
        if key in ("runs", "id", "type", "last_job"):
            continue
        if key == "topology" and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------- material

_MATERIAL_ALIASES = {
    "e": "E_mpa", "empa": "E_mpa", "youngsmodulusmpa": "E_mpa", "youngmodulusmpa": "E_mpa",
    "elasticmodulusmpa": "E_mpa",
    "nu": "nu", "poisson": "nu", "poissonsratio": "nu", "poissonratio": "nu",
    "yieldmpa": "yield_mpa", "yieldstrengthmpa": "yield_mpa", "sigmaympa": "yield_mpa",
    "densitygcm3": "density_g_cm3", "density": "density_g_cm3", "rhogcm3": "density_g_cm3",
    "kwmk": "k_w_mk", "conductivitywmk": "k_w_mk", "thermalconductivitywmk": "k_w_mk",
    "alphaperk": "alpha_per_k", "alpha": "alpha_per_k", "cteperk": "alpha_per_k",
    "expansionperk": "alpha_per_k", "thermalexpansionperk": "alpha_per_k",
    "cpjkgk": "cp_j_kgk", "cp": "cp_j_kgk", "specificheatjkgk": "cp_j_kgk",
}
_MATERIAL_META = frozenset({"key", "name", "label", "source", "notes", "note", "description",
                            "constitutive_model"})


def _canonical_material(props: Mapping, where: str, notes: Optional[list]) -> dict:
    out: dict = {}
    for key, value in props.items():
        canon = _MATERIAL_ALIASES.get(_norm(key))
        if canon is None:
            if key not in _MATERIAL_META and notes is not None and not str(key).startswith("_"):
                notes.append(f"{where}.{key} is not a topology solver property; kept in the "
                             "study, not sent to the solver")
            continue
        if canon in out and out[canon] != value:
            raise _invalid(where, f"conflicting values for {canon}")
        out[canon] = value
    return out


def run_material(material: Any, physics: str,
                 resolve_named: Callable[[str], dict], notes: list) -> dict:
    """The strict physical material dict the native run consumes.

    ``material`` is a library key (str), or an object whose optional ``key`` names a
    library row that its explicit properties override. Only physical keys
    (:data:`MATERIAL_KEYS`) plus ``name`` are returned; every key the physics needs must be
    present — nothing is filled from family nominals.
    """
    if material is None or material == "" or material == {}:
        if not REQUIRED_MATERIAL[physics]:
            return {}  # flow optimizes the fluid domain; its properties live in flow{}
        raise TopologyInputError(
            "missing_material",
            f"physics {physics} needs a material: a library key such as \"6061-T6\" or an "
            f"object with {list(REQUIRED_MATERIAL[physics]) or ['name']}.", "study.material")
    base: dict = {}
    overlay: Mapping = {}
    name: Optional[str] = None
    if isinstance(material, str):
        base = dict(resolve_named(material.strip()))
        name = material.strip()
    elif isinstance(material, dict):
        overlay = material
        key = material.get("key")
        if isinstance(key, str) and key.strip():
            base = dict(resolve_named(key.strip()))
            name = key.strip()
        if isinstance(material.get("name"), str) and material["name"].strip():
            name = material["name"].strip()
    else:
        raise _invalid("study.material", "must be a material name or a property object")
    model = overlay.get("constitutive_model", base.get("constitutive_model", "isotropic"))
    if model != "isotropic" and physics in _ELASTIC_PHYSICS:
        raise TopologyInputError(
            "missing_material",
            f"material {name or material!r} has constitutive_model={model!r}; {physics} needs "
            "an isotropic E_mpa/nu, which must not be substituted. Pass explicit isotropic "
            "properties.", "study.material")
    props = _canonical_material(base, "material(library)", None)
    props.update(_canonical_material(overlay, "study.material", notes))
    out: dict = {}
    for key in MATERIAL_KEYS:
        if key not in props or props[key] is None:
            continue
        value = _finite(props[key], f"material.{key}")
        if key == "nu":
            if not -1.0 < value < 0.5:
                raise _invalid("material.nu", "must be in (-1, 0.5)")
        elif value <= 0.0:
            raise _invalid(f"material.{key}", "must be > 0")
        out[key] = value
    missing = [k for k in REQUIRED_MATERIAL[physics] if k not in out]
    if missing:
        raise TopologyInputError(
            "missing_material",
            f"physics {physics} needs material {missing} (material {name or 'object'} lacks "
            "them). Library rows carry mechanical properties only; give thermal properties "
            "explicitly, e.g. {\"key\":\"6061-T6\",\"k_w_mk\":167,\"alpha_per_k\":2.36e-5}.",
            "study.material")
    if name:
        out["name"] = name
    return out


# ---------------------------------------------------------------- capability gate

def capability_matrix(reply: Any) -> dict:
    """The capability object from a native topology_capabilities reply."""
    caps = reply
    if isinstance(reply, dict) and "combinations" not in reply and isinstance(
            reply.get("capabilities"), dict):
        caps = reply["capabilities"]
    if not isinstance(caps, dict) or not isinstance(caps.get("combinations"), list):
        raise TopologyInputError("capabilities_unavailable",
                                 "the native topology_capabilities reply carries no "
                                 "combinations matrix; nothing can be offered or run.")
    return caps


def select_combination(caps: dict, topology: dict) -> dict:
    """The native matrix row for the study's method x physics, refusing an unsupported pair."""
    physics, method = topology.get("physics"), topology.get("method")
    if physics not in PHYSICS:
        raise TopologyInputError("invalid_input", f"topology.physics must be one of "
                                 f"{list(PHYSICS)}, got {physics!r}", "topology.physics")
    if method not in METHODS:
        raise TopologyInputError("invalid_input", f"topology.method must be one of "
                                 f"{list(METHODS)}, got {method!r}", "topology.method")
    row = next((c for c in caps["combinations"] if isinstance(c, dict)
                and c.get("method") == method and c.get("physics") == physics), None)
    supported_pairs = sorted(f"{c.get('method')}x{c.get('physics')}"
                             for c in caps["combinations"]
                             if isinstance(c, dict) and c.get("supported") is True
                             and c.get("objectives"))
    if row is None or row.get("supported") is not True or not row.get("objectives"):
        reason = (row or {}).get("reason") or "not listed by the native capability matrix"
        raise TopologyInputError(
            "unsupported_combination",
            f"method {method} does not support physics {physics}: {reason}. Supported pairs: "
            f"{supported_pairs}.", "topology.method")
    objective = topology.get("objective")
    if objective is not None and objective not in row["objectives"]:
        raise TopologyInputError(
            "unsupported_combination",
            f"objective {objective!r} is not offered for {method}x{physics}; offered: "
            f"{row['objectives']}", "topology.objective")
    return row


def apply_print_process(study: dict, combination: dict, notes: list) -> None:
    """Fill only absent, offered parameters; explicit study values always win."""
    if "print_process" not in study:
        return
    process = print_process(study["print_process"])
    topology = study["topology"]
    offered = set(combination.get("parameters") or [])
    filled = {}
    for key, value in process["topology"].items():
        if key in offered and key not in topology and value is not None:
            topology[key] = value
            filled[key] = value
    if filled:
        notes.append(f"print_process {process['id']} filled topology keys: "
                     + ", ".join(f"{k}={json.dumps(v)}" for k, v in filled.items()))


def run_settings(topology: dict, combination: dict) -> dict:
    """The ``topology`` object sent to the solver: exactly the keys this pair offers.

    A key the pair does not offer is refused, never dropped: the native settings parser
    rejects unknown keys and a silently unsent limit would be an unconstrained result
    presented as a constrained one. ``result_dir`` is server-owned (see allocate_run_dir).
    """
    offered = {"physics", "method", "objective", *combination.get("parameters", ()),
               *combination.get("limits", ()), *(combination.get("defaults") or {}).keys()}
    unknown = sorted(k for k in topology if k not in offered and k != "result_dir")
    if unknown:
        raise TopologyInputError(
            "unsupported_combination",
            f"topology keys {unknown} are not offered for {topology.get('method')}x"
            f"{topology.get('physics')}. Offered parameters: "
            f"{sorted(combination.get('parameters', ()))}; limits: "
            f"{sorted(combination.get('limits', ()))}. Remove them (study-level notes belong "
            "outside topology{}).", "topology")
    return {k: v for k, v in topology.items() if k != "result_dir"}


# ---------------------------------------------------------------- run requirements

def require_topology_settings(study: Mapping, combination: Optional[Mapping] = None) -> None:
    """Refuse incomplete optimization intent before saving or exporting the study."""
    topology = study["topology"]
    if "volume_fraction" not in topology:
        raise TopologyInputError(
            "missing_volume_fraction", "A volume fraction in (0, 1] is required.",
            "study.topology.volume_fraction")
    vf = _finite(topology["volume_fraction"], "study.topology.volume_fraction")
    if not 0.0 < vf <= 1.0:
        raise TopologyInputError(
            "out_of_range", "Volume fraction must be in (0, 1].",
            "study.topology.volume_fraction")
    penalty = topology.get("overhang_penalty", 0.0)
    penalty = _finite(penalty, "study.topology.overhang_penalty")
    has_direction = topology.get("build_direction") is not None
    plate = study.get("build_plate")
    has_plate = isinstance(plate, Mapping) and bool(plate.get("faces"))
    if ("overhang_angle_deg" in topology or penalty > 0.0) and not (
            has_direction or has_plate):
        raise TopologyInputError(
            "build_direction_required",
            "Overhang control needs a build direction or a bed face (build_plate).",
            "study.topology.build_direction")
    # Native Settings defaults the angle to 45 degrees, including penalty-only studies.
    limits = (combination or {}).get("limits")
    has_limit = any(key in topology and topology[key] is not None
                    for key in (limits if limits is not None else
                                ("stress_limit_mpa", "max_displacement_mm",
                                 "max_temperature_c", "min_frequency_hz",
                                 "min_buckling_factor", "max_pressure_drop_pa")))
    if topology.get("objective") == "mass" and not has_limit:
        raise TopologyInputError(
            "mass_needs_limit", "Mass minimization needs at least one response limit.",
            "study.topology.objective")


def _any_driving(loads: Iterable[dict]) -> bool:
    for load in loads:
        if load.get("type") == "pressure" and load.get("magnitude"):
            return True
        if load.get("type") == "force" and any(load.get("vec") or load.get("vector") or ()):
            return True
    return False


def require_run_inputs(study: dict, physics: str) -> None:
    """Refuse, naming the field, a setup the physics cannot solve meaningfully."""
    cases = study.get("load_cases") or []
    if physics in STRUCTURAL_PHYSICS:
        fixtures = list(study.get("fixtures") or [])
        for case in cases:
            fixtures.extend(case.get("fixtures") or [])
        if not fixtures:
            raise TopologyInputError(
                "missing_selection",
                f"physics {physics} needs at least one structural fixture "
                "(fixtures:[{faces:[<support face>], dofs?:[0,1,2]}]); an unsupported domain "
                "has rigid-body motion and no static/eigen solution.", "fixtures")
        if physics in LOAD_DRIVEN_PHYSICS:
            loads = list(study.get("loads") or [])
            for case in cases:
                loads.extend(case.get("loads") or [])
            if not _any_driving(loads):
                raise TopologyInputError(
                    "missing_selection",
                    f"physics {physics} needs at least one non-zero load "
                    '(loads:[{faces:["top"], type:"force", vec:[0,0,-500]}]); an unloaded '
                    "domain has an identically zero response.", "loads")
    if physics in THERMAL_PHYSICS:
        thermal = study.get("thermal") or {}
        if not thermal.get("fixtures") and not thermal.get("loads"):
            raise TopologyInputError(
                "missing_selection",
                f"physics {physics} needs a thermal section with fixtures (temperature_c) "
                "and/or loads (flux/power/convection).", "thermal")
    if physics in FLOW_PHYSICS:
        flow = study.get("flow") or {}
        need = [k for k in ("viscosity_pa_s", "density_kg_m3") if k not in flow]
        if physics == "cht":
            need += [k for k in _CHT_FLUID_KEYS if k not in flow]
        if need:
            raise TopologyInputError("missing_material",
                                     f"physics {physics} needs flow {need}", "flow")
        for key in ("inlets", "outlets"):
            if not flow.get(key):
                raise TopologyInputError(
                    "missing_selection",
                    f"physics {physics} needs at least one flow {key[:-1]} "
                    f"(flow.{key}:[{{faces:[...], ...}}])", f"flow.{key}")


# ---------------------------------------------------------------- run envelope

def _mesh_options(study: dict) -> dict:
    out: dict = {}
    for src, dst in (("mesh_fineness", "fineness"), ("mesh_target_mm", "target_size_mm"),
                     ("mesh_min_mm", "min_size_mm")):
        if study.get(src) is not None:
            out[dst] = study[src]
    return out


def build_envelope(study: dict, *, settings: dict, material: dict,
                   resolve: Callable[[Any], list], step_path: str, result_dir: str,
                   notes: list) -> dict:
    """The resolved native run envelope for one canonical study.

    ``resolve(selector)`` returns the doc_face_points facesets for one selector; it is
    called once per distinct selector. Sections the physics does not use are kept in the
    persisted study but not sent (the native preparation refuses them); each omission is
    reported in ``notes``.
    """
    physics = settings["physics"]
    cache: dict = {}

    def faceset(entry: dict, where: str) -> dict:
        sets: list = []
        for selector in entry["faces"]:
            key = json.dumps(selector, sort_keys=True, default=str)
            if key not in cache:
                cache[key] = list(resolve(selector) or [])
            sets.extend(cache[key])
        if not sets or not any(s.get("points") for s in sets):
            raise TopologyInputError("missing_selection",
                                     f"{where} resolved to no surface points", where)
        label = _label_of(entry) or " + ".join(selector_text(s) for s in entry["faces"])
        return merge_facesets(sets, label)

    def labelled(entry: dict, body: dict) -> dict:
        label = _label_of(entry)
        if label:
            body["label"] = label
        return body

    def fixture(entry: dict, where: str) -> dict:
        body = {"faces": faceset(entry, where)}
        if "dofs" in entry:
            body["dofs"] = list(entry["dofs"])
        return labelled(entry, body)

    def load(entry: dict, where: str) -> dict:
        body: dict = {"faces": faceset(entry, where), "type": entry["type"]}
        if entry["type"] == "force":
            body["vector"] = list(entry["vec"])
        else:
            body["magnitude"] = entry["magnitude"]
        return labelled(entry, body)

    def region(entry: dict, where: str) -> dict:
        if "box_min_mm" in entry:
            return labelled(entry, {"box_min_mm": entry["box_min_mm"],
                                    "box_max_mm": entry["box_max_mm"]})
        body: dict = {"faces": faceset(entry, where)}
        if "thickness_mm" in entry:
            body["thickness_mm"] = entry["thickness_mm"]
        return labelled(entry, body)

    def omitted(key: str, family: str) -> None:
        if study.get(key):
            notes.append(f"{key} is kept in the study but not sent: physics {physics} has no "
                         f"{family} boundary")

    env: dict = {"name": study["name"], "step_path": step_path, "mesh_order": 1,
                 "material": material, "topology": {**settings, "result_dir": result_dir},
                 **_mesh_options(study)}
    if study.get("build_plate") is not None:
        plate = study["build_plate"]
        env["build_plate"] = labelled(plate, {"faces": faceset(plate, "build_plate")})
    if physics in STRUCTURAL_PHYSICS:
        env["fixtures"] = [fixture(e, f"fixtures[{i}]")
                           for i, e in enumerate(study.get("fixtures") or [])]
        env["loads"] = [load(e, f"loads[{i}]") for i, e in enumerate(study.get("loads") or [])]
        cases = []
        for i, case in enumerate(study.get("load_cases") or []):
            body: dict = {"loads": [load(e, f"load_cases[{i}].loads[{j}]")
                                    for j, e in enumerate(case["loads"])]}
            for key in ("name", "weight"):
                if key in case:
                    body[key] = case[key]
            if case.get("fixtures"):
                body["fixtures"] = [fixture(e, f"load_cases[{i}].fixtures[{j}]")
                                    for j, e in enumerate(case["fixtures"])]
            cases.append(body)
        if cases:
            env["load_cases"] = cases
    else:
        for key in ("fixtures", "loads", "load_cases"):
            omitted(key, "structural")
    if physics in THERMAL_PHYSICS:
        thermal = study.get("thermal") or {}
        tenv: dict = {}
        if "reference_temperature_c" in thermal:
            tenv["reference_temperature_c"] = thermal["reference_temperature_c"]
        tenv["fixtures"] = [labelled(e, {"faces": faceset(e, f"thermal.fixtures[{i}]"),
                                         "temperature_c": e["temperature_c"]})
                            for i, e in enumerate(thermal.get("fixtures") or [])]
        tloads = []
        for i, e in enumerate(thermal.get("loads") or []):
            body = {"faces": faceset(e, f"thermal.loads[{i}]"), "type": e["type"]}
            for key in ("magnitude_w_m2", "power_w", "h_w_m2k", "ambient_c"):
                if key in e:
                    body[key] = e[key]
            tloads.append(labelled(e, body))
        tenv["loads"] = tloads
        env["thermal"] = tenv
    else:
        omitted("thermal", "thermal")
    if physics in FLOW_PHYSICS:
        flow = study.get("flow") or {}
        fenv: dict = {k: flow[k] for k in _FLOW_SCALARS if k in flow}
        fenv["inlets"] = []
        for i, e in enumerate(flow.get("inlets") or []):
            body = {"faces": faceset(e, f"flow.inlets[{i}]"), "velocity_m_s": e["velocity_m_s"]}
            if "temperature_c" in e:
                body["temperature_c"] = e["temperature_c"]
            fenv["inlets"].append(labelled(e, body))
        fenv["outlets"] = [labelled(e, {"faces": faceset(e, f"flow.outlets[{i}]"),
                                        "pressure_pa": e["pressure_pa"]})
                           for i, e in enumerate(flow.get("outlets") or [])]
        if flow.get("walls"):
            fenv["walls"] = [labelled(e, {"faces": faceset(e, f"flow.walls[{i}]")})
                             for i, e in enumerate(flow["walls"])]
        env["flow"] = fenv
    else:
        omitted("flow", "flow")
    for key in ("preserve", "exclude"):
        if study.get(key):
            env[key] = [region(e, f"{key}[{i}]") for i, e in enumerate(study[key])]
    return env


# ---------------------------------------------------------------- job-stage validation

def _env_faceset(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"sim_run_topology {where}.faces must be a resolved faceset object")
    points = value.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError(f"sim_run_topology {where}.faces has no resolved points")
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 3 or not all(
                isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
                for c in point):
            raise ValueError(f"sim_run_topology {where}.faces points must be finite [x,y,z]")


def _env_check(fn: Callable, *args) -> None:
    try:
        fn(*args)
    except TopologyInputError as exc:
        raise ValueError(f"sim_run_topology {exc}") from None


def _env_geometry(study: dict) -> None:
    step = study.get("step_path")
    inline = "points" in study or "tets" in study
    if step is None and not inline:
        raise ValueError("sim_run_topology study needs step_path or inline points/tets")
    if step is not None and inline:
        raise ValueError("sim_run_topology study takes step_path OR inline points/tets, not both")
    if step is not None:
        try:
            ready = isinstance(step, str) and Path(step).is_file() and Path(step).stat().st_size > 0
        except OSError:
            ready = False
        if not ready:
            raise ValueError(f"sim_run_topology STEP geometry is unavailable: {step!r}")
        return
    points, tets = study.get("points"), study.get("tets")
    if not isinstance(points, list) or len(points) < 12 or len(points) % 3:
        raise ValueError("sim_run_topology inline points must be a flat xyz list (>= 4 nodes)")
    if not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
               for c in points):
        raise ValueError("sim_run_topology inline points must be finite")
    if not isinstance(tets, list) or len(tets) < 4 or len(tets) % 4:
        raise ValueError("sim_run_topology inline tets must be a flat tet4 index list")
    n = len(points) // 3
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n for i in tets):
        raise ValueError("sim_run_topology inline tets index outside the point list")


def validate_run_envelope(study: Any) -> None:
    """Topology-specific input check of the ``core_rpc`` stage (raises ValueError).

    Runs in the parent before any job row exists, against the self-contained envelope
    the child will hand to the native solver: geometry, tet4 order, vocabulary, server
    result directory, strict material, section applicability per physics, resolved
    facesets and BC value shapes. Capability support itself was checked against the
    native matrix when the envelope was built; the native solver remains the authority.
    """
    if not isinstance(study, dict):
        raise ValueError("sim_run_topology params need a study object")
    _env_geometry(study)
    if study.get("mesh_order", 1) != 1:
        raise ValueError("sim_run_topology meshes tet4 only (mesh_order 1)")
    topology = study.get("topology")
    if not isinstance(topology, dict):
        raise ValueError("sim_run_topology study needs a topology object")
    physics, method = topology.get("physics"), topology.get("method")
    if physics not in PHYSICS:
        raise ValueError(f"sim_run_topology topology.physics must be one of {list(PHYSICS)}")
    if method not in METHODS:
        raise ValueError(f"sim_run_topology topology.method must be one of {list(METHODS)}")
    _env_check(require_topology_settings, study)
    result_dir = topology.get("result_dir")
    if not isinstance(result_dir, str) or not os.path.isabs(result_dir) or not os.path.isdir(
            result_dir):
        raise ValueError("sim_run_topology topology.result_dir must be the server-allocated "
                         "absolute run directory")
    material = study.get("material")
    if not isinstance(material, dict):
        raise ValueError("sim_run_topology study needs a physical material object")
    stray = sorted(k for k in material if k not in MATERIAL_KEYS and k != "name")
    if stray:
        raise ValueError(f"sim_run_topology material carries non-physical keys {stray}")
    for key in MATERIAL_KEYS:
        if key in material:
            value = material[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(
                    value):
                raise ValueError(f"sim_run_topology material.{key} must be finite")
    missing = [k for k in REQUIRED_MATERIAL[physics] if k not in material]
    if missing:
        raise ValueError(f"sim_run_topology physics {physics} needs material {missing}")

    structural = any(study.get(k) for k in ("fixtures", "load_cases")) or bool(study.get("loads"))
    if structural and physics not in STRUCTURAL_PHYSICS:
        raise ValueError(f"sim_run_topology fixtures/loads/load_cases do not apply to {physics}")
    if ("thermal" in study) != (physics in THERMAL_PHYSICS):
        raise ValueError(f"sim_run_topology thermal section "
                         f"{'missing for' if physics in THERMAL_PHYSICS else 'does not apply to'}"
                         f" {physics}")
    if ("flow" in study) != (physics in FLOW_PHYSICS):
        raise ValueError(f"sim_run_topology flow section "
                         f"{'missing for' if physics in FLOW_PHYSICS else 'does not apply to'}"
                         f" {physics}")

    def bc_list(items: Any, where: str, check: Callable[[dict, str], None]) -> list:
        if items is None:
            return []
        if not isinstance(items, list):
            raise ValueError(f"sim_run_topology {where} must be a list")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"sim_run_topology {where}[{i}] must be an object")
            check(item, f"{where}[{i}]")
        return items

    def fixture(item: dict, where: str) -> None:
        _env_faceset(item.get("faces"), where)
        if "dofs" in item:
            _env_check(_norm_dofs, item["dofs"], f"{where}.dofs")

    def load(item: dict, where: str) -> None:
        _env_faceset(item.get("faces"), where)
        if item.get("type") == "force":
            _env_check(_vec3, item.get("vector"), f"{where}.vector")
        elif item.get("type") == "pressure":
            _env_check(_finite, item.get("magnitude"), f"{where}.magnitude")
        else:
            raise ValueError(f"sim_run_topology {where}.type must be force or pressure")

    loads = list(bc_list(study.get("loads"), "loads", load))
    fixtures = list(bc_list(study.get("fixtures"), "fixtures", fixture))
    for i, case in enumerate(bc_list(study.get("load_cases"), "load_cases",
                                     lambda c, w: None)):
        if "weight" in case:
            _env_check(_positive, case["weight"], f"load_cases[{i}].weight")
        case_loads = bc_list(case.get("loads"), f"load_cases[{i}].loads", load)
        if not case_loads:
            raise ValueError(f"sim_run_topology load_cases[{i}] has no loads")
        loads.extend(case_loads)
        fixtures.extend(bc_list(case.get("fixtures"), f"load_cases[{i}].fixtures", fixture))
    if physics in STRUCTURAL_PHYSICS and not fixtures:
        raise ValueError(f"sim_run_topology physics {physics} needs a structural fixture")
    if physics in LOAD_DRIVEN_PHYSICS and not any(
            (ld.get("type") == "pressure" and ld.get("magnitude"))
            or (ld.get("type") == "force" and any(ld.get("vector") or ()))
            for ld in loads):
        raise ValueError(f"sim_run_topology physics {physics} needs a non-zero load")

    def region(item: dict, where: str) -> None:
        if "box_min_mm" in item or "box_max_mm" in item:
            lo = item.get("box_min_mm")
            hi = item.get("box_max_mm")
            _env_check(_vec3, lo, f"{where}.box_min_mm")
            _env_check(_vec3, hi, f"{where}.box_max_mm")
            if any(a >= b for a, b in zip(lo, hi)):
                raise ValueError(f"sim_run_topology {where} box min must be below max")
        else:
            _env_faceset(item.get("faces"), where)
            if "thickness_mm" in item:
                _env_check(_finite, item["thickness_mm"], f"{where}.thickness_mm")

    bc_list(study.get("preserve"), "preserve", region)
    bc_list(study.get("exclude"), "exclude", region)
    if study.get("build_plate") is not None:
        plate = study["build_plate"]
        if not isinstance(plate, dict):
            raise ValueError("sim_run_topology build_plate must be an object")
        _env_faceset(plate.get("faces"), "build_plate")

    if physics in THERMAL_PHYSICS:
        thermal = study["thermal"]
        if not isinstance(thermal, dict):
            raise ValueError("sim_run_topology thermal must be an object")

        def tfix(item: dict, where: str) -> None:
            _env_faceset(item.get("faces"), where)
            _env_check(_temperature, item.get("temperature_c"), f"{where}.temperature_c")

        def tload(item: dict, where: str) -> None:
            _env_faceset(item.get("faces"), where)
            kind = item.get("type")
            keys = {"flux": ("magnitude_w_m2",), "power": ("power_w",),
                    "convection": ("h_w_m2k", "ambient_c")}.get(kind)
            if keys is None:
                raise ValueError(f"sim_run_topology {where}.type must be flux, power or "
                                 "convection")
            for key in keys:
                _env_check(_finite, item.get(key), f"{where}.{key}")

        tf = bc_list(thermal.get("fixtures"), "thermal.fixtures", tfix)
        tl = bc_list(thermal.get("loads"), "thermal.loads", tload)
        if not tf and not tl:
            raise ValueError(f"sim_run_topology physics {physics} needs thermal fixtures or "
                             "loads")
    if physics in FLOW_PHYSICS:
        flow = study["flow"]
        if not isinstance(flow, dict):
            raise ValueError("sim_run_topology flow must be an object")
        need = ["viscosity_pa_s", "density_kg_m3"] + (
            list(_CHT_FLUID_KEYS) if physics == "cht" else [])
        for key in need:
            _env_check(_positive, flow.get(key), f"flow.{key}")

        def inlet(item: dict, where: str) -> None:
            _env_faceset(item.get("faces"), where)
            _env_check(_vec3, item.get("velocity_m_s"), f"{where}.velocity_m_s")

        def outlet(item: dict, where: str) -> None:
            _env_faceset(item.get("faces"), where)
            _env_check(_finite, item.get("pressure_pa"), f"{where}.pressure_pa")

        if not bc_list(flow.get("inlets"), "flow.inlets", inlet):
            raise ValueError("sim_run_topology flow needs at least one inlet")
        if not bc_list(flow.get("outlets"), "flow.outlets", outlet):
            raise ValueError("sim_run_topology flow needs at least one outlet")
        bc_list(flow.get("walls"), "flow.walls",
                lambda item, where: _env_faceset(item.get("faces"), where))


# ---------------------------------------------------------------- artifact directories

#: Env vars naming the project jail, strictest first: the fs_* jail, the turn's project
#: directory, then the project save directory.
PROJECT_ROOT_VARS = ("CHUDWARE_FS_ROOT", "CHUDWARE_PROJECT_DIR", "CHUDWARE_PROJECT_SAVE_DIR")


def project_jail(env: Optional[Mapping] = None) -> Path:
    """The real project directory every topology artifact must live under."""
    env = os.environ if env is None else env
    for var in PROJECT_ROOT_VARS:
        raw = (env.get(var) or "").strip()
        if raw:
            root = Path(os.path.realpath(os.path.expanduser(raw)))
            if root.is_dir():
                return root
    raise TopologyInputError(
        "project_root_unavailable",
        "topology artifacts are written under the project folder, and none is configured "
        f"({' / '.join(PROJECT_ROOT_VARS)} are unset or not directories). Open or attach a "
        "project first.", "result_dir")


def contained(path: Any, root: Any) -> bool:
    """True when the REAL path lies inside the real root (symlinks resolved)."""
    try:
        real = os.path.realpath(os.fspath(path))
        base = os.path.realpath(os.fspath(root))
        return os.path.commonpath((real, base)) == base
    except (TypeError, ValueError):
        return False


def slug(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip(".-")
    return out[:64] or "study"


def new_run_id() -> str:
    return f"topo-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def allocate_run_dir(study_name: str, run_id: str, *, result_dir: Optional[str] = None,
                     env: Optional[Mapping] = None,
                     spool: Optional[Path] = None) -> Path:
    """Create the run's artifact directory exclusively, inside the project jail.

    Default: ``<project>/simulation/topology/<study slug>/<run_id>/``. A caller's
    ``result_dir`` (relative = under the project) becomes the parent of ``<run_id>`` and
    must resolve — symlinks included — inside the project and outside the job spool, which
    is pruned. The run directory itself must not exist: a run never writes into another
    run's evidence.
    """
    jail = project_jail(env)
    if result_dir is not None:
        if not isinstance(result_dir, str) or not result_dir.strip():
            raise _invalid("topology.result_dir", "must be a non-empty path")
        requested = Path(os.path.expanduser(result_dir.strip()))
        if not requested.is_absolute():
            requested = jail / requested
        base = Path(os.path.realpath(requested))
    else:
        base = jail / "simulation" / "topology" / slug(study_name)
    if not contained(base, jail):
        raise TopologyInputError(
            "path_outside_project",
            f"result_dir {str(base)!r} is outside the project folder {str(jail)!r}; "
            "topology artifacts are server-owned and stay in the project.", "topology.result_dir")
    if spool is None:
        from services.sim import jobs as _jobs  # noqa: PLC0415 — jobs imports this module

        spool = _jobs.jobs_dir()
    if contained(base, spool) or contained(spool, base):
        raise TopologyInputError(
            "path_outside_project",
            f"result_dir {str(base)!r} overlaps the job spool {str(spool)!r}, which is pruned; "
            "choose a project folder.", "topology.result_dir")
    base.mkdir(parents=True, exist_ok=True)
    if not contained(base, jail):
        raise TopologyInputError("path_outside_project",
                                 f"result_dir {str(base)!r} resolved outside the project",
                                 "topology.result_dir")
    run = base / run_id
    run.mkdir(exist_ok=False)
    real = Path(os.path.realpath(run))
    if not contained(real, jail):
        run.rmdir()
        raise TopologyInputError("path_outside_project",
                                 f"run directory {str(real)!r} resolved outside the project",
                                 "topology.result_dir")
    return real


def sha256_file(path: Any) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    digest = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Any) -> dict:
    p = Path(path)
    return {"path": str(p), "sha256": sha256_file(p), "bytes": p.stat().st_size}


def _fresh_path(directory: Path, name: str) -> Path:
    """``name`` in ``directory``, or ``stem.N.suffix`` when taken — never an existing file."""
    target = directory / name
    stem, suffix = target.stem, target.suffix
    n = 1
    while target.exists():
        target = directory / f"{stem}.{n}{suffix}"
        n += 1
    return target


def write_json_new(directory: Path, name: str, value: Any) -> dict:
    """Write JSON to a file that did not exist (exclusive create); return its hash record."""
    text = json.dumps(value, allow_nan=False, indent=1, sort_keys=True)
    while True:
        target = _fresh_path(directory, name)
        try:
            with open(target, "x", encoding="utf-8") as fh:
                fh.write(text)
            break
        except FileExistsError:
            continue
    return file_record(target)


# ---------------------------------------------------------------- collection

def externalize_bulk(result: dict, run_dir: Path) -> tuple[dict, dict]:
    """Move per-element/per-node arrays out of the result into run-directory files.

    Returns (compact result, {artifact key: record}). The study record keeps only the
    path/hash; the arrays survive spool pruning in the project.
    """
    compact = {k: v for k, v in result.items() if k not in BULK_RESULT_KEYS}
    records: dict = {}
    for key in BULK_RESULT_KEYS:
        value = result.get(key)
        if value in (None, {}, []):
            continue
        rec = write_json_new(run_dir, f"{key}.json", value)
        records[key] = {**rec, "format": "json", "label": f"{key} field (solver output)"}
    return compact, records


def _overlay_entry(value: Any) -> Optional[dict]:
    if isinstance(value, str) and value.strip():
        return {"path": value, "format": "aicm", "label": "density overlay"}
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return {"format": "aicm", "label": "density overlay", **value}
    return None


def verify_artifacts(artifacts: Any, run_dir: Path, overlay: Any = None) -> tuple[dict, list]:
    """Hash every artifact the solver reported; mark missing/stale; refuse foreign paths.

    An artifact must be a file inside this run's directory. A path outside it is never
    hashed or offered (it could be any file on the machine): it is kept with
    ``missing: true`` and a note. ``stale`` means the solver reported a sha256 that the
    bytes on disk no longer match.
    """
    notes: list = []
    out: dict = {}
    entries = dict(artifacts) if isinstance(artifacts, dict) else {}
    overlay_entry = _overlay_entry(overlay)
    if overlay_entry is not None and "overlay_aicm" not in entries:
        entries["overlay_aicm"] = overlay_entry
    for key, raw in entries.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            notes.append(f"artifact {key!r} has no path; not registered")
            continue
        entry = dict(raw)
        p = Path(entry["path"])
        if not p.is_absolute():
            p = run_dir / p
        if not contained(p, run_dir):
            entry.update(missing=True, outside_run_dir=True)
            notes.append(f"artifact {key!r} ({entry['path']}) is outside the run directory; "
                         "not hashed or offered")
            out[key] = entry
            continue
        real = Path(os.path.realpath(p))
        entry["path"] = str(real)
        if not real.is_file():
            entry["missing"] = True
            notes.append(f"artifact {key!r} is missing on disk: {real}")
            out[key] = entry
            continue
        digest = sha256_file(real)
        reported = raw.get("sha256")
        entry["bytes"] = real.stat().st_size
        if isinstance(reported, str) and reported and reported.lower() != digest:
            entry["stale"] = True
            entry["reported_sha256"] = reported
            notes.append(f"artifact {key!r} no longer matches the hash the solver reported")
        entry["sha256"] = digest
        entry.pop("missing", None)
        out[key] = entry
    return out, notes


def surface_artifact(artifacts: dict, *, intact: bool = True) -> Optional[tuple[str, dict]]:
    """Print surface first, raw surface second, then other supported surface formats."""
    keys = dict.fromkeys(("surface_print_stl", "surface_stl", *artifacts))
    for key in keys:
        entry = artifacts.get(key)
        if not isinstance(entry, dict):
            continue
        fmt = str(entry.get("format") or Path(entry.get("path", "")).suffix.lstrip(".")).lower()
        if fmt in SURFACE_FORMATS and (not intact or not (
                entry.get("missing") or entry.get("stale"))):
            return key, entry
    return None


def effective_build_direction(result: Mapping, settings: Mapping) -> tuple[list, str]:
    overhang = ((result.get("manufacturing") or {}).get("enforcement") or {}).get(
        "overhang") or {}
    if overhang.get("build_direction") is not None:
        return list(overhang["build_direction"]), overhang.get(
            "build_direction_source") or "build_direction"
    if settings.get("build_direction") is not None:
        return list(settings["build_direction"]), "build_direction"
    return [0.0, 0.0, 1.0], "assumed"


def dfam_report(result: dict, artifacts: dict, settings: Mapping,
                print_process: Optional[dict] = None) -> dict:
    """Manufacturing block with a REAL DFAM check and an honest, never-true print_ready.

    services.dfam runs on the exported thresholded surface only when the solver validated
    that geometry; its rows are design-stage evidence. Print readiness belongs to
    slice_part alone (its passing G-code check is what catches mid-air extrusion), and
    collection never slices, so ``print_ready`` is always false here. The reason says
    whether geometry/DFAM passed and that sliced G-code validation is still missing.
    """
    manufacturing = dict(result.get("manufacturing") or {})
    geometry_valid = manufacturing.get("geometry_valid") is True
    surface = surface_artifact(artifacts)
    process = (print_process or {}).get("process", "fdm")
    rules = f"{process.upper()} rules"
    build, build_source = effective_build_direction(result, settings)
    dfam: dict
    if not geometry_valid:
        dfam = {"checked": False, "passed": False,
                "summary": "not run: the solver did not validate the thresholded geometry"}
    elif surface is None:
        dfam = {"checked": False, "passed": False,
                "summary": "not run: no intact surface mesh artifact (stl/3mf/obj/ply)"}
    else:
        key, entry = surface
        overhang = settings.get("overhang_angle_deg")
        min_member = settings.get("min_member_mm")
        params = None
        try:
            from services.dfam import PrintParams, analyze_printability, load_mesh_file  # noqa: PLC0415

            kwargs: dict = {k: v for k, v in (print_process or {}).items()
                            if k in {"process", "nozzle_mm", "line_width_mm", "layer_height_mm"}}
            kwargs.setdefault("process", process)
            if isinstance(overhang, (int, float)) and not isinstance(overhang, bool) \
                    and 0.0 < overhang <= 90.0:
                kwargs["overhang_max_deg"] = float(overhang)
            if isinstance(min_member, (int, float)) and not isinstance(min_member, bool) \
                    and min_member > 0.0:
                kwargs["min_wall_mm"] = float(min_member)
            params = PrintParams(**kwargs)
            report = analyze_printability(load_mesh_file(entry["path"]), params, tuple(build))
        except Exception as exc:  # noqa: BLE001 — a check that did not run is reported, not passed
            dfam = {"checked": False, "passed": False, "artifact": key,
                    "summary": f"DFAM check could not run: {type(exc).__name__}: {exc}"}
        else:
            rows = [{"check": r.get("check"), "status": r.get("status"), "note": r.get("note")}
                    for r in report.get("checks", [])]
            fails = [r["check"] for r in rows if r["status"] == "fail"]
            warns = [r["check"] for r in rows if r["status"] == "warn"]
            named = "; ".join(f"{label} {', '.join(checks)}"
                              for label, checks in (("fail:", fails), ("warn:", warns)) if checks)
            dfam = {"checked": True, "passed": report.get("status") == "pass",
                    "status": report.get("status"), "artifact": key,
                    "build_dir": report.get("build_dir"),
                    "overhang_max_deg": params.overhang_max_deg,
                    "min_wall_mm": params.min_wall_mm, "checks": rows,
                    "summary": (f"all DFAM checks pass ({rules})" if not named else
                                f"DFAM {report.get('status')} ({rules}): {named}")}
        if params is not None:
            dfam["layer_height_mm"] = params.layer_height_mm
            if process == "fdm" or "nozzle_mm" in (print_process or {}):
                dfam["nozzle_mm"] = params.nozzle_mm
    dfam.setdefault("status", "not_checked")
    dfam.setdefault("checks", [])
    dfam.setdefault("build_dir", build)
    dfam["process"] = process
    if build_source == "assumed":
        dfam["build_dir_assumed"] = True
    claimed = manufacturing.get("print_ready") is True
    manufacturing["dfam"] = dfam
    manufacturing["print_ready"] = False
    if result.get("ok") is not True:
        why = "the computation did not finish"
    elif not geometry_valid:
        why = "geometry not validated"
    elif not (dfam["checked"] and dfam["passed"]):
        why = dfam["summary"]
    else:
        why = f"geometry validated and DFAM passed ({rules})"
    manufacturing["print_ready_reason"] = (
        f"not print-ready: {why}; no sliced G-code has been validated (slice the exported "
        "surface with slice_part, whose passing G-code check is the print gate)")
    if claimed:
        manufacturing["print_ready_reason"] += " (solver claim overridden)"
    return manufacturing


def outcome(result: Mapping) -> dict:
    """Computation, convergence and feasibility, never collapsed into one flag.

    A run that did not complete has no feasibility verdict: ``feasible`` is None for it,
    whatever its last iterate reported.
    """
    ok = result.get("ok") is True
    converged = result.get("converged") is True
    feasible = result.get("feasible") if ok else None
    termination = str(result.get("termination") or "")
    why = result.get("termination_detail") or result.get("error")
    if termination == "cancelled":
        headline = "cancelled" + ("" if ok or not why else f": {why}")
    elif not ok:
        headline = f"failed: {why or termination or 'solver error'}"
    elif termination == "max_iterations":
        headline = "iteration limit reached (not converged)"
    elif feasible is False or termination == "infeasible":
        headline = "infeasible: the computation completed but a constraint is violated"
    elif converged:
        headline = "converged and feasible"
    else:
        headline = f"not converged ({termination or 'no criterion met'})"
    return {"computation_ok": ok, "converged": ok and converged and feasible is not False
            and termination not in ("max_iterations", "infeasible", "cancelled"),
            "feasible": feasible, "termination": termination, "headline": headline}


# ---------------------------------------------------------------- unfinished runs
# A failed or cancelled job is retained in its study as well, but only with what the
# solver actually wrote. Never a re-labelled success: no feasibility verdict, no DFAM, no
# print-ready claim, and no metric a dead worker never reported.

#: Files core/sim/topopt_run.cpp writes into its ``native-*`` run directory BEFORE the
#: final reply (key, file name, format). checkpoint.json is replaced atomically; a
#: ``*.tmp`` beside it is an interrupted write and is never registered.
NATIVE_RUN_FILES = (("run", "run.json", "json"), ("inputs", "study.json", "json"),
                    ("normalized_spec", "normalized_spec.json", "json"),
                    ("history_log", "history.jsonl", "jsonl"),
                    ("checkpoint", "checkpoint.json", "json"))
NATIVE_CHECKPOINT_SCHEMA = "chudware.topology.native_checkpoint/1"

#: A failed run's own reply may claim these; none is meaningful for an unfinished design.
_UNFINISHED_DROPPED = ("feasible", "overlay_aicm")


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recover_native_run(run_dir: Path, settings: Mapping) -> tuple[dict, dict, list]:
    """(artifact entries, recovered facts, notes) for a run whose native reply never came.

    Only files the native adapter really wrote into a ``native-*`` child of this run's own
    directory are registered (hashing happens in :func:`verify_artifacts`). The completed
    iteration count comes from whole ``history.jsonl`` lines; the checkpoint iteration only
    from a checkpoint of the documented schema for this run's method and physics.
    """
    entries: dict = {}
    recovered: dict = {}
    notes: list = []
    children = sorted(c for c in run_dir.glob("native-*")
                      if c.is_dir() and (c / "run.json").is_file() and contained(c, run_dir))
    if not children:
        notes.append("the solver wrote no run evidence before it stopped")
        return entries, recovered, notes
    for child in children:
        suffix = "" if len(children) == 1 else f"@{child.name}"
        facts: dict = {"native_run_dir": str(child)}
        for key, name, fmt in NATIVE_RUN_FILES:
            path = child / name
            if path.is_file():
                entries[key + suffix] = {"path": str(path), "format": fmt,
                                         "label": f"native {name} (written before the stop)"}
        history = child / "history.jsonl"
        if history.is_file():
            lines = history.read_text(encoding="utf-8", errors="replace").split("\n")
            whole = [ln for ln in lines[:-1] if ln.strip()]
            complete = 0
            for line in whole:
                try:
                    json.loads(line)
                except ValueError:
                    break
                complete += 1
            if lines[-1].strip():
                notes.append(f"{history} ends in an interrupted line; it is not counted")
            facts["iterations_recorded"] = complete
        checkpoint = _read_json_file(child / "checkpoint.json")
        if isinstance(checkpoint, dict):
            iteration = checkpoint.get("iteration")
            if (checkpoint.get("schema") == NATIVE_CHECKPOINT_SCHEMA
                    and checkpoint.get("method") == settings.get("method")
                    and checkpoint.get("physics") == settings.get("physics")
                    and isinstance(iteration, int) and not isinstance(iteration, bool)
                    and iteration >= 0):
                facts["checkpoint_iteration"] = iteration
            else:
                notes.append(f"{child / 'checkpoint.json'} is not a {NATIVE_CHECKPOINT_SCHEMA} "
                             "checkpoint of this run's method and physics; no iteration read")
        elif (child / "checkpoint.json").is_file():
            notes.append(f"{child / 'checkpoint.json'} is unreadable")
        if suffix:
            recovered.setdefault("native_runs", []).append(facts)
        else:
            recovered.update(facts)
    return entries, recovered, notes


def unfinished_manufacturing(reported: Any, reason: str) -> dict:
    """Manufacturing block of a run that did not complete: never DFAM-checked or ready."""
    manufacturing = dict(reported) if isinstance(reported, dict) else {}
    claimed = manufacturing.get("print_ready") is True
    manufacturing["dfam"] = {"checked": False, "passed": False,
                             "summary": f"not run: {reason}"}
    manufacturing["print_ready"] = False
    manufacturing["print_ready_reason"] = f"not print-ready: {reason}" + (
        " (solver claim overridden)" if claimed else "")
    return manufacturing


def unfinished_result(native: Optional[Mapping], *, state: str, error: str,
                      settings: Mapping) -> dict:
    """The retained result of a failed (``state`` "failed") or cancelled run.

    ``native`` is the reply the solver wrote (an ok:false packet), or None when the worker
    failed before replying or was killed. Its fields are kept as reported — summary,
    history, artifacts and arrays describe the last completed iterate — except the
    completion flags: ``ok``/``converged`` are false, ``feasible`` is dropped, and
    ``termination`` is "cancelled" for a cancelled job, else the solver's own failure
    termination, else "failed". Bulk arrays still go through :func:`externalize_bulk`.
    """
    out = {k: v for k, v in (native or {}).items() if k not in _UNFINISHED_DROPPED}
    native_termination = str(out.get("termination") or "")
    if state == "cancelled":
        termination = "cancelled"
    elif native_termination and native_termination != "converged":
        termination = native_termination
    else:
        termination = "failed"
    if native and native.get("ok") is True:
        out["solver_reported_ok"] = True
    out.update(ok=False, converged=False, termination=termination,
               method=out.get("method") or settings.get("method"),
               physics=out.get("physics") or settings.get("physics"))
    out.setdefault("error", error)
    out.setdefault("termination_detail", error)
    out["manufacturing"] = unfinished_manufacturing(
        out.get("manufacturing"),
        "the run was cancelled before it completed" if state == "cancelled"
        else "the run failed before it completed")
    return out


def run_record(run_id: str, *, label: str, started_utc: float, settings: dict,
               result: dict, job_id: str, artifact_dir: str) -> dict:
    """The compact retained-run entry (host/src/ui/sim_topology.h RUN RECORD)."""
    return {"run_id": run_id, "label": label, "started_utc": started_utc,
            "settings": settings, "result": result, "job_id": job_id,
            "artifact_dir": artifact_dir}


#: last_job states a later write for the same run must never move back to "running".
FINAL_JOB_STATES = ("collected", "failed", "cancelled")


def apply_run_record(study: dict, record: dict, *, state: str = "collected") -> tuple[dict, dict]:
    """Append ``record`` to the study's runs, exactly once, without touching other keys.

    A record whose run_id is already retained is not appended again (a re-delivered
    collect). A run collected after a NEWER run was submitted is retained as evidence but
    does not overwrite ``last_job``/``result_summary`` (it is stale for the live setup).
    ``state`` is the run's final last_job state: "collected", "failed" or "cancelled".
    """
    if state not in FINAL_JOB_STATES:
        raise ValueError(f"run state must be one of {FINAL_JOB_STATES}, got {state!r}")
    out = dict(study)
    runs = out.get("runs")
    runs = list(runs) if isinstance(runs, list) else []
    duplicate = any(isinstance(r, dict) and r.get("run_id") == record["run_id"] for r in runs)
    if not duplicate:
        runs.append(record)
    out["runs"] = runs
    last = out.get("last_job") if isinstance(out.get("last_job"), dict) else {}
    superseded = bool(last.get("run_id")) and last.get("run_id") != record["run_id"]
    if not superseded:
        job = {k: v for k, v in last.items() if k != "error"}
        job.update(run_id=record["run_id"], job_id=record["job_id"], state=state,
                   collected_utc=time.time())
        if state != "collected":
            job["error"] = record["result"].get("error")
        out["last_job"] = job
        out["result_summary"] = outcome(record["result"])["headline"]
    return out, {"duplicate": duplicate, "superseded_by": last.get("run_id") if superseded
                 else None}
