r"""C26-B — extended extrude end conditions: up_to_next, second direction, draft, mid_plane.

Run:  & $py -m pytest engine\doc\test_extrude_ends.py -q   (from repo root)

Pytest-style (also importable as plain functions). Covers:
1. up_to_next boss onto a stepped body lands exactly on the next face (volume assert).
2. up_to_next cut stops at the next face (partial removal, not through-all).
3. up_to_next with no body -> clean "requires an existing body" error.
4. end2 blind both-direction boss -> total height = depth + depth2.
5. taper (draft) changes the top-face area in the right direction (inward narrows).
6. draft_outward flips the sign (top grows).
7. mid_plane total depth == depth (half each side).
"""

from __future__ import annotations

import math

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


def _top_face_sel(owner: str, cx: float, cy: float, z: float, area: float) -> dict:
    return {"kind": "face", "surf": "plane", "centroid": [cx, cy, z],
            "axis": [0.0, 0.0, 1.0], "area_or_len": area,
            "owner_feature": owner, "hint_index": 0}


# --------------------------------------------------------------------------- test 1: up_to_next boss

def test_up_to_next_boss_lands_on_step() -> None:
    """Stepped body with an air gap, built from primitive boxes + datum planes (so the
    probe sketch never needs a face selector):
      - base  40x40x10  at z[0,10]
      - roof  40x40x10  at z[20,30]  (placed on datum plane z=20)
      - front wall 40x20x10 at z[10,20], y[0,20] (datum plane z=10) — joins base+roof.
    Rear half (y[20,40]) has a 10mm gap between base top (z=10) and roof bottom (z=20).

    Probe: a 10x10 region on a datum plane at z=10, in the rear half, extruded
    up_to_next UPWARD -> must rise exactly 10mm to the roof underside (z=20)."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(10.0)},
              refs={"base": "plane.top"}),                                  # z=10 plane
        _feat("f2", "plane", {"method": "offset", "offset": _dim(20.0)},
              refs={"base": "plane.top"}),                                  # z=20 plane
        # base
        _feat("f3", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
        # roof (on z=20 plane)
        _feat("f4", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(10),
                            "centered": False, "merge": True}, refs={"at": "f2"}),
        # front wall y[0,20], z[10,20] joins base+roof into one solid
        _feat("f5", "box", {"length": _dim(40), "width": _dim(20), "height": _dim(10),
                            "centered": False, "merge": True}, refs={"at": "f1"}),
        # probe sketch on the z=10 datum plane, rear half (y 25..35)
        _rect_sketch("f6", 15, 25, 10, 10, "f1"),
        _feat("f7", "extrude", {
            "mode": "boss", "end": "up_to_next", "depth": _dim(99.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f6", 20, 30, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f3", "f4", "f5", "f6", "f7"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    # base 16000 + roof 16000 + wall 8000 + probe 10x10x10=1000
    expected = 16000.0 + 16000.0 + 8000.0 + 1000.0
    assert abs(vol - expected) / expected < 0.02, \
        f"up_to_next boss vol {vol:.1f} vs expected {expected} (probe should rise 10mm to z=20)"
    print(f"[up_to_next-boss] vol={vol:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 2: up_to_next cut

def test_up_to_next_cut_stops_at_next_face() -> None:
    """A 40x40x30 solid block with a blind pocket opened from the BOTTOM in the rear half
    (z[0,15], y[20,40]) — leaving an internal downward-facing ceiling at z=15 in the rear.
    A 10x10 cut probe on the block top (datum z=30) over the rear half, extruded
    up_to_next DOWNWARD (flip), must stop at that internal ceiling (z=15) — removing only
    the solid slab z[15,30] (10x10x15 = 1500 mm3), NOT punching into the pocket void.

    The pocket is made by an extrude-cut of the rear-half region sketched on plane.top
    (z=0), cut UP 15mm into the block."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(30.0)},
              refs={"base": "plane.top"}),                                   # z=30 top plane
        # solid block z[0,30]
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(30),
                            "centered": False}, refs={"at": "origin"}),
        # pocket sketch: rear half (y[20,40]) on plane.top (z=0)
        _rect_sketch("f3", 0, 20, 40, 20, "plane.top"),
        # cut up 15mm (normal of plane.top is +Z) -> removes z[0,15] in rear half
        _feat("f4", "extrude", {
            "mode": "cut", "end": "blind", "depth": _dim(15.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f3", 20, 30, 800.0)]}),
        # cut probe on the block top (z=30 datum), rear half
        _rect_sketch("f5", 15, 25, 10, 10, "f1"),
        _feat("f6", "extrude", {
            "mode": "cut", "end": "up_to_next", "depth": _dim(99.0),
            "flip": True, "merge": True,
        }, refs={"profile": [_region_sel("f5", 20, 30, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f2", "f3", "f4", "f5", "f6"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    full = 40.0 * 40.0 * 30.0 - 40.0 * 20.0 * 15.0  # block minus pocket = 48000 - 12000 = 36000
    # up_to_next cut from z=30 downward stops at the internal ceiling z=15: removes 10x10x15.
    expected = full - 1500.0
    assert abs(vol - expected) / expected < 0.03, \
        f"up_to_next cut vol {vol:.1f} vs expected {expected}"
    assert vol < full, "cut removed nothing"
    print(f"[up_to_next-cut] vol={vol:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 3: up_to_next no body

def test_up_to_next_requires_body() -> None:
    """up_to_next with no existing body -> clean FeatureError."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "up_to_next", "depth": _dim(10.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "up_to_next with no body should fail"
    assert "up_to_next requires an existing body" in s["error"], \
        f"expected clear error; got: {s['error']}"
    print(f"[up_to_next-nobody] clean error: {s['error'][:60]}")


# --------------------------------------------------------------------------- test 4: end2 both directions

def test_end2_blind_both_directions_total_height() -> None:
    """Boss with end=blind depth=10 (dir1) and end2=blind depth2=15 (dir2) ->
    total height 25 mm; volume = 20x20x25."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "end2": "blind", "depth2": _dim(15.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"end2 both-dir failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = 20.0 * 20.0 * 25.0
    assert abs(vol - expected) / expected < 0.01, \
        f"both-dir vol {vol:.1f} vs expected {expected}"
    # resolved echoes depth2
    assert abs(s["resolved"].get("depth2", 0.0) - 15.0) < 1e-6, \
        f"resolved.depth2 should be 15; got {s['resolved'].get('depth2')}"
    print(f"[end2-blind] total-height vol={vol:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 5/6: taper

def _top_face_area(res, z_target: float) -> float:
    """Sum the area of planar faces whose center is at z ~= z_target."""
    body = res.bodies[0].shape
    total = 0.0
    for f in body.faces():
        try:
            c = f.center()
        except Exception:  # noqa: BLE001
            continue
        if abs(c.Z - z_target) < 0.5:
            total += f.area
    return total


def test_taper_inward_narrows_top() -> None:
    """Default draft (inward) with positive draft angle narrows the top face area
    relative to the 20x20=400 base."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "draft": _dim(10.0), "draft_outward": False,
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"taper inward failed: {s['error']}"
    top_area = _top_face_area(res, 10.0)
    # inward draft: top smaller than 400
    assert 0 < top_area < 400.0 * 0.99, \
        f"inward draft should shrink top; base 400, top {top_area:.1f}"
    # geometric check: each side moves in by 10*tan(10deg) ~ 1.763 mm -> side ~ 16.47
    side = 20.0 - 2.0 * 10.0 * math.tan(math.radians(10.0))
    expected_top = side * side
    assert abs(top_area - expected_top) / expected_top < 0.05, \
        f"top area {top_area:.1f} vs expected ~{expected_top:.1f}"
    print(f"[taper-inward] top area {top_area:.1f} (base 400, expected ~{expected_top:.1f})")


def test_taper_outward_grows_top() -> None:
    """draft_outward=True flips the sign -> top face larger than the base."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "draft": _dim(10.0), "draft_outward": True,
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"taper outward failed: {s['error']}"
    top_area = _top_face_area(res, 10.0)
    assert top_area > 400.0 * 1.01, \
        f"outward draft should grow top; base 400, top {top_area:.1f}"
    print(f"[taper-outward] top area {top_area:.1f} (base 400, grows)")


# --------------------------------------------------------------------------- test 7: mid_plane total depth

def test_mid_plane_total_depth_equals_depth() -> None:
    """SW semantics: mid_plane depth=12 -> total height 12 (±6 each side), vol=20x20x12."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "mid_plane", "depth": _dim(12.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"mid_plane failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = 20.0 * 20.0 * 12.0
    assert abs(vol - expected) / expected < 0.01, \
        f"mid_plane total depth vol {vol:.1f} vs expected {expected}"
    # Z-extent of the body should be 12 (±6 about the sketch plane z=0)
    bb = res.bodies[0].shape.bounding_box()
    z_extent = bb.max.Z - bb.min.Z
    assert abs(z_extent - 12.0) < 0.05, f"mid_plane z-extent {z_extent:.3f} should be 12"
    assert abs(bb.max.Z - 6.0) < 0.05 and abs(bb.min.Z + 6.0) < 0.05, \
        f"mid_plane should straddle z=0: zmin={bb.min.Z}, zmax={bb.max.Z}"
    print(f"[mid_plane] vol={vol:.1f} z-extent={z_extent:.2f} (total depth = 12)")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_up_to_next_boss_lands_on_step()
    test_up_to_next_cut_stops_at_next_face()
    test_up_to_next_requires_body()
    test_end2_blind_both_directions_total_height()
    test_taper_inward_narrows_top()
    test_taper_outward_grows_top()
    test_mid_plane_total_depth_equals_depth()
    print("\nEXTRUDE-ENDS TESTS PASSED")


if __name__ == "__main__":
    main()
