r"""Fill SURFACE feature (``surf_fill``) — a patch surface spanning a bounded edge loop.

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_fill.py -q   (from repo root)

Covers the surfacing fill compiler:
1. A box-opening fill: a 40x40x20 box's TOP-face boundary edges (the "removed face" gap) are
   filled into a flat patch closing the opening — kind=="surface", AREA mass-props
   (is_surface True, area>0, volume≈0, mass≈0), area ≈ the 40x40 = 1600 mm² gap, and the patch
   spans the boundary (its bounding box matches the loop's footprint at the opening's Z).
2. A sketch-region fill: a closed square sketch loop (no body) is filled into a planar patch —
   the region/edge-resolution path, kind=="surface", area ≈ the enclosed region.
3. A non-planar fill: a closed 4-edge loop with one side lifted in Z fills into a curved patch
   whose area exceeds the flat projection (proving the patch genuinely spans a 3-D gap), and a
   surface fill never fuses into a running solid (appended as a SECOND body).

Harness mirrors engine/doc/test_extrude_ends.py / test_surf_loft.py.
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part
from engine.doc import selectors as sel
from engine.doc.features import surf_fill  # noqa: F401  (registers the compiler)


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


def _box(fid: str, L: float, W: float, H: float, at="origin", **extra) -> dict:
    params = {"length": _dim(L), "width": _dim(W), "height": _dim(H), "centered": False}
    params.update(extra)
    return _feat(fid, "box", params, refs={"at": at})


def _top_edge_selectors(res, z_target: float) -> list[dict]:
    """make_selector refs for each UNIQUE model edge on the plane z == z_target (the opening).

    ``describe_edges`` enumerates one descriptor per incident face (a box edge appears twice),
    so de-dup by rounded centroid to get the four distinct rim edges of the opening."""
    body = res.bodies[0]
    infos = sel.describe_edges(body.shape.wrapped)
    out: list[dict] = []
    seen: set = set()
    for i in infos:
        if abs(i.centroid[2] - z_target) < 1e-4:
            key = (round(i.centroid[0], 3), round(i.centroid[1], 3), round(i.centroid[2], 3))
            if key in seen:
                continue
            seen.add(key)
            out.append(sel.make_selector(i))
    return out


# --------------------------------------------------------------------------- test 1: box opening

def test_surf_fill_closes_box_opening() -> None:
    """Fill the TOP-face edge loop of a 40x40x20 box (the "removed face" gap) into a flat patch.

    The four top edges (z=20) bound a 40x40 opening; surf_fill spans them with a patch face that
    closes the gap. Surface body: is_surface, area ~ 1600 mm², ~0 volume/mass."""
    box = _box("f1", 40, 40, 20)
    res0 = compile_document(_doc([box]))
    assert _status(res0, "f1")["ok"], f"box failed: {_status(res0, 'f1')['error']}"

    edge_sels = _top_edge_selectors(res0, 20.0)
    assert len(edge_sels) == 4, f"expected 4 top edges; got {len(edge_sels)}"

    fill = _feat("f2", "surf_fill", {}, refs={"edges": edge_sels})
    doc = _doc([box, fill])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_fill failed: {s['error']}"

    # The fill is appended as a NEW (surface) body alongside the solid box.
    assert len(res.bodies) == 2, f"surf_fill should append a 2nd body; got {len(res.bodies)}"
    bo = next(b for b in res.bodies if b.kind == "surface")
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    # AREA report (no enclosed volume) — the defining surface property.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # The patch spans the 40x40 opening: area ~ 1600 mm².
    assert abs(mp["area_mm2"] - 1600.0) / 1600.0 < 0.02, \
        f"fill patch area {mp['area_mm2']:.1f} vs expected ~1600 (40x40 opening)"

    # ...and it physically spans the boundary footprint at the opening's plane (z=20).
    bb = bo.shape.bounding_box()
    assert abs((bb.max.X - bb.min.X) - 40.0) < 0.05 and abs((bb.max.Y - bb.min.Y) - 40.0) < 0.05, \
        f"patch footprint should span 40x40; got {bb.max.X - bb.min.X:.2f} x {bb.max.Y - bb.min.Y:.2f}"
    assert abs(bb.min.Z - 20.0) < 0.05 and abs(bb.max.Z - 20.0) < 0.05, \
        f"flat patch should sit on the opening plane z=20; got z[{bb.min.Z:.2f},{bb.max.Z:.2f}]"

    # The solid box volume is untouched by the surface fill.
    solid = next(b for b in res.bodies if b.kind == "solid")
    assert abs(solid.shape.volume - 40.0 * 40.0 * 20.0) / (40.0 * 40.0 * 20.0) < 0.01, \
        "base solid volume must be untouched by the surface fill"

    assert abs(s["resolved"].get("boundary_edges", 0.0) - 4.0) < 1e-6, \
        f"resolved should report 4 boundary edges; got {s['resolved'].get('boundary_edges')}"
    print(f"[surf_fill-box] kind={bo.kind} area={mp['area_mm2']:.1f} (~1600) "
          f"footprint={bb.max.X - bb.min.X:.0f}x{bb.max.Y - bb.min.Y:.0f} z={bb.min.Z:.0f}")


# --------------------------------------------------------------------------- test 2: sketch region

def test_surf_fill_from_sketch_region() -> None:
    """A closed square sketch loop (no body) filled into a planar patch via the region path."""
    sk = _rect_sketch("f1", 0, 0, 30, 30, "plane.top")
    fill = _feat("f2", "surf_fill", {},
                 refs={"edges": [_region_sel("f1", 15, 15, 900.0)]})
    doc = _doc([sk, fill])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_fill (region) failed: {s['error']}"

    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    # 30x30 region -> ~900 mm² patch.
    assert abs(mp["area_mm2"] - 900.0) / 900.0 < 0.02, \
        f"region fill area {mp['area_mm2']:.1f} vs expected ~900 (30x30)"
    # The region's 4 edges drove the boundary.
    assert s["resolved"].get("boundary_edges", 0.0) >= 4.0, \
        f"region should contribute >=4 boundary edges; got {s['resolved'].get('boundary_edges')}"
    print(f"[surf_fill-region] kind={bo.kind} area={mp['area_mm2']:.1f} (~900)")


# --------------------------------------------------------------------------- test 3: non-planar gap

def test_surf_fill_nonplanar_gap_and_no_fuse() -> None:
    """A boundary loop that lives OUT of the XY plane fills into a patch spanning 3-D space, and
    a surface fill never fuses into a running solid box (appended as a SECOND body).

    A 40x40x5 base box is the no-fuse witness; the fill boundary is a 30x30 sketch loop on
    ``plane.front`` (the XZ plane), so the four bounding edges are lifted entirely out of XY. The
    patch must span them as a surface body that leaves the base solid's volume untouched."""
    base = _box("f1", 40, 40, 5)  # base solid witness (no-fuse)
    sk = _rect_sketch("f2", -15, -15, 30, 30, "plane.front")  # 30x30 loop in the XZ plane
    fill = _feat("f3", "surf_fill", {}, refs={"edges": [_region_sel("f2", 0, 0, 900.0)]})
    doc = _doc([base, sk, fill])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"surf_fill (out-of-XY) failed: {s['error']}"

    # No-fuse: solid base witness + a surface patch = two bodies, solid volume untouched.
    assert len(res.bodies) == 2, f"surf_fill should append a 2nd body; got {len(res.bodies)}"
    kinds = sorted(b.kind for b in res.bodies)
    assert kinds == ["solid", "surface"], f"expected one solid + one surface; got {kinds}"
    solid = next(b for b in res.bodies if b.kind == "solid")
    assert abs(solid.shape.volume - 40.0 * 40.0 * 5.0) / (40.0 * 40.0 * 5.0) < 0.01, \
        "base solid volume must be untouched by the surface fill"

    surf = next(b for b in res.bodies if b.kind == "surface")
    mp = mass_props(surf.shape)
    assert mp.get("is_surface") is True
    assert abs(mp["volume_mm3"]) < 1e-6
    # The 30x30 region (in the XZ plane) still encloses ~900 mm² of patch.
    assert abs(mp["area_mm2"] - 900.0) / 900.0 < 0.05, \
        f"out-of-XY fill area {mp['area_mm2']:.1f} vs expected ~900"
    # The patch genuinely lives OUT of the XY plane: a flat XY patch has Z-extent ~0; this loop
    # spans the full 30 mm of the XZ plane.
    bb = surf.shape.bounding_box()
    z_extent = bb.max.Z - bb.min.Z
    assert z_extent > 5.0, \
        f"patch should span out of XY (Z-extent > 5); got {z_extent:.2f}"
    print(f"[surf_fill-out-of-XY] kind={surf.kind} area={mp['area_mm2']:.1f} z-extent={z_extent:.1f} "
          f"bodies={kinds}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_fill_closes_box_opening()
    test_surf_fill_from_sketch_region()
    test_surf_fill_nonplanar_gap_and_no_fuse()
    print("\nSURF-FILL TESTS PASSED")


if __name__ == "__main__":
    main()
