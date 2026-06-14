r"""Boundary SURFACE feature (``surf_boundary``) — a surface filled inside a net of curves.

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_boundary.py -q   (from repo root)

Covers the surfacing boundary compiler (SolidWorks "Boundary Surface"):
1. A closed planar quad of 4 sketch segments (refs.dir1) fills into a covering boundary surface —
   kind=="surface", AREA mass-props (is_surface, area>0, volume≈0, mass≈0), and the surface
   spans the input loop (bbox matches the quad).
2. A gently-curved boundary net (3 lines + 1 bulging arc) fills a surface that follows the curved
   side — the surface area exceeds the flat quad's and the bbox reaches the arc apex.
3. A surface boundary never fuses into a running solid: with a base box present, surf_boundary
   appends a SECOND body and leaves the solid's volume untouched.
4. dir1 carrying two stacked section loops (an over-constrained fill) gracefully falls back to an
   OPEN lofted shell through the sections — kind=="surface", spans the section stack in Z.

Harness mirrors engine/doc/test_extrude_ends.py / test_surf_loft.py.
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.evaluate import check, mass_props
from engine.doc.schema import validate_part
from engine.doc.features import surf_boundary  # noqa: F401  (registers the compiler)


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


def _region_sel(sketch_id: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _has_free_edges(shape) -> bool:
    """True when the body is an OPEN shell — check()'s free-edge census fires."""
    return any(i.get("kind") == "free_edges" for i in check(shape).get("issues", []))


# --------------------------------------------------------------------------- test 1: planar fill

def test_planar_quad_fills_boundary_surface() -> None:
    """A closed 40x40 quad (4 sketch segments) on plane.top fills into a covering boundary
    surface: kind=="surface", AREA mass-props, and the surface spans the input loop."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_boundary", {},
              refs={"dir1": [_region_sel("f1", 20, 20, 1600.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"sketch failed: {_status(res, 'f1')['error']}"
    s = _status(res, "f2")
    assert s["ok"], f"surf_boundary failed: {s['error']}"

    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"
    assert s["resolved"].get("_method") == "filling", \
        f"a single closed loop should use the filling path; got {s['resolved']}"

    # AREA report (no enclosed volume) — the defining surface property.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # The fitted surface fills the flat 40x40 quad → area ≈ 1600 mm².
    assert abs(mp["area_mm2"] - 1600.0) / 1600.0 < 0.02, \
        f"planar fill area {mp['area_mm2']:.1f} vs expected ~1600"

    # The surface SPANS the boundary loop: its bbox matches the input quad (0..40 in X & Y,
    # planar at z≈0).
    bb = bo.shape.bounding_box()
    assert abs(bb.min.X) < 0.05 and abs(bb.max.X - 40.0) < 0.05, \
        f"bbox X should span [0,40]; got [{bb.min.X:.2f},{bb.max.X:.2f}]"
    assert abs(bb.min.Y) < 0.05 and abs(bb.max.Y - 40.0) < 0.05, \
        f"bbox Y should span [0,40]; got [{bb.min.Y:.2f},{bb.max.Y:.2f}]"
    assert abs(bb.max.Z - bb.min.Z) < 0.05, \
        f"planar boundary surface should be flat; got z-extent {bb.max.Z - bb.min.Z:.3f}"
    print(f"[planar] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"bbox X[{bb.min.X:.1f},{bb.max.X:.1f}] Y[{bb.min.Y:.1f},{bb.max.Y:.1f}]")


# --------------------------------------------------------------------------- test 2: curved net

def _arc_quad_sketch(fid: str) -> dict:
    """A closed quad whose TOP edge is an upward-bulging arc (apex ≈ y=48).

    Bottom (0,0)->(40,0), right (40,0)->(40,40), arc (40,40)->(0,40) bulging to (20,48),
    left (0,40)->(0,0). The arc geometry: center (20,19), radius 29 (so apex 19+29=48 at x=20,
    and the endpoints (0,40)/(40,40) sit on the circle: 20²+21²=29²)."""
    r, cy = 29.0, 19.0
    a_start = math.degrees(math.atan2(40 - cy, 40 - 20))   # at (40,40)
    a_end = math.degrees(math.atan2(40 - cy, 0 - 20))      # at (0,40)
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0, 0], "p2": [40, 0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [40, 0], "p2": [40, 40]},
            {"id": "e3", "type": "arc", "construction": False,
             "center": [20, cy], "radius": r,
             "start": a_start, "end": a_end, "ccw": True},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [0, 40], "p2": [0, 0]},
        ],
        "constraints": [],
    }, refs={"plane": "plane.top"})


def test_curved_boundary_net_follows_arc() -> None:
    """A gently-curved boundary net (3 lines + 1 bulging arc) fills a surface that follows the
    curved side: the area exceeds the flat 40x40 quad (the bulge adds material) and the bbox
    reaches the arc apex at y≈48."""
    doc = _doc([
        _arc_quad_sketch("f1"),
        _feat("f2", "surf_boundary", {},
              refs={"dir1": [_region_sel("f1", 20, 30, 1800.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"arc sketch failed: {_status(res, 'f1')['error']}"
    s = _status(res, "f2")
    assert s["ok"], f"surf_boundary (curved) failed: {s['error']}"

    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    # The bulged top adds area beyond the flat 1600 mm² quad.
    assert mp["area_mm2"] > 1600.0 * 1.02, \
        f"curved-net area {mp['area_mm2']:.1f} should exceed the flat quad's ~1600"

    # The surface SPANS the curved boundary: the bbox top reaches the arc apex (y≈48), the
    # sides at x∈[0,40].
    bb = bo.shape.bounding_box()
    assert abs(bb.min.X) < 0.1 and abs(bb.max.X - 40.0) < 0.1, \
        f"bbox X should span [0,40]; got [{bb.min.X:.2f},{bb.max.X:.2f}]"
    assert abs(bb.min.Y) < 0.1, f"bbox Y min should be 0; got {bb.min.Y:.2f}"
    assert abs(bb.max.Y - 48.0) < 0.3, \
        f"bbox Y max should reach the arc apex ~48; got {bb.max.Y:.2f}"
    print(f"[curved] area={mp['area_mm2']:.1f} (>1600) bbox Y max={bb.max.Y:.1f} (~48)")


# --------------------------------------------------------------------------- test 3: never fuses

def test_surface_does_not_fuse_into_solid() -> None:
    """With a base solid present, surf_boundary appends a SECOND (surface) body and leaves the
    solid's volume untouched — surfaces never merge into the running solid."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(80), "width": _dim(80), "height": _dim(5),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f2", 0, 0, 40, 40, "plane.top"),
        _feat("f3", "surf_boundary", {},
              refs={"dir1": [_region_sel("f2", 20, 20, 1600.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"box failed: {_status(res, 'f1')['error']}"
    assert _status(res, "f3")["ok"], f"surf_boundary failed: {_status(res, 'f3')['error']}"

    assert len(res.bodies) == 2, f"surf_boundary should append a 2nd body; got {len(res.bodies)}"
    kinds = sorted(b.kind for b in res.bodies)
    assert kinds == ["solid", "surface"], f"expected one solid + one surface; got {kinds}"

    solid = next(b for b in res.bodies if b.kind == "solid")
    surf = next(b for b in res.bodies if b.kind == "surface")
    expected_vol = 80.0 * 80.0 * 5.0
    assert abs(solid.shape.volume - expected_vol) / expected_vol < 0.01, \
        "base solid volume must be untouched by the boundary surface"
    assert mass_props(surf.shape).get("is_surface") is True
    print(f"[no-fuse] bodies={kinds} solid_vol={solid.shape.volume:.0f}")


# --------------------------------------------------------------------------- test 4: loft fallback

def test_stacked_sections_fall_back_to_open_loft() -> None:
    """When dir1 carries two stacked closed section loops (which a single fill cannot span), the
    feature falls back to lofting an OPEN shell through them — kind=="surface", spanning the
    section stack in Z, with free edges at the un-capped ends."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(30.0)},
              refs={"base": "plane.top"}),                       # z=30 plane
        _rect_sketch("f2", 0, 0, 40, 40, "plane.top"),           # big square at z=0
        _rect_sketch("f3", 10, 10, 20, 20, "f1"),                # small square at z=30
        _feat("f4", "surf_boundary", {}, refs={"dir1": [
            _region_sel("f2", 20, 20, 1600.0),
            _region_sel("f3", 20, 20, 400.0),
        ]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"surf_boundary fallback failed: {s['error']}"
    assert s["resolved"].get("_method") == "thrusections", \
        f"stacked sections should hit the loft fallback; got {s['resolved']}"

    bo = res.bodies[-1]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0 and abs(mp["volume_mm3"]) < 1e-6

    # The open lofted shell spans the section stack in Z (z∈[0,30]).
    bb = bo.shape.bounding_box()
    assert abs(bb.min.Z) < 0.1 and abs(bb.max.Z - 30.0) < 0.1, \
        f"fallback shell should span z[0,30]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    assert _has_free_edges(bo.shape), "fallback lofted surface must be an OPEN shell (free edges)"
    print(f"[fallback] method=thrusections area={mp['area_mm2']:.1f} "
          f"z[{bb.min.Z:.1f},{bb.max.Z:.1f}] open=yes")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_planar_quad_fills_boundary_surface()
    test_curved_boundary_net_follows_arc()
    test_surface_does_not_fuse_into_solid()
    test_stacked_sections_fall_back_to_open_loft()
    print("\nSURF-BOUNDARY TESTS PASSED")


if __name__ == "__main__":
    main()
