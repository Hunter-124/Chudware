r"""Sweep — SolidWorks-parity options: guide curves, twist, orientation modes, thin feature.

Run:  & $py -m pytest engine\doc\test_sweep.py -q   (from repo root)

Pytest-style (also importable as plain functions). Mirrors test_extrude_ends.py's harness.
Covers:
1. Plain sweep (square profile up a straight path) lands a known volume.
2. Twisted sweep (1 turn): the section rotates along the path — the xy footprint of a square
   grows from half-extent 5 to the rotated-corner half-extent ~5*sqrt(2), volume preserved.
3. Guide-curve sweep (helix auxiliary spine): the guide reorients the section, so the swept
   footprint differs from the un-guided sweep, volume preserved. (refs.guides is a new sweep ref
   slot the schema does not yet list, so that doc is compiled directly — see the test's NOTE.)
4. Orientation keep_normal (fixed trihedron) on a slanted path: sections stay parallel — the
   solid builds and keeps its cross-section axis-aligned (distinct from follow-path).
5. Thin feature: sweeping the profile as a thin wall yields a HOLLOW tube (inner void volume
   removed; the wall-only volume matches outer-minus-inner).
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


def _assert_all_ok(res, fids) -> None:
    for fid in fids:
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"


def _square_profile(fid: str, half: float, plane_ref) -> dict:
    """A square profile centered on the origin of its sketch plane (side = 2*half)."""
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [-half, -half], "p2": [half, -half]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [half, -half], "p2": [half, half]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [half, half], "p2": [-half, half]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [-half, half], "p2": [-half, -half]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _rect_profile(fid: str, hw: float, hh: float, plane_ref) -> dict:
    """A non-square rectangle (so rotation/orientation shows up in the footprint)."""
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [-hw, -hh], "p2": [hw, -hh]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [hw, -hh], "p2": [hw, hh]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [hw, hh], "p2": [-hw, hh]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [-hw, hh], "p2": [-hw, -hh]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _line_path(fid: str, p1, p2, plane_ref) -> dict:
    """A single-line spine sketch (lifted through its plane to give the 3-D path)."""
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": p1, "p2": p2},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region(sketch_id: str, index: int = 0) -> dict:
    return {"kind": "region", "sketch": sketch_id, "index": index}


def _path_ref(sketch_id: str) -> dict:
    return {"kind": "region", "sketch": sketch_id}


def _body(res):
    return res.bodies[0].shape


def _xy_extent(shape) -> tuple[float, float]:
    bb = shape.bounding_box()
    return (bb.max.X - bb.min.X, bb.max.Y - bb.min.Y)


# --------------------------------------------------------------------------- test 1: plain sweep

def test_plain_sweep_known_volume() -> None:
    """A 10x10 square swept 50mm up a straight Z path -> 5000 mm3 prism, 0<=z<=50."""
    doc = _doc([
        _square_profile("f1", 5.0, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),  # lifts to a +Z line
        _feat("f3", "sweep", {"mode": "boss", "merge": True},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    _assert_all_ok(res, ("f1", "f2", "f3"))
    b = _body(res)
    vol = b.volume
    assert abs(vol - 5000.0) / 5000.0 < 0.01, f"plain sweep vol {vol:.1f} vs 5000"
    bb = b.bounding_box()
    assert abs(bb.min.Z) < 0.05 and abs(bb.max.Z - 50.0) < 0.05, \
        f"plain sweep should span z[0,50]; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"
    print(f"[plain] vol={vol:.1f} (expected 5000)")


# --------------------------------------------------------------------------- test 2: twist

def test_twisted_sweep_rotates_section() -> None:
    """One full turn of twist rotates the square section along the path. A non-twisted square
    has xy half-extent 5 (footprint 10x10); a twisted one passes through 45deg so the footprint
    grows toward the rotated-corner extent ~10*sqrt(2)/... -> diagonal 5*sqrt(2)~7.07 each side.
    Volume is preserved (twist only reorients material)."""
    plain = _doc([
        _square_profile("f1", 5.0, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        _feat("f3", "sweep", {"mode": "boss", "merge": True},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    twisted = _doc([
        _square_profile("f1", 5.0, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        _feat("f3", "sweep", {"mode": "boss", "merge": True, "twist_turns": _dim(1.0)},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    _assert_valid(plain)
    _assert_valid(twisted)
    rp = compile_document(plain)
    rt = compile_document(twisted)
    _assert_all_ok(rp, ("f1", "f2", "f3"))
    _assert_all_ok(rt, ("f1", "f2", "f3"))

    bp, bt = _body(rp), _body(rt)
    ext_p = _xy_extent(bp)
    ext_t = _xy_extent(bt)
    # Plain footprint ~10x10; twisted bbox must be larger in xy (square sweeps through 45deg).
    assert ext_t[0] > ext_p[0] + 0.5 and ext_t[1] > ext_p[1] + 0.5, \
        f"twist should enlarge the xy footprint: plain {ext_p}, twisted {ext_t}"
    # The enlarged footprint should approach the rotated-corner extent ~2*5*sqrt(2)=14.14.
    diag = 2.0 * 5.0 * math.sqrt(2.0)
    assert abs(ext_t[0] - diag) / diag < 0.05, \
        f"twisted footprint {ext_t[0]:.2f} should approach {diag:.2f}"
    # Twist only reorients -> volume preserved.
    assert abs(bt.volume - bp.volume) / bp.volume < 0.02, \
        f"twist must preserve volume: plain {bp.volume:.1f}, twisted {bt.volume:.1f}"
    # resolved echoes the total twist in degrees (1 turn = 360).
    st = _status(rt, "f3")
    assert abs(st["resolved"].get("twist_degrees", 0.0) - 360.0) < 1e-6, \
        f"resolved.twist_degrees should be 360; got {st['resolved'].get('twist_degrees')}"
    print(f"[twist] plain xy {ext_p} -> twisted xy {ext_t} (diag {diag:.2f}); vol {bt.volume:.1f}")


# --------------------------------------------------------------------------- test 3: guide curve

def test_guide_curve_steers_section() -> None:
    """A helix guide (auxiliary spine) reorients the section as it travels up a straight path.
    A non-square 8x3 rectangle keeps an 8x3 footprint with no guide; the helix guide rotates the
    section so the xy footprint differs measurably. Volume is preserved (reorientation only)."""
    no_guide = _doc([
        _rect_profile("f1", 4.0, 1.5, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        _feat("f3", "sweep", {"mode": "boss", "merge": True},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    with_guide = _doc([
        _rect_profile("f1", 4.0, 1.5, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        # helix: 1 turn over 50mm, radius 12, axis +Z at origin -> a spiral around the spine.
        _feat("f3", "helix",
              {"pitch": _dim(50.0), "height": _dim(50.0), "radius": _dim(12.0),
               "turns": _dim(1.0)}, refs={"base": "plane.top"}),
        _feat("f4", "sweep", {"mode": "boss", "merge": True},
              refs={"profile": [_region("f1")], "path": _path_ref("f2"),
                    "guides": [_path_ref("f3")]}),
    ])
    _assert_valid(no_guide)
    # refs.guides is now a registered sweep ref slot (schema.py's _SPEC["sweep"] lists "guides"),
    # so the guide doc validates cleanly through the normal path — no expect-failure shim needed.
    _assert_valid(with_guide)
    rn = compile_document(no_guide)
    rg = compile_document(with_guide)
    _assert_all_ok(rn, ("f1", "f2", "f3"))
    _assert_all_ok(rg, ("f1", "f2", "f3", "f4"))

    bn, bg = _body(rn), _body(rg)
    ext_n = _xy_extent(bn)
    ext_g = _xy_extent(bg)
    # Un-guided footprint is the raw 8x3 rectangle. The guide rotates the section -> footprint
    # changes shape (here it widens in the previously-narrow direction).
    changed = abs(ext_g[0] - ext_n[0]) > 0.4 or abs(ext_g[1] - ext_n[1]) > 0.4
    assert changed, f"guide curve should change the swept footprint: no-guide {ext_n}, guide {ext_g}"
    # Reorientation preserves volume.
    assert abs(bg.volume - bn.volume) / bn.volume < 0.05, \
        f"guide should preserve volume: no-guide {bn.volume:.1f}, guide {bg.volume:.1f}"
    sg = _status(rg, "f4")
    assert sg["resolved"].get("guides") == 1, \
        f"resolved.guides should report 1 applied guide; got {sg['resolved'].get('guides')}"
    print(f"[guide] no-guide xy {ext_n} -> guided xy {ext_g}; vol {bg.volume:.1f}")


# --------------------------------------------------------------------------- test 4: orientation

def test_keep_normal_orientation_builds_parallel_sections() -> None:
    """orientation=keep_normal (fixed trihedron) on a slanted path keeps sections parallel to the
    start frame. The 8x8 square section stays axis-aligned in xy (half-extent 4 in both x and y),
    so the body's xy footprint matches the profile width plus the slant's x-travel."""
    doc = _doc([
        _square_profile("f1", 4.0, "plane.top"),
        # slanted path in XZ: sketch (0,0)->(20,40) lifts to x:0..20, z:0..40.
        _line_path("f2", [0, 0], [20, 40], "plane.front"),
        _feat("f3", "sweep",
              {"mode": "boss", "merge": True, "orientation": "keep_normal"},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    _assert_all_ok(res, ("f1", "f2", "f3"))
    b = _body(res)
    bb = b.bounding_box()
    # Fixed trihedron keeps the section axis-aligned: y half-extent stays the profile's 4 (no
    # rotation pulls material out in y). The body spans x = -4..(20+4)=24, y = -4..4.
    assert abs((bb.max.Y - bb.min.Y) - 8.0) < 0.2, \
        f"keep_normal y-extent should stay ~8 (parallel sections); got {bb.max.Y - bb.min.Y:.2f}"
    assert bb.max.X > 20.0 and bb.min.X < 0.0, \
        f"body should follow the slant in x; got x[{bb.min.X:.2f},{bb.max.X:.2f}]"
    st = _status(res, "f3")
    assert st["resolved"].get("orientation") == "keep_normal", \
        f"resolved.orientation should echo keep_normal; got {st['resolved'].get('orientation')}"
    print(f"[keep_normal] y-extent {bb.max.Y - bb.min.Y:.2f}, x[{bb.min.X:.1f},{bb.max.X:.1f}]")


# --------------------------------------------------------------------------- test 5: thin feature

def test_thin_sweep_is_hollow() -> None:
    """Thin feature: a 10x10 square swept 50mm with a 2mm thin wall is a HOLLOW tube. The wall
    volume = outer(10x10) - inner(6x6) over the 50mm length = (100-36)*50 = 3200 mm3, which is
    strictly less than the solid 5000 -> material is hollowed out."""
    solid = _doc([
        _square_profile("f1", 5.0, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        _feat("f3", "sweep", {"mode": "boss", "merge": True},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    thin = _doc([
        _square_profile("f1", 5.0, "plane.top"),
        _line_path("f2", [0, 0], [0, 50], "plane.front"),
        _feat("f3", "sweep",
              {"mode": "boss", "merge": True, "thin": True, "thin_thickness": _dim(2.0)},
              refs={"profile": [_region("f1")], "path": _path_ref("f2")}),
    ])
    _assert_valid(solid)
    _assert_valid(thin)
    rs = compile_document(solid)
    rt = compile_document(thin)
    _assert_all_ok(rs, ("f1", "f2", "f3"))
    _assert_all_ok(rt, ("f1", "f2", "f3"))

    bs, bt = _body(rs), _body(rt)
    # Hollow: the thin tube has strictly less volume than the solid prism.
    assert bt.volume < bs.volume - 100.0, \
        f"thin sweep should hollow the prism: solid {bs.volume:.1f}, thin {bt.volume:.1f}"
    expected_wall = (100.0 - 36.0) * 50.0  # outer 10x10 minus inner 6x6, over 50mm
    assert abs(bt.volume - expected_wall) / expected_wall < 0.02, \
        f"thin wall vol {bt.volume:.1f} vs expected {expected_wall:.1f}"
    # A hollow tube has more faces than the 6-face solid prism (outer + inner + 2 end rings).
    assert len(bt.faces()) > len(bs.faces()), \
        f"hollow tube should have more faces than the solid prism ({len(bt.faces())} vs {len(bs.faces())})"
    st = _status(rt, "f3")
    assert abs(st["resolved"].get("thin_thickness", 0.0) - 2.0) < 1e-6, \
        f"resolved.thin_thickness should be 2; got {st['resolved'].get('thin_thickness')}"
    print(f"[thin] solid {bs.volume:.1f} -> hollow {bt.volume:.1f} (expected wall {expected_wall:.1f})")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_plain_sweep_known_volume()
    test_twisted_sweep_rotates_section()
    test_guide_curve_steers_section()
    test_keep_normal_orientation_builds_parallel_sections()
    test_thin_sweep_is_hollow()
    print("\nSWEEP TESTS PASSED")


if __name__ == "__main__":
    main()
