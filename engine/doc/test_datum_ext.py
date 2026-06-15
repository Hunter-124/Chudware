"""Extended datum reference-geometry methods — SW-parity plane / axis / point constructions.

Run:  & $py -m pytest engine/doc/test_datum_ext.py -q   (from repo root)

Companion to ``test_datum_methods.py`` (which covers the original selector-dependent datum methods).
This file exercises the SolidWorks-parity additions (docs/SW_PARITY_SPEC.md §2 — Reference
Plane/Axis/Point):

  Plane:  parallel_point  (parallel to a base plane, through a point)
          normal_to_curve (plane whose normal is a curve tangent at a point)
          on_surface      (tangent to a surface at a point)
  Axis:   on_surface      (a surface's revolve/extrude direction, or two on-surface points)
  Point:  on_curve        (at a parameter / fraction along a curve)
          intersection    (curve-curve / curve-surface)
          centroid        (of a face, or of the whole body)
          coordinate      (explicit XYZ)

Unlike ``test_datum_methods.py`` (which only asserts ``ok``), these tests assert the resulting
datum's *geometry* — plane origin + normal, axis direction + position, point coordinates. The
compiler core does not surface the datum table on its result, so each test builds a
``CompileContext`` and calls the datum compiler directly (the exact callable the core invokes),
reading ``CompileOutput.datum.obj`` (a build123d ``Plane`` / ``Axis`` / ``Vector``). A handful of
tests ALSO run the full ``compile_document`` pipeline to prove the feature validates and rebuilds
clean end-to-end.

Selector fixtures are built the same way as ``test_datum_methods.py``: compile a primitive,
``describe_*`` its topology, ``make_selector`` the entity of interest, tag its ``owner_feature``.
"""

from __future__ import annotations

import math

import numpy as np

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.features.base import CompileContext, FeatureError
from engine.doc.features.datum import (
    compile_axis,
    compile_plane,
    compile_point,
    reserved_datums,
)
from engine.doc.schema import validate_part
from engine.doc.selectors import (
    describe_edges,
    describe_faces,
    describe_vertices,
    make_selector,
)

_dim = T._dim
_feat = T._feat
_doc = T._doc
_status = T._status


# --------------------------------------------------------------------------- fixtures


def _box_shape(size: float = 20.0):
    """Compile a centered box and return its build123d shape for topology description."""
    doc = _doc([_feat("f1", "box", {"length": _dim(size), "width": _dim(size),
                                     "height": _dim(size)})])
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    return res.bodies[0].shape


def _cyl_shape(radius: float = 8.0, height: float = 30.0):
    doc = _doc([_feat("f1", "cylinder", {"radius": _dim(radius), "height": _dim(height)})])
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    return res.bodies[0].shape


def _ctx(shape=None) -> CompileContext:
    """A compile context seeded with the reserved datums and (optionally) one working body.

    The body the datum compilers resolve geometry selectors against is ``ctx.bodies[-1]`` — the
    raw build123d shape (NOT the ``BodyOut`` wrapper the core's result exposes), matching what the
    compiler core threads in.
    """
    return CompileContext(
        equations_env={},
        bodies=[shape] if shape is not None else [],
        datums=reserved_datums(),
        _resolved_params={},
    )


def _sel(info, owner: str = "f1") -> dict:
    s = make_selector(info)
    s["owner_feature"] = owner
    return s


def _vertex_sel_near(shape, target, owner: str = "f1") -> dict:
    """Vertex selector nearest a target xyz (deterministic corner pick)."""
    infos = describe_vertices(shape.wrapped)
    tgt = np.asarray(target, dtype=float)
    best = min(infos, key=lambda i: float(np.linalg.norm(i.centroid - tgt)))
    return _sel(best, owner)


def _top_face_sel(shape, owner: str = "f1") -> dict:
    """Selector for the +Z planar top face of a centered box."""
    infos = describe_faces(shape.wrapped)
    top = max((i for i in infos if i.surf == "plane" and i.axis is not None
               and abs(i.axis[2]) > 0.9), key=lambda i: i.centroid[2])
    return _sel(top, owner)


def _vertical_edge_sel_near(shape, xy, owner: str = "f1") -> dict:
    """The vertical (Z) box edge whose XY footprint is nearest ``xy``."""
    infos = describe_edges(shape.wrapped)
    vert = [i for i in infos if i.surf == "line" and i.axis is not None and abs(i.axis[2]) > 0.9]
    assert vert, "need a vertical edge"
    tgt = np.asarray(xy, dtype=float)
    best = min(vert, key=lambda i: float(np.linalg.norm(i.centroid[:2] - tgt)))
    return _sel(best, owner)


def _bottom_edge_sel(shape, axis_idx: int, fixed, owner: str = "f1") -> dict:
    """A bottom (z=-10 on a size-20 box) horizontal edge running along ``axis_idx`` (0=X,1=Y).

    ``fixed`` is the (x or y) value the OTHER horizontal coordinate is pinned to, so the two edges
    selected for an intersection test are the two that actually share a corner.
    """
    infos = describe_edges(shape.wrapped)
    other = 1 - axis_idx
    cands = [i for i in infos if i.surf == "line" and i.axis is not None
             and abs(i.axis[axis_idx]) > 0.9 and i.centroid[2] < -5.0
             and abs(i.centroid[other] - fixed) < 0.1]
    assert cands, f"need a bottom edge along axis {axis_idx} at {other}={fixed}"
    return _sel(cands[0], owner)


def _v(vec) -> np.ndarray:
    return np.array([vec.X, vec.Y, vec.Z], dtype=float)


# =========================================================================== PLANE


def test_plane_parallel_point() -> None:
    """A plane PARALLEL to plane.top through the box top corner (10,10,10): same +Z normal, but
    its origin sits on the picked corner (so it floats at z=10)."""
    shape = _box_shape(20.0)
    thru = _vertex_sel_near(shape, (10, 10, 10))
    out = compile_plane(
        {"id": "f2", "type": "plane", "params": {"method": "parallel_point"},
         "refs": {"base": "plane.top", "points": [thru]}}, _ctx(shape))
    pl = out.datum.obj
    assert np.allclose(_v(pl.origin), [10, 10, 10], atol=1e-6), f"origin {_v(pl.origin)}"
    assert np.allclose(_v(pl.z_dir), [0, 0, 1], atol=1e-6), f"normal {_v(pl.z_dir)}"
    print(f"[plane-parallel_point] origin={_v(pl.origin)} normal={_v(pl.z_dir)}")


def test_plane_parallel_point_validates_and_compiles() -> None:
    """End-to-end: parallel_point plane validates and rebuilds ok through compile_document."""
    shape = _box_shape(20.0)
    thru = _vertex_sel_near(shape, (-10, -10, -10))
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "parallel_point"},
              refs={"base": "plane.top", "points": [thru]}),
    ])
    assert validate_part(doc) == [], "parallel_point doc must validate"
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"parallel_point compile failed: {s['error']}"
    print("[plane-parallel_point-e2e] validate + compile ok")


def test_plane_parallel_point_requires_point() -> None:
    """No through-point -> contained FeatureError (never a crash)."""
    shape = _box_shape(20.0)
    try:
        compile_plane(
            {"id": "f2", "type": "plane", "params": {"method": "parallel_point"},
             "refs": {"base": "plane.top"}}, _ctx(shape))
        raise AssertionError("expected a FeatureError for the missing through-point")
    except FeatureError as exc:
        assert "points[0]" in str(exc), f"unexpected message: {exc}"
    print("[plane-parallel_point-err] missing point is a contained error")


def test_plane_normal_to_curve_midpoint() -> None:
    """A plane NORMAL to a vertical box edge at its midpoint: the edge runs +Z so the plane normal
    is +Z and the origin is the edge midpoint (z=0 on a centered box)."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (-10, -10))
    out = compile_plane(
        {"id": "f2", "type": "plane",
         "params": {"method": "normal_to_curve", "fraction": _dim(0.5)},
         "refs": {"second": edge}}, _ctx(shape))
    pl = out.datum.obj
    assert np.allclose(_v(pl.origin), [-10, -10, 0], atol=1e-6), f"origin {_v(pl.origin)}"
    # Normal is the curve tangent (±Z) — assert it's the Z axis up to sign.
    assert abs(abs(pl.z_dir.Z) - 1.0) < 1e-6 and abs(pl.z_dir.X) < 1e-6 and abs(pl.z_dir.Y) < 1e-6, \
        f"normal {_v(pl.z_dir)} should be the Z axis"
    print(f"[plane-normal_to_curve] origin={_v(pl.origin)} normal={_v(pl.z_dir)}")


def test_plane_normal_to_curve_endpoints() -> None:
    """fraction 0 vs 1 land the plane origin at opposite ends of the edge (z=-10 vs z=+10)."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (10, 10))
    out0 = compile_plane(
        {"id": "f2", "type": "plane",
         "params": {"method": "normal_to_curve", "fraction": _dim(0.0)},
         "refs": {"second": edge}}, _ctx(shape))
    out1 = compile_plane(
        {"id": "f3", "type": "plane",
         "params": {"method": "normal_to_curve", "fraction": _dim(1.0)},
         "refs": {"second": edge}}, _ctx(shape))
    z0 = out0.datum.obj.origin.Z
    z1 = out1.datum.obj.origin.Z
    assert abs(abs(z0) - 10.0) < 1e-6 and abs(abs(z1) - 10.0) < 1e-6, f"z0={z0} z1={z1}"
    assert abs(z0 - z1) > 19.0, f"endpoints should be ~20 mm apart; got |{z0}-{z1}|"
    print(f"[plane-normal_to_curve-ends] z(fr=0)={z0:.1f} z(fr=1)={z1:.1f}")


def test_plane_on_surface_planar_face() -> None:
    """Plane TANGENT to a box's planar top face: z_dir = the face normal (+Z), origin = the
    projected face centroid (0,0,10). For a planar face the tangent plane IS the face plane."""
    shape = _box_shape(20.0)
    face = _top_face_sel(shape)
    out = compile_plane(
        {"id": "f2", "type": "plane", "params": {"method": "on_surface"},
         "refs": {"base": face}}, _ctx(shape))
    pl = out.datum.obj
    assert np.allclose(_v(pl.origin), [0, 0, 10], atol=1e-6), f"origin {_v(pl.origin)}"
    assert np.allclose(_v(pl.z_dir), [0, 0, 1], atol=1e-6), f"normal {_v(pl.z_dir)}"
    print(f"[plane-on_surface-planar] origin={_v(pl.origin)} normal={_v(pl.z_dir)}")


def test_plane_on_surface_cylinder_is_radial_tangent() -> None:
    """Plane TANGENT to a cylinder's side face at a rim point (8,0,15): the origin lands on the
    surface (radius 8 from the axis) and the normal is RADIAL (+X here) — a true tangent plane,
    distinct from the planar case."""
    shape = _cyl_shape(8.0, 30.0)
    side = next(i for i in describe_faces(shape.wrapped) if i.surf == "cylinder")
    rim = _vertex_sel_near(shape, (8, 0, 0))
    out = compile_plane(
        {"id": "f2", "type": "plane", "params": {"method": "on_surface"},
         "refs": {"base": _sel(side), "points": [rim]}}, _ctx(shape))
    pl = out.datum.obj
    o, n = _v(pl.origin), _v(pl.z_dir)
    assert abs(math.hypot(o[0], o[1]) - 8.0) < 1e-4, f"origin should be on the r=8 surface: {o}"
    # Normal is radial: parallel to the (x,y) of the origin, i.e. the outward direction.
    radial = np.array([o[0], o[1], 0.0])
    radial /= np.linalg.norm(radial)
    assert abs(abs(float(np.dot(n, np.r_[radial[:2], 0.0]))) - 1.0) < 1e-4, \
        f"normal {n} should be radial {radial}"
    assert abs(n[2]) < 1e-4, "tangent-to-cylinder normal has no Z component"
    print(f"[plane-on_surface-cyl] origin={o} normal={n} (radial tangent)")


# =========================================================================== AXIS


def test_axis_on_surface_planar_face_normal() -> None:
    """Axis from a PLANAR face's normal (the extrude/pull direction): box top face -> +Z axis
    through the face centroid (0,0,10)."""
    shape = _box_shape(20.0)
    face = _top_face_sel(shape)
    out = compile_axis(
        {"id": "f2", "type": "axis", "params": {"method": "on_surface"},
         "refs": {"a": face}}, _ctx(shape))
    ax = out.datum.obj
    assert np.allclose(_v(ax.direction), [0, 0, 1], atol=1e-6), f"dir {_v(ax.direction)}"
    assert abs(ax.position.Z - 10.0) < 1e-6, f"axis should pass through z=10; got {ax.position.Z}"
    print(f"[axis-on_surface-planar] dir={_v(ax.direction)} z={ax.position.Z}")


def test_axis_on_surface_cylinder_revolve_axis() -> None:
    """Axis from a CYLINDRICAL face = the cylinder's revolve axis (the Z axis for an upright
    cylinder)."""
    shape = _cyl_shape(8.0, 30.0)
    side = next(i for i in describe_faces(shape.wrapped) if i.surf == "cylinder")
    out = compile_axis(
        {"id": "f2", "type": "axis", "params": {"method": "on_surface"},
         "refs": {"a": _sel(side)}}, _ctx(shape))
    ax = out.datum.obj
    assert abs(abs(ax.direction.Z) - 1.0) < 1e-6, f"revolve axis should be Z; got {_v(ax.direction)}"
    assert math.hypot(ax.position.X, ax.position.Y) < 1e-6, \
        f"axis should pass through the cylinder centerline; got {_v(ax.position)}"
    print(f"[axis-on_surface-cyl] dir={_v(ax.direction)} pos_xy=({ax.position.X},{ax.position.Y})")


def test_axis_on_surface_two_points() -> None:
    """Axis through two on-surface points: two bottom corners (-10,-10,-10) -> (10,-10,-10)
    gives the +X axis at z=-10."""
    shape = _box_shape(20.0)
    pa = _vertex_sel_near(shape, (-10, -10, -10))
    pb = _vertex_sel_near(shape, (10, -10, -10))
    out = compile_axis(
        {"id": "f2", "type": "axis", "params": {"method": "on_surface"},
         "refs": {"a": pa, "b": pb}}, _ctx(shape))
    ax = out.datum.obj
    assert np.allclose(_v(ax.direction), [1, 0, 0], atol=1e-6), f"dir {_v(ax.direction)}"
    assert np.allclose(_v(ax.position), [-10, -10, -10], atol=1e-6), f"pos {_v(ax.position)}"
    print(f"[axis-on_surface-2pt] dir={_v(ax.direction)} pos={_v(ax.position)}")


def test_axis_on_surface_validates_and_compiles() -> None:
    """End-to-end through compile_document: an axis on_surface from a cylinder face validates and
    rebuilds clean."""
    shape = _cyl_shape(8.0, 30.0)
    side = next(i for i in describe_faces(shape.wrapped) if i.surf == "cylinder")
    doc = _doc([
        _feat("f1", "cylinder", {"radius": _dim(8), "height": _dim(30)}),
        _feat("f2", "axis", {"method": "on_surface"}, refs={"a": _sel(side)}),
    ])
    assert validate_part(doc) == [], "axis on_surface doc must validate"
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"axis on_surface compile failed: {s['error']}"
    print("[axis-on_surface-e2e] validate + compile ok")


# =========================================================================== POINT


def test_point_on_curve_fractions() -> None:
    """Points at fraction 0 / 0.5 / 1 along a vertical box edge land at z=-10 / 0 / +10."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (-10, -10))
    zs = []
    for fr in (0.0, 0.5, 1.0):
        out = compile_point(
            {"id": "f2", "type": "point",
             "params": {"method": "on_curve", "fraction": _dim(fr)},
             "refs": {"target": edge}}, _ctx(shape))
        p = out.datum.obj
        assert np.allclose([p.X, p.Y], [-10, -10], atol=1e-6), f"xy off the edge: {_v(p)}"
        zs.append(p.Z)
    assert np.allclose(sorted(zs), [-10, 0, 10], atol=1e-6), f"fractional z's {zs}"
    print(f"[point-on_curve] fraction 0/0.5/1 -> z={sorted(zs)}")


def test_point_on_curve_default_is_midpoint() -> None:
    """on_curve with no fraction param defaults to the curve midpoint (z=0 here)."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (10, 10))
    out = compile_point(
        {"id": "f2", "type": "point", "params": {"method": "on_curve"},
         "refs": {"target": edge}}, _ctx(shape))
    p = out.datum.obj
    assert abs(p.Z) < 1e-6, f"default should be the midpoint z=0; got {p.Z}"
    print(f"[point-on_curve-default] midpoint z={p.Z}")


def test_point_intersection_curve_curve() -> None:
    """Intersection of two box bottom edges that share the corner (-10,-10,-10)."""
    shape = _box_shape(20.0)
    e_along_x = _bottom_edge_sel(shape, axis_idx=0, fixed=-10.0)   # runs +X at y=-10
    e_along_y = _bottom_edge_sel(shape, axis_idx=1, fixed=-10.0)   # runs +Y at x=-10
    out = compile_point(
        {"id": "f2", "type": "point",
         "params": {"method": "intersection", "other": e_along_y},
         "refs": {"target": e_along_x}}, _ctx(shape))
    p = out.datum.obj
    assert np.allclose(_v(p), [-10, -10, -10], atol=1e-4), f"intersection {_v(p)}"
    print(f"[point-intersection-cc] {_v(p)}")


def test_point_intersection_curve_surface() -> None:
    """Intersection of a vertical box edge with the top face: the top corner the edge ends at."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (10, 10))
    top = _top_face_sel(shape)
    out = compile_point(
        {"id": "f2", "type": "point",
         "params": {"method": "intersection", "other": top},
         "refs": {"target": edge}}, _ctx(shape))
    p = out.datum.obj
    assert np.allclose(_v(p), [10, 10, 10], atol=1e-4), f"intersection {_v(p)}"
    print(f"[point-intersection-cs] {_v(p)}")


def test_point_intersection_non_touching_errors() -> None:
    """Two parallel, non-touching bottom edges -> contained 'do not intersect' error."""
    shape = _box_shape(20.0)
    e_y_neg = _bottom_edge_sel(shape, axis_idx=0, fixed=-10.0)   # runs +X at y=-10
    e_y_pos = _bottom_edge_sel(shape, axis_idx=0, fixed=10.0)    # runs +X at y=+10 (parallel, 20mm away)
    try:
        compile_point(
            {"id": "f2", "type": "point",
             "params": {"method": "intersection", "other": e_y_pos},
             "refs": {"target": e_y_neg}}, _ctx(shape))
        raise AssertionError("expected a FeatureError for non-touching edges")
    except FeatureError as exc:
        assert "do not intersect" in str(exc), f"unexpected message: {exc}"
    print("[point-intersection-gap] non-touching edges are a contained error")


def test_point_intersection_requires_other() -> None:
    """intersection with no params.other -> contained error."""
    shape = _box_shape(20.0)
    edge = _vertical_edge_sel_near(shape, (-10, -10))
    try:
        compile_point(
            {"id": "f2", "type": "point", "params": {"method": "intersection"},
             "refs": {"target": edge}}, _ctx(shape))
        raise AssertionError("expected a FeatureError for the missing 'other' entity")
    except FeatureError as exc:
        assert "other" in str(exc), f"unexpected message: {exc}"
    print("[point-intersection-noother] missing 'other' is a contained error")


def test_point_centroid_face() -> None:
    """Area centroid of a box top face = its center (0,0,10)."""
    shape = _box_shape(20.0)
    top = _top_face_sel(shape)
    out = compile_point(
        {"id": "f2", "type": "point", "params": {"method": "centroid"},
         "refs": {"target": top}}, _ctx(shape))
    p = out.datum.obj
    assert np.allclose(_v(p), [0, 0, 10], atol=1e-6), f"face centroid {_v(p)}"
    print(f"[point-centroid-face] {_v(p)}")


def test_point_centroid_body() -> None:
    """Volume centroid of the whole running body — a centered box yields the origin (0,0,0).

    (The asymmetric / genuine-mass-integral case is proven in test_point_centroid_body_asymmetric.)
    """
    shape = _box_shape(20.0)
    out = compile_point(
        {"id": "f2", "type": "point", "params": {"method": "centroid", "body": True},
         "refs": {}}, _ctx(shape))
    p = out.datum.obj
    assert np.allclose(_v(p), [0, 0, 0], atol=1e-6), f"centered-box centroid {_v(p)}"
    print(f"[point-centroid-body] centered box centroid={_v(p)}")


def test_point_centroid_body_asymmetric() -> None:
    """Volume centroid of an L-shaped body skews toward the heavier arm (a genuine mass integral).

    Build an L: a 30x10x10 base bar along +X plus a 10x20x10 wing on its near end (both corner-
    placed from the origin). The combined centroid x must be LESS than the base bar's own mid-x
    (15), because the wing piles extra mass at low x."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(30), "width": _dim(10), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
        _feat("f2", "box", {"length": _dim(10), "width": _dim(20), "height": _dim(10),
                            "centered": False, "merge": True}, refs={"at": "origin"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    shape = res.bodies[0].shape
    out = compile_point(
        {"id": "f3", "type": "point", "params": {"method": "centroid", "body": True},
         "refs": {}}, _ctx(shape))
    p = out.datum.obj
    # Sanity: the body centroid equals the kernel's own volume centroid of the shape.
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    pr = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape.wrapped, pr)
    cm = pr.CentreOfMass()
    assert np.allclose(_v(p), [cm.X(), cm.Y(), cm.Z()], atol=1e-6), \
        f"datum centroid {_v(p)} != kernel centroid {(cm.X(), cm.Y(), cm.Z())}"
    # The wing (extra mass at x<10) pulls the centroid below the base bar's own mid-x of 15.
    assert p.X < 15.0, f"L-body centroid x={p.X} should skew below 15 toward the wing"
    print(f"[point-centroid-body-asym] L-body centroid={_v(p)} (x<15 confirms mass skew)")


def test_point_coordinate_explicit_xyz() -> None:
    """An explicit XYZ point needs no geometry reference at all."""
    out = compile_point(
        {"id": "f1", "type": "point",
         "params": {"method": "coordinate", "coordinate": [3.0, -4.0, 7.5]},
         "refs": {}}, _ctx(None))
    p = out.datum.obj
    assert np.allclose(_v(p), [3.0, -4.0, 7.5], atol=1e-9), f"coordinate {_v(p)}"
    print(f"[point-coordinate] {_v(p)}")


def test_point_coordinate_validates_and_compiles() -> None:
    """End-to-end: a coordinate point validates and compiles with an empty refs object."""
    doc = _doc([
        _feat("f1", "point", {"method": "coordinate", "coordinate": [1.0, 2.0, 3.0]}),
    ])
    assert validate_part(doc) == [], "coordinate point doc must validate"
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"coordinate point compile failed: {s['error']}"
    print("[point-coordinate-e2e] validate + compile ok")


# --------------------------------------------------------------------------- main (house runner)


def main() -> None:
    test_plane_parallel_point()
    test_plane_parallel_point_validates_and_compiles()
    test_plane_parallel_point_requires_point()
    test_plane_normal_to_curve_midpoint()
    test_plane_normal_to_curve_endpoints()
    test_plane_on_surface_planar_face()
    test_plane_on_surface_cylinder_is_radial_tangent()
    test_axis_on_surface_planar_face_normal()
    test_axis_on_surface_cylinder_revolve_axis()
    test_axis_on_surface_two_points()
    test_axis_on_surface_validates_and_compiles()
    test_point_on_curve_fractions()
    test_point_on_curve_default_is_midpoint()
    test_point_intersection_curve_curve()
    test_point_intersection_curve_surface()
    test_point_intersection_non_touching_errors()
    test_point_intersection_requires_other()
    test_point_centroid_face()
    test_point_centroid_body()
    test_point_centroid_body_asymmetric()
    test_point_coordinate_explicit_xyz()
    test_point_coordinate_validates_and_compiles()
    print("\nDATUM EXT TESTS PASSED")


if __name__ == "__main__":
    main()
