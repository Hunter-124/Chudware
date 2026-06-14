"""Assembly compile + mate-solve + tessellation (C20).

An assembly document (``aicad.asm/1``, doc_schema §6) is rebuilt very differently from a part:

1. **Component loading** — each component's ``source.path`` is opened ONCE (cached per path+mtime):
     ``.step+``  -> :func:`engine.doc.stepplus.open` -> compile the embedded part doc -> bodies.
     bare ``.step`` / ``.stl`` / ``.iges`` / ``.brep`` -> :func:`engine.doc.formats.import_path`.
   The result is a list of build123d solids in the component's LOCAL frame, plus a per-component
   topology cache (faces/edges described once) for mate-ref resolution.

2. **Mate-ref resolution** — every mate ref ``{component, selector}`` resolves the selector against
   that component's local geometry (C03 :mod:`engine.doc.selectors`) into a :class:`MateGeom`
   (plane normal+point / cylinder axis+radius / point) in the component's local frame.

3. **Mate solve** — :func:`engine.doc.mate_solver.solve_mates` over nsolve places every floating
   component; fixed components are pinned. Solved transforms are written back into the doc's
   ``components[*].transform`` (so a save persists the solved pose) and a per-mate status list +
   DOF summary are reported.

4. **Tessellation** — each component contributes ONE AICM body (``body_id`` = component ordinal,
   ``name`` = component name, a distinct colour per ordinal). Bodies are transformed by the solved
   component transform; when the explode flag is active, an additional explode offset (from the
   doc ``explode`` steps, scaled by ``factor``) is applied per component.

The assembly rebuild result mimics the part :class:`engine.doc.compiler.CompileResult` shape just
enough for the session layer (bodies list, per-body owner = component id, body metadata for the
ops-response). Component bodies are kept world-transformed for interference/BOM evaluation.

Canonical units: mm / degrees / grams.
"""

from __future__ import annotations

import math
import os
import colorsys
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from engine.doc.mate_solver import MateGeom, solve_mates, _transform_to_params, _params_to_transform


# --------------------------------------------------------------------------- component cache


@dataclass
class _LoadedComponent:
    """Cached load of a component source file (local-frame geometry)."""

    bodies: list                                   # build123d solids, LOCAL frame
    faces: list = field(default_factory=list)      # TopoInfo per face (across all bodies, local)
    edges: list = field(default_factory=list)      # TopoInfo per edge
    verts: list = field(default_factory=list)
    face_shapes: list = field(default_factory=list)  # the build123d Face objects (local), parallel
    edge_shapes: list = field(default_factory=list)
    vert_shapes: list = field(default_factory=list)


_LOAD_CACHE: dict[str, _LoadedComponent] = {}


def _resolve_source_path(path: str, base_dir: Optional[str]) -> str:
    """Resolve a (possibly assembly-relative) source path to an absolute filesystem path."""
    if os.path.isabs(path):
        return path
    if base_dir:
        cand = os.path.join(base_dir, path)
        if os.path.exists(cand):
            return cand
    # repo-relative fallback
    return os.path.abspath(path)


def load_component(path: str, base_dir: Optional[str] = None) -> _LoadedComponent:
    """Load a component source into local-frame bodies + a topology description, cached per mtime."""
    abspath = _resolve_source_path(path, base_dir)
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        mtime = 0.0
    key = f"{abspath}:{mtime}"
    cached = _LOAD_CACHE.get(key)
    if cached is not None:
        return cached

    bodies = _load_bodies(abspath)
    lc = _describe_component(bodies)
    _LOAD_CACHE[key] = lc
    return lc


def _load_bodies(abspath: str) -> list:
    """Return local-frame build123d solids for a component file."""
    lower = abspath.lower()
    if lower.endswith(".step+") or lower.endswith(".stepplus"):
        from engine.doc import stepplus
        from engine.doc.session import _rebuild, close_session
        sess, _warn = stepplus.open(abspath)
        result = sess.last_result or _rebuild(sess)
        bodies = [bo.shape for bo in result.bodies]
        # the component session is transient — close it so it doesn't linger in the registry.
        close_session(sess.doc_id)
        if not bodies:
            raise ValueError(f"component '{abspath}' produced no bodies")
        return bodies
    # bare formats
    from engine.doc.formats import import_path
    pairs = import_path(abspath)
    bodies = [s for _n, s in pairs if getattr(s, "wrapped", None) is not None]
    if not bodies:
        raise ValueError(f"component '{abspath}' produced no solid bodies")
    return bodies


def _describe_component(bodies: list) -> _LoadedComponent:
    """Describe every face/edge/vertex of the component's bodies (local frame) for mate resolution.

    The topo lists are flat across all bodies in body order; selectors resolve against the whole
    component (a component is one rigid unit, so cross-body ambiguity is acceptable for v1)."""
    from engine.doc import selectors as sel

    lc = _LoadedComponent(bodies=list(bodies))
    fidx = 0
    eidx = 0
    vidx = 0
    for bo in bodies:
        raw = bo.wrapped
        faces = sel.describe_faces(raw)
        for info in faces:
            info.index = fidx
            info.hint_index = fidx
            fidx += 1
        lc.faces.extend(faces)
        lc.face_shapes.extend(list(bo.faces()))

        edges = sel.describe_edges(raw)
        for info in edges:
            info.index = eidx
            info.hint_index = eidx
            eidx += 1
        lc.edges.extend(edges)
        lc.edge_shapes.extend(list(bo.edges()))

        verts = sel.describe_vertices(raw)
        for info in verts:
            info.index = vidx
            info.hint_index = vidx
            vidx += 1
        lc.verts.extend(verts)
        lc.vert_shapes.extend(list(bo.vertices()))
    return lc


# --------------------------------------------------------------------------- mate-ref geometry


def _bbox_diag(bodies: list) -> float:
    try:
        from build123d import Compound
        shape = bodies[0] if len(bodies) == 1 else Compound(children=list(bodies))
        bb = shape.bounding_box()
        d = math.sqrt((bb.max.X - bb.min.X) ** 2 + (bb.max.Y - bb.min.Y) ** 2
                      + (bb.max.Z - bb.min.Z) ** 2)
        return max(d, 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _selector_to_mategeom(selector: dict, lc: _LoadedComponent) -> Optional[MateGeom]:
    """Resolve a selector against a component's LOCAL geometry into a MateGeom (or None if dangling).

    Maps the resolved entity's surface/curve type to a mate-geometry primitive:
      planar face        -> plane (point=centroid, normal=axis)
      cylinder/cone face -> axis  (point=centroid, direction=axis, radius from the face)
      line edge          -> axis  (point=midpoint, direction=axis, radius=0)
      circle edge        -> axis  (point=centre, direction=circle axis, radius=circle radius)
      vertex             -> point
    """
    from engine.doc import selectors as sel
    kind = selector.get("kind")
    diag = _bbox_diag(lc.bodies)

    if kind == "face":
        rr = sel.resolve(selector, lc.faces, diag)
        if rr.index is None:
            return None
        info = lc.faces[rr.index]
        face_shape = lc.face_shapes[rr.index]
        if info.surf == "plane":
            return MateGeom("plane", point=info.centroid, normal=info.axis if info.axis is not None else [0, 0, 1])
        if info.surf in ("cylinder", "cone"):
            radius = _cyl_radius(face_shape)
            return MateGeom("axis", point=info.centroid,
                            direction=info.axis if info.axis is not None else [0, 0, 1],
                            radius=radius)
        if info.surf == "sphere":
            return MateGeom("sphere", point=info.centroid, radius=_sphere_radius(face_shape))
        # other surfaces: treat as a plane at the centroid with its (best) normal.
        return MateGeom("plane", point=info.centroid,
                        normal=info.axis if info.axis is not None else [0, 0, 1])

    if kind == "edge":
        rr = sel.resolve(selector, lc.edges, diag)
        if rr.index is None:
            return None
        info = lc.edges[rr.index]
        if info.surf == "circle":
            radius = _circle_radius(lc.edge_shapes[rr.index])
            # circle centre, not midpoint: use the circle's location.
            centre = _circle_centre(lc.edge_shapes[rr.index], info.centroid)
            return MateGeom("axis", point=centre,
                            direction=info.axis if info.axis is not None else [0, 0, 1],
                            radius=radius)
        # line / other: axis through midpoint
        return MateGeom("axis", point=info.centroid,
                        direction=info.axis if info.axis is not None else [0, 0, 1], radius=0.0)

    if kind == "vertex":
        rr = sel.resolve(selector, lc.verts, diag)
        if rr.index is None:
            return None
        info = lc.verts[rr.index]
        return MateGeom("point", point=info.centroid)

    return None


def _cyl_radius(face_shape) -> float:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Cone
        adaptor = BRepAdaptor_Surface(face_shape.wrapped)
        if adaptor.GetType() == GeomAbs_Cylinder:
            return float(adaptor.Cylinder().Radius())
        if adaptor.GetType() == GeomAbs_Cone:
            return float(adaptor.Cone().RefRadius())
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _sphere_radius(face_shape) -> float:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Sphere
        adaptor = BRepAdaptor_Surface(face_shape.wrapped)
        if adaptor.GetType() == GeomAbs_Sphere:
            return float(adaptor.Sphere().Radius())
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _circle_radius(edge_shape) -> float:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Circle
        adaptor = BRepAdaptor_Curve(edge_shape.wrapped)
        if adaptor.GetType() == GeomAbs_Circle:
            return float(adaptor.Circle().Radius())
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _circle_centre(edge_shape, fallback) -> np.ndarray:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Circle
        adaptor = BRepAdaptor_Curve(edge_shape.wrapped)
        if adaptor.GetType() == GeomAbs_Circle:
            c = adaptor.Circle().Location()
            return np.array([c.X(), c.Y(), c.Z()], dtype=float)
    except Exception:  # noqa: BLE001
        pass
    return np.asarray(fallback, dtype=float)


# --------------------------------------------------------------------------- assembly result


@dataclass
class AsmBodyOut:
    body_id: int
    component_id: str
    name: str
    shape_local: Any                 # local-frame compound of the component's bodies
    transform: list                  # solved 16-float row-major (world placement, no explode)
    color: list                      # rgb triple [0..1]


@dataclass
class AssemblyResult:
    bodies: list                                   # AsmBodyOut per (non-suppressed) component
    component_status: list = field(default_factory=list)   # [{id, ok, ms, error, dangling_refs}]
    mate_status: list = field(default_factory=list)        # [{id, type, status}]
    dof: int = 0
    over_defined: bool = False
    converged: bool = True
    dof_summary: str = ""
    transforms: dict = field(default_factory=dict)         # component_id -> 16f (solved)
    warm: dict = field(default_factory=dict)               # component_id -> (t, w) for next solve


# --------------------------------------------------------------------------- compile


def _component_color(ordinal: int) -> list:
    """A distinct, pleasant colour per component ordinal (golden-angle hue walk)."""
    h = (0.58 + 0.61803398875 * ordinal) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.45, 0.86)
    return [round(r, 4), round(g, 4), round(b, 4)]


def _compound(bodies: list):
    from build123d import Compound
    if len(bodies) == 1:
        return bodies[0]
    return Compound(children=list(bodies))


# --------------------------------------------------------------------------- component patterns
#
# Assembly component patterns (asm_linear_pattern / asm_circular_pattern) replicate one or more
# SEED components (already placed by the mate solve) along a direction (linear) or about an axis
# (circular), in the assembly WORLD frame. Each instance is a clone of the seed's local shape with
# an extra world-frame placement PRE-multiplied onto the seed's solved transform.
#
# Instances are NOT components: they have no entry in doc["components"], take no part in the mate
# solve, and are excluded from interference / BOM (which iterate doc["components"]). They carry
# synthetic body_id / component_id values that cannot collide with real component ordinals
# (body_id >= _PATTERN_BODY_BASE; component_id = "<seed>#<k>").


_PATTERN_BODY_BASE = 1_000_000  # synthetic instance body_ids start here (never a component ordinal)


def _feat_dim(params: dict, key: str, default: float) -> float:
    """Read a feature param that is a {value, expr} dim or a bare number; fall back to default."""
    v = params.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return float(default)


def _world_axis_from_ref(ref: Any) -> Optional[np.ndarray]:
    """Resolve a direction/axis ref to a unit WORLD vector (datum/world-axis strings, v1).

    Supported v1 forms (assembly world frame == datum frame):
      "axis.x" / "axis.y" / "axis.z"            -> the world axis
      "plane.front"/"plane.top"/"plane.right"   -> the plane NORMAL (front=XZ→Y, top=XY→Z, right=YZ→X)
      a 3-vector list/tuple                      -> taken as a raw direction (normalised)
    Model-edge / model-face picks are a follow-up (return None → caller reports it as dangling).
    """
    if ref is None:
        return None
    if isinstance(ref, (list, tuple)) and len(ref) == 3 \
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ref):
        v = np.asarray(ref, dtype=float)
    elif isinstance(ref, str):
        s = ref.strip().lower()
        axis_map = {"axis.x": [1, 0, 0], "axis.y": [0, 1, 0], "axis.z": [0, 0, 1],
                    "x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}
        # Datum-plane normals: Z-up convention (front=XZ plane → +Y, top=XY → +Z, right=YZ → +X).
        plane_normal = {"plane.front": [0, 1, 0], "plane.top": [0, 0, 1], "plane.right": [1, 0, 0],
                        "front": [0, 1, 0], "top": [0, 0, 1], "right": [1, 0, 0]}
        if s in axis_map:
            v = np.asarray(axis_map[s], dtype=float)
        elif s in plane_normal:
            v = np.asarray(plane_normal[s], dtype=float)
        else:
            return None
    else:
        return None
    n = np.linalg.norm(v)
    if n < 1e-9:
        return None
    return v / n


def _translate16(vec: np.ndarray) -> np.ndarray:
    """A 4x4 pure-translation matrix for a world vector."""
    M = np.eye(4)
    M[0, 3], M[1, 3], M[2, 3] = float(vec[0]), float(vec[1]), float(vec[2])
    return M


def _rotation16_about_axis(axis_pt: np.ndarray, axis_dir: np.ndarray, angle_deg: float) -> np.ndarray:
    """A 4x4 matrix rotating by angle_deg about the WORLD line (axis_pt, axis_dir)."""
    a = axis_dir / (np.linalg.norm(axis_dir) or 1.0)
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    x, y, z = float(a[0]), float(a[1]), float(a[2])
    # Rodrigues rotation matrix.
    Rm = np.array([
        [c + x * x * (1 - c),     x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
        [y * x * (1 - c) + z * s, c + y * y * (1 - c),     y * z * (1 - c) - x * s],
        [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
    ], dtype=float)
    M = np.eye(4)
    M[:3, :3] = Rm
    # Rotate about a point not the origin: T(p) · R · T(-p).
    p = np.asarray(axis_pt, dtype=float).reshape(3)
    M[:3, 3] = p - Rm @ p
    return M


def _xform16_to_mat(xform16: list) -> np.ndarray:
    return np.asarray(xform16, dtype=float).reshape(4, 4)


def _mat_to_xform16(M: np.ndarray) -> list:
    return [float(x) for x in M.reshape(16)]


def _compile_component_patterns(doc: dict, placed: list, color_base: int) -> list:
    """Expand asm_linear_pattern / asm_circular_pattern features into synthetic instance bodies.

    `placed` is the list of solved component :class:`AsmBodyOut` (indexed by component_id via a map
    we build here). Returns ONLY the new instance bodies (the caller appends them). Patterns with a
    dangling direction/axis or unknown seed components are silently skipped (no instances) — they
    surface via the feature_status the session layer reports, not here.
    """
    by_cid = {b.component_id: b for b in placed}
    instances: list[AsmBodyOut] = []
    body_id = _PATTERN_BODY_BASE
    color_ord = color_base

    for feat in doc.get("features", []) or []:
        if not isinstance(feat, dict) or feat.get("suppressed"):
            continue
        ftype = feat.get("type")
        if ftype not in ("asm_linear_pattern", "asm_circular_pattern"):
            continue
        refs = feat.get("refs", {}) or {}
        params = feat.get("params", {}) or {}
        seed_cids = [c for c in (refs.get("components") or []) if c in by_cid]
        if not seed_cids:
            continue

        # Build the list of additional instance transforms (k = 1..count-1; instance 0 = the seed,
        # already present). Each entry is a 4x4 world PRE-multiplier applied to the seed transform.
        offsets: list[np.ndarray] = []
        if ftype == "asm_linear_pattern":
            count1 = max(1, int(round(_feat_dim(params, "count1", 2))))
            spacing1 = _feat_dim(params, "spacing1", 10.0)
            d1 = _world_axis_from_ref(refs.get("dir1"))
            if d1 is None or count1 < 2:
                continue
            for k in range(1, count1):
                offsets.append(_translate16(d1 * (spacing1 * k)))
        else:  # asm_circular_pattern
            count = max(1, int(round(_feat_dim(params, "count", 4))))
            angle_total = _feat_dim(params, "angle", 360.0)
            equal = bool(params.get("equal_spacing", True))
            ax = _world_axis_from_ref(refs.get("axis"))
            if ax is None or count < 2:
                continue
            step = (angle_total / count) if equal else angle_total
            for k in range(1, count):
                offsets.append(_rotation16_about_axis(np.zeros(3), ax, step * k))

        for seed_cid in seed_cids:
            seed = by_cid[seed_cid]
            seed_mat = _xform16_to_mat(seed.transform)
            for k, off in enumerate(offsets, start=1):
                inst_mat = off @ seed_mat
                color_ord += 1
                instances.append(AsmBodyOut(
                    body_id=body_id,
                    component_id=f"{seed_cid}#{k}",
                    name=f"{seed.name}#{k}",
                    shape_local=seed.shape_local,
                    transform=_mat_to_xform16(inst_mat),
                    color=seed.color,
                ))
                body_id += 1
    return instances


def compile_assembly(doc: dict, base_dir: Optional[str] = None,
                     warm: Optional[dict] = None) -> AssemblyResult:
    """Compile an assembly: load components, resolve mate refs, solve mates, place bodies.

    Returns an :class:`AssemblyResult` whose ``transforms`` should be written back into the doc by
    the caller (so a save persists the solved pose). ``warm`` (component_id -> (t,w)) speeds the
    solve and stabilises the rotational branch across re-solves (e.g. drag)."""
    import time

    components = [c for c in doc.get("components", []) if not c.get("suppressed")]
    mates = doc.get("mates", [])

    # --- load every component -------------------------------------------
    loaded: dict[str, _LoadedComponent] = {}
    component_status: list[dict] = []
    for comp in components:
        cid = comp["id"]
        t0 = time.perf_counter()
        try:
            lc = load_component(comp["source"]["path"], base_dir)
            loaded[cid] = lc
            component_status.append({"id": cid, "ok": True,
                                     "ms": round((time.perf_counter() - t0) * 1000, 3),
                                     "error": None, "dangling_refs": []})
        except Exception as exc:  # noqa: BLE001
            component_status.append({"id": cid, "ok": False,
                                     "ms": round((time.perf_counter() - t0) * 1000, 3),
                                     "error": f"{type(exc).__name__}: {exc}",
                                     "dangling_refs": []})

    # --- resolve mate refs to local-frame MateGeom ----------------------
    resolved_geometry: dict = {}
    dangling: dict[str, list[str]] = {}
    for mate in mates:
        if mate.get("suppressed"):
            continue
        mid = mate["id"]
        for i, ref in enumerate(mate.get("refs", [])[:2]):
            cid = ref.get("component")
            lc = loaded.get(cid)
            sel_obj = ref.get("selector")
            if lc is None or not isinstance(sel_obj, dict):
                dangling.setdefault(mid, []).append(f"refs[{i}]")
                continue
            geom = _selector_to_mategeom(sel_obj, lc)
            if geom is None:
                dangling.setdefault(mid, []).append(f"refs[{i}]")
                continue
            resolved_geometry[(mid, i)] = geom

    # --- solve mates ----------------------------------------------------
    solve_components = [
        {"id": c["id"], "transform": c.get("transform") or _IDENT16,
         "fixed": bool(c.get("fixed", False)), "suppressed": False}
        for c in components if c["id"] in loaded
    ]
    solve_res = solve_mates(solve_components, mates, resolved_geometry, warm=warm)

    # --- build placed bodies --------------------------------------------
    bodies: list[AsmBodyOut] = []
    transforms: dict[str, list] = {}
    for ordinal, comp in enumerate(components, start=1):
        cid = comp["id"]
        lc = loaded.get(cid)
        if lc is None:
            continue
        xform = solve_res.transforms.get(cid, comp.get("transform") or _IDENT16)
        transforms[cid] = list(xform)
        bodies.append(AsmBodyOut(
            body_id=ordinal,
            component_id=cid,
            name=comp.get("name") or cid,
            shape_local=_compound(lc.bodies),
            transform=list(xform),
            color=_component_color(ordinal - 1),
        ))

    # --- component patterns ---------------------------------------------
    # Expand asm_linear_pattern / asm_circular_pattern into synthetic instance bodies (NOT
    # components: excluded from the mate solve + interference/BOM, which iterate doc["components"]).
    try:
        instances = _compile_component_patterns(doc, bodies, color_base=len(components))
        bodies.extend(instances)
    except Exception:  # noqa: BLE001 — a bad pattern must never sink the whole assembly rebuild
        pass

    # merge dangling info into component_status
    for st in component_status:
        # a component is flagged dangling if a mate referencing it dangled
        pass

    mate_status = []
    for m in solve_res.mate_status:
        st = m.status
        if m.mate_id in dangling:
            st = "unresolved"
        mate_status.append({"id": m.mate_id, "type": m.type, "status": st,
                            "dangling_refs": dangling.get(m.mate_id, [])})

    return AssemblyResult(
        bodies=bodies,
        component_status=component_status,
        mate_status=mate_status,
        dof=solve_res.dof,
        over_defined=solve_res.over_defined,
        converged=solve_res.converged,
        dof_summary=solve_res.summary,
        transforms=transforms,
        warm={cid: solve_res.params[cid] for cid in solve_res.params},
    )


_IDENT16 = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


# --------------------------------------------------------------------------- placement helpers


def transform_to_location(xform16: list):
    """A build123d Location from a 16-float row-major rigid transform."""
    from build123d import Location
    from OCP.gp import gp_Trsf
    M = np.asarray(xform16, dtype=float).reshape(4, 4)
    trsf = gp_Trsf()
    trsf.SetValues(*[float(M[i, j]) for i in range(3) for j in range(4)])
    return Location(trsf)


def placed_world_shape(body: AsmBodyOut, explode_offset: Optional[np.ndarray] = None):
    """Return the component's WORLD-placed build123d shape (solved transform + optional explode)."""
    loc = transform_to_location(body.transform)
    shape = body.shape_local.located(loc)
    if explode_offset is not None and np.any(np.abs(explode_offset) > 1e-9):
        from build123d import Pos
        shape = Pos(float(explode_offset[0]), float(explode_offset[1]),
                    float(explode_offset[2])) * shape
    return shape


def explode_offsets(doc: dict, factor: float) -> dict:
    """Per-component explode offset vector (mm) from the doc's ``explode`` steps, scaled by factor."""
    out: dict[str, np.ndarray] = {}
    for step in doc.get("explode", []) or []:
        cid = step.get("component")
        vec = np.asarray(step.get("vector", [0, 0, 0]), dtype=float).reshape(3)
        dist = float(step.get("distance", 0.0))
        n = np.linalg.norm(vec)
        unit = vec / n if n > 1e-9 else vec
        out[cid] = unit * dist * float(factor)
    return out
