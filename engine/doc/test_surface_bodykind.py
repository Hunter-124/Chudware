r"""Surfacing FOUNDATION — body-kind threading (solid default | surface).

Run:  & $py -m pytest engine\doc\test_surface_bodykind.py -q   (from repo root)

Covers the shared-core surface-body plumbing added for the Surfacing feature set:
1. A ``surf_planar`` feature compiles to a BodyOut whose ``kind == "surface"``.
2. ``mass_props`` of that surface body returns an AREA report: ``is_surface`` True,
   ``area_mm2`` > 0, ``volume_mm3`` ≈ 0, ``mass_g`` ≈ 0.
3. ``tessellate_body(..., two_sided=True)`` emits MORE triangles than the one-sided default
   (the back-face duplicate), and the one-sided count is unchanged (byte-identity guard).
4. A classic solid feature still reports ``kind == "solid"`` (the default path is untouched).

Harness mirrors engine/doc/test_extrude_ends.py.
"""

from __future__ import annotations

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part
from engine.doc.tessellate import tessellate_body


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


# --------------------------------------------------------------------------- test 1+2+3: surface

def test_surf_planar_is_surface_body() -> None:
    """A sketch + a surf_planar feature → a single SURFACE body (kind=="surface")."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 30, "plane.top"),
        _feat("f2", "surf_planar", {},
              refs={"profile": [_region_sel("f1", 10, 15, 600.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"surf_planar failed: {s['error']}"
    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    # mass_props returns an AREA report — no volume.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"
    # 20x30 planar region → area ~600 mm^2.
    assert abs(mp["area_mm2"] - 600.0) / 600.0 < 0.02, \
        f"area should be ~600; got {mp['area_mm2']:.2f}"

    # Two-sided tessellation emits the back-face duplicate → more triangles than one-sided.
    one = tessellate_body(bo.shape, body_id=1, name="s", color=(0.5, 0.5, 0.5))
    two = tessellate_body(bo.shape, body_id=1, name="s", color=(0.5, 0.5, 0.5), two_sided=True)
    assert len(two.idx) == 2 * len(one.idx), \
        f"two_sided should double the triangles; one={len(one.idx)} two={len(two.idx)}"
    assert len(one.idx) > 0, "one-sided surface should still tessellate"
    print(f"[surf_planar] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"tris one={len(one.idx)} two={len(two.idx)}")


# --------------------------------------------------------------------------- test 4: solid default

def test_solid_feature_still_reports_solid_kind() -> None:
    """A classic solid box reports kind=='solid' (the default path is byte-identical)."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(10),
                            "centered": False}, refs={"at": "origin"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"box failed: {_status(res, 'f1')['error']}"
    assert len(res.bodies) == 1
    bo = res.bodies[0]
    assert bo.kind == "solid", f"solid body kind should be 'solid'; got {bo.kind!r}"

    # mass_props takes the classic volume path — real volume, no is_surface flag.
    mp = mass_props(bo.shape)
    assert not mp.get("is_surface"), "solid mass_props must not flag is_surface"
    assert abs(mp["volume_mm3"] - 4000.0) / 4000.0 < 0.01, \
        f"solid volume should be 4000; got {mp['volume_mm3']:.1f}"

    # A solid tessellates one-sided by default (two_sided defaults False).
    one = tessellate_body(bo.shape, body_id=1, name="b", color=(0.5, 0.5, 0.5))
    assert len(one.idx) > 0
    print(f"[solid] kind={bo.kind} vol={mp['volume_mm3']:.1f} tris={len(one.idx)}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_planar_is_surface_body()
    test_solid_feature_still_reports_solid_kind()
    print("\nSURFACE-BODYKIND TESTS PASSED")


if __name__ == "__main__":
    main()
