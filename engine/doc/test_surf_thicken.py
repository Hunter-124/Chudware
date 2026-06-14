r"""surf_thicken — open SURFACE body offset into a closed SOLID (surface→solid bridge).

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_thicken.py -q   (from repo root)

surf_thicken takes an open surface body (a surf_planar face, a surf_extrude tube wall, …) and
offsets it by a wall ``thickness`` to make a watertight SOLID (kind="solid"), replacing the input
surface body. The defining check is volume ≈ surface_area × thickness:

1.  Flat 40×40 surf_planar plate thickened t=2 (one side) → SOLID, volume ≈ 40·40·2 = 3200,
    mass_props volume > 0, kind flips surface→solid, and the slab spans z[0,2].
2.  side="mid" straddles the sketch plane (±t/2) → same 3200 volume, z[-1,1].
3.  side="one" with flip grows the wall to the −Z side → z[-2,0], same volume.
4.  A surf_extrude TUBE WALL (open shell) thickened → a hollow tube SOLID whose wall volume
    ≈ perimeter · height · thickness (open-shell path, not just a flat face).
5.  thickness <= 0 / no surface body → clean contained FeatureError.

Mirrors engine/doc/test_extrude_ends.py (helpers + pytest-style functions + a main()).

The compiler under test (engine/doc/features/surf_thicken.py) is imported only by this test;
surf_planar / surf_extrude are imported so their compilers register too.
"""

from __future__ import annotations

from engine.doc.features import surf_thicken  # noqa: F401  (registers the compiler under test)
from engine.doc.features import surf_planar    # noqa: F401  (build the input surface)
from engine.doc.features import surf_extrude   # noqa: F401  (tube-wall surface)

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


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


def _rect_sketch(fid: str, x0: float, y0: float, w: float, h: float,
                 plane_ref) -> dict:
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


def _body_sel(owner: str) -> dict:
    """A body selector naming the surface body's owning feature (surf_planar/surf_extrude)."""
    return {"kind": "body", "owner_feature": owner}


def _surface_body(res):
    """The open surface body in the result (volume ≈ 0), or None if all are solids."""
    for b in res.bodies:
        if abs(b.shape.volume) <= 1e-6:
            return b
    return None


def _solid_body(res):
    """The body with the largest enclosed volume (the thickened solid)."""
    return max(res.bodies, key=lambda b: b.shape.volume)


# --------------------------------------------------------------------------- test 1: flat plate

def test_planar_thicken_one_side_volume_equals_area_times_thickness() -> None:
    """A 40×40 surf_planar plate (area 1600) thickened t=2 one-side → a SOLID of
    volume ≈ 1600·2 = 3200 mm³, kind=="solid", mass_props volume > 0, spanning z[0,2]."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _feat("f3", "surf_thicken", {"thickness": _dim(2.0), "side": "one"},
              refs={"surface": _body_sel("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f2", "f3"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"

    # surf_thicken REPLACES the surface body → exactly one body, and it is a SOLID.
    assert len(res.bodies) == 1, f"thicken should consume the surface; got {len(res.bodies)} bodies"
    solid = res.bodies[0]
    assert solid.kind == "solid", f"thickened body kind should be 'solid'; got {solid.kind!r}"

    vol = solid.shape.volume
    expected = 40.0 * 40.0 * 2.0  # area × thickness = 3200
    assert abs(vol - expected) / expected < 0.01, \
        f"thicken vol {vol:.1f} vs expected {expected} (area 1600 × thickness 2)"
    assert vol > 0.0, "mass_props volume must be > 0 for a solid"

    # resolved echoes thickness + volume; the feature reports kind solid.
    assert abs(_status(res, "f3")["resolved"].get("thickness", 0.0) - 2.0) < 1e-6
    assert _status(res, "f3")["resolved"].get("volume", 0.0) > 0.0

    bb = solid.shape.bounding_box()
    assert abs(bb.min.Z) < 0.05 and abs(bb.max.Z - 2.0) < 0.05, \
        f"one-side thicken should span z[0,2]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[planar-one] vol={vol:.1f} (expected {expected}) kind={solid.kind} "
          f"z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 2: mid

def test_planar_thicken_mid_straddles_plane() -> None:
    """side='mid' straddles the sketch plane (±t/2): same 3200 volume, z[-1,1]."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _feat("f3", "surf_thicken", {"thickness": _dim(2.0), "side": "mid"},
              refs={"surface": _body_sel("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"mid thicken failed: {s['error']}"
    solid = res.bodies[0]
    assert solid.kind == "solid"
    vol = solid.shape.volume
    expected = 40.0 * 40.0 * 2.0
    assert abs(vol - expected) / expected < 0.01, \
        f"mid thicken vol {vol:.1f} vs expected {expected}"
    bb = solid.shape.bounding_box()
    assert abs(bb.min.Z + 1.0) < 0.05 and abs(bb.max.Z - 1.0) < 0.05, \
        f"mid thicken should straddle z[-1,1]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[planar-mid] vol={vol:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 3: flip

def test_planar_thicken_flip_grows_negative_side() -> None:
    """side='one' with flip grows the wall to the −Z side: z[-2,0], same 3200 volume."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _feat("f3", "surf_thicken", {"thickness": _dim(2.0), "side": "one", "flip": True},
              refs={"surface": _body_sel("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"flip thicken failed: {s['error']}"
    solid = res.bodies[0]
    vol = solid.shape.volume
    expected = 40.0 * 40.0 * 2.0
    assert abs(vol - expected) / expected < 0.01, \
        f"flip thicken vol {vol:.1f} vs expected {expected}"
    bb = solid.shape.bounding_box()
    assert abs(bb.min.Z + 2.0) < 0.05 and abs(bb.max.Z) < 0.05, \
        f"flip should grow to −Z, z[-2,0]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[planar-flip] vol={vol:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 4: tube wall

def test_extruded_tube_wall_thicken_makes_hollow_solid() -> None:
    """A surf_extrude open tube wall (40×40 loop, height 10 → side-wall shell, no caps)
    thickened t=2 → a hollow tube SOLID whose wall volume ≈ perimeter · height · thickness
    = 160 · 10 · 2 = 3200 mm³ (outward offset bulks the outer corners slightly)."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_extrude", {"end": "blind", "depth": _dim(10.0)},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _feat("f3", "surf_thicken", {"thickness": _dim(2.0), "side": "one"},
              refs={"surface": _body_sel("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f2", "f3"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    assert len(res.bodies) == 1, f"thicken should consume the surface; got {len(res.bodies)}"
    solid = res.bodies[0]
    assert solid.kind == "solid", f"tube wall thicken kind should be solid; got {solid.kind!r}"
    vol = solid.shape.volume
    # perimeter 160 × height 10 × thickness 2 = 3200; allow generous tolerance for corner bulk.
    expected = 160.0 * 10.0 * 2.0
    assert abs(vol - expected) / expected < 0.10, \
        f"tube-wall thicken vol {vol:.1f} vs ~{expected} (perimeter·height·thickness)"
    assert vol > 0.0
    # Hollow: the solid has an interior void → far less than the filled tube (40·40·10=16000).
    assert vol < 16000.0 * 0.5, "thickened tube wall must be hollow (much less than a filled box)"
    print(f"[tube-wall] vol={vol:.1f} (~{expected}) kind={solid.kind}")


# --------------------------------------------------------------------------- test 5: errors

def test_thicken_errors_are_contained() -> None:
    """thickness <= 0 and no-surface-body both raise CLEAN contained FeatureErrors
    (ok:false), never crashing the rebuild."""
    # (a) thickness <= 0
    doc_a = _doc([
        _rect_sketch("f1", 0, 0, 40, 40, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 20, 20, 1600.0)]}),
        _feat("f3", "surf_thicken", {"thickness": _dim(0.0), "side": "one"},
              refs={"surface": _body_sel("f2")}),
    ])
    _assert_valid(doc_a)
    res_a = compile_document(doc_a)
    s_a = _status(res_a, "f3")
    assert not s_a["ok"], "thickness 0 should fail"
    assert "thickness must be > 0" in s_a["error"], f"got: {s_a['error']}"

    # (b) no surface body at all → clean error.
    doc_b = _doc([
        _feat("f1", "surf_thicken", {"thickness": _dim(2.0)},
              refs={"surface": _body_sel("nope")}),
    ])
    _assert_valid(doc_b)
    res_b = compile_document(doc_b)
    s_b = _status(res_b, "f1")
    assert not s_b["ok"], "no surface body should fail"
    assert "no surface body" in s_b["error"] or "did not resolve" in s_b["error"], \
        f"got: {s_b['error']}"
    print(f"[errors] thickness<=0 + no-surface both clean")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_planar_thicken_one_side_volume_equals_area_times_thickness()
    test_planar_thicken_mid_straddles_plane()
    test_planar_thicken_flip_grows_negative_side()
    test_extruded_tube_wall_thicken_makes_hollow_solid()
    test_thicken_errors_are_contained()
    print("\nSURF-THICKEN TESTS PASSED")


if __name__ == "__main__":
    main()
