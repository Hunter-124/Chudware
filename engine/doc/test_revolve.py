r"""C-revolve — SolidWorks-parity revolve options: Dir2, Mid-Plane, Up-To-Surface,
thin feature, and sketch-centerline axis.

Run:  & $py -m pytest engine\doc\test_revolve.py -q   (from repo root)

Pytest-style (also importable as plain functions). Mirrors the harness pattern of
test_extrude_ends.py (build a doc, validate, compile, assert geometry).

Geometry baseline used throughout: a rectangular profile sketched in the XZ plane
(``plane.front``) offset from the Z axis, revolved about the Z axis (``axis.z``) →
a tube/ring whose volume is the Pappus solid of revolution. For a full 360° revolve
of a rectangle x∈[R0,R1], z∈[z0,z1] about Z:
    V = pi * (R1^2 - R0^2) * (z1 - z0)
A partial revolve of angle θ scales that by θ/360.

Covers:
1. Baseline single-direction blind revolve still works (known volume).
2. Direction 2 adds material (Dir1 90° + Dir2 90° = 180° wedge, double the volume).
3. Mid-Plane is symmetric about the sketch plane (straddles y=0 in the swept frame).
4. Up-To-Surface revolves until a body face and trims there (bounded volume).
5. Thin feature produces a hollow result (volume < solid revolve, body still valid).
6. Sketch construction CENTERLINE is used as the axis when the axis ref is a region.
7. Full 360° revolve volume sanity (closed ring).
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- harness

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


def _rect_sketch(fid: str, x0: float, z0: float, w: float, h: float, plane_ref,
                 extra_entities: list[dict] | None = None) -> dict:
    """A rectangle in sketch-local (u,v) space x∈[x0,x0+w], z∈[z0,z0+h].

    On ``plane.front`` (XZ) the sketch u-axis is world +X and v-axis is world +Z,
    so the rectangle lives in the XZ plane offset from the Z axis — ready to revolve
    about Z into a ring/tube. ``extra_entities`` (e.g. a construction centerline) are
    appended verbatim.
    """
    ents = [
        {"id": "e1", "type": "line", "construction": False,
         "p1": [x0, z0], "p2": [x0 + w, z0]},
        {"id": "e2", "type": "line", "construction": False,
         "p1": [x0 + w, z0], "p2": [x0 + w, z0 + h]},
        {"id": "e3", "type": "line", "construction": False,
         "p1": [x0 + w, z0 + h], "p2": [x0, z0 + h]},
        {"id": "e4", "type": "line", "construction": False,
         "p1": [x0, z0 + h], "p2": [x0, z0]},
    ]
    if extra_entities:
        ents.extend(extra_entities)
    return _feat(fid, "sketch", {"entities": ents, "constraints": []},
                 refs={"plane": plane_ref})


def _region_sel(sketch_id: str) -> dict:
    """Whole-sketch region profile (all material regions)."""
    return {"kind": "region", "sketch": sketch_id}


def _ring_volume(r0: float, r1: float, height: float, angle_deg: float) -> float:
    """Exact volume of revolving rect x∈[r0,r1], z-height ``height`` about Z by angle."""
    return math.pi * (r1 * r1 - r0 * r0) * height * (angle_deg / 360.0)


# --------------------------------------------------------------------------- 1. baseline

def test_baseline_blind_revolve_volume() -> None:
    """Single-direction blind 120° revolve of a rectangle r∈[10,20], h=5 about Z."""
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(120.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"baseline revolve failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = _ring_volume(10, 20, 5, 120.0)
    assert abs(vol - expected) / expected < 0.02, \
        f"baseline 120° revolve vol {vol:.1f} vs expected {expected:.1f}"
    print(f"[baseline] vol={vol:.1f} (expected {expected:.1f})")


# --------------------------------------------------------------------------- 2. Dir2

def test_dir2_adds_material() -> None:
    """Dir1 90° + Dir2 90° (about the reversed axis) = a 180° sweep — exactly double
    the material of the 90° Dir1-only revolve."""
    base_doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(90.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(base_doc)
    base = compile_document(base_doc)
    assert _status(base, "f2")["ok"], _status(base, "f2")["error"]
    vol_dir1 = base.bodies[0].shape.volume

    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(90.0), "dir2": True, "angle2": _dim(90.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"dir2 revolve failed: {s['error']}"
    vol_both = res.bodies[0].shape.volume

    expected_both = _ring_volume(10, 20, 5, 180.0)
    assert abs(vol_both - expected_both) / expected_both < 0.02, \
        f"dir2 (180° total) vol {vol_both:.1f} vs expected {expected_both:.1f}"
    # Dir2 must ADD material relative to Dir1-only.
    assert vol_both > vol_dir1 * 1.5, \
        f"Dir2 should add material: dir1={vol_dir1:.1f}, both={vol_both:.1f}"
    assert abs(s["resolved"].get("angle2", 0.0) - 90.0) < 1e-6, \
        f"resolved.angle2 should echo 90; got {s['resolved'].get('angle2')}"
    print(f"[dir2] dir1={vol_dir1:.1f} both={vol_both:.1f} (expected {expected_both:.1f})")


# --------------------------------------------------------------------------- 3. Mid-Plane

def test_mid_plane_is_symmetric() -> None:
    """Mid-Plane 90° revolve straddles the sketch plane (±45°). The profile plane is
    the world XZ plane (y=0); a symmetric ±45° sweep about Z must produce a body
    symmetric in Y (ymin ≈ -ymax) and with the same volume as a one-direction 90°."""
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "end": "mid_plane", "angle": _dim(90.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"mid_plane revolve failed: {s['error']}"
    body = res.bodies[0].shape
    vol = body.volume
    expected = _ring_volume(10, 20, 5, 90.0)
    assert abs(vol - expected) / expected < 0.03, \
        f"mid_plane 90° vol {vol:.1f} vs expected {expected:.1f}"
    bb = body.bounding_box()
    # Symmetric about y=0: |ymin| ≈ |ymax|, and both non-trivial.
    assert bb.max.Y > 1.0 and bb.min.Y < -1.0, \
        f"mid_plane should straddle y=0: ymin={bb.min.Y:.3f}, ymax={bb.max.Y:.3f}"
    assert abs(bb.max.Y + bb.min.Y) < 0.05 * max(bb.max.Y, 1.0), \
        f"mid_plane not symmetric in Y: ymin={bb.min.Y:.3f}, ymax={bb.max.Y:.3f}"
    print(f"[mid_plane] vol={vol:.1f} ymin={bb.min.Y:.2f} ymax={bb.max.Y:.2f}")


# --------------------------------------------------------------------------- 4. Up-To-Surface

def test_up_to_surface_trims_to_body() -> None:
    """A wall blocks the CCW revolve before it completes. A profile revolved up_to_surface
    about Z must stop at that wall: the boss adds only the slab swept from the start (0°) up
    to the wall face, strictly more than zero and strictly less than a full 360° ring.

    Wall geometry: a datum plane offset from plane.front (z_dir=-Y) by -19 lands at y=+19;
    a centered box (X=60→x[-30,30], Y=22→y[8,30], Z=20→z[-10,10]) on it spans y∈[8,30] and
    fully covers the profile's z∈[0,5]. The revolve sweeps the +X profile CCW into +Y and is
    terminated where it meets the wall's y=8 face."""
    doc = _doc([
        _feat("f1", "plane", {"method": "offset", "offset": _dim(-19.0)},
              refs={"base": "plane.front"}),                  # datum plane at y=+19
        _feat("f2", "box", {"length": _dim(60), "width": _dim(22), "height": _dim(20),
                            "centered": True}, refs={"at": "f1"}),   # wall y∈[8,30], z∈[-10,10]
        _rect_sketch("f3", 10, 0, 10, 5, "plane.front"),
        _feat("f4", "revolve", {
            "mode": "boss", "end": "up_to_surface", "angle": _dim(360.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f3")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f1", "f2", "f3", "f4"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    wall_vol = 60.0 * 22.0 * 20.0
    full_ring = _ring_volume(10, 20, 5, 360.0)
    # boss = wall + (start slab up to the wall). Strictly above the wall, strictly below
    # wall + a full ring (the sweep was terminated by the surface).
    assert vol > wall_vol + 1.0, \
        f"up_to_surface added no material: vol {vol:.1f} vs wall {wall_vol:.1f}"
    assert vol < wall_vol + full_ring - 1.0, \
        f"up_to_surface should be trimmed (not a full ring): vol {vol:.1f}"
    print(f"[up_to_surface] vol={vol:.1f} (wall {wall_vol:.1f}, +slab up to surface)")


def test_up_to_surface_requires_body() -> None:
    """up_to_surface with no existing body → clean FeatureError."""
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "end": "up_to_surface", "angle": _dim(360.0),
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "up_to_surface with no body should fail"
    assert "requires an existing body" in s["error"], \
        f"expected clear error; got: {s['error']}"
    print(f"[up_to_surface-nobody] clean error: {s['error'][:60]}")


# --------------------------------------------------------------------------- 5. Thin feature

def test_thin_feature_is_hollow() -> None:
    """Thin revolve hollows the solid: the thin result volume is strictly less than the
    equivalent solid revolve, and still a valid non-empty body (a wall remains)."""
    solid_doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(360.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(solid_doc)
    solid = compile_document(solid_doc)
    assert _status(solid, "f2")["ok"], _status(solid, "f2")["error"]
    vol_solid = solid.bodies[0].shape.volume

    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(360.0), "thin": True,
            "thin_thickness": _dim(2.0), "thin_side": "inside",
            "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"thin revolve failed: {s['error']}"
    vol_thin = res.bodies[0].shape.volume
    assert vol_thin > 1.0, f"thin revolve produced no body (vol {vol_thin:.1f})"
    assert vol_thin < vol_solid - 1.0, \
        f"thin should be hollow: thin {vol_thin:.1f} vs solid {vol_solid:.1f}"
    assert abs(s["resolved"].get("thin", 0.0) - 2.0) < 1e-6, \
        f"resolved.thin should echo thickness 2; got {s['resolved'].get('thin')}"
    print(f"[thin] solid={vol_solid:.1f} thin={vol_thin:.1f} (hollow wall)")


# --------------------------------------------------------------------------- 6. Centerline axis

def test_centerline_axis_from_construction_line() -> None:
    """No explicit axis edge: the axis ref is a region pointing at the sketch, so the
    revolve spins about the sketch's CONSTRUCTION CENTERLINE. The centerline is a vertical
    construction line at u=0 (the world Z axis on plane.front), so revolving the rectangle
    r∈[10,20] about it must match a revolve about axis.z."""
    centerline = {
        "id": "e9", "type": "line", "construction": True,
        "p1": [0.0, -5.0], "p2": [0.0, 10.0],
    }
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front", extra_entities=[centerline]),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(180.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")],
                 # Region axis ref → use the sketch's construction centerline.
                 "axis": {"kind": "region", "sketch": "f1"}}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"centerline-axis revolve failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = _ring_volume(10, 20, 5, 180.0)
    assert abs(vol - expected) / expected < 0.02, \
        f"centerline-axis 180° vol {vol:.1f} vs expected {expected:.1f}"
    print(f"[centerline] vol={vol:.1f} (expected {expected:.1f}, spun about construction line)")


def test_centerline_axis_missing_construction_line_errors() -> None:
    """Region axis ref but the sketch has NO construction line → clean error."""
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(180.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")],
                 "axis": {"kind": "region", "sketch": "f1"}}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "missing centerline should fail"
    assert "construction" in s["error"].lower() or "centerline" in s["error"].lower(), \
        f"expected centerline error; got: {s['error']}"
    print(f"[centerline-missing] clean error: {s['error'][:70]}")


# --------------------------------------------------------------------------- 7. Full ring

def test_full_360_ring_volume() -> None:
    """A full 360° revolve of r∈[10,20], h=5 about Z is a closed ring with the exact
    Pappus volume (a single solid body)."""
    doc = _doc([
        _rect_sketch("f1", 10, 0, 10, 5, "plane.front"),
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(360.0), "flip": False, "merge": True,
        }, refs={"profile": [_region_sel("f1")], "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"full ring revolve failed: {s['error']}"
    assert len(res.bodies) == 1, f"full ring should be one body, got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    expected = _ring_volume(10, 20, 5, 360.0)
    assert abs(vol - expected) / expected < 0.01, \
        f"full ring vol {vol:.1f} vs expected {expected:.1f}"
    print(f"[full-ring] vol={vol:.1f} (expected {expected:.1f})")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_baseline_blind_revolve_volume()
    test_dir2_adds_material()
    test_mid_plane_is_symmetric()
    test_up_to_surface_trims_to_body()
    test_up_to_surface_requires_body()
    test_thin_feature_is_hollow()
    test_centerline_axis_from_construction_line()
    test_centerline_axis_missing_construction_line_errors()
    test_full_360_ring_volume()
    print("\nREVOLVE TESTS PASSED")


if __name__ == "__main__":
    main()
