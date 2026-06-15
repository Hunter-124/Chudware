r"""Delete-Face feature (``surf_delete_face``) — remove faces from a SOLID, leaving an open SHELL.

Run:  & $py -m pytest engine\doc\test_surf_delete_face.py -q   (from repo root)

``surf_delete_face`` is SolidWorks "Delete Face → Delete": picking face(s) on a SOLID body and
deleting them punches a hole in the boundary skin, so the body can no longer enclose a volume — it
flips ``kind="solid"`` → ``kind="surface"`` (an OPEN shell) and REPLACES the solid in the body
list. This suite mirrors engine/doc/test_extrude_ends.py / test_surf_sweep.py and covers:

1. A 20x20x10 box (solid) with its TOP face deleted → an OPEN shell surface body:
   ``kind=="surface"``; mass_props ``is_surface`` True with ``area_mm2`` ≈ (box surface area 1600
   minus the deleted top face 400) = 1200 and ``volume_mm3`` ≈ 0; the shell is OPEN (free boundary
   edges ring the removed top; OCCT ``IsClosed`` False) — the defining surface property.
2. A dangling face selector → a contained error (ok:false), never crashing the rebuild.

The "surface body" plumbing itself (kind threading, area mass-props, two-sided tessellation) is
owned by engine/doc/test_surface_bodykind.py; here we assert the DELETE-FACE geometry.
"""

from __future__ import annotations

from engine.doc.features import surf_delete_face  # noqa: F401  (registers the compiler)

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


def _top_face_sel(owner: str, cx: float, cy: float, z: float, area: float) -> dict:
    """A planar top-face selector (normal +Z) on the box owned by *owner*."""
    return {"kind": "face", "surf": "plane", "centroid": [cx, cy, z],
            "axis": [0.0, 0.0, 1.0], "area_or_len": area,
            "owner_feature": owner, "hint_index": 0}


def _free_edge_count(shape) -> tuple[int, int]:
    """(total, free) edge counts — a free (boundary) edge bounds exactly one face.

    An OPEN shell has free edges (its boundary); a CLOSED shell (a box's outer skin) has none."""
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


# --------------------------------------------------------------------------- test 1: delete top face

def test_delete_top_face_opens_box_into_shell() -> None:
    """A 20x20x10 box (solid) at z[0,10] with its TOP face (z=10) deleted → an OPEN shell.

    Box surface area = 2*(20*20) + 4*(20*10) = 800 + 800 = 1600 mm^2. Deleting the 20x20=400 top
    leaves a 5-face open shell (bottom + 4 walls) with area ≈ 1600 - 400 = 1200 mm^2, volume ≈ 0.
    The shell is OPEN — free boundary edges ring the missing top (a closed box skin has none)."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
        _feat("f2", "surf_delete_face", {}, refs={
            "faces": [_top_face_sel("f1", 10, 10, 10, 400.0)],
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f1", "f2"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"

    assert len(res.bodies) == 1, f"delete-face replaces the solid; expected 1 body, got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should flip to 'surface'; got {bo.kind!r}"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # area ≈ box surface area (1600) minus the deleted top face (400) = 1200.
    box_area = 2.0 * (20.0 * 20.0) + 4.0 * (20.0 * 10.0)   # 1600
    deleted_area = 20.0 * 20.0                              # 400 (top)
    expected_area = box_area - deleted_area                 # 1200
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"area should be ~{expected_area} (box {box_area} minus top {deleted_area}); got {mp['area_mm2']:.2f}"

    # The opened shell must be OPEN (the removed top leaves a free boundary loop).
    assert not _is_closed_shell(bo.shape), "delete-face shell must be OPEN (a face was removed)"
    total, free = _free_edge_count(bo.shape)
    assert free > 0, f"an open shell must have free boundary edges; total={total} free={free}"
    # The square hole left by the top is bounded by 4 free edges.
    assert free == 4, f"the missing top should leave 4 free boundary edges; got {free}"

    assert abs(_status(res, "f2")["resolved"].get("deleted_faces", 0.0) - 1.0) < 1e-9, \
        "resolved.deleted_faces should be 1"
    print(f"[delete-top] kind={bo.kind} area={mp['area_mm2']:.1f} vol={mp['volume_mm3']:.3g} "
          f"open={not _is_closed_shell(bo.shape)} free_edges={free}")


# --------------------------------------------------------------------------- test 2: dangling selector

def test_delete_face_dangling_selector_is_contained() -> None:
    """A face selector that resolves to nothing (off in space) → a contained error (ok:false),
    not a crash. The rebuild continues on the last good (solid) body."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
        _feat("f2", "surf_delete_face", {}, refs={
            # centroid far from any real face → no match
            "faces": [_top_face_sel("f1", 500, 500, 500, 400.0)],
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "a dangling face selector should fail the feature"
    # The prior solid body survives (rebuild fell back to the last good body).
    assert res.bodies, "the box body should survive a failed delete-face"
    assert res.bodies[-1].kind == "solid", "the body should still be the un-deleted solid"
    print(f"[dangling] contained error: {s['error'][:70]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_delete_top_face_opens_box_into_shell()
    test_delete_face_dangling_selector_is_contained()
    print("\nSURF-DELETE-FACE TESTS PASSED")


if __name__ == "__main__":
    main()
