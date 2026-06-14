r"""Surfacing — offset-surface feature (``surf_offset``): a surface parallel to a picked face.

Run:  & $py -m pytest engine\doc\test_surf_offset.py -q   (from repo root)

``surf_offset`` resolves one or more picked FACES from the running body and builds a new OPEN
surface parallel to them, displaced ``params.distance`` (signed) along their normals. The result
is a NEW SURFACE body (``kind=="surface"``), so ``mass_props`` reports an AREA report
(``is_surface`` True, ``area_mm2``, ``volume_mm3`` ≈ 0) rather than a solid mass.

Covers:
1. Offset a box TOP face (z=10, area=1600) UPWARD by distance=5 → a planar surface at z=15,
   parallel to and ``distance`` above the top face. Asserts ``kind=="surface"``, ``is_surface``,
   ``area_mm2`` ≈ the source face area, ``volume_mm3`` ≈ 0, and the offset surface's z plane.
2. NEGATIVE distance offsets the same top face DOWNWARD (to z=5) — the sign convention check.
3. A dangling face selector → contained FeatureError (ok:false), never crashing the rebuild.

Harness mirrors engine/doc/test_extrude_ends.py and test_surf_extrude.py.
"""

from __future__ import annotations

# Import the feature module so its @register decorator runs.
from engine.doc.features import surf_offset  # noqa: F401  (registers surf_offset)

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part
from engine.doc.selectors import describe_faces, make_selector


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


def _box_doc(fid: str, length: float, width: float, height: float) -> dict:
    """A single corner-at-origin box (z[0,height])."""
    return _doc([
        _feat(fid, "box", {"length": _dim(length), "width": _dim(width),
                           "height": _dim(height), "centered": False},
              refs={"at": "origin"}),
    ])


def _top_face_selector(res, owner: str) -> dict:
    """Build the validated selector for the body's planar TOP face (highest +Z plane)."""
    body = res.bodies[0]
    infos = describe_faces(body.shape.wrapped)
    top = None
    for i in infos:
        if i.surf == "plane" and i.axis is not None and abs(i.axis[2]) > 0.9:
            if top is None or i.centroid[2] > top.centroid[2]:
                top = i
    assert top is not None, "could not find a top planar face on the box"
    sel = make_selector(top)
    sel["owner_feature"] = owner
    return sel


def _is_solid(shape) -> bool:
    """True when *shape* has a real enclosed volume (a solid), False for an open surface.

    A build123d primitive (e.g. ``Box``) wraps a COMPOUND-of-one-solid rather than a bare
    ``TopAbs_SOLID``, so classify by enclosed volume — the meaningful solid-vs-surface
    distinction the core itself uses (``evaluate._is_surface_shape``)."""
    try:
        return abs(shape.volume) > 1e-6
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- test 1: offset up

def test_surf_offset_top_face_up() -> None:
    """Offset the box top face (z=10, area=1600) UP by distance=5 → a planar surface body at
    z=15, parallel to and 5 mm above the top face. kind=="surface"; mass_props is an AREA report
    (is_surface, area ≈ 1600, volume ≈ 0)."""
    length, width, height = 40.0, 40.0, 10.0
    distance = 5.0
    top_area = length * width  # 1600

    # Build the box first so we can construct an exact top-face selector against it.
    box_doc = _box_doc("f1", length, width, height)
    _assert_valid(box_doc)
    res0 = compile_document(box_doc)
    top_sel = _top_face_selector(res0, owner="f1")
    assert abs(top_sel["centroid"][2] - height) < 1e-6, \
        f"box top face should be at z={height}; got {top_sel['centroid'][2]}"

    doc = _doc([
        _feat("f1", "box", {"length": _dim(length), "width": _dim(width),
                            "height": _dim(height), "centered": False},
              refs={"at": "origin"}),
        _feat("f2", "surf_offset", {"distance": _dim(distance)},
              refs={"faces": [top_sel]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_offset failed: {s['error']}"

    # The solid box (body 0) stays; the offset surface is a NEW (2nd) body.
    assert len(res.bodies) == 2, f"expected box + surface body; got {len(res.bodies)}"
    bo = res.bodies[-1]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"
    assert not _is_solid(bo.shape), "offset surface must be an open surface, not a solid"

    # mass_props → AREA report, no volume/mass.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # Area of a planar offset equals the source face area (parallel translation preserves area).
    assert abs(mp["area_mm2"] - top_area) / top_area < 0.01, \
        f"offset surface area {mp['area_mm2']:.2f} vs expected {top_area} (== source face area)"

    # The offset surface sits at z = top_z + distance, parallel to the top face.
    expected_z = height + distance  # 15
    bb = bo.shape.bounding_box()
    assert abs(bb.min.Z - expected_z) < 0.05 and abs(bb.max.Z - expected_z) < 0.05, \
        f"offset surface should be the plane z={expected_z}; got z[{bb.min.Z:.3f},{bb.max.Z:.3f}]"
    assert abs(mp["com"][2] - expected_z) < 0.05, \
        f"offset surface centroid z {mp['com'][2]:.3f} should be {expected_z}"

    # The original solid box is untouched (z[0,10], full volume).
    box_bo = res.bodies[0]
    assert _is_solid(box_bo.shape), "the running solid must remain a solid"
    assert abs(box_bo.shape.volume - length * width * height) < 1.0, \
        "the offset must not consume or alter the running solid"

    # resolved echoes the signed distance.
    assert abs(s["resolved"].get("distance", 0.0) - distance) < 1e-6, \
        f"resolved.distance should be {distance}; got {s['resolved'].get('distance')}"
    print(f"[surf_offset-up] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"z={bb.min.Z:.2f} (expected {expected_z})")


# --------------------------------------------------------------------------- test 2: negative offset

def test_surf_offset_negative_distance_goes_down() -> None:
    """A NEGATIVE distance offsets the top face (z=10) DOWNWARD to z=5 — the sign convention:
    positive = along the outward face normal, negative = opposite."""
    length, width, height = 40.0, 40.0, 10.0
    distance = -5.0

    box_doc = _box_doc("f1", length, width, height)
    _assert_valid(box_doc)
    res0 = compile_document(box_doc)
    top_sel = _top_face_selector(res0, owner="f1")

    doc = _doc([
        _feat("f1", "box", {"length": _dim(length), "width": _dim(width),
                            "height": _dim(height), "centered": False},
              refs={"at": "origin"}),
        _feat("f2", "surf_offset", {"distance": _dim(distance)},
              refs={"faces": [top_sel]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_offset (negative) failed: {s['error']}"

    bo = res.bodies[-1]
    assert bo.kind == "surface"
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True
    assert abs(mp["area_mm2"] - length * width) / (length * width) < 0.01

    expected_z = height + distance  # 10 + (-5) = 5
    bb = bo.shape.bounding_box()
    assert abs(bb.min.Z - expected_z) < 0.05 and abs(bb.max.Z - expected_z) < 0.05, \
        f"negative offset should land at z={expected_z}; got z[{bb.min.Z:.3f},{bb.max.Z:.3f}]"
    # And it must be strictly BELOW the source top face (z=10) — the sign is honoured.
    assert bb.max.Z < height - 1e-3, "negative distance must move the surface inward (down)"
    print(f"[surf_offset-neg] z={bb.min.Z:.2f} (expected {expected_z}, below top z={height})")


# --------------------------------------------------------------------------- test 3: dangling ref

def test_surf_offset_dangling_face_is_contained() -> None:
    """A face selector that resolves to nothing → contained FeatureError (ok:false), not a
    crash. (Selector centroid placed far off the body so it cannot match.)"""
    length, width, height = 40.0, 40.0, 10.0
    bad_sel = {
        "kind": "face", "surf": "plane",
        "centroid": [9999.0, 9999.0, 9999.0],
        "axis": [0.0, 0.0, 1.0], "area_or_len": 1.0,
        "owner_feature": "f1", "hint_index": 0,
    }
    doc = _doc([
        _feat("f1", "box", {"length": _dim(length), "width": _dim(width),
                            "height": _dim(height), "centered": False},
              refs={"at": "origin"}),
        _feat("f2", "surf_offset", {"distance": _dim(5.0)},
              refs={"faces": [bad_sel]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "a dangling face selector should fail the feature"
    assert "dangling" in s["error"].lower() or "no faces" in s["error"].lower(), \
        f"expected a dangling/unresolved error; got: {s['error']}"
    # The running solid body is still present (rebuild continued on the last good body).
    assert len(res.bodies) == 1 and _is_solid(res.bodies[0].shape), \
        "the box must survive the contained failure"
    print(f"[surf_offset-dangling] clean error: {s['error'][:70]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_offset_top_face_up()
    test_surf_offset_negative_distance_goes_down()
    test_surf_offset_dangling_face_is_contained()
    print("\nSURF-OFFSET TESTS PASSED")


if __name__ == "__main__":
    main()
