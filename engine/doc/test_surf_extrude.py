r"""Surfacing — extruded-surface feature (``surf_extrude``): the open side-wall surface.

Run:  & $py -m pytest engine\doc\test_surf_extrude.py -q   (from repo root)

``surf_extrude`` sweeps a profile's BOUNDARY WIRE(s) along the extrude direction to make the
SIDE-WALL surface only — NO end caps — so the result is an OPEN shell, not a closed solid.

Covers:
1. blind: a 20x40 rectangle extruded depth=10 → a surface body (kind=="surface") whose
   ``mass_props`` is an AREA report: ``is_surface`` True, ``area_mm2`` ≈ perimeter×depth
   (open tube wall, no caps), ``volume_mm3`` ≈ 0, ``mass_g`` ≈ 0. The shell is OPEN (free
   edges present; ShapeType is not SOLID).
2. mid_plane: depth=12 straddles the sketch plane z[-6,6]; area still perimeter×depth.
3. A profile WITH A HOLE (square + circular hole) → outer + inner tube walls; area =
   (outer + inner perimeter) × depth.

Harness mirrors engine/doc/test_extrude_ends.py and test_surface_bodykind.py.
"""

from __future__ import annotations

import math

# Import the feature modules so their @register decorators run.
from engine.doc.features import surf_extrude  # noqa: F401  (registers surf_extrude)
from engine.doc.features import extrude  # noqa: F401  (surf_extrude reuses its resolvers)

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part

from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape


# --------------------------------------------------------------------------- helpers

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": False, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _rect_sketch(fid: str, x0: float, y0: float, w: float, h: float, plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x0, y0], "p2": [x0 + w, y0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [x0 + w, y0], "p2": [x0 + w, y0 + h]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [x0 + w, y0 + h], "p2": [x0, y0 + h]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [x0, y0 + h], "p2": [x0, y0]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _square_with_hole_sketch(fid: str, x0: float, y0: float, side: float,
                             cx: float, cy: float, r: float, plane_ref) -> dict:
    """A square outer loop + a concentric circular inner loop → a region with a hole."""
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x0, y0], "p2": [x0 + side, y0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [x0 + side, y0], "p2": [x0 + side, y0 + side]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [x0 + side, y0 + side], "p2": [x0, y0 + side]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [x0, y0 + side], "p2": [x0, y0]},
            {"id": "e5", "type": "circle", "construction": False,
             "center": [cx, cy], "radius": r},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region_sel(sketch_id: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _free_edge_count(shape) -> int:
    """Number of boundary (free) edges — edges used by exactly ONE face. >0 ⇒ open shell."""
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
    return sum(1 for i in range(1, m.Extent() + 1) if m.FindFromIndex(i).Extent() == 1)


def _is_solid(shape) -> bool:
    return shape.wrapped.ShapeType() == TopAbs_SOLID


# --------------------------------------------------------------------------- test 1: blind wall

def test_surf_extrude_blind_open_wall() -> None:
    """A 20x40 rectangle extruded blind depth=10 → an open SURFACE tube wall (no caps).
    Area = perimeter (2*(20+40)=120) * depth (10) = 1200 mm². Volume ≈ 0; the shell is OPEN
    (free edges present, not a closed solid)."""
    w, h, depth = 20.0, 40.0, 10.0
    doc = _doc([
        _rect_sketch("f1", 0, 0, w, h, "plane.top"),
        _feat("f2", "surf_extrude", {"end": "blind", "depth": _dim(depth)},
              refs={"profile": [_region_sel("f1", w / 2, h / 2, w * h)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_extrude blind failed: {s['error']}"
    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"

    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    # mass_props → AREA report, no volume/mass.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # The defining area assertion: open wall area = boundary perimeter × depth (NO end caps).
    perimeter = 2.0 * (w + h)
    expected_area = perimeter * depth  # 120 * 10 = 1200
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"open-wall area {mp['area_mm2']:.2f} vs expected {expected_area} (perimeter×depth, no caps)"
    # Sanity: an open wall has strictly LESS area than the same closed box (which would add the
    # two w*h caps, +2*20*40 = +1600).
    assert mp["area_mm2"] < expected_area + 2.0 * w * h - 1e-6, \
        "open wall must not include the end-cap area"

    # The shell is OPEN: free boundary edges exist and it is not a closed solid.
    assert not _is_solid(bo.shape), "surf_extrude must produce an open shell, not a solid"
    assert _free_edge_count(bo.shape) > 0, "open shell must have free (boundary) edges"

    # resolved echoes the linear depth.
    assert abs(s["resolved"].get("depth", 0.0) - depth) < 1e-6, \
        f"resolved.depth should be {depth}; got {s['resolved'].get('depth')}"
    print(f"[surf_extrude-blind] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"(expected {expected_area}) free_edges={_free_edge_count(bo.shape)}")


# --------------------------------------------------------------------------- test 2: mid_plane

def test_surf_extrude_mid_plane_straddles() -> None:
    """mid_plane depth=12 straddles the sketch plane → z[-6,6]; the wall area is still
    perimeter × depth (the total height)."""
    w, h, depth = 20.0, 20.0, 12.0
    doc = _doc([
        _rect_sketch("f1", 0, 0, w, h, "plane.top"),
        _feat("f2", "surf_extrude", {"end": "mid_plane", "depth": _dim(depth)},
              refs={"profile": [_region_sel("f1", w / 2, h / 2, w * h)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_extrude mid_plane failed: {s['error']}"
    bo = res.bodies[0]
    assert bo.kind == "surface"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True
    perimeter = 2.0 * (w + h)
    expected_area = perimeter * depth  # 80 * 12 = 960
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"mid_plane area {mp['area_mm2']:.2f} vs expected {expected_area}"

    bb = bo.shape.bounding_box()
    assert abs(bb.min.Z + depth / 2.0) < 0.05 and abs(bb.max.Z - depth / 2.0) < 0.05, \
        f"mid_plane should straddle z=0; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    assert not _is_solid(bo.shape) and _free_edge_count(bo.shape) > 0, "must stay an open shell"
    print(f"[surf_extrude-mid] area={mp['area_mm2']:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 3: profile w/ hole

def test_surf_extrude_profile_with_hole() -> None:
    """A square (side 30) with a concentric circular hole (r=5) extruded depth=8 → the OUTER
    tube wall PLUS the INNER (hole) tube wall. Area = (outer perimeter + inner perimeter) ×
    depth = (4*30 + 2*pi*5) * 8."""
    side, r, depth = 30.0, 5.0, 8.0
    cx = cy = side / 2.0
    doc = _doc([
        _square_with_hole_sketch("f1", 0, 0, side, cx, cy, r, "plane.top"),
        # Whole-sketch profile → the material (even-depth) region, i.e. the square minus the hole.
        _feat("f2", "surf_extrude", {"end": "blind", "depth": _dim(depth)},
              refs={"profile": [_region_sel("f1", cx, cy, side * side)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_extrude with hole failed: {s['error']}"
    bo = res.bodies[0]
    assert bo.kind == "surface"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True
    assert abs(mp["volume_mm3"]) < 1e-6
    outer_peri = 4.0 * side
    inner_peri = 2.0 * math.pi * r
    expected_area = (outer_peri + inner_peri) * depth
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.02, \
        f"with-hole area {mp['area_mm2']:.2f} vs expected {expected_area:.2f} " \
        f"(outer+inner perimeter × depth)"
    # Both an outer and an inner tube ⇒ strictly more area than the outer wall alone.
    assert mp["area_mm2"] > outer_peri * depth + 1e-6, \
        "the hole's inner wall must contribute extra surface area"
    assert not _is_solid(bo.shape) and _free_edge_count(bo.shape) > 0, "must stay an open shell"
    print(f"[surf_extrude-hole] area={mp['area_mm2']:.1f} (expected {expected_area:.1f})")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_extrude_blind_open_wall()
    test_surf_extrude_mid_plane_straddles()
    test_surf_extrude_profile_with_hole()
    print("\nSURF-EXTRUDE TESTS PASSED")


if __name__ == "__main__":
    main()
