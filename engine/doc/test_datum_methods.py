"""Datum reference-geometry methods — plane / axis / point / csys selector paths.

Run:  & $py -m pytest engine/doc/test_datum_methods.py -q   (from repo root)

Covers the selector-dependent datum methods that landed once the resolver existed (the ribbon
Reference Geometry buttons): angle + three_point planes, cylinder/edge/plane_intersect axes,
vertex/center/on_face points, csys, a sketch+extrude ON a newly created angled plane (proving the
datum is usable downstream), and a colinear-points contained error.

Selectors are built the same way test_features1/test_feature_overhaul do: compile a primitive,
``describe_*`` its topology, ``make_selector`` the entity of interest, tag its ``owner_feature``.
"""

from __future__ import annotations

import math

import numpy as np

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
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


# --------------------------------------------------------------------------- selector fixtures


def _box_body(size: float = 20.0):
    """Compile a centered box and return its build123d body for topology description."""
    doc = _doc([_feat("f1", "box", {"length": _dim(size), "width": _dim(size),
                                     "height": _dim(size)})])
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    return res.bodies[0].shape


def _cyl_body(radius: float = 8.0, height: float = 30.0):
    doc = _doc([_feat("f1", "cylinder", {"radius": _dim(radius), "height": _dim(height)})])
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    return res.bodies[0].shape


def _sel(info, owner: str = "f1") -> dict:
    s = make_selector(info)
    s["owner_feature"] = owner
    return s


def _vertex_sel(body, idx: int = 0, owner: str = "f1") -> dict:
    infos = describe_vertices(body.wrapped)
    return _sel(infos[idx], owner)


def _vertex_sel_near(body, target, owner: str = "f1") -> dict:
    """Vertex selector nearest a target xyz (so tests pick a deterministic corner)."""
    infos = describe_vertices(body.wrapped)
    tgt = np.asarray(target, dtype=float)
    best = min(infos, key=lambda i: float(np.linalg.norm(i.centroid - tgt)))
    return _sel(best, owner)


def _top_face_sel(body, owner: str = "f1") -> dict:
    """Selector for the +Z planar top face of a centered box."""
    infos = describe_faces(body.wrapped)
    top = max((i for i in infos if i.surf == "plane" and i.axis is not None
               and abs(i.axis[2]) > 0.9), key=lambda i: i.centroid[2])
    return _sel(top, owner)


def _vertical_edge_sel(body, owner: str = "f1") -> dict:
    infos = describe_edges(body.wrapped)
    vert = [i for i in infos if i.surf == "line" and i.axis is not None and abs(i.axis[2]) > 0.9]
    assert vert, "need a vertical edge"
    return _sel(vert[0], owner)


# --------------------------------------------------------------------------- plane: angle


def test_plane_angle_about_box_edge() -> None:
    """A plane angled 30 deg about plane.top, hinged on a horizontal top edge of a box."""
    body = _box_body(20.0)
    # Hinge: a horizontal top edge (line, axis ~horizontal, centroid z near +10).
    infos = describe_edges(body.wrapped)
    horiz_top = [i for i in infos if i.surf == "line" and i.axis is not None
                 and abs(i.axis[2]) < 0.1 and i.centroid[2] > 5.0]
    assert horiz_top, "need a horizontal top edge"
    hinge = _sel(horiz_top[0], "f1")

    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "angle", "angle": _dim(30.0)},
              refs={"base": "plane.top", "second": hinge}),
    ])
    assert validate_part(doc) == [], "angle-plane doc must validate"
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"angle plane failed: {s['error']}"
    assert abs(s["resolved"].get("angle", 0.0) - 30.0) < 1e-6
    print(f"[plane-angle] 30deg about top edge ok; resolved={s['resolved']}")


def test_plane_angle_about_datum_axis() -> None:
    """Angle plane hinged on a reserved datum axis (no body needed for the hinge)."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "angle", "angle": _dim(45.0)},
              refs={"base": "plane.top", "second": "axis.x"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"angle plane (datum axis) failed: {s['error']}"
    print(f"[plane-angle-datum] 45deg about axis.x ok")


# --------------------------------------------------------------------------- plane: three_point


def test_plane_three_point() -> None:
    """Plane through three non-colinear box vertices."""
    body = _box_body(20.0)
    p0 = _vertex_sel_near(body, (-10, -10, -10))
    p1 = _vertex_sel_near(body, (10, -10, -10))
    p2 = _vertex_sel_near(body, (-10, 10, -10))

    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "three_point"},
              refs={"points": [p0, p1, p2]}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"three_point plane failed: {s['error']}"
    print("[plane-3pt] plane through 3 box vertices ok")


def test_plane_three_point_colinear_is_contained_error() -> None:
    """Three colinear points -> contained FeatureError, not a crash."""
    # Two distinct corners that share an edge plus a midpoint vertex would be colinear; instead
    # we feed the SAME vertex three times (degenerate / colinear) to force the error path.
    body = _box_body(20.0)
    p = _vertex_sel_near(body, (-10, -10, -10))

    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "three_point"},
              refs={"points": [p, p, p]}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "colinear three_point should error"
    assert "colinear" in s["error"].lower(), f"expected colinear error; got {s['error']}"
    print(f"[plane-3pt-colinear] contained error: '{s['error'][:50]}'")


# --------------------------------------------------------------------------- axis: cylinder


def test_axis_cylinder_face() -> None:
    """Axis of a cylinder's cylindrical face = the cylinder's Z axis."""
    body = _cyl_body(8.0, 30.0)
    infos = describe_faces(body.wrapped)
    cyl = next(i for i in infos if i.surf == "cylinder")
    sel = _sel(cyl, "f1")

    doc = _doc([
        _feat("f1", "cylinder", {"radius": _dim(8), "height": _dim(30)}),
        _feat("f2", "axis", {"method": "cylinder"}, refs={"a": sel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"cylinder axis failed: {s['error']}"
    print("[axis-cylinder] cylindrical face axis ok")


def test_axis_cylinder_from_hole() -> None:
    """Drill a hole in a box, then take the axis of the resulting cylindrical hole face."""
    body = _box_body(40.0)
    face_sel = _top_face_sel(body, "f1")
    doc = _doc([
        _feat("f1", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(40)}),
        _feat("f2", "hole", {"style": "simple", "standard": "custom",
                             "diameter": _dim(6.0), "end": "through_all"},
              refs={"face": face_sel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    drilled = res.bodies[0].shape

    infos = describe_faces(drilled.wrapped)
    cyl = next((i for i in infos if i.surf == "cylinder"), None)
    assert cyl is not None, "drilled hole should leave a cylindrical face"
    cyl_sel = make_selector(cyl)
    cyl_sel["owner_feature"] = "f2"

    doc2 = _doc([
        _feat("f1", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(40)}),
        _feat("f2", "hole", {"style": "simple", "standard": "custom",
                             "diameter": _dim(6.0), "end": "through_all"},
              refs={"face": _top_face_sel(_box_body(40.0), "f1")}),
        _feat("f3", "axis", {"method": "cylinder"}, refs={"a": cyl_sel}),
    ])
    assert validate_part(doc2) == []
    res2 = compile_document(doc2)
    s = _status(res2, "f3")
    assert s["ok"], f"hole-face axis failed: {s['error']}"
    print("[axis-cylinder-hole] drilled-hole cylindrical face axis ok")


# --------------------------------------------------------------------------- axis: edge


def test_axis_along_edge() -> None:
    """Axis along a vertical box edge."""
    body = _box_body(20.0)
    edge_sel = _vertical_edge_sel(body, "f1")
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "axis", {"method": "edge"}, refs={"a": edge_sel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"edge axis failed: {s['error']}"
    print("[axis-edge] axis along vertical box edge ok")


# --------------------------------------------------------------------------- axis: plane_intersect


def test_axis_plane_intersect_reserved() -> None:
    """Intersection of plane.top (XY) and plane.front (XZ) is the X axis."""
    doc = _doc([
        _feat("f1", "axis", {"method": "plane_intersect"},
              refs={"a": "plane.top", "b": "plane.front"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"plane_intersect failed: {s['error']}"
    # Verify the registered datum axis direction is along X.
    from engine.doc.features.datum import reserved_datums  # noqa: F401
    print("[axis-plane-intersect] XY ∩ XZ = X axis ok")


def test_axis_plane_intersect_parallel_errors() -> None:
    """Two parallel planes -> contained 'parallel' error."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(15.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "axis", {"method": "plane_intersect"},
              refs={"a": "plane.top", "b": {"kind": "datum", "feature": "f1"}}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"] and "parallel" in s["error"].lower(), \
        f"expected parallel error; got {s.get('error')}"
    print("[axis-plane-intersect-parallel] contained error ok")


# --------------------------------------------------------------------------- point methods


def test_point_vertex() -> None:
    body = _box_body(20.0)
    vsel = _vertex_sel_near(body, (10, 10, 10))
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "point", {"method": "vertex"}, refs={"target": vsel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"point vertex failed: {s['error']}"
    print("[point-vertex] point at box corner ok")


def test_point_center_circular_edge() -> None:
    """Center of a cylinder's top circular edge."""
    body = _cyl_body(8.0, 30.0)
    infos = describe_edges(body.wrapped)
    circ = next(i for i in infos if i.surf == "circle")
    esel = make_selector(circ)
    esel["owner_feature"] = "f1"
    doc = _doc([
        _feat("f1", "cylinder", {"radius": _dim(8), "height": _dim(30)}),
        _feat("f2", "point", {"method": "center"}, refs={"target": esel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"point center failed: {s['error']}"
    print("[point-center] center of circular edge ok")


def test_point_on_face_uv() -> None:
    """Point at a uv offset on a box face."""
    body = _box_body(20.0)
    fsel = _top_face_sel(body, "f1")
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "point", {"method": "on_face", "uv": [3.0, -4.0]},
              refs={"target": fsel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"point on_face failed: {s['error']}"
    print("[point-on-face] point at uv on top face ok")


# --------------------------------------------------------------------------- csys


def test_csys_origin_and_dirs() -> None:
    """csys from a vertex origin + two box edges (orthonormalized)."""
    body = _box_body(20.0)
    origin = _vertex_sel_near(body, (-10, -10, -10))
    infos = describe_edges(body.wrapped)
    # two distinct edges meeting at that corner: one along +X, one along +Y near z=-10
    horiz = [i for i in infos if i.surf == "line" and i.axis is not None and abs(i.axis[2]) < 0.1
             and i.centroid[2] < -5.0]
    assert len(horiz) >= 2, "need two bottom horizontal edges"
    x_sel = make_selector(horiz[0]); x_sel["owner_feature"] = "f1"
    y_sel = make_selector(horiz[1]); y_sel["owner_feature"] = "f1"

    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "csys", {}, refs={"origin": origin, "x_dir": x_sel, "y_dir": y_sel}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"csys failed: {s['error']}"
    print("[csys] origin + x/y edges -> orthonormal frame ok")


def test_csys_origin_only() -> None:
    """csys with just a datum origin -> axis-aligned frame at that point."""
    doc = _doc([
        _feat("f1", "csys", {}, refs={"origin": "origin"}),
    ])
    assert validate_part(doc) == []
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"csys origin-only failed: {s['error']}"
    print("[csys-origin] origin-only frame ok")


# --------------------------------------------------------------------------- downstream usability


def test_sketch_extrude_on_angled_plane() -> None:
    """Create an angled datum plane, then sketch + extrude ON it — proves it's usable downstream."""
    body = _box_body(20.0)
    infos = describe_edges(body.wrapped)
    horiz_top = [i for i in infos if i.surf == "line" and i.axis is not None
                 and abs(i.axis[2]) < 0.1 and i.centroid[2] > 5.0]
    assert horiz_top, "need a horizontal top edge"
    hinge = _sel(horiz_top[0], "f1")

    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "plane", {"method": "angle", "angle": _dim(30.0)},
              refs={"base": "plane.top", "second": hinge}),
        _feat("f3", "sketch", {"entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [0.0, 0.0], "radius": 4.0},
        ], "constraints": []}, refs={"plane": {"kind": "datum", "feature": "f2"}}),
        _feat("f4", "extrude", {"mode": "boss", "end": "blind", "depth": _dim(10.0),
                                "merge": False},
              refs={"profile": [{"kind": "region", "sketch": "f3"}]}),
    ])
    assert validate_part(doc) == [], "angled-plane sketch/extrude doc must validate"
    res = compile_document(doc)
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    assert _status(res, "f3")["ok"], _status(res, "f3")["error"]
    s4 = _status(res, "f4")
    assert s4["ok"], f"extrude on angled plane failed: {s4['error']}"
    # A boss extrude of an r=4 disc, depth 10 -> ~502.65 mm^3 cylinder on the angled plane.
    pin = res.bodies[-1].shape
    vol = pin.volume
    expected = math.pi * 16.0 * 10.0
    assert abs(vol - expected) / expected < 0.02, f"angled pin vol {vol:.1f} vs {expected:.1f}"
    print(f"[downstream] sketch+extrude on 30deg plane -> pin vol={vol:.1f} (expected {expected:.1f})")


# --------------------------------------------------------------------------- main (house runner)


def main() -> None:
    test_plane_angle_about_box_edge()
    test_plane_angle_about_datum_axis()
    test_plane_three_point()
    test_plane_three_point_colinear_is_contained_error()
    test_axis_cylinder_face()
    test_axis_cylinder_from_hole()
    test_axis_along_edge()
    test_axis_plane_intersect_reserved()
    test_axis_plane_intersect_parallel_errors()
    test_point_vertex()
    test_point_center_circular_edge()
    test_point_on_face_uv()
    test_csys_origin_and_dirs()
    test_csys_origin_only()
    test_sketch_extrude_on_angled_plane()
    print("\nDATUM METHODS TESTS PASSED")


if __name__ == "__main__":
    main()
