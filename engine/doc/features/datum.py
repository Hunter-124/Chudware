"""Datum feature compilers: plane / axis / point / csys (feature_params.md §Datums).

Datums produce reference geometry stored in the compile context (build123d ``Plane`` / ``Axis``
/ ``Vector`` / ``Location``), never bodies. The reserved built-in datums (doc_schema.md §5) are
seeded by the compiler core via :func:`reserved_datums`; these compilers handle authored datum
features.

Two resolution paths are in play:

* datum-against-datum + numeric params — ``offset`` / ``midplane`` plane, ``two_points`` axis.
  These resolve via :meth:`CompileContext.datum` (reserved + upstream datum ids).
* geometry-selector dependent — ``angle`` / ``three_point`` plane, ``cylinder`` / ``edge`` /
  ``plane_intersect`` axis, every ``point`` method, ``csys`` x_dir/y_dir. These resolve a
  face/edge/vertex selector against the LAST body in ``ctx.bodies`` using the SAME machinery as
  ``hole.py`` (``describe_*`` + ``resolve`` from :mod:`engine.doc.selectors`).

Every method contains its failure: an unresolved selector raises a :class:`FeatureError` carrying
``dangling_refs``; an OCCT/geometry exception is wrapped, never allowed to crash the rebuild.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Plane, Vector

from .base import CompileContext, CompileOutput, Datum, FeatureError, register
from engine.doc.selectors import (
    ResolveResult,
    describe_edges,
    describe_faces,
    describe_vertices,
    resolve,
)


def reserved_datums() -> dict[str, Datum]:
    """The implicit per-document datums (doc_schema.md §5), in canonical Z-up geometry.

    plane.top  = Z=0, +Z   (build123d Plane.XY)
    plane.front= Y=0, -Y   (Plane.XZ — its z_dir is (0,-1,0))
    plane.right= X=0, +X   (Plane.YZ — its z_dir is (1,0,0))
    """

    return {
        "origin": Datum("point", Vector(0, 0, 0), "origin"),
        "plane.top": Datum("plane", Plane.XY, "plane.top"),
        "plane.front": Datum("plane", Plane.XZ, "plane.front"),
        "plane.right": Datum("plane", Plane.YZ, "plane.right"),
        "axis.x": Datum("axis", Axis.X, "axis.x"),
        "axis.y": Datum("axis", Axis.Y, "axis.y"),
        "axis.z": Datum("axis", Axis.Z, "axis.z"),
    }


# --------------------------------------------------------------------------- selector helpers


def _is_datum_ref(ref: Any) -> bool:
    """True for a reserved/feature-id string or a ``{"kind":"datum",...}`` selector."""
    return isinstance(ref, str) or (isinstance(ref, dict) and ref.get("kind") == "datum")


def _last_body(ctx: CompileContext, what: str):
    """The body geometry selectors resolve against (the running solid), or a clean error."""
    if not ctx.bodies:
        raise FeatureError(f"{what}: no body to resolve a geometry selector against")
    return ctx.bodies[-1]


def _bbox_diag(body) -> float:
    try:
        bb = body.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _describe_pool(kind: str, pool) -> list:
    """One ``TopoInfo`` per *deduplicated* topology entity, index-aligned to *pool*.

    A whole-shape ``describe_*`` walks raw ``TopExp`` occurrences, so a shared edge/vertex appears
    multiple times (a box has 24 edge-occurrences for 12 edges, 48 vertices for 8). Those duplicates
    score identically and dangle as "symmetric ambiguity". Describing each ``body.edges()`` /
    ``body.vertices()`` entity individually yields one info per pickable entity whose list index
    matches the pool, so ``resolve`` returns a usable index.
    """
    describer = {"face": describe_faces, "edge": describe_edges,
                 "vertex": describe_vertices}[kind]
    infos = []
    for i, ent in enumerate(pool):
        sub = describer(ent.wrapped)
        if not sub:
            continue
        info = sub[0]
        info.index = i
        info.hint_index = i
        infos.append(info)
    return infos


def _resolve_sel(sel: dict, body, kind: str, slot: str):
    """Resolve a face/edge/vertex selector to its build123d entity on *body*.

    *kind* selects the describer (``face`` / ``edge`` / ``vertex``); *slot* names the ref slot for
    the dangling message. Raises a contained :class:`FeatureError` with ``dangling_refs`` when the
    selector does not resolve.
    """
    if not isinstance(sel, dict):
        raise FeatureError(f"{slot}: expected a {kind} selector, got {sel!r}")
    pool = {"face": body.faces, "edge": body.edges, "vertex": body.vertices}[kind]()
    infos = _describe_pool(kind, pool)

    rr: ResolveResult = resolve(sel, infos, _bbox_diag(body))
    if rr.index is None:
        raise FeatureError(
            f"{slot}: {kind} selector dangling — {rr.dangling_reason}",
            dangling_refs=[f"{slot}: {rr.dangling_reason}"],
        )
    return pool[rr.index]


def _vertex_point(sel: Any, ctx: CompileContext, body, slot: str) -> Vector:
    """A 3-D point from a vertex selector OR a point/vertex datum ref."""
    if _is_datum_ref(sel):
        d = ctx.datum(sel)
        if d.kind != "point":
            raise FeatureError(f"{slot}: datum '{sel}' has kind '{d.kind}', expected a point")
        return Vector(d.obj)
    v = _resolve_sel(sel, body, "vertex", slot)
    return Vector(v.X, v.Y, v.Z)


def _face_axis(face, slot: str) -> tuple[Vector, Vector]:
    """Axis (location, direction) of a cylindrical or conical face."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Cone

    ad = BRepAdaptor_Surface(face.wrapped)
    st = ad.GetType()
    if st == GeomAbs_Cylinder:
        ax = ad.Cylinder().Axis()
    elif st == GeomAbs_Cone:
        ax = ad.Cone().Axis()
    else:
        raise FeatureError(f"{slot}: face is not cylindrical/conical (axis undefined)")
    loc, dr = ax.Location(), ax.Direction()
    return Vector(loc.X(), loc.Y(), loc.Z()), Vector(dr.X(), dr.Y(), dr.Z())


def _edge_line_dir(edge, slot: str) -> tuple[Vector, Vector]:
    """(point, unit direction) of a straight model edge."""
    try:
        p0 = edge.start_point()
        p1 = edge.end_point()
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"{slot}: cannot read edge endpoints ({exc})")
    d = Vector(p1.X - p0.X, p1.Y - p0.Y, p1.Z - p0.Z)
    if d.length < 1e-9:
        raise FeatureError(f"{slot}: degenerate (zero-length) edge")
    return Vector(p0), d


def _plane_of(ref: Any, ctx: CompileContext, body, slot: str) -> Plane:
    """Resolve a planar datum OR a planar face selector to a build123d ``Plane``."""
    if _is_datum_ref(ref):
        d = ctx.datum(ref)
        if d.kind != "plane":
            raise FeatureError(f"{slot}: datum '{ref}' has kind '{d.kind}', expected a plane")
        return d.obj
    face = _resolve_sel(ref, body, "face", slot)
    try:
        return Plane(face)
    except Exception:  # noqa: BLE001
        c = face.center()
        try:
            n = face.normal_at()
        except Exception:  # noqa: BLE001
            raise FeatureError(f"{slot}: face is not planar")
        return Plane(origin=c, z_dir=n)


def _rotate_plane_about_axis(plane: Plane, point: Vector, direction: Vector,
                             angle_deg: float) -> Plane:
    """A copy of *plane* rotated *angle_deg* about the hinge axis (point, direction)."""
    from OCP.gp import gp_Trsf, gp_Ax1, gp_Pnt, gp_Dir, gp_Vec

    trsf = gp_Trsf()
    trsf.SetRotation(
        gp_Ax1(gp_Pnt(point.X, point.Y, point.Z),
               gp_Dir(direction.X, direction.Y, direction.Z)),
        math.radians(angle_deg))
    o = gp_Pnt(plane.origin.X, plane.origin.Y, plane.origin.Z)
    o.Transform(trsf)
    xv = gp_Vec(plane.x_dir.X, plane.x_dir.Y, plane.x_dir.Z)
    xv.Transform(trsf)
    zv = gp_Vec(plane.z_dir.X, plane.z_dir.Y, plane.z_dir.Z)
    zv.Transform(trsf)
    return Plane(origin=Vector(o.X(), o.Y(), o.Z()),
                 x_dir=Vector(xv.X(), xv.Y(), xv.Z()),
                 z_dir=Vector(zv.X(), zv.Y(), zv.Z()))


# --------------------------------------------------------------------------- plane


@register("plane")
def compile_plane(feat: dict, ctx: CompileContext) -> CompileOutput:
    params = feat.get("params", {})
    refs = feat.get("refs", {})
    method = params.get("method", "offset")
    fid = feat["id"]

    if method == "offset":
        base = ctx.datum(refs.get("base", "plane.top"))
        if base.kind != "plane":
            raise FeatureError(f"plane offset needs a planar base, got {base.kind}")
        offset = ctx.dim(params, "offset", 10.0)
        if params.get("flip"):
            offset = -offset
        plane = base.obj.offset(offset)
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("plane", plane, fid),
                             resolved={"offset": offset})

    if method == "midplane":
        body = ctx.bodies[-1] if ctx.bodies else None
        a = _plane_of(refs.get("base", "plane.top"), ctx, body, "base")
        b = _plane_of(refs.get("second", "plane.top"), ctx, body, "second")
        mid_o = (Vector(a.origin) + Vector(b.origin)) * 0.5
        plane = Plane(origin=mid_o, z_dir=a.z_dir)
        return CompileOutput(bodies=list(ctx.bodies), datum=Datum("plane", plane, fid))

    if method == "angle":
        body = _last_body(ctx, "plane angle")
        base = _plane_of(refs.get("base", "plane.top"), ctx, body, "base")
        hinge = refs.get("second")
        if hinge is None:
            raise FeatureError("plane angle: refs.second (a hinge edge) is required")
        # The hinge is an edge — a model edge selector or a datum axis.
        if _is_datum_ref(hinge):
            d = ctx.datum(hinge)
            if d.kind != "axis":
                raise FeatureError("plane angle: datum hinge must be an axis")
            ax = d.obj
            point = Vector(ax.position)
            direction = Vector(ax.direction)
        else:
            edge = _resolve_sel(hinge, body, "edge", "second")
            point, direction = _edge_line_dir(edge, "second")
        angle = ctx.dim(params, "angle", 45.0)
        if params.get("flip"):
            angle = -angle
        try:
            plane = _rotate_plane_about_axis(base, point, direction, angle)
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"plane angle: rotation failed: {exc}")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("plane", plane, fid),
                             resolved={"angle": angle})

    if method == "three_point":
        body = _last_body(ctx, "plane three_point")
        pts_ref = refs.get("points")
        if not isinstance(pts_ref, list) or len(pts_ref) < 3:
            raise FeatureError("plane three_point: refs.points must be three vertex selectors")
        pts = [_vertex_point(pts_ref[i], ctx, body, f"points[{i}]") for i in range(3)]
        v1 = pts[1] - pts[0]
        v2 = pts[2] - pts[0]
        n = v1.cross(v2)
        if n.length < 1e-9 or v1.length < 1e-9 or v2.length < 1e-9:
            raise FeatureError("plane three_point: the three points are colinear (no unique plane)")
        try:
            plane = Plane(origin=pts[0], x_dir=v1, z_dir=n)
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"plane three_point: failed to build plane: {exc}")
        return CompileOutput(bodies=list(ctx.bodies), datum=Datum("plane", plane, fid))

    raise FeatureError(f"plane: unknown method '{method}'")


# --------------------------------------------------------------------------- axis


@register("axis")
def compile_axis(feat: dict, ctx: CompileContext) -> CompileOutput:
    params = feat.get("params", {})
    refs = feat.get("refs", {})
    method = params.get("method", "cylinder")
    fid = feat["id"]

    if method == "two_points":
        body = ctx.bodies[-1] if ctx.bodies else None
        pa = _vertex_point(refs.get("a"), ctx, body, "a")
        pb = _vertex_point(refs.get("b"), ctx, body, "b")
        d = pb - pa
        if d.length < 1e-9:
            raise FeatureError("axis two_points: coincident points")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("axis", Axis(pa, d), fid))

    if method == "cylinder":
        body = _last_body(ctx, "axis cylinder")
        a = refs.get("a")
        if a is None:
            raise FeatureError("axis cylinder: refs.a (a cylindrical/conical face) is required")
        if _is_datum_ref(a):
            d = ctx.datum(a)
            if d.kind != "axis":
                raise FeatureError("axis cylinder: datum ref.a must be an axis")
            return CompileOutput(bodies=list(ctx.bodies), datum=Datum("axis", d.obj, fid))
        face = _resolve_sel(a, body, "face", "a")
        loc, direction = _face_axis(face, "a")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("axis", Axis(loc, direction), fid))

    if method == "edge":
        body = _last_body(ctx, "axis edge")
        a = refs.get("a")
        if a is None:
            raise FeatureError("axis edge: refs.a (a linear model edge) is required")
        if _is_datum_ref(a):
            d = ctx.datum(a)
            if d.kind != "axis":
                raise FeatureError("axis edge: datum ref.a must be an axis")
            return CompileOutput(bodies=list(ctx.bodies), datum=Datum("axis", d.obj, fid))
        edge = _resolve_sel(a, body, "edge", "a")
        point, direction = _edge_line_dir(edge, "a")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("axis", Axis(point, direction), fid))

    if method == "plane_intersect":
        body = ctx.bodies[-1] if ctx.bodies else None
        pa = _plane_of(refs.get("a"), ctx, body, "a")
        pb = _plane_of(refs.get("b"), ctx, body, "b")
        na, nb = Vector(pa.z_dir), Vector(pb.z_dir)
        direction = na.cross(nb)
        if direction.length < 1e-9:
            raise FeatureError("axis plane_intersect: planes are parallel (no intersection line)")
        # A point on both planes: solve for the line that lies in both. Use the linear system
        # [na; nb; direction] · x = [na·oa; nb·ob; 0].
        try:
            import numpy as np
            oa, ob = Vector(pa.origin), Vector(pb.origin)
            A = np.array([[na.X, na.Y, na.Z],
                          [nb.X, nb.Y, nb.Z],
                          [direction.X, direction.Y, direction.Z]], dtype=float)
            rhs = np.array([na.dot(oa), nb.dot(ob), 0.0], dtype=float)
            sol = np.linalg.solve(A, rhs)
            point = Vector(float(sol[0]), float(sol[1]), float(sol[2]))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"axis plane_intersect: could not locate intersection line ({exc})")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("axis", Axis(point, direction), fid))

    raise FeatureError(f"axis: unknown method '{method}'")


# --------------------------------------------------------------------------- point


@register("point")
def compile_point(feat: dict, ctx: CompileContext) -> CompileOutput:
    params = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat["id"]
    method = params.get("method", "vertex")
    target = refs.get("target")
    if target is None:
        raise FeatureError("point: refs.target is required")

    # A point/vertex datum target resolves directly regardless of method.
    if _is_datum_ref(target):
        d = ctx.datum(target)
        if d.kind == "point":
            return CompileOutput(bodies=list(ctx.bodies),
                                 datum=Datum("point", Vector(d.obj), fid))
        raise FeatureError(f"point: datum target '{target}' has kind '{d.kind}', not a point")

    body = _last_body(ctx, "point")

    if method == "vertex":
        v = _resolve_sel(target, body, "vertex", "target")
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("point", Vector(v.X, v.Y, v.Z), fid))

    if method == "center":
        # Center of a circular edge, or the center of a spherical/cylindrical face.
        kind = target.get("kind") if isinstance(target, dict) else None
        if kind == "edge":
            edge = _resolve_sel(target, body, "edge", "target")
            try:
                c = edge.arc_center
            except Exception as exc:  # noqa: BLE001
                raise FeatureError(f"point center: edge has no center (not circular): {exc}")
            return CompileOutput(bodies=list(ctx.bodies),
                                 datum=Datum("point", Vector(c), fid))
        if kind == "face":
            face = _resolve_sel(target, body, "face", "target")
            c = face.center()
            return CompileOutput(bodies=list(ctx.bodies),
                                 datum=Datum("point", Vector(c.X, c.Y, c.Z), fid))
        raise FeatureError("point center: target must be a circular edge or a face")

    if method == "on_face":
        face = _resolve_sel(target, body, "face", "target")
        uv = params.get("uv")
        try:
            plane = Plane(face)
        except Exception:  # noqa: BLE001
            c = face.center()
            return CompileOutput(bodies=list(ctx.bodies),
                                 datum=Datum("point", Vector(c.X, c.Y, c.Z), fid))
        if isinstance(uv, (list, tuple)) and len(uv) >= 2:
            try:
                u, v = float(uv[0]), float(uv[1])
            except (TypeError, ValueError):
                raise FeatureError("point on_face: uv must be two numbers")
            p = plane.from_local_coords((u, v, 0.0))
        else:
            p = Vector(face.center())
        return CompileOutput(bodies=list(ctx.bodies),
                             datum=Datum("point", Vector(p.X, p.Y, p.Z), fid))

    raise FeatureError(f"point: unknown method '{method}'")


# --------------------------------------------------------------------------- csys


def _orthonormal_frame(x_dir: Vector, y_hint: Vector | None) -> tuple[Vector, Vector, Vector]:
    """Gram-Schmidt an orthonormal (x, y, z) frame from x_dir + a y hint.

    x is normalised x_dir; y is the y hint made orthogonal to x; z = x × y. When the hint is
    absent or parallel to x, a stable arbitrary perpendicular is chosen.
    """
    x = x_dir.normalized()
    y: Vector | None = None
    if y_hint is not None and y_hint.length > 1e-9:
        proj = y_hint - x * x.dot(y_hint)
        if proj.length >= 1e-9:
            y = proj.normalized()
    if y is None:
        # arbitrary perpendicular to x (stable: avoid the near-parallel reference)
        ref = Vector(0, 0, 1) if abs(x.Z) < 0.9 else Vector(1, 0, 0)
        y = (ref - x * x.dot(ref)).normalized()
    z = x.cross(y).normalized()
    return x, y, z


@register("csys")
def compile_csys(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat["id"]
    body = ctx.bodies[-1] if ctx.bodies else None

    origin = refs.get("origin", "origin")
    o = _vertex_point(origin, ctx, body, "origin")

    # x_dir / y_dir: optional edge selectors or datum axes. Each yields a direction.
    def _dir_from(ref: Any, slot: str) -> Vector | None:
        if ref is None:
            return None
        if _is_datum_ref(ref):
            d = ctx.datum(ref)
            if d.kind == "axis":
                return Vector(d.obj.direction)
            raise FeatureError(f"csys {slot}: datum must be an axis")
        edge = _resolve_sel(ref, body, "edge", slot)
        _, direction = _edge_line_dir(edge, slot)
        return direction

    x_dir = _dir_from(refs.get("x_dir"), "x_dir")
    y_dir = _dir_from(refs.get("y_dir"), "y_dir")

    if x_dir is None:
        # No directions picked: axis-aligned frame at the origin.
        plane = Plane(origin=o)
    else:
        x, y, z = _orthonormal_frame(x_dir, y_dir)
        plane = Plane(origin=o, x_dir=x, z_dir=z)

    return CompileOutput(bodies=list(ctx.bodies), datum=Datum("csys", plane, fid))
