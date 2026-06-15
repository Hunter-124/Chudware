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

SW-parity extension (P0 "Extrude — end conditions" / docs/SW_PARITY_GAPS.md):
8.  up_to_surface lands the profile exactly on a referenced planar face.
9.  offset_from_surface stops a signed offset past/short of the surface.
10. up_to_vertex lands on the plane through a referenced vertex.
11. up_to_body passes the profile THROUGH a referenced body to its far surface.
12. thin one-side wall = ring volume (perimeter inset by thickness).
13. thin two-side wall straddles the loop (±½ thickness).
14. Dir2 up_to_surface (full end-condition matrix in the second direction).
15. merge=False makes a NEW BODY instead of fusing.
16. up_to_surface with no body / missing target -> clean contained errors.
17. up_to_reference / offset_from_reference planar datum targets.
18. start from_reference / offset_from_reference.
19. non-planar reference targets fail cleanly.
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


def _planar_face_sel(cx: float, cy: float, z: float, area: float,
                     axis: list | None = None) -> dict:
    """A planar-face selector (for params.up_to / up_to2 surface targets)."""
    return {"kind": "face", "surf": "plane", "centroid": [cx, cy, z],
            "axis": axis or [0.0, 0.0, 1.0], "area_or_len": area,
            "owner_feature": None, "hint_index": 0}


def _vertex_sel(cx: float, cy: float, cz: float) -> dict:
    """A vertex selector (for params.up_to vertex targets)."""
    return {"kind": "vertex", "surf": None, "centroid": [cx, cy, cz],
            "axis": None, "area_or_len": None,
            "owner_feature": None, "hint_index": 0}


def _z_extent(res, body_index: int = 0) -> tuple[float, float]:
    bb = res.bodies[body_index].shape.bounding_box()
    return bb.min.Z, bb.max.Z


def _cyl_face_sel(owner: str) -> dict:
    return {"kind": "face", "surf": "cylinder", "centroid": [10.0, 0.0, 10.0],
            "axis": [0.0, 0.0, 1.0], "area_or_len": 1200.0,
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


# --------------------------------------------------------------------------- test 8: up_to_surface

def test_up_to_surface_lands_on_face() -> None:
    """A 40x40x20 block at z[0,20]. A 10x10 probe on a datum plane at z=40, extruded DOWN
    (flip) with end=up_to_surface targeting the block TOP face (z=20), must land exactly on
    z=20 — the new body spans z[20,40] (10x10x20 = 2000 mm3). merge=False keeps it separate
    so the assert is unambiguous."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(40.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f3", 15, 15, 10, 10, "f1"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "up_to_surface", "depth": _dim(1.0),
            "flip": True, "merge": False,
            "up_to": _planar_face_sel(20, 20, 20, 1600.0),
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f2", "f3", "f4"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    assert len(res.bodies) == 2, f"merge=False should make a 2nd body; got {len(res.bodies)}"
    # The new (probe) body is the one whose volume is ~2000.
    vols = sorted(b.shape.volume for b in res.bodies)
    assert abs(vols[0] - 2000.0) / 2000.0 < 0.01, \
        f"up_to_surface probe vol {vols[0]:.1f} vs expected 2000 (should land on z=20)"
    # resolved depth == 20 (40 - 20)
    assert abs(_status(res, "f4")["resolved"].get("depth", 0.0) - 20.0) < 1e-3, \
        f"resolved depth should be 20; got {_status(res, 'f4')['resolved'].get('depth')}"
    # probe body straddles z[20,40]
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    bb = probe.shape.bounding_box()
    assert abs(bb.min.Z - 20.0) < 0.05 and abs(bb.max.Z - 40.0) < 0.05, \
        f"probe should span z[20,40]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[up_to_surface] probe vol={vols[0]:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# ----------------------------------------------------------------- test 8b: up_to_surface via refs.up_to

def test_up_to_surface_via_refs_slot() -> None:
    """Same geometry/result as test 8, but the up-to target lives in refs.up_to (the
    schema-declared slot the host PM form fills) instead of params.up_to. refs.up_to is the
    canonical home; params.up_to remains a back-compat fallback. The probe must still land on
    z=20, spanning z[20,40] (vol 2000) — proving the host's refs path drives the same geometry."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(40.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f3", 15, 15, 10, 10, "f1"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "up_to_surface", "depth": _dim(1.0),
            "flip": True, "merge": False,
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)],
                 "up_to": _planar_face_sel(20, 20, 20, 1600.0)}),
    ])
    _assert_valid(doc)  # refs.up_to must validate as a _SEL_OR_DATUM face selector
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"up_to_surface via refs.up_to failed: {s['error']}"
    assert len(res.bodies) == 2, f"merge=False should make a 2nd body; got {len(res.bodies)}"
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    assert abs(probe.shape.volume - 2000.0) / 2000.0 < 0.01, \
        f"refs.up_to probe vol {probe.shape.volume:.1f} vs expected 2000 (should land on z=20)"
    bb = probe.shape.bounding_box()
    assert abs(bb.min.Z - 20.0) < 0.05 and abs(bb.max.Z - 40.0) < 0.05, \
        f"probe should span z[20,40]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[up_to_surface-refs] probe vol={probe.shape.volume:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 9: offset_from_surface

def test_offset_from_surface_stops_past_face() -> None:
    """Same geometry as test 8 but end=offset_from_surface with offset=+5 — the probe stops
    5 mm PAST the surface (z=20) along the (downward) direction, i.e. at z=15. Span z[15,40]
    = 25 mm, volume 10x10x25 = 2500."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(40.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f3", 15, 15, 10, 10, "f1"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "offset_from_surface", "depth": _dim(1.0),
            "offset": _dim(5.0), "flip": True, "merge": False,
            "up_to": _planar_face_sel(20, 20, 20, 1600.0),
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"offset_from_surface failed: {s['error']}"
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    vol = probe.shape.volume
    assert abs(vol - 2500.0) / 2500.0 < 0.01, \
        f"offset_from_surface vol {vol:.1f} vs expected 2500 (20mm + 5mm offset)"
    bb = probe.shape.bounding_box()
    assert abs(bb.min.Z - 15.0) < 0.05, \
        f"offset should reach z=15 (5mm past z=20); got zmin={bb.min.Z:.2f}"
    assert abs(s["resolved"].get("offset", 0.0) - 5.0) < 1e-6, "resolved.offset should be 5"
    # offset_from_surface must differ from plain up_to_surface (which would stop at z=20).
    assert bb.min.Z < 20.0 - 1e-3, "offset must push past the surface"
    print(f"[offset_from_surface] vol={vol:.1f} zmin={bb.min.Z:.1f} (5mm past z=20)")


# --------------------------------------------------------------------------- test 10: up_to_vertex

def test_up_to_vertex_lands_on_vertex_plane() -> None:
    """A 40x40x25 block has top corner vertices at z=25. A 10x10 probe on plane.top (z=0)
    extruded UP with end=up_to_vertex targeting a top vertex (0,0,25) lands on the plane
    z=25 — volume 10x10x25 = 2500."""
    doc = _doc([
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(25),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f3", 15, 15, 10, 10, "plane.top"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "up_to_vertex", "depth": _dim(1.0),
            "flip": False, "merge": False,
            "up_to": _vertex_sel(0, 0, 25),
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"up_to_vertex failed: {s['error']}"
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    vol = probe.shape.volume
    assert abs(vol - 2500.0) / 2500.0 < 0.01, \
        f"up_to_vertex vol {vol:.1f} vs expected 2500"
    bb = probe.shape.bounding_box()
    assert abs(bb.max.Z - 25.0) < 0.05, f"probe top should reach z=25; got {bb.max.Z:.2f}"
    assert abs(s["resolved"].get("depth", 0.0) - 25.0) < 1e-3, "resolved depth should be 25"
    print(f"[up_to_vertex] vol={vol:.1f} top z={bb.max.Z:.1f}")


# --------------------------------------------------------------------------- test 11: up_to_body

def test_up_to_body_passes_through_body() -> None:
    """A 40x40x20 block sits at z[10,30] (built on a datum at z=10). A 10x10 probe on
    plane.top (z=0) extruded UP with end=up_to_body passes THROUGH the block to its far
    surface (z=30) — span z[0,30], volume 10x10x30 = 3000. This is distinct from up_to_next,
    which would stop at the NEAR surface z=10 (vol 1000)."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(10.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False}, refs={"at": "f1"}),
        _rect_sketch("f3", 15, 15, 10, 10, "plane.top"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "up_to_body", "depth": _dim(1.0),
            "flip": False, "merge": False,
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"up_to_body failed: {s['error']}"
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    vol = probe.shape.volume
    assert abs(vol - 3000.0) / 3000.0 < 0.02, \
        f"up_to_body vol {vol:.1f} vs expected 3000 (passes through to z=30)"
    bb = probe.shape.bounding_box()
    assert abs(bb.max.Z - 30.0) < 0.1, f"probe should reach the far surface z=30; got {bb.max.Z:.2f}"
    # Must reach BEYOND the near surface (z=10) — otherwise it's up_to_next, not up_to_body.
    assert bb.max.Z > 10.0 + 1e-3, "up_to_body must pass the near surface"
    print(f"[up_to_body] vol={vol:.1f} top z={bb.max.Z:.1f} (through to far surface)")


# --------------------------------------------------------------------------- test 12/13: thin feature

def test_thin_one_side_ring_volume() -> None:
    """Thin extrude, one-side (inside) wall: a 20x20 profile with thin_thickness=2 extruded
    blind 10 -> a square tube. Wall = 20x20 outer minus 16x16 inner = 144 mm2 * 10 = 1440."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "thin": True, "thin_thickness": _dim(2.0), "thin_side": "one",
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"thin one-side failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = (20.0 * 20.0 - 16.0 * 16.0) * 10.0  # 144 * 10 = 1440
    assert abs(vol - expected) / expected < 0.02, \
        f"thin one-side vol {vol:.1f} vs expected {expected}"
    # A thin wall removes material vs a solid extrude (4000) — the defining property.
    assert vol < 4000.0 * 0.9, "thin wall must be much less than a solid extrude"
    assert abs(s["resolved"].get("thin_thickness", 0.0) - 2.0) < 1e-6
    print(f"[thin-one] vol={vol:.1f} (expected {expected}, solid would be 4000)")


def test_thin_two_side_straddles_loop() -> None:
    """Thin extrude, two-side wall: thickness=2 straddles the 20x20 loop (±1) -> outer 22x22,
    inner 18x18, ring 22^2-18^2 = 160 * 10 = 1600. The outer footprint grows to 22x22, which
    a one-side (inside) wall never does — the discriminating check."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "thin": True, "thin_thickness": _dim(2.0), "thin_side": "two",
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"thin two-side failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = (22.0 * 22.0 - 18.0 * 18.0) * 10.0  # 160 * 10 = 1600
    assert abs(vol - expected) / expected < 0.02, \
        f"thin two-side vol {vol:.1f} vs expected {expected}"
    # Two-side grows the outer footprint to 22x22 (straddles ±1).
    bb = res.bodies[0].shape.bounding_box()
    assert abs((bb.max.X - bb.min.X) - 22.0) < 0.1, \
        f"two-side outer width should be 22; got {bb.max.X - bb.min.X:.2f}"
    print(f"[thin-two] vol={vol:.1f} outer width={bb.max.X - bb.min.X:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 14: Dir2 surface

def test_dir2_up_to_surface_full_matrix() -> None:
    """The full end-condition matrix in Direction 2: Dir1 blind +5 (up to z=5), Dir2
    up_to_surface DOWN onto a block top face at z=-10. Result spans z[-10,5] = 15 mm,
    volume 10x10x15 = 1500. The block sits below at z[-30,-10] so the probe never fuses with
    it (merge=False)."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(-10.0)},
              refs={"base": "plane.top"}),
        _feat("f2", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False, "flip": True}, refs={"at": "f1"}),
        _rect_sketch("f3", 15, 15, 10, 10, "plane.top"),
        _feat("f4", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(5.0),
            "end2": "up_to_surface", "depth2": _dim(1.0), "merge": False,
            "up_to2": _planar_face_sel(20, 20, -10, 1600.0),
        }, refs={"profile": [_region_sel("f3", 20, 20, 100.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"Dir2 up_to_surface failed: {s['error']}"
    probe = min(res.bodies, key=lambda b: b.shape.volume)
    vol = probe.shape.volume
    assert abs(vol - 1500.0) / 1500.0 < 0.02, \
        f"Dir2 up_to_surface vol {vol:.1f} vs expected 1500"
    bb = probe.shape.bounding_box()
    assert abs(bb.min.Z + 10.0) < 0.05 and abs(bb.max.Z - 5.0) < 0.05, \
        f"probe should span z[-10,5]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    assert abs(s["resolved"].get("depth2", 0.0) - 10.0) < 1e-3, \
        f"resolved.depth2 should be 10; got {s['resolved'].get('depth2')}"
    print(f"[dir2-surface] vol={vol:.1f} z[{bb.min.Z:.1f},{bb.max.Z:.1f}]")


# --------------------------------------------------------------------------- test 15: new body

def test_merge_false_makes_new_body() -> None:
    """A base box + a non-overlapping blind boss with merge=False yields TWO bodies (the
    boss is a New Body, not fused). With merge=True (and overlap) it would be one body."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f2", 0, 0, 20, 20, "plane.top"),  # same footprint, would overlap
        _feat("f3", "extrude", {
            "mode": "boss", "end": "blind", "depth": _dim(10.0),
            "flip": False, "merge": False,
        }, refs={"profile": [_region_sel("f2", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f3")["ok"], f"new-body extrude failed: {_status(res, 'f3')['error']}"
    assert len(res.bodies) == 2, \
        f"merge=False should yield 2 bodies; got {len(res.bodies)}"
    # The op is still 'add' (it appends a body), and the new body is the 4000-volume boss.
    vols = sorted(b.shape.volume for b in res.bodies)
    assert abs(vols[1] - 4000.0) < 1.0, f"new body should be 4000; got {vols[1]:.1f}"
    print(f"[new-body] {len(res.bodies)} bodies, vols={[round(v,1) for v in vols]}")


# --------------------------------------------------------------------------- test 16: errors

def test_up_to_surface_requires_body_and_target() -> None:
    """up_to_surface with no body -> clean error; and with a body but no params.up_to ->
    clean 'needs a target' error. Both contained (ok:false), never crashing the rebuild."""
    # (a) no body
    doc_a = _doc([
        _rect_sketch("f1", 0, 0, 20, 20, "plane.top"),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "up_to_surface", "depth": _dim(10.0),
            "up_to": _planar_face_sel(10, 10, 30, 400.0),
        }, refs={"profile": [_region_sel("f1", 10, 10, 400.0)]}),
    ])
    _assert_valid(doc_a)
    res_a = compile_document(doc_a)
    s_a = _status(res_a, "f2")
    assert not s_a["ok"], "up_to_surface with no body should fail"
    assert "requires an existing body" in s_a["error"], f"got: {s_a['error']}"

    # (b) body present, but no up_to target
    doc_b = _doc([
        _feat("f0", "box", {"length": _dim(40), "width": _dim(40), "height": _dim(20),
                            "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f1", 60, 60, 20, 20, "plane.top"),  # off to the side
        _feat("f2", "extrude", {
            "mode": "boss", "end": "up_to_surface", "depth": _dim(10.0),
        }, refs={"profile": [_region_sel("f1", 70, 70, 400.0)]}),
    ])
    _assert_valid(doc_b)
    res_b = compile_document(doc_b)
    s_b = _status(res_b, "f2")
    assert not s_b["ok"], "up_to_surface with no target should fail"
    assert "needs a target" in s_b["error"], f"got: {s_b['error']}"
    print(f"[up_to_surface-errors] no-body + no-target both clean")


# --------------------------------------------------------------------------- test 8-10: references

def test_up_to_reference_datum_depth() -> None:
    """A sketch on Top extruded up to datum plane z=15 has exactly 15mm height."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(15.0)},
              refs={"base": "plane.top"}),
        _rect_sketch("f2", 0, 0, 20, 20, "plane.top"),
        _feat("f3", "extrude", {
            "mode": "boss", "end": "up_to_reference", "depth": _dim(99.0),
            "flip": False, "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f2", 10, 10, 400.0)], "end_ref": "f1"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], s["error"]
    vol = res.bodies[0].shape.volume
    assert abs(vol - 20 * 20 * 15) < 0.5, f"up_to_reference volume {vol:.1f}"
    assert abs(s["resolved"]["depth"] - 15.0) < 1e-6
    print(f"[up_to_reference] datum z=15 depth={s['resolved']['depth']:.1f}")


def test_offset_from_reference_datum_depth() -> None:
    """Offset from reference moves the terminating plane by the signed offset."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(20.0)},
              refs={"base": "plane.top"}),
        _rect_sketch("f2", 0, 0, 20, 20, "plane.top"),
        _feat("f3", "extrude", {
            "mode": "boss", "end": "offset_from_reference",
            "end_offset": _dim(-5.0), "depth": _dim(99.0),
            "flip": False, "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f2", 10, 10, 400.0)], "end_ref": "f1"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], s["error"]
    assert abs(res.bodies[0].shape.volume - 20 * 20 * 15) < 0.5
    assert abs(s["resolved"]["depth"] - 15.0) < 1e-6
    assert abs(s["resolved"]["end_offset"] + 5.0) < 1e-6
    print(f"[offset_from_reference] datum z=20 offset=-5 depth={s['resolved']['depth']:.1f}")


def test_start_from_reference_and_offset() -> None:
    """Start from datum + offset shifts the profile before a blind extrusion."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(5.0)},
              refs={"base": "plane.top"}),
        _rect_sketch("f2", 0, 0, 20, 20, "plane.top"),
        _feat("f3", "extrude", {
            "mode": "boss", "start": "offset_from_reference", "start_offset": _dim(2.0),
            "end": "blind", "depth": _dim(10.0),
            "flip": False, "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f2", 10, 10, 400.0)], "start_ref": "f1"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], s["error"]
    bb = res.bodies[0].shape.bounding_box()
    assert abs(bb.min.Z - 7.0) < 0.05, f"start z {bb.min.Z:.3f}"
    assert abs(bb.max.Z - 17.0) < 0.05, f"end z {bb.max.Z:.3f}"
    assert abs(s["resolved"]["start_offset"] - 7.0) < 1e-6
    print("[start_offset_from_reference] start z=7, blind depth=10")


def test_reference_requires_planar_target() -> None:
    """A cylindrical face cannot be used as an up-to-reference target in v1."""
    doc = _doc([
        _feat("f1", "cylinder", {"radius": _dim(10), "height": _dim(20),
                                  "centered": False}, refs={"at": "origin"}),
        _rect_sketch("f2", -5, -5, 10, 10, "plane.top"),
        _feat("f3", "extrude", {
            "mode": "boss", "end": "up_to_reference", "depth": _dim(10.0),
            "flip": False, "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f2", 0, 0, 100.0)], "end_ref": _cyl_face_sel("f1")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "non-planar reference must fail"
    assert "planar" in s["error"], s["error"]
    print(f"[reference-nonplanar] clean error: {s['error'][:80]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_up_to_next_boss_lands_on_step()
    test_up_to_next_cut_stops_at_next_face()
    test_up_to_next_requires_body()
    test_end2_blind_both_directions_total_height()
    test_taper_inward_narrows_top()
    test_taper_outward_grows_top()
    test_mid_plane_total_depth_equals_depth()
    test_up_to_surface_lands_on_face()
    test_offset_from_surface_stops_past_face()
    test_up_to_vertex_lands_on_vertex_plane()
    test_up_to_body_passes_through_body()
    test_thin_one_side_ring_volume()
    test_thin_two_side_straddles_loop()
    test_dir2_up_to_surface_full_matrix()
    test_merge_false_makes_new_body()
    test_up_to_surface_requires_body_and_target()
    test_up_to_reference_datum_depth()
    test_offset_from_reference_datum_depth()
    test_start_from_reference_and_offset()
    test_reference_requires_planar_target()
    print("\nEXTRUDE-ENDS TESTS PASSED")


if __name__ == "__main__":
    main()
