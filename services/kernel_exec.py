"""Chudware modeling kernel executor (services-side).

Re-homed from ``engine_oracle/kernel.py`` in T-RF20: executing AI-written build123d
fundamentally needs a Python interpreter, so this module CANNOT be ported to the C++
core — it is kept as a thin services-side executor, not engine logic. The frozen
legacy worker method ``run_model`` (contracts/worker_rpc.md) imports
``run_code_multi`` from here; nothing in ``engine_oracle`` should import this back.

Executes AI-generated build123d code and turns the resulting solid into:
  * precise STEP + mesh STL on disk,
  * mass properties (volume, surface area, bounding box, center of mass),
  * a tessellation for the live viewer.

The model-author contract: the script runs with the full ``build123d`` namespace in
scope and assigns the final shape to ``result`` (a Part/Solid/Compound, or a
BuildPart/BuildSketch/BuildLine context which we unwrap).

``run_code`` executes in-process (handy for tests). ``run_code_isolated`` runs the same
thing in a fresh subprocess so a bad-geometry OCCT crash cannot take down the caller.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional
from services.agent.request_context import (
    bind_context, cell_path, current_context, in_cell, sandbox_argv,
)

# build123d logs verbosely at INFO. The MCP stdio transport uses stdout for protocol
# JSON, so any stray logging must be kept out of the way: quiet the logger and make sure
# the lastResort handler writes to stderr, never stdout.
logging.getLogger("build123d").setLevel(logging.WARNING)

RESULT_NAMES = ("result", "part", "shape", "model", "obj")

# Names a multi-part script may bind in ADDITION to (or instead of) ``result``:
#   parts    = {"base": solid_a, "lid": solid_b}   # named, individually positioned bodies
#   assembly = Compound(children=[...])            # the bodies placed together for fit-test
PARTS_NAMES = ("parts", "bodies", "components")
ASSEMBLY_NAMES = ("assembly", "asm")

# Pairwise overlap below this (mm^3) is treated as numerical/touching noise, not a real
# interference. Real clashes (e.g. two 20mm cubes 10mm apart -> 4000 mm^3) are far above it.
INTERFERENCE_EPS = 1e-3


@dataclass
class ModelResult:
    ok: bool
    error: Optional[str] = None
    stdout: str = ""
    stl_path: Optional[str] = None
    step_path: Optional[str] = None
    triangles: int = 0
    volume: Optional[float] = None
    area: Optional[float] = None
    bbox: Optional[dict] = None          # {min,max,size,center} each [x,y,z]
    faces: Optional[dict] = None         # {total,planar,cylindrical} face census
    warnings: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass
class AssemblyResult:
    """Result of a multi-part / assembly build. A SUPERSET of ModelResult: the same
    top-level fields (ok/error/stdout/stl_path/step_path/triangles/volume/area/bbox/
    warnings) describe the COMBINED assembly, so existing single-part consumers (the agent
    loop, the worker, the C++ host) read it unchanged — while ``parts`` and ``fit`` carry the
    per-part exports and the interference/clearance report on top."""
    ok: bool
    error: Optional[str] = None
    stdout: str = ""
    is_assembly: bool = False            # True when the script defined parts/assembly
    stl_path: Optional[str] = None       # combined assembly mesh (or the lone part)
    step_path: Optional[str] = None      # combined assembly STEP
    triangles: int = 0
    volume: Optional[float] = None
    area: Optional[float] = None
    bbox: Optional[dict] = None          # combined assembly bbox {min,max,size,center}
    faces: Optional[dict] = None         # {total,planar,cylindrical} face census (combined)
    parts: list = field(default_factory=list)   # [{name,stl_path,step_path,triangles,volume,area,bbox}]
    fit: Optional[dict] = None           # interference/clearance report (see _fit_report)
    warnings: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _to_shape(obj: Any):
    """Unwrap a build123d builder context to its geometry, if needed."""
    for attr in ("part", "sketch", "line"):
        inner = getattr(obj, attr, None)
        if inner is not None and not callable(inner):
            return inner
    return obj


def _find_result(ns: dict):
    for name in RESULT_NAMES:
        if name in ns and ns[name] is not None:
            return _to_shape(ns[name])
    return None


# Keyword aliases models commonly reach for on the solid primitives. Maps alias ->
# (canonical_kwarg, factor); factor 0.5 turns a diameter into a radius. Applied only when the
# canonical kwarg isn't already supplied, so correct calls are never disturbed. This stops a
# trivial typo like `Cylinder(r=4)` (instead of `radius=4`) from crashing an entire script.
_RAD = ("radius", 1.0)
_DIA = ("radius", 0.5)
_HGT = ("height", 1.0)
_PRIMITIVE_KWALIASES = {
    "Box": {"l": ("length", 1.0), "w": ("width", 1.0), "h": _HGT, "depth": _HGT},
    "Cylinder": {"r": _RAD, "d": _DIA, "diameter": _DIA, "h": _HGT, "length": _HGT},
    "Sphere": {"r": _RAD, "d": _DIA, "diameter": _DIA},
    "Cone": {"r1": ("bottom_radius", 1.0), "r2": ("top_radius", 1.0),
             "bottom_r": ("bottom_radius", 1.0), "top_r": ("top_radius", 1.0),
             "d1": ("bottom_radius", 0.5), "d2": ("top_radius", 0.5), "h": _HGT},
    "Torus": {"r1": ("major_radius", 1.0), "r2": ("minor_radius", 1.0),
              "major_r": ("major_radius", 1.0), "minor_r": ("minor_radius", 1.0)},
    "Circle": {"r": _RAD, "d": _DIA, "diameter": _DIA},
    "RegularPolygon": {"r": _RAD, "n": ("side_count", 1.0), "sides": ("side_count", 1.0),
                       "num_sides": ("side_count", 1.0)},
    "Hole": {"r": _RAD, "d": _DIA, "diameter": _DIA,
             "h": ("depth", 1.0), "height": ("depth", 1.0)},
    "CounterBoreHole": {"r": _RAD, "d": _DIA, "diameter": _DIA},
    "CounterSinkHole": {"r": _RAD, "d": _DIA, "diameter": _DIA},
}


def _forgiving_primitive(real, aliases):
    """Wrap a build123d primitive so aliased kwargs are renamed (and diameters halved) before
    construction; correct calls pass straight through."""
    def make(*args, _real=real, _aliases=aliases, **kw):
        for alias, (canon, factor) in _aliases.items():
            if alias in kw and canon not in kw:
                v = kw.pop(alias)
                kw[canon] = v * factor if factor != 1 else v
        return _real(*args, **kw)
    return make


def _build123d_namespace() -> dict:
    import build123d as b3d

    ns = {k: getattr(b3d, k) for k in dir(b3d) if not k.startswith("_")}
    # Wrap the solid primitives so common keyword typos (r=/d=/h=, l=/w=) build instead of
    # crashing the whole script. Done before aliasing below so the lowercase aliases inherit
    # the forgiving wrappers too.
    for _cls, _aliases in _PRIMITIVE_KWALIASES.items():
        _real = ns.get(_cls)
        if callable(_real):
            ns[_cls] = _forgiving_primitive(_real, _aliases)
    # Forgiving aliases for names weaker models commonly reach for (lowercase / CadQuery
    # style). Only fill names build123d doesn't already define, so the real API (and its
    # lowercase verbs like extrude/fillet/chamfer) is never shadowed.
    _alias = {
        "rect": "Rectangle", "rectangle": "Rectangle", "circle": "Circle",
        "ellipse": "Ellipse", "box": "Box", "cylinder": "Cylinder",
        "sphere": "Sphere", "cone": "Cone", "torus": "Torus", "wedge": "Wedge",
        "polygon": "RegularPolygon", "text": "Text",
    }
    for alias, real in _alias.items():
        if alias not in ns and real in ns:
            ns[alias] = ns[real]
    if "cube" not in ns and "Box" in ns:
        _Box = ns["Box"]
        ns["cube"] = lambda s, _Box=_Box: _Box(s, s, s)
    # extrude(...): accept the common keyword aliases models use for build123d's `amount`.
    _real_extrude = ns.get("extrude")
    if callable(_real_extrude):
        def extrude(*args, _f=_real_extrude, **kw):
            for alt in ("height", "distance", "length", "dist", "depth"):
                if alt in kw and "amount" not in kw:
                    kw["amount"] = kw.pop(alt)
            return _f(*args, **kw)
        ns["extrude"] = extrude
    # fillet/chamfer: build123d takes the edges/vertices as the FIRST POSITIONAL arg, and the
    # magnitude kwarg is `radius` for fillet but `length` for chamfer. Models routinely get this
    # wrong - `chamfer(solid, edges=es, radius=r)` / `fillet(part, edges=es, length=r)` - which
    # crashes the whole script ("unexpected keyword argument 'edges'"). Wrap both so the edges
    # can arrive via edges=/objects= (dropping a spurious solid passed positionally) and the
    # magnitude via either radius=/length=/size=, mapped to the canonical kwarg. Correct calls
    # pass straight through untouched.
    def _wrap_edge_op(real, mag_kw):
        def op(*args, _f=real, _mag=mag_kw, **kw):
            target = None
            for k in ("edges", "objects", "edge", "object", "targets", "edge_list"):
                if k in kw:
                    target = kw.pop(k)
                    break
            if target is not None:
                args = (target,)  # the edges/vertices to operate on (ignore a stray solid arg)
            if _mag not in kw:
                for k in ("radius", "length", "size", "amount", "dist", "r"):
                    if k in kw:
                        kw[_mag] = kw.pop(k)
                        break
            try:
                return _f(*args, **kw)
            except Exception as first_err:  # noqa: BLE001
                # The #1 fillet/chamfer failure is a magnitude too large for a thin wall —
                # and a cosmetic feature should never kill an otherwise-good part. Retry at
                # progressively smaller sizes, announcing any clamp on stdout (which is fed
                # back to the model) so the downgrade is visible, never silent.
                mag = kw.get(_mag)
                if not isinstance(mag, (int, float)) or mag <= 0:
                    raise
                m = float(mag)
                for _ in range(3):
                    m /= 2.0
                    if m < 0.2:
                        break
                    try:
                        out = _f(*args, **{**kw, _mag: m})
                    except Exception:  # noqa: BLE001
                        continue
                    print(f"[kernel] {getattr(_f, '__name__', 'edge-op')} {_mag}={mag:g} was "
                          f"too large for this geometry; clamped to {_mag}={m:g} so the build "
                          f"could finish.")
                    return out
                raise first_err
        return op
    for _name, _mag in (("fillet", "radius"), ("chamfer", "length")):
        _real = ns.get(_name)
        if callable(_real):
            ns[_name] = _wrap_edge_op(_real, _mag)
    return ns


def _face_census(shape) -> Optional[dict]:
    """Count faces by geometry type. Round features (holes, bosses, fillets on round
    edges) contribute CYLINDRICAL faces — so "cylindrical: 0" is hard, render-independent
    evidence that a part has no round holes/bosses, which a weak model can act on."""
    try:
        from build123d import GeomType

        faces = shape.faces()
        return {
            "total": len(faces),
            "planar": len(faces.filter_by(GeomType.PLANE)),
            "cylindrical": len(faces.filter_by(GeomType.CYLINDER)),
        }
    except Exception:  # noqa: BLE001
        return None


def _mass_props(shape, warnings: list) -> dict:
    out: dict = {}
    try:
        bb = shape.bounding_box()
        out["bbox"] = {
            "min": [bb.min.X, bb.min.Y, bb.min.Z],
            "max": [bb.max.X, bb.max.Y, bb.max.Z],
            "size": [bb.size.X, bb.size.Y, bb.size.Z],
        }
    except Exception as e:  # noqa: BLE001
        warnings.append(f"bounding_box failed: {e}")
    try:
        out["volume"] = float(shape.volume)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"volume failed: {e}")
    try:
        out["area"] = float(shape.area)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"area failed: {e}")
    try:
        c = shape.center()
        if out.get("bbox") is not None:
            out["bbox"]["center"] = [c.X, c.Y, c.Z]
    except Exception as e:  # noqa: BLE001
        warnings.append(f"center failed: {e}")
    return out


def _export(shape, out_dir: Path, name: str, warnings: list) -> tuple[Optional[str], Optional[str], int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / f"{name}.stl"
    step_path = out_dir / f"{name}.step"
    tris = 0

    # STEP (precise B-rep)
    try:
        from build123d import export_step

        export_step(shape, str(step_path))
    except Exception as e:  # noqa: BLE001
        try:
            shape.export_step(str(step_path))
        except Exception as e2:  # noqa: BLE001
            warnings.append(f"STEP export failed: {e} / {e2}")
            step_path = None

    # STL (mesh for viewer/render)
    try:
        from build123d import export_stl

        export_stl(shape, str(stl_path), tolerance=0.05, angular_tolerance=0.1)
    except Exception as e:  # noqa: BLE001
        try:
            shape.export_stl(str(stl_path), tolerance=0.05, angular_tolerance=0.1)
        except Exception as e2:  # noqa: BLE001
            warnings.append(f"STL export failed: {e} / {e2}")
            stl_path = None

    # triangle count via tessellation
    try:
        _verts, faces = shape.tessellate(0.05)
        tris = len(faces)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"tessellate failed: {e}")

    return (str(stl_path) if stl_path else None,
            str(step_path) if step_path else None,
            tris)


_STAR_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+build123d(?:\.\w+)?[ \t]+import[ \t]+\*[ \t]*$", re.M)

# Hints appended to selected tracebacks before they reach the model. Reserved for mistakes
# whose raw error text is too cryptic to self-correct from: e.g. Python 3.11 enums raise just
# "AttributeError: Pos" for `Mode.Pos` / `Align.Pos` — no mention of the enum, the valid
# members, or the standalone `Pos(x, y, z)` the model actually wanted.
_ERROR_HINTS = (
    (re.compile(r"AttributeError: (?:Pos|Rot|Position|Location)\b"),
     "HINT: Pos and Rot are STANDALONE location builders - write `Pos(x, y, z) * shape` "
     "(or `Rot(ax, ay, az) * shape`). There is no `.Pos` attribute on anything: "
     "`Mode.Pos`, `Align.Pos` and `solid.Pos` do not exist. Mode members are "
     "ADD/SUBTRACT/INTERSECT/REPLACE/PRIVATE; Align members are MIN/CENTER/MAX."),
    (re.compile(r"unsupported operand type\(s\) for \*: '\w+' and '(?:Pos|Rot|Location)'"),
     "HINT: the location goes on the LEFT of the `*`: write `Pos(x, y, z) * shape` "
     "(or `Rot(ax, ay, az) * shape`), never `shape * Pos(...)`."),
)


def _hint_error(err: str) -> str:
    """Append a corrective hint to cryptic errors the model is known to misread."""
    for pat, hint in _ERROR_HINTS:
        if pat.search(err):
            return err.rstrip() + "\n\n" + hint
    return err


def _exec_model(code: str) -> tuple[dict, str, Optional[str]]:
    """Execute model ``code`` in a fresh build123d namespace.

    Returns ``(namespace, captured_stdout, error_or_None)``. Shared by the single-part
    (``run_code``) and multi-part (``run_code_multi``) entry points so they exec identically.
    """
    if in_cell() and os.environ.get("CHUDWARE_ACTOR_SANDBOX") != "1":
        raise ValueError("cell_refused")
    # The full namespace is injected below; drop a redundant `from build123d import *` so it
    # can't re-bind (and clobber) our forgiving wrappers/aliases like extrude/rect.
    code = _STAR_IMPORT_RE.sub("", code)
    g = {"__name__": "__cad_model__"}
    g.update(_build123d_namespace())
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            exec(compile(code, "<model>", "exec"), g)  # noqa: S102
    except Exception:  # noqa: BLE001
        return g, stdout.getvalue(), _hint_error(traceback.format_exc())
    return g, stdout.getvalue(), None


def run_code(code: str, out_dir: Path, name: str = "model") -> ModelResult:
    """Execute build123d ``code`` in-process and export the resulting solid."""
    warnings: list = []
    g, stdout, err = _exec_model(code)
    if err is not None:
        return ModelResult(ok=False, error=err, stdout=stdout)

    shape = _find_result(g)
    if shape is None:
        return ModelResult(
            ok=False,
            stdout=stdout,
            error=("No result found. Assign the final shape to a variable named "
                   f"one of {RESULT_NAMES}."),
        )

    stl_path, step_path, tris = _export(shape, out_dir, name, warnings)
    props = _mass_props(shape, warnings)
    return ModelResult(
        ok=True,
        stdout=stdout,
        stl_path=stl_path,
        step_path=step_path,
        triangles=tris,
        volume=props.get("volume"),
        area=props.get("area"),
        bbox=props.get("bbox"),
        faces=_face_census(shape),
        warnings=warnings,
    )


def _safe_name(raw, idx: int) -> str:
    """A filesystem/identifier-safe part name; falls back to part{idx} when empty."""
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(raw)).strip("_")
    return s or f"part{idx + 1}"


def _lookup(ns: dict, names: tuple) -> Any:
    for n in names:
        if n in ns and ns[n] is not None:
            return ns[n]
    return None


def _members_from(obj: Any) -> Optional[list]:
    """Turn a parts/assembly binding into an ordered list of (name, shape), or None if it
    isn't a recognized container. Accepts a dict {name: solid}, a list/tuple [solid, ...],
    or a Compound (its children, named by ``.label`` when present)."""
    if isinstance(obj, dict):
        return [(_safe_name(k, i), _to_shape(v))
                for i, (k, v) in enumerate(obj.items()) if v is not None]
    if isinstance(obj, (list, tuple)):
        return [(_safe_name(getattr(v, "label", ""), i), _to_shape(v))
                for i, v in enumerate(obj) if v is not None]
    shape = _to_shape(obj)
    children = getattr(shape, "children", None)
    if children:  # a Compound / assembly container -> one member per child
        kids = list(children)
        if kids:
            return [(_safe_name(getattr(c, "label", ""), i), c) for i, c in enumerate(kids)]
    return None


def _resolve_members(ns: dict) -> tuple[list, Any, bool]:
    """Inspect the executed namespace for a multi-part definition.

    Returns ``(members, explicit_assembly, is_multi)`` where members is an ordered list of
    ``(name, shape)`` positioned bodies. ``parts`` wins over ``assembly`` for naming so the
    fit report and the rendered/exported compound never describe different bodies. When only
    an explicit ``assembly`` Compound is given, members are taken from its children.
    """
    parts = _lookup(ns, PARTS_NAMES)
    assembly = _lookup(ns, ASSEMBLY_NAMES)
    members = _members_from(parts) if parts is not None else None
    explicit_asm = None
    if members is None and assembly is not None:
        members = _members_from(assembly)
        # Reuse a model-built Compound directly (avoids re-parenting its children).
        if getattr(_to_shape(assembly), "children", None):
            explicit_asm = _to_shape(assembly)
    is_multi = bool(members)
    return (members or []), explicit_asm, is_multi


def _bbox_dict(shape) -> Optional[dict]:
    try:
        bb = shape.bounding_box()
        return {
            "min": [bb.min.X, bb.min.Y, bb.min.Z],
            "max": [bb.max.X, bb.max.Y, bb.max.Z],
            "size": [bb.size.X, bb.size.Y, bb.size.Z],
        }
    except Exception:  # noqa: BLE001
        return None


def _intersect_volume(a, b) -> Optional[float]:
    """Overlap volume of two solids (0.0 when disjoint), or None if the boolean failed."""
    try:
        inter = a & b
        return float(inter.volume) if inter is not None else 0.0
    except Exception:  # noqa: BLE001
        return None


def _min_distance(a, b) -> Optional[float]:
    """Minimum gap (mm) between two solids via OCCT's exact distance query; None on failure."""
    try:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape

        dss = BRepExtrema_DistShapeShape(a.wrapped, b.wrapped)
        if dss.IsDone():
            return float(dss.Value())
    except Exception:  # noqa: BLE001
        pass
    return None


def _fit_report(members: list, warnings: list) -> dict:
    """Pairwise interference (overlap volume) + clearance (min distance) across the parts,
    plus per-part bounding boxes. ``ok`` is True when nothing interferes."""
    part_bb = []
    for name, shp in members:
        part_bb.append({"name": name, "bbox": _bbox_dict(shp),
                        "volume": _safe_float(getattr(shp, "volume", None))})

    interferences: list = []
    clearances: list = []
    n = len(members)
    for i in range(n):
        for j in range(i + 1, n):
            ni, si = members[i]
            nj, sj = members[j]
            ov = _intersect_volume(si, sj)
            if ov is None:
                warnings.append(f"interference check {ni}<->{nj} failed")
                continue
            if ov > INTERFERENCE_EPS:
                interferences.append({"a": ni, "b": nj, "volume": ov})
            else:
                d = _min_distance(si, sj)
                if d is not None:
                    clearances.append({"a": ni, "b": nj, "distance": d})

    if interferences:
        big = max(interferences, key=lambda x: x["volume"])
        summary = (f"{len(interferences)} interfering pair(s); largest "
                   f"{big['a']}<->{big['b']} = {big['volume']:.1f} mm^3")
    elif clearances:
        tight = min(clearances, key=lambda x: x["distance"])
        summary = (f"No interference. Tightest gap {tight['a']}<->{tight['b']} = "
                   f"{tight['distance']:.2f} mm")
    else:
        summary = "No interference detected."

    return {
        "ok": not interferences,
        "summary": summary,
        "parts": part_bb,
        "interferences": interferences,
        "clearances": clearances,
    }


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def run_code_multi(code: str, out_dir: Path, name: str = "model") -> AssemblyResult:
    """Execute build123d ``code`` and, if it defines multiple parts, export each part plus a
    combined assembly and compute a fit (interference/clearance) report.

    Multi-part contract (in ADDITION to the single-``result`` contract): assign
      ``parts = {"base": solid_a, "lid": solid_b}``   # named, positioned bodies, and/or
      ``assembly = Compound(children=[...])``         # the bodies placed together
    Position bodies with ``Pos(x,y,z) * solid`` / ``Rot(rx,ry,rz) * solid``.

    Back-compat: a script that only assigns ``result`` (no parts/assembly) takes the
    single-part path and yields the same geometry/props as ``run_code`` — wrapped in an
    AssemblyResult with ``is_assembly=False`` and a single entry in ``parts``.
    """
    if in_cell() and "--exec" not in sys.argv:
        return _run_cell_model(code, out_dir, name)
    warnings: list = []
    g, stdout, err = _exec_model(code)
    if err is not None:
        return AssemblyResult(ok=False, error=err, stdout=stdout)

    members, explicit_asm, is_multi = _resolve_members(g)

    if not is_multi:
        # Single-part fallback: behave like run_code but in AssemblyResult shape.
        shape = _find_result(g)
        if shape is None:
            return AssemblyResult(
                ok=False, stdout=stdout,
                error=("No result/parts/assembly found. Assign the final shape to "
                       f"`result` (one of {RESULT_NAMES}), or define `parts`/`assembly`."))
        stl_path, step_path, tris = _export(shape, out_dir, name, warnings)
        props = _mass_props(shape, warnings)
        part = {"name": name, "stl_path": stl_path, "step_path": step_path,
                "triangles": tris, "volume": props.get("volume"),
                "area": props.get("area"), "bbox": props.get("bbox")}
        return AssemblyResult(
            ok=True, stdout=stdout, is_assembly=False,
            stl_path=stl_path, step_path=step_path, triangles=tris,
            volume=props.get("volume"), area=props.get("area"), bbox=props.get("bbox"),
            faces=_face_census(shape), parts=[part], fit=None, warnings=warnings)

    # ---- multi-part: per-part export, fit report, then the combined assembly ----
    parts_out: list = []
    for idx, (pname, shp) in enumerate(members):
        safe = _safe_name(pname, idx)
        p_stl, p_step, p_tris = _export(shp, out_dir, f"{name}_{safe}", warnings)
        p_props = _mass_props(shp, warnings)
        parts_out.append({
            "name": safe, "stl_path": p_stl, "step_path": p_step, "triangles": p_tris,
            "volume": p_props.get("volume"), "area": p_props.get("area"),
            "bbox": p_props.get("bbox")})

    fit = _fit_report(members, warnings)

    # Combined assembly: reuse the model's Compound when it gave one, else group the parts
    # into a non-fused Compound (keeps bodies distinct; volume = sum, not a boolean union).
    if explicit_asm is not None:
        asm_shape = explicit_asm
    else:
        try:
            from build123d import Compound

            asm_shape = Compound(children=[s for _, s in members])
        except Exception as e:  # noqa: BLE001
            warnings.append(f"assembly grouping failed: {e}")
            asm_shape = members[0][1] if members else None

    a_stl = a_step = None
    a_tris = 0
    a_props: dict = {}
    if asm_shape is not None:
        a_stl, a_step, a_tris = _export(asm_shape, out_dir, f"{name}_assembly", warnings)
        a_props = _mass_props(asm_shape, warnings)

    return AssemblyResult(
        ok=True, stdout=stdout, is_assembly=True,
        stl_path=a_stl, step_path=a_step, triangles=a_tris,
        volume=a_props.get("volume"), area=a_props.get("area"), bbox=a_props.get("bbox"),
        faces=_face_census(asm_shape) if asm_shape is not None else None,
        parts=parts_out, fit=fit, warnings=warnings)


def _model_result_from_payload(data: dict) -> ModelResult:
    """Build a :class:`ModelResult` from subprocess JSON, dropping assembly-only keys."""
    names = {f.name for f in fields(ModelResult)}
    return ModelResult(**{k: v for k, v in data.items() if k in names})


def _run_cell_model(code: str, out_dir: Path, name: str) -> AssemblyResult:
    out_dir = cell_path(str(out_dir), private=True)
    payload = json.dumps({"code": code, "out_dir": str(out_dir), "name": name,
                          "ctx": current_context()})
    try:
        proc = subprocess.run(
            sandbox_argv([sys.executable, "-m", "services.kernel_exec", "--exec"]),
            input=payload, capture_output=True, text=True, cwd="/work",
        )
        if proc.returncode != 0:
            return AssemblyResult(ok=False, error=f"Model worker exited ({proc.returncode}): {proc.stderr}")
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        return AssemblyResult(**data)
    except (OSError, ValueError, IndexError, TypeError) as exc:
        return AssemblyResult(ok=False, error=f"Model sandbox failed: {type(exc).__name__}: {exc}")


def run_code_isolated(code: str, out_dir: Path, name: str = "model",
                      timeout: float = 60.0) -> ModelResult:
    """Run ``code`` in a fresh subprocess; isolates OCCT crashes/hangs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"code": code, "out_dir": str(out_dir), "name": name,
                          "ctx": current_context()})
    try:
        proc = subprocess.run(
            sandbox_argv([sys.executable, "-m", "services.kernel_exec", "--exec"]),
            input=payload, capture_output=True, text=True, timeout=timeout,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    except subprocess.TimeoutExpired:
        return ModelResult(ok=False, error=f"Model build timed out after {timeout}s")
    if proc.returncode != 0 and not proc.stdout.strip():
        return ModelResult(ok=False, error=f"Worker crashed (rc={proc.returncode}).\n{proc.stderr}")
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return ModelResult(ok=False, error=f"Bad worker output:\n{proc.stdout}\n{proc.stderr}")
    return _model_result_from_payload(data)


def _main_exec() -> None:
    req = json.loads(sys.stdin.read())
    # Match the worker path: multi-part scripts export per-part + assembly meshes.
    with bind_context(req.get("ctx")):
        res = run_code_multi(req["code"], Path(req["out_dir"]), req.get("name", "model"))
    sys.stdout.write(res.to_json().replace("\n", " "))
    sys.stdout.write("\n")


if __name__ == "__main__":
    if "--exec" in sys.argv:
        _main_exec()
