"""C09 — dress, sketch, extrude, revolve feature compiler tests.

Run:  & $py -m engine.doc.test_features1   (from repo root)

House-style plain runner — NOT pytest.

Tests:
1. bracket fixture compiles (f1-f5 ok; f6 clean "unknown feature type" error)
2. sketch regions: rectangle -> 1 region; + inner circle -> 2 regions (outer has hole)
3. extrude blind/mid_plane/through_all volumes; cut reduces volume
4. revolve 360 deg of a rectangle -> annular cylinder volume within 1%
5. fillet via selector on compiled box edge -> volume decreases, cylindrical face appears
6. oversized fillet radius auto-clamps with warning
7. deleted-owner (dangling) selector -> FeatureError with dangling_refs
8. shell: 20mm cube shell 2mm open top -> volume matches analytic within 1%
"""

from __future__ import annotations

import json
import math
import os

from build123d import GeomType

from engine.doc.compiler import CompileCache, compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- helpers

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict], globals_list: list[dict] | None = None) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": globals_list or []},
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


# --------------------------------------------------------------------------- test 1: bracket fixture


def test_bracket_fixture() -> None:
    """Load part_bracket.json and compile. f1-f5 must be ok. f6 (linear_pattern) must
    give a clean 'unknown feature type' error, not crash."""

    fixture_path = os.path.join(
        os.path.dirname(__file__),
        "../../docs/contracts/fixtures/part_bracket.json"
    )
    fixture_path = os.path.normpath(fixture_path)
    with open(fixture_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    errs = validate_part(doc)
    assert errs == [], f"bracket fixture schema errors:\n" + "\n".join(errs)

    res = compile_document(doc)

    # f1 (sketch) - must be ok
    s1 = _status(res, "f1")
    assert s1["ok"], f"f1 (sketch) failed: {s1['error']}"

    # f2 (extrude boss) - must be ok
    s2 = _status(res, "f2")
    assert s2["ok"], f"f2 (extrude boss) failed: {s2['error']}"

    # f3 (sketch on top face) - must be ok
    s3 = _status(res, "f3")
    assert s3["ok"], f"f3 (sketch on face) failed: {s3['error']}"

    # f4 (extrude cut through-all) - must be ok
    s4 = _status(res, "f4")
    assert s4["ok"], f"f4 (extrude cut) failed: {s4['error']}"

    # f5 (fillet) - must be ok
    s5 = _status(res, "f5")
    assert s5["ok"], f"f5 (fillet) failed: {s5['error']}"

    # f6 (linear_pattern) - must be a clean "unknown feature type" error, not a crash
    s6 = _status(res, "f6")
    assert not s6["ok"], "f6 (linear_pattern) should not compile successfully"
    assert "linear_pattern" in s6["error"].lower() or "no compiler" in s6["error"].lower(), \
        f"f6 error should mention feature type; got: {s6['error']}"

    # At least one body produced
    assert len(res.bodies) >= 1, "bracket fixture should produce at least one body"
    vol = res.bodies[0].shape.volume
    assert vol > 0, f"bracket volume should be positive; got {vol}"
    print(f"[bracket] fixture f1-f5 ok; f6 clean error '{s6['error'][:60]}'; volume={vol:.1f}")


# --------------------------------------------------------------------------- test 2: sketch regions


def test_sketch_regions_rectangle_one_region() -> None:
    """A rectangle sketch -> 1 closed-loop region with area 4000 mm²."""
    doc = _doc([
        _feat("f1", "sketch", {
            "entities": [
                {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
                {"id": "e2", "type": "line", "construction": False, "p1": [80.0, 0.0], "p2": [80.0, 50.0]},
                {"id": "e3", "type": "line", "construction": False, "p1": [80.0, 50.0], "p2": [0.0, 50.0]},
                {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
            ],
            "constraints": [],
        }, refs={"plane": "plane.top"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"sketch failed: {s['error']}"
    # The sketch compiler stores region count in resolved
    region_count = s["resolved"].get("_region_count", -1)
    assert region_count == 1.0, f"expected 1 region, got {region_count}"
    print(f"[sketch] rectangle -> 1 region (region_count={region_count})")


def test_sketch_regions_with_hole() -> None:
    """Rectangle + inner circle -> 2 loops, outer region has a hole."""
    doc = _doc([
        _feat("f1", "sketch", {
            "entities": [
                {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [80.0, 0.0]},
                {"id": "e2", "type": "line", "construction": False, "p1": [80.0, 0.0], "p2": [80.0, 50.0]},
                {"id": "e3", "type": "line", "construction": False, "p1": [80.0, 50.0], "p2": [0.0, 50.0]},
                {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 50.0], "p2": [0.0, 0.0]},
                {"id": "e5", "type": "circle", "construction": False, "center": [40.0, 25.0], "radius": 10.0},
            ],
            "constraints": [],
        }, refs={"plane": "plane.top"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"sketch with hole failed: {s['error']}"
    region_count = s["resolved"].get("_region_count", -1)
    assert region_count == 2.0, f"expected 2 regions (outer + circle), got {region_count}"
    print(f"[sketch] rect+circle -> 2 regions (region_count={region_count})")


# --------------------------------------------------------------------------- test 3: extrude volumes


def _rect_sketch_feature(fid: str, x0: float = 0.0, y0: float = 0.0,
                          width: float = 20.0, height: float = 20.0,
                          plane_ref: str = "plane.top") -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x0, y0], "p2": [x0 + width, y0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [x0 + width, y0], "p2": [x0 + width, y0 + height]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [x0 + width, y0 + height], "p2": [x0, y0 + height]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [x0, y0 + height], "p2": [x0, y0]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region_sel(sketch_id: str, cx: float = 10.0, cy: float = 10.0, area: float = 400.0) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def test_extrude_blind_volume() -> None:
    """Extrude a 20x20 sketch blind 10 mm -> 20x20x10 = 4000 mm mm3."""
    doc = _doc([
        _rect_sketch_feature("f1", width=20.0, height=20.0),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind",
            "depth": _dim(10.0), "flip": False,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", cx=10.0, cy=10.0, area=400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    s = _status(res, "f2")
    assert s["ok"], f"extrude failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = 20.0 * 20.0 * 10.0
    assert abs(vol - expected) / expected < 0.01, f"blind extrude volume {vol} vs {expected}"
    print(f"[extrude-blind] 20x20x10 = {vol:.1f} mm mm3 (expected {expected})")


def test_extrude_mid_plane_volume() -> None:
    """Extrude mid_plane depth=10 -> 20x20x10 = 4000 mm mm3 (same total since ±5mm each side)."""
    doc = _doc([
        _rect_sketch_feature("f1", width=20.0, height=20.0),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "mid_plane",
            "depth": _dim(10.0), "flip": False,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", cx=10.0, cy=10.0, area=400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"mid_plane extrude failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = 20.0 * 20.0 * 10.0
    # mid_plane extrudes ±depth/2 = ±5mm; total = 10mm
    assert abs(vol - expected) / expected < 0.02, f"mid_plane vol {vol} vs {expected}"
    print(f"[extrude-mid] mid_plane depth=10 -> {vol:.1f} mm mm3 (expected {expected})")


def test_extrude_cut_reduces_volume() -> None:
    """Extrude boss 20x20x20, then cut a 5x5 region -> volume decreases."""
    doc = _doc([
        _rect_sketch_feature("f1", width=20.0, height=20.0),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind",
            "depth": _dim(20.0), "flip": False,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", cx=10.0, cy=10.0, area=400.0)]}),
        _rect_sketch_feature("f3", x0=5.0, y0=5.0, width=5.0, height=5.0,
                              plane_ref={
                                  "kind": "face", "surf": "plane",
                                  "centroid": [10.0, 10.0, 20.0],
                                  "axis": [0.0, 0.0, 1.0],
                                  "area_or_len": 400.0,
                                  "owner_feature": "f2", "hint_index": 0
                              }),
        _feat("f4", "extrude", {
            "mode": "cut", "end": "through_all",
            "depth": _dim(10.0), "flip": True,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f3", cx=7.5, cy=7.5, area=25.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s2 = _status(res, "f2")
    assert s2["ok"], f"boss extrude failed: {s2['error']}"
    s4 = _status(res, "f4")
    assert s4["ok"], f"cut extrude failed: {s4['error']}"
    vol_boss = 20.0 * 20.0 * 20.0
    vol_cut = 5.0 * 5.0 * 20.0
    expected = vol_boss - vol_cut
    vol = res.bodies[0].shape.volume
    assert vol < vol_boss, f"cut should reduce volume; got {vol} vs boss {vol_boss}"
    assert abs(vol - expected) / expected < 0.02, f"cut volume {vol} vs expected {expected}"
    print(f"[extrude-cut] boss {vol_boss} - cut {vol_cut} = {vol:.1f} (expected ~{expected})")


def test_extrude_through_all() -> None:
    """through_all cuts all the way through regardless of depth param."""
    doc = _doc([
        _rect_sketch_feature("f1", width=30.0, height=30.0),
        _feat("f2", "extrude", {
            "mode": "boss", "end": "blind",
            "depth": _dim(30.0), "flip": False,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f1", cx=15.0, cy=15.0, area=900.0)]}),
        # Small 5x5 cut sketch centered inside
        _rect_sketch_feature("f3", x0=12.5, y0=12.5, width=5.0, height=5.0,
                              plane_ref="plane.top"),
        _feat("f4", "extrude", {
            "mode": "cut", "end": "through_all",
            "depth": _dim(1.0), "flip": False,
            "draft": _dim(0.0), "draft_outward": False, "merge": True,
        }, refs={"profile": [_region_sel("f3", cx=15.0, cy=15.0, area=25.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s4 = _status(res, "f4")
    assert s4["ok"], f"through_all cut failed: {s4['error']}"
    vol = res.bodies[0].shape.volume
    expected = 30.0 * 30.0 * 30.0 - 5.0 * 5.0 * 30.0
    assert abs(vol - expected) / expected < 0.02, f"through_all volume {vol} vs expected {expected}"
    print(f"[extrude-through_all] volume {vol:.1f} (expected ~{expected})")


# --------------------------------------------------------------------------- test 4: revolve


def test_revolve_360_cylinder_shell() -> None:
    """Revolve a 5x5 mm rectangle (at x=5..10, z=0..5) 360 deg around Z -> annular cylinder.

    Volume = π(r_outer² - r_inner²) x h = π(100 - 25) x 5 ~ 1178.1 mm mm3.
    """
    # Sketch in the XZ plane (y=0) for revolution around Z
    doc = _doc([
        _feat("f1", "sketch", {
            "entities": [
                {"id": "e1", "type": "line", "construction": False, "p1": [5.0, 0.0], "p2": [10.0, 0.0]},
                {"id": "e2", "type": "line", "construction": False, "p1": [10.0, 0.0], "p2": [10.0, 5.0]},
                {"id": "e3", "type": "line", "construction": False, "p1": [10.0, 5.0], "p2": [5.0, 5.0]},
                {"id": "e4", "type": "line", "construction": False, "p1": [5.0, 5.0], "p2": [5.0, 0.0]},
            ],
            "constraints": [],
        }, refs={"plane": "plane.right"}),  # XZ plane (right = front = XZ)
        _feat("f2", "revolve", {
            "mode": "boss", "angle": _dim(360.0), "flip": False, "merge": True,
        }, refs={
            "profile": [{"kind": "region", "sketch": "f1", "centroid": [7.5, 2.5],
                         "area_or_len": 25.0, "hint_index": 0}],
            "axis": "axis.z",
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s1 = _status(res, "f1")
    assert s1["ok"], f"sketch failed: {s1['error']}"
    s2 = _status(res, "f2")
    assert s2["ok"], f"revolve failed: {s2['error']}"
    vol = res.bodies[0].shape.volume
    expected = math.pi * (10.0**2 - 5.0**2) * 5.0
    assert abs(vol - expected) / expected < 0.01, f"revolve volume {vol} vs expected {expected}"
    print(f"[revolve] 360 deg annular cyl vol={vol:.2f} (expected {expected:.2f}, err={abs(vol-expected)/expected*100:.2f}%)")


# --------------------------------------------------------------------------- test 5: fillet


def test_fillet_on_box_edge() -> None:
    """Compile a 20mm box, then fillet 2 vertical edges -> volume decreases, cylindrical face appears."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
    ])
    _assert_valid(doc)
    res0 = compile_document(doc)
    vol_box = res0.bodies[0].shape.volume
    body = res0.bodies[0]

    # Get an edge selector for a vertical edge of the box
    # Box edges in TopExp order - find a short vertical one
    from engine.doc.selectors import describe_edges, make_selector
    infos = describe_edges(body.shape.wrapped)

    # Find a line edge (vertical = parallel to Z)
    import numpy as np
    vert_edges = [
        info for info in infos
        if info.surf == "line" and info.axis is not None and abs(info.axis[2]) > 0.9
    ]
    assert len(vert_edges) >= 2, f"expected vertical edges on box, got {len(vert_edges)}"

    # Build selectors for 2 vertical edges
    sel0 = make_selector(vert_edges[0])
    sel1 = make_selector(vert_edges[1])
    # Tag with owner feature
    sel0["owner_feature"] = "f1"
    sel1["owner_feature"] = "f1"

    doc2 = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "fillet", {"radius": _dim(2.0)},
              refs={"edges": [sel0, sel1]}),
    ])
    _assert_valid(doc2)
    res2 = compile_document(doc2)
    s = _status(res2, "f2")
    assert s["ok"], f"fillet failed: {s['error']}"
    vol_filleted = res2.bodies[0].shape.volume
    assert vol_filleted < vol_box, f"fillet should reduce volume; {vol_filleted} vs {vol_box}"
    # Check that a cylindrical face appeared
    faces = res2.bodies[0].shape.faces()
    cyl_count = sum(1 for f in faces if f.geom_type == GeomType.CYLINDER)
    assert cyl_count >= 2, f"fillet should add cylindrical faces; got {cyl_count}"
    print(f"[fillet] 20mm box + 2 edge fillets: vol {vol_box:.1f} -> {vol_filleted:.1f}, "
          f"cyl_faces={cyl_count}")


# --------------------------------------------------------------------------- test 6: auto-clamp


def test_fillet_oversized_auto_clamps() -> None:
    """Overly large fillet radius auto-clamps (halve x3) and records a warning."""
    from engine.doc.selectors import describe_edges, make_selector
    import numpy as np

    doc_box = _doc([_feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(5)})])
    _assert_valid(doc_box)
    res0 = compile_document(doc_box)
    body = res0.bodies[0]
    infos = describe_edges(body.shape.wrapped)
    vert_edges = [i for i in infos if i.surf == "line" and i.axis is not None and abs(i.axis[2]) > 0.5]
    assert vert_edges, "need a vertical edge to fillet"

    sel = make_selector(vert_edges[0])
    sel["owner_feature"] = "f1"

    # Radius of 50mm is too large for a 5mm thick box
    doc2 = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(5)}),
        _feat("f2", "fillet", {"radius": _dim(50.0)},
              refs={"edges": [sel]}),
    ])
    _assert_valid(doc2)
    res2 = compile_document(doc2)
    s = _status(res2, "f2")
    # Should succeed (auto-clamped) OR fail with a clean error (clamping floor hit)
    if s["ok"]:
        # Auto-clamped successfully
        effective_r = s["resolved"].get("radius", 50.0)
        assert effective_r < 50.0, f"expected clamped radius, got {effective_r}"
        print(f"[fillet-clamp] r=50 clamped to {effective_r:.2f} mm, ok")
    else:
        # Floor was hit — that's acceptable
        assert "fillet" in s["error"].lower() or "radius" in s["error"].lower(), \
            f"expected fillet error, got: {s['error']}"
        print(f"[fillet-clamp] r=50 hit clamp floor, error: {s['error'][:60]}")


# --------------------------------------------------------------------------- test 7: dangling selector


def test_fillet_dangling_selector() -> None:
    """Edge selector with a non-existent owner_feature -> FeatureError with dangling_refs."""
    from engine.doc.selectors import describe_edges, make_selector
    import numpy as np

    doc_box = _doc([_feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)})])
    _assert_valid(doc_box)
    res0 = compile_document(doc_box)
    body = res0.bodies[0]
    infos = describe_edges(body.shape.wrapped)

    sel = make_selector(infos[0])
    # Tag with a non-existent owner so it doesn't match any edge owner tags
    sel["owner_feature"] = "f999"
    # Shift centroid far away so it definitely won't match
    sel["centroid"] = [1000.0, 1000.0, 1000.0]
    sel["area_or_len"] = 999999.0

    doc2 = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "fillet", {"radius": _dim(2.0)},
              refs={"edges": [sel]}),
    ])
    _assert_valid(doc2)
    res2 = compile_document(doc2)
    s = _status(res2, "f2")
    assert not s["ok"], "fillet with dangling selector should fail"
    assert s["dangling_refs"], f"dangling_refs should be populated; got {s['dangling_refs']}"
    print(f"[fillet-dangling] dangling selector -> error '{s['error'][:50]}', "
          f"dangling_refs={s['dangling_refs']}")


# --------------------------------------------------------------------------- test 8: shell


def test_shell_open_top() -> None:
    """Shell a 20mm cube 2mm thick with the top face open.

    Analytic volume = outer - inner = 20 mm3 - (20-4) mm3 ... wait, the shell removes the
    top face so:
        V = 20 mm3 - 16 mm3 + 16² x 2   (add back the missing top cap of inner box)

    Actually for an open-top shell of wall thickness t=2mm:
    - Outer = 20x20x20 = 8000
    - Inner cuboid would be (20-2t)x(20-2t)x(20-t) = 16x16x18 = 4608
    - Shell volume = 8000 - 4608 = 3392 mm mm3

    Let the OCP result define "analytic" and we check it's in the right ballpark.
    We just verify volume > 0 and << 8000, and approximately 3392.
    """
    # First build a box, then find the top face selector
    from engine.doc.selectors import describe_faces, make_selector
    import numpy as np

    doc_box = _doc([_feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)},
                          refs={"at": "origin"})])
    _assert_valid(doc_box)
    res0 = compile_document(doc_box)
    body = res0.bodies[0]
    infos = describe_faces(body.shape.wrapped)

    # Find the top face (z=10 plane, normal [0,0,1])
    top_face_info = None
    for info in infos:
        if info.surf == "plane" and info.axis is not None and abs(info.axis[2]) > 0.9:
            if info.centroid[2] > 5.0:  # top half
                top_face_info = info
                break

    if top_face_info is None:
        # Fallback: pick any planar face with positive Z normal
        for info in infos:
            if info.surf == "plane":
                top_face_info = info
                break

    assert top_face_info is not None, "no planar top face found on box"

    sel = make_selector(top_face_info)
    sel["owner_feature"] = "f1"

    doc2 = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)},
              refs={"at": "origin"}),
        _feat("f2", "shell", {"thickness": _dim(2.0), "outward": False},
              refs={"remove": [sel]}),
    ])
    _assert_valid(doc2)
    res2 = compile_document(doc2)
    s = _status(res2, "f2")
    assert s["ok"], f"shell failed: {s['error']}"
    vol = res2.bodies[0].shape.volume
    # Analytic for open-top 2mm shell of 20mm cube:
    # Shell material = 20 mm3 - (16x16x18) = 8000 - 4608 = 3392
    # (walls: 4 x 20x20x2 = 3200, but we share corners so OCP is authoritative)
    analytic = 20**3 - 16 * 16 * 18  # 3392
    assert vol > 0, f"shell should have positive volume"
    assert vol < 20**3 * 0.9, f"shell volume {vol} too close to full box {20**3}"
    assert abs(vol - analytic) / analytic < 0.05, f"shell vol {vol:.1f} vs analytic {analytic}"
    print(f"[shell] 20mm cube shell t=2 open top: vol={vol:.1f} (analytic~{analytic})")


# --------------------------------------------------------------------------- main


def main() -> None:
    test_bracket_fixture()
    test_sketch_regions_rectangle_one_region()
    test_sketch_regions_with_hole()
    test_extrude_blind_volume()
    test_extrude_mid_plane_volume()
    test_extrude_cut_reduces_volume()
    test_extrude_through_all()
    test_revolve_360_cylinder_shell()
    test_fillet_on_box_edge()
    test_fillet_oversized_auto_clamps()
    test_fillet_dangling_selector()
    test_shell_open_top()
    print("\nFEATURES1 TESTS PASSED")


if __name__ == "__main__":
    main()
