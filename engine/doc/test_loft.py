r"""Loft SolidWorks-parity options: 2+ sections + guide curves + centerline + start/end
tangency + thin feature  (docs/SW_PARITY_SPEC.md §2 "Loft — core").

Run:  & $py -m pytest engine\doc\test_loft.py -q   (from repo root)

Pytest-style (also importable as plain functions). Covers:
1. Plain 2-section loft (20x20 -> 10x10 over 40mm) lofts a truncated pyramid of the known
   ThruSections volume, with the right z-extent and bbox.
2. Start/end tangency (smooth, tangent-continuous blend) changes the solid vs the ruled
   (straight) blend on a 3-section loft — proving the tangency option is wired through.
3. Centerline loft: a curved spine sketched on the Front plane steers the blend so the solid
   bows out in +X well past the section extent (follows the centerline).
4. Guide-curve loft: an auxiliary guide rail changes the blend (>1% volume) vs the plain ruled
   loft of the same sections, and the result stays a single valid solid.
5. Thin loft: circular sections with a wall thickness produce a hollow shell (volume far below
   the equivalent solid loft), and the bounding box still matches the outer sections.
"""

from __future__ import annotations

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


def _rect_sketch(fid: str, x0: float, y0: float, w: float, h: float, plane_ref) -> dict:
    """A closed rectangle region sketch (a loft section)."""
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


def _circle_sketch(fid: str, cx: float, cy: float, r: float, plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [cx, cy], "radius": r},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _spline_sketch(fid: str, points: list[list[float]], plane_ref) -> dict:
    """An open spline curve sketch — used as a centerline / guide rail.

    On the Front plane (XZ) sketch-u maps to world-X and sketch-v to world-Z, so a spline
    through [u,v] points lays a curve in the world XZ plane spanning the section stack.
    """
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "spline", "construction": False, "points": points},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _z40_plane(fid: str) -> dict:
    """A datum plane offset 40mm above plane.top (the upper section plane)."""
    return _feat(fid, "plane", {"method": "offset", "offset": _dim(40.0)},
                 refs={"base": "plane.top"})


def _section(sketch_id: str) -> dict:
    """A whole-region section selector (single region per section sketch)."""
    return {"kind": "region", "sketch": sketch_id, "index": 0}


# --------------------------------------------------------------------------- test 1: plain

def test_plain_two_section_loft_volume() -> None:
    """20x20 (z=0) -> 10x10 (z=40) loft = a truncated square pyramid.

    Volume of a frustum = h/3 * (A1 + A2 + sqrt(A1*A2)); A1=400, A2=100, h=40
    = 40/3 * (400 + 100 + 200) = 40/3 * 700 = 9333.33.  (Matches the ThruSections build.)
    """
    doc = _doc([
        _z40_plane("f1"),
        _rect_sketch("f2", -10, -10, 20, 20, "plane.top"),
        _rect_sketch("f3", -5, -5, 10, 10, "f1"),
        _feat("f4", "loft", {"mode": "boss", "merge": True},
              refs={"sections": [_section("f2"), _section("f3")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    _assert_all_ok(res, ("f2", "f3", "f4"))
    body = res.bodies[0].shape
    vol = body.volume
    expected = 40.0 / 3.0 * (400.0 + 100.0 + 200.0)  # 9333.33
    assert abs(vol - expected) / expected < 0.01, \
        f"plain loft vol {vol:.1f} vs frustum {expected:.1f}"
    bb = body.bounding_box()
    z_ext = bb.max.Z - bb.min.Z
    assert abs(z_ext - 40.0) < 0.05, f"loft z-extent {z_ext:.3f} should span 40"
    # base section spans x[-10,10]; top spans x[-5,5]; bbox max x = 10.
    assert abs(bb.max.X - 10.0) < 0.05, f"loft xmax {bb.max.X:.3f} should be the base half-width 10"
    print(f"[plain] vol={vol:.1f} (frustum {expected:.1f}) z-ext={z_ext:.2f}")


# --------------------------------------------------------------------------- test 2: tangency

def test_start_end_tangency_changes_shape_vs_ruled() -> None:
    """A 3-section loft (20 -> 16 -> 20) built ruled (straight transitions) vs with start/end
    tangency (smooth, tangent-continuous blend). Tangency forces the surface to leave/enter its
    end sections tangentially, bowing the side walls in — a measurably different volume than the
    straight ruled blend. This proves the tangency option is wired into the compiler."""
    sections = [
        _rect_sketch("f2", -10, -10, 20, 20, "plane.top"),
        None,  # placeholder; mid plane + sketch inserted below
    ]
    mid_plane = _feat("f5", "plane", {"method": "offset", "offset": _dim(20.0)},
                      refs={"base": "plane.top"})
    base = [
        _z40_plane("f1"),
        _rect_sketch("f2", -10, -10, 20, 20, "plane.top"),
        mid_plane,
        _rect_sketch("f6", -8, -8, 16, 16, "f5"),
        _rect_sketch("f3", -10, -10, 20, 20, "f1"),
    ]
    secs = [_section("f2"), _section("f6"), _section("f3")]

    ruled_doc = _doc(base + [
        _feat("f9", "loft", {"mode": "boss", "ruled": True}, refs={"sections": secs}),
    ])
    _assert_valid(ruled_doc)
    ruled_res = compile_document(ruled_doc)
    _assert_all_ok(ruled_res, ("f2", "f6", "f3", "f9"))
    ruled_vol = ruled_res.bodies[0].shape.volume

    tang_doc = _doc(base + [
        _feat("f9", "loft",
              {"mode": "boss", "tangency_start": "tangent", "tangency_end": "tangent"},
              refs={"sections": secs}),
    ])
    _assert_valid(tang_doc)
    tang_res = compile_document(tang_doc)
    s9 = _status(tang_res, "f9")
    assert s9["ok"], f"tangency loft failed: {s9['error']}"
    tang_vol = tang_res.bodies[0].shape.volume

    # The tangent/smooth blend must differ from the ruled blend by a clear margin.
    rel = abs(ruled_vol - tang_vol) / ruled_vol
    assert rel > 0.01, \
        f"tangency should change the shape: ruled {ruled_vol:.1f} vs tangent {tang_vol:.1f} (rel {rel:.4f})"
    # The resolved status should record the tangency request.
    assert "_tangency" in s9.get("resolved", {}), "tangency should be echoed in resolved"
    print(f"[tangency] ruled {ruled_vol:.1f} vs tangent {tang_vol:.1f} (rel {rel:.3f})")


# --------------------------------------------------------------------------- test 3: centerline

def test_centerline_loft_follows_curve() -> None:
    """20x20 (z=0) -> 10x10 (z=40) lofted along a CURVED centerline that bows to X=20 at the
    middle (a Front-plane spline through (0,0)->(20,20)->(0,40), i.e. world (0,0,0)->(20,0,20)
    ->(0,0,40)). The solid must follow the spine and reach well past the sections' own +X
    extent (10mm) — proving the centerline steers the loft."""
    doc = _doc([
        _z40_plane("f1"),
        _rect_sketch("f2", -10, -10, 20, 20, "plane.top"),
        _rect_sketch("f3", -5, -5, 10, 10, "f1"),
        _spline_sketch("f4", [[0, 0], [20, 20], [0, 40]], "plane.front"),
        _feat("f9", "loft", {"mode": "boss"},
              refs={"sections": [_section("f2"), _section("f3")],
                    "centerline": {"kind": "region", "sketch": "f4"}}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    _assert_all_ok(res, ("f2", "f3", "f4", "f9"))
    body = res.bodies[0].shape
    bb = body.bounding_box()
    # A straight (no-centerline) loft of these sections reaches only X=10. Following the curved
    # spine bows it to ~X=27 — assert it clears 15 (comfortably past the section extent).
    assert bb.max.X > 15.0, \
        f"centerline loft xmax {bb.max.X:.2f} should bow well past the 10mm section extent"
    s9 = _status(res, "f9")
    assert "_guided" in s9.get("resolved", {}), "centerline should be echoed in resolved"
    assert body.volume > 0.0, "centerline loft must be a non-empty solid"
    print(f"[centerline] xmax={bb.max.X:.2f} (sections reach 10) vol={body.volume:.1f}")


# --------------------------------------------------------------------------- test 4: guide curve

def test_guide_curve_changes_loft_vs_ruled() -> None:
    """A guide rail (auxiliary spine) steers the section orientation along the loft. Build a
    strongly tapered loft (20x20 -> 6x6) with vs without a Front-plane guide that swings hard
    in +X; the guided blend redistributes material so its volume differs from the plain ruled
    loft by a clear margin, and stays a single valid solid.

    (Kernel note: OCCT's stable auxiliary-spine mode steers the binormal; a guide coplanar with
    the spine re-parameterizes the blend rather than bulging it radially — see loft.py.)"""
    base = [
        _z40_plane("f1"),
        _rect_sketch("f2", -10, -10, 20, 20, "plane.top"),
        _rect_sketch("f3", -3, -3, 6, 6, "f1"),
    ]
    secs = [_section("f2"), _section("f3")]

    ruled_doc = _doc(base + [
        _feat("f9", "loft", {"mode": "boss", "ruled": True}, refs={"sections": secs}),
    ])
    _assert_valid(ruled_doc)
    ruled_res = compile_document(ruled_doc)
    _assert_all_ok(ruled_res, ("f2", "f3", "f9"))
    ruled_vol = ruled_res.bodies[0].shape.volume

    guide_doc = _doc(base + [
        _spline_sketch("f4", [[10, 0], [50, 4], [3, 40]], "plane.front"),
        _feat("f9", "loft", {"mode": "boss"},
              refs={"sections": secs, "guides": [{"kind": "region", "sketch": "f4"}]}),
    ])
    _assert_valid(guide_doc)
    guide_res = compile_document(guide_doc)
    s9 = _status(guide_res, "f9")
    assert s9["ok"], f"guided loft failed: {s9['error']}"
    body = guide_res.bodies[0].shape
    guide_vol = body.volume

    # The guide must change the blend vs the plain ruled loft.
    rel = abs(ruled_vol - guide_vol) / ruled_vol
    assert rel > 0.01, \
        f"guide should change the loft: ruled {ruled_vol:.1f} vs guided {guide_vol:.1f} (rel {rel:.4f})"
    # And the result must be a single, valid, non-degenerate solid (no collapse).
    assert len(body.solids()) == 1, "guided loft should yield exactly one solid"
    bb = body.bounding_box()
    assert (bb.max.Y - bb.min.Y) > 1.0, "guided loft must not collapse in Y"
    assert "_guided" in s9.get("resolved", {}), "guide should be echoed in resolved"
    print(f"[guide] ruled {ruled_vol:.1f} vs guided {guide_vol:.1f} (rel {rel:.3f})")


# --------------------------------------------------------------------------- test 5: thin

def test_thin_loft_is_hollow() -> None:
    """A thin loft between two circles (r=10 at z=0, r=6 at z=40) with a 2mm wall must be hollow
    — its volume far below the equivalent solid loft — while the outer bounding box still matches
    the r=10 base section."""
    sol_doc = _doc([
        _z40_plane("f1"),
        _circle_sketch("f2", 0, 0, 10, "plane.top"),
        _circle_sketch("f3", 0, 0, 6, "f1"),
        _feat("f9", "loft", {"mode": "boss"},
              refs={"sections": [_section("f2"), _section("f3")]}),
    ])
    _assert_valid(sol_doc)
    sol_res = compile_document(sol_doc)
    _assert_all_ok(sol_res, ("f2", "f3", "f9"))
    solid_vol = sol_res.bodies[0].shape.volume

    thin_doc = _doc([
        _z40_plane("f1"),
        _circle_sketch("f2", 0, 0, 10, "plane.top"),
        _circle_sketch("f3", 0, 0, 6, "f1"),
        _feat("f9", "loft",
              {"mode": "boss", "thin": True, "thin_thickness": _dim(2.0),
               "thin_side": "inside"},
              refs={"sections": [_section("f2"), _section("f3")]}),
    ])
    _assert_valid(thin_doc)
    thin_res = compile_document(thin_doc)
    s9 = _status(thin_res, "f9")
    assert s9["ok"], f"thin loft failed: {s9['error']}"
    body = thin_res.bodies[0].shape
    thin_vol = body.volume

    # A 2mm wall on a ~r8 average section is roughly a thin shell — well under half the solid.
    assert thin_vol < solid_vol * 0.7, \
        f"thin loft should be hollow: thin {thin_vol:.1f} vs solid {solid_vol:.1f}"
    assert thin_vol > 0.0, "thin loft must have material"
    # Outer bounding box unchanged: r=10 base -> diameter 20.
    bb = body.bounding_box()
    dx = bb.max.X - bb.min.X
    assert abs(dx - 20.0) < 0.5, f"thin loft outer x-extent {dx:.2f} should still be ~20"
    assert abs(s9["resolved"].get("thin_thickness", 0.0) - 2.0) < 1e-6, \
        "thin_thickness should be echoed in resolved"
    print(f"[thin] thin {thin_vol:.1f} vs solid {solid_vol:.1f} (hollow) outer-dx={dx:.2f}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_plain_two_section_loft_volume()
    test_start_end_tangency_changes_shape_vs_ruled()
    test_centerline_loft_follows_curve()
    test_guide_curve_changes_loft_vs_ruled()
    test_thin_loft_is_hollow()
    print("\nLOFT TESTS PASSED")


if __name__ == "__main__":
    main()
