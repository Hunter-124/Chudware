r"""Swept-surface feature (``surf_sweep``) — an open SHELL swept from a profile wire.

Run:  & $py -m pytest engine\doc\test_surf_sweep.py -q   (from repo root)

``surf_sweep`` is the surfacing sibling of the solid ``sweep``: it sweeps a profile WIRE along a
path WITHOUT capping the ends, leaving an open surface body (``kind=="surface"``) — SolidWorks
"Swept Surface". This suite mirrors engine/doc/test_extrude_ends.py and covers:

1. A CLOSED square profile (10x10) swept along a straight 50 mm path → an open tube WALL (no end
   caps): ``kind=="surface"``; mass_props ``is_surface`` True with ``area_mm2`` ≈ profile-wire
   perimeter (40) × path-length (50) = 2000 and ``volume_mm3`` ≈ 0; the shell is OPEN (free
   boundary edges at both ends; OCCT ``IsClosed`` False).
2. A CLOSED circular profile (r=5) swept along a straight 40 mm path → an open cylindrical wall
   whose area ≈ circumference (2*pi*5) × path-length (40); volume ≈ 0; shell open. (A different
   profile topology — one curved side face, 2 seam-free boundary loops.)
3. A missing path → caught by schema validation as a required-ref error (the contained-error
   guarantee for a required ref).

The "surface body" plumbing itself (kind threading, area mass-props, two-sided tessellation) is
owned by engine/doc/test_surface_bodykind.py; here we assert the SWEPT-shell geometry.
"""

from __future__ import annotations

from engine.doc.features import surf_sweep  # noqa: F401  (registers the compiler)

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
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


def _line_sketch(fid: str, p1: list[float], p2: list[float], plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": p1, "p2": p2},
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


def _region_sel(sketch_id: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _path_region(sketch_id: str) -> dict:
    return {"kind": "region", "sketch": sketch_id}


def _free_edge_count(shape) -> tuple[int, int]:
    """(total, free) edge counts of a shape — a free (boundary) edge bounds exactly one face.

    An OPEN shell has free edges (its boundary); a CLOSED shell (e.g. a box's outer skin) has
    none. Mirrors the watertightness check OCCT uses internally."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
    total = m.Extent()
    free = sum(1 for i in range(1, total + 1) if m.FindFromIndex(i).Extent() == 1)
    return total, free


def _is_closed_shell(shape) -> bool:
    from OCP.BRep import BRep_Tool
    return bool(BRep_Tool.IsClosed_s(shape.wrapped))


# --------------------------------------------------------------------------- test 1: closed profile

def test_closed_profile_sweep_is_open_tube_wall() -> None:
    """A closed 10x10 square profile on plane.top (XY, z=0) swept along a straight 50 mm path
    (a line on plane.front going +Z) → an OPEN tube wall: a surface body with NO end caps.

    Area = profile-wire perimeter (4 * 10 = 40) * path length (50) = 2000 mm^2 for a straight
    sweep (each side wall is a 10x50 rectangle, 4 of them). Volume ≈ 0 (open shell). The shell
    must be OPEN — free boundary edges at the two un-capped ends (the solid sweep would cap them)."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 10, 10, "plane.top"),       # profile (XY at z=0)
        _line_sketch("f2", [5, 0], [5, 50], "plane.front"),  # path: line along +Z (x=5)
        _feat("f3", "surf_sweep", {}, refs={
            "profile": [_region_sel("f1", 5, 5, 100.0)],
            "path": _path_region("f2"),
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f1", "f2", "f3"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"

    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # area ≈ perimeter(40) * path-length(50) = 2000 for a straight sweep.
    expected_area = 40.0 * 50.0
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"area should be ~{expected_area} (perimeter 40 * path 50); got {mp['area_mm2']:.2f}"

    # The swept shell must be OPEN (un-capped ends) — its defining surface property.
    assert not _is_closed_shell(bo.shape), "swept surface shell must be OPEN (no end caps)"
    total, free = _free_edge_count(bo.shape)
    assert free > 0, f"an open shell must have free boundary edges; total={total} free={free}"
    # A square tube wall: 4 side faces, two open square ends → 8 boundary edges.
    assert free == 8, f"a square tube wall should have 8 free boundary edges; got {free}"
    print(f"[closed-profile] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"open={not _is_closed_shell(bo.shape)} free_edges={free}")


# --------------------------------------------------------------------------- test 2: circle profile

def test_circle_profile_sweep_is_open_cylinder_wall() -> None:
    """A closed circular profile (r=5 on plane.top, z=0) swept along a straight 40 mm path (+Z)
    → an open CYLINDRICAL wall (no caps). Area ≈ circumference (2*pi*5) * path-length (40);
    volume ≈ 0; shell open. A different profile topology than the square (one curved side face)."""
    import math

    r = 5.0
    path_len = 40.0
    doc = _doc([
        _circle_sketch("f1", 5, 5, r, "plane.top"),          # closed circular profile
        _line_sketch("f2", [5, 0], [5, path_len], "plane.front"),  # path: line along +Z
        _feat("f3", "surf_sweep", {}, refs={
            "profile": [_region_sel("f1", 5, 5, math.pi * r * r)],
            "path": _path_region("f2"),
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f1", "f2", "f3"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"

    bo = res.bodies[-1]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    expected_area = 2.0 * math.pi * r * path_len  # circumference * length
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"area should be ~{expected_area:.1f} (circumference * path {path_len}); got {mp['area_mm2']:.2f}"

    assert not _is_closed_shell(bo.shape), "swept cylinder wall must be OPEN (no end caps)"
    _, free = _free_edge_count(bo.shape)
    assert free > 0, "an open cylinder wall must have free boundary edges (the two end loops)"
    print(f"[circle-profile] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"open={not _is_closed_shell(bo.shape)} free_edges={free}")


# --------------------------------------------------------------------------- test 3: missing path

def test_surf_sweep_requires_path() -> None:
    """surf_sweep with no refs.path → a contained error. ``refs.path`` is a required ref, so the
    schema validator catches it before compile (the contained-error guarantee for required refs)."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 10, 10, "plane.top"),
        _feat("f2", "surf_sweep", {}, refs={"profile": [_region_sel("f1", 5, 5, 100.0)]}),
    ])
    errs = validate_part(doc)
    assert errs, "surf_sweep with no path should fail validation"
    assert any("path" in e and "f2" in e for e in errs), \
        f"expected a required-path validation error for f2; got: {errs}"
    print(f"[no-path] caught at validation: {errs[0]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_closed_profile_sweep_is_open_tube_wall()
    test_circle_profile_sweep_is_open_cylinder_wall()
    test_surf_sweep_requires_path()
    print("\nSURF-SWEEP TESTS PASSED")


if __name__ == "__main__":
    main()
