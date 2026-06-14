r"""Replace-face feature (``surf_replace_face``) — SolidWorks "Replace Face".

Run:  & $py -m pytest engine\doc\test_surf_replace_face.py -q   (from repo root)

``surf_replace_face`` swaps one or more faces of the running SOLID with a (larger) replacement
surface, re-healing back into a SOLID. The replacement surface extends/retrims the solid up to
the new surface (the deleted faces' side is discarded; the body's boundary now follows the
replacement). The result is still a SOLID (``kind="solid"``) with a CHANGED volume.

The replacement surface here is a large planar surface body (``surf_planar`` of a 60x60 sketch on
an offset datum plane) that fully crosses the 40x40 box, picked via a FACE selector. (A surface
body produced by ``surf_planar``/``surf_offset`` carries no ``tool_body``, so it is not in
``ctx.tool_bodies``; a face selector resolves it cleanly through ``split._resolve_tool_face``.
The compiler also accepts a BODY selector via ``boolean._resolve_body_set`` for surface bodies
that DO publish a tool body.)

Covers:
1. Replace the box TOP face (z=10) with a planar surface at z=6 → the solid is retrimmed down to
   z=6: still a SOLID, volume 40x40x6 = 9600 (CHANGED from the 16000 box), mass_props volume > 0.
2. A different replacement level (z=3) changes the volume differently (4800) — the top truly
   follows the replacement surface, not a fixed amount.
3. ``surf_replace_face`` with no solid → contained FeatureError (ok:false), never a crash.

Harness mirrors engine/doc/test_extrude_ends.py.
"""

from __future__ import annotations

# Import the feature module so its @register decorator runs (it is imported ONLY by this test).
from engine.doc.features import surf_replace_face  # noqa: F401  (registers surf_replace_face)

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


def _planar_face_sel(cx: float, cy: float, z: float, area: float,
                     owner: str | None = None) -> dict:
    """A planar-face selector (a target face on the solid, or the replacement surface face)."""
    return {"kind": "face", "surf": "plane", "centroid": [cx, cy, z],
            "axis": [0.0, 0.0, 1.0], "area_or_len": area,
            "owner_feature": owner, "hint_index": 0}


def _is_solid(shape) -> bool:
    """True when *shape* has a real enclosed volume (a solid), False for an open surface."""
    try:
        return abs(shape.volume) > 1e-6
    except Exception:  # noqa: BLE001
        return False


def _replace_doc(box_l: float, box_w: float, box_h: float,
                 surf_z: float, surf_size: float) -> dict:
    """Box + a large planar replacement surface at z=surf_z + a surf_replace_face on the box top.

    The replacement surface (``surf_size`` square, centred to overhang the box) fully crosses the
    box, so the splitter can retrim the solid down to it. The box top face (z=box_h) is the target.
    """
    surf_origin = (box_l - surf_size) / 2.0  # centre the larger square over the box
    return _doc([
        _feat("f1", "box", {"length": _dim(box_l), "width": _dim(box_w),
                            "height": _dim(box_h), "centered": False},
              refs={"at": "origin"}),
        _feat("f2", "plane", {"method": "offset", "offset": _dim(surf_z)},
              refs={"base": "plane.top"}),
        _rect_sketch("f3", surf_origin, surf_origin, surf_size, surf_size, "f2"),
        _feat("f4", "surf_planar", {},
              refs={"profile": [_region_sel("f3", box_l / 2.0, box_w / 2.0,
                                            surf_size * surf_size)]}),
        _feat("f5", "surf_replace_face", {}, refs={
            "faces": [_planar_face_sel(box_l / 2.0, box_w / 2.0, box_h,
                                       box_l * box_w, owner="f1")],
            "surface": _planar_face_sel(box_l / 2.0, box_w / 2.0, surf_z,
                                        surf_size * surf_size, owner="f4"),
        }),
    ])


# --------------------------------------------------------------------------- test 1: retrim down

def test_replace_top_with_lower_surface_changes_volume() -> None:
    """Replace the box top face (z=10) with a planar surface at z=6 → the solid is retrimmed down
    to z=6. Still a SOLID, volume 40x40x6 = 9600 (CHANGED from the 16000 box)."""
    box_l, box_w, box_h = 40.0, 40.0, 10.0
    surf_z = 6.0
    box_vol = box_l * box_w * box_h          # 16000
    expected = box_l * box_w * surf_z        # 9600

    doc = _replace_doc(box_l, box_w, box_h, surf_z, surf_size=60.0)
    _assert_valid(doc)
    res = compile_document(doc)
    for fid in ("f1", "f2", "f3", "f4", "f5"):
        s = _status(res, fid)
        assert s["ok"], f"{fid} failed: {s['error']}"

    s5 = _status(res, "f5")
    assert s5["resolved"].get("_replaced_faces") == 1.0, "should report 1 replaced face"

    # The modified body is the one with a real volume (the surface body has zero volume).
    solid = max(res.bodies, key=lambda b: abs(b.shape.volume))
    assert solid.kind == "solid", f"result must be a SOLID; got kind={solid.kind!r}"
    assert _is_solid(solid.shape), "replace face must heal back to a solid with enclosed volume"

    vol = solid.shape.volume
    assert abs(vol - expected) / expected < 0.02, \
        f"retrimmed volume {vol:.1f} vs expected {expected} (top now follows z={surf_z})"
    # The defining property: the volume CHANGED from the original box.
    assert abs(vol - box_vol) > 0.05 * box_vol, \
        f"replace face must change the volume; got {vol:.1f} == box {box_vol}"

    # The retrimmed top sits at the replacement surface (z=6), not the old top (z=10).
    bb = solid.shape.bounding_box()
    assert abs(bb.max.Z - surf_z) < 0.05, \
        f"top should follow the replacement surface z={surf_z}; got zmax={bb.max.Z:.3f}"
    assert abs(bb.min.Z) < 0.05, "the bottom face must be untouched (z=0)"

    # mass_props is a real SOLID report: positive volume, not a surface.
    mp = mass_props(solid.shape)
    assert not mp.get("is_surface"), f"result must NOT be a surface; got {mp}"
    assert mp["volume_mm3"] > 0.0, f"mass_props volume must be > 0; got {mp['volume_mm3']}"
    assert abs(mp["volume_mm3"] - expected) / expected < 0.02, \
        f"mass_props volume {mp['volume_mm3']:.1f} vs expected {expected}"

    print(f"[replace-down] kind={solid.kind} vol={vol:.1f} "
          f"(box was {box_vol}, top now z={bb.max.Z:.1f})")


# --------------------------------------------------------------------------- test 2: different level

def test_replace_level_drives_volume() -> None:
    """A LOWER replacement surface (z=3) retrims the box further → volume 40x40x3 = 4800. The top
    truly follows the replacement surface (a different level → a different volume), proving the
    feature is not removing a fixed amount."""
    box_l, box_w, box_h = 40.0, 40.0, 10.0
    surf_z = 3.0
    expected = box_l * box_w * surf_z        # 4800

    doc = _replace_doc(box_l, box_w, box_h, surf_z, surf_size=60.0)
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f5")
    assert s["ok"], f"replace face failed: {s['error']}"

    solid = max(res.bodies, key=lambda b: abs(b.shape.volume))
    assert solid.kind == "solid"
    assert _is_solid(solid.shape)
    vol = solid.shape.volume
    assert abs(vol - expected) / expected < 0.02, \
        f"z={surf_z} replacement volume {vol:.1f} vs expected {expected}"
    bb = solid.shape.bounding_box()
    assert abs(bb.max.Z - surf_z) < 0.05, \
        f"top should follow z={surf_z}; got zmax={bb.max.Z:.3f}"
    print(f"[replace-level] vol={vol:.1f} top z={bb.max.Z:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 3: no body

def test_replace_face_requires_solid() -> None:
    """surf_replace_face with no existing solid → clean contained FeatureError (ok:false)."""
    doc = _doc([
        _feat("f1", "surf_replace_face", {}, refs={
            "faces": [_planar_face_sel(0, 0, 0, 1.0, owner=None)],
            "surface": _planar_face_sel(0, 0, 0, 1.0, owner=None),
        }),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert not s["ok"], "replace face with no solid should fail"
    assert "requires an existing solid" in s["error"], \
        f"expected a clear no-solid error; got: {s['error']}"
    print(f"[replace-nobody] clean error: {s['error'][:60]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_replace_top_with_lower_surface_changes_volume()
    test_replace_level_drives_volume()
    test_replace_face_requires_solid()
    print("\nSURF-REPLACE-FACE TESTS PASSED")


if __name__ == "__main__":
    main()
