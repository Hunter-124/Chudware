r"""Lofted SURFACE feature (``surf_loft``) — an open shell blended through 2+ sections.

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_loft.py -q   (from repo root)

Covers the surfacing loft compiler (sibling of the solid ``loft``):
1. Two parallel section sketches (a square and a smaller square on a plane 30 mm up) lofted
   into an OPEN funnel-wall surface — kind=="surface", AREA mass-props (is_surface, area>0,
   volume≈0, mass≈0), and the shell is OPEN (free edges at the two un-capped ends).
2. A ruled loft differs geometrically from the default (smooth) loft — different wall area —
   proving the ``ruled`` flag is honored.
3. A surface loft never fuses into a running solid: with a base box present, surf_loft appends
   a SECOND body and leaves the solid's volume untouched.

Harness mirrors engine/doc/test_extrude_ends.py.
"""

from __future__ import annotations

from engine.doc.compiler import compile_document
from engine.doc.evaluate import check, mass_props
from engine.doc.schema import validate_part
from engine.doc.features import surf_loft  # noqa: F401  (registers the compiler)


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


def _has_free_edges(shape) -> bool:
    """True when the body is an OPEN shell — check()'s free-edge census fires."""
    return any(i.get("kind") == "free_edges" for i in check(shape).get("issues", []))


# A funnel wall: a 40x40 square at z=0 lofted up to a 20x20 square on a datum plane at z=30.
# ``base`` is an optional list of leading features; ``i0`` offsets the f<N> numbering so a
# caller can prepend its own f-features without id collisions.
def _funnel_features(loft_id: str, params: dict, i0: int = 1) -> list[dict]:
    fp, fs1, fs2 = f"f{i0}", f"f{i0 + 1}", f"f{i0 + 2}"
    return [
        _feat(fp, "plane", {"method": "offset", "offset": _dim(30.0)},
              refs={"base": "plane.top"}),                       # z=30 plane
        _rect_sketch(fs1, 0, 0, 40, 40, "plane.top"),            # big square at z=0
        _rect_sketch(fs2, 10, 10, 20, 20, fp),                   # small square at z=30
        _feat(loft_id, "surf_loft", params, refs={"sections": [
            _region_sel(fs1, 20, 20, 1600.0),
            _region_sel(fs2, 20, 20, 400.0),
        ]}),
    ]


# --------------------------------------------------------------------------- test 1: open surface

def test_surf_loft_is_open_surface() -> None:
    """Two parallel section squares lofted into an open funnel wall (no caps)."""
    doc = _doc(_funnel_features("f4", {}))
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"surf_loft failed: {s['error']}"

    bos = res.bodies
    assert len(bos) == 1, f"expected 1 surface body; got {len(bos)}"
    bo = bos[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    # AREA report (no enclosed volume) — the defining surface property.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # Ballpark the slant-wall area: 4 trapezoids, parallel sides 40 & 20, slant height
    # sqrt(30^2 + 10^2) ≈ 31.62 → each ~ (40+20)/2 * 31.62 ≈ 948.7; ×4 ≈ 3795 mm².
    import math
    slant = math.hypot(30.0, 10.0)
    approx = 4.0 * ((40.0 + 20.0) / 2.0) * slant
    assert abs(mp["area_mm2"] - approx) / approx < 0.1, \
        f"wall area {mp['area_mm2']:.1f} vs expected ~{approx:.1f}"

    # The shell must be OPEN: the two section loops are un-capped free edges.
    assert _has_free_edges(bo.shape), "lofted surface must be an OPEN shell (free edges at the ends)"
    print(f"[surf_loft] kind={bo.kind} area={mp['area_mm2']:.1f} (~{approx:.1f}) open=yes")


# --------------------------------------------------------------------------- test 2: ruled vs smooth

def _vase_features(loft_id: str, params: dict) -> list[dict]:
    """A 3-section circular loft: wide (r=20) → narrow (r=8) → wide (r=20) up the Z axis.
    With ≥3 sections the SMOOTH blend bows the wall (a curved vase profile) while the RULED
    blend joins the circles with straight conical spans — so their wall areas differ."""
    return [
        _feat("f1", "plane", {"method": "offset", "offset": _dim(25.0)},
              refs={"base": "plane.top"}),                       # z=25
        _feat("f2", "plane", {"method": "offset", "offset": _dim(50.0)},
              refs={"base": "plane.top"}),                       # z=50
        _circle_sketch("f3", 0, 0, 20, "plane.top"),             # wide at z=0
        _circle_sketch("f4", 0, 0, 8, "f1"),                     # narrow waist at z=25
        _circle_sketch("f5", 0, 0, 20, "f2"),                    # wide at z=50
        _feat(loft_id, "surf_loft", params, refs={"sections": [
            _region_sel("f3", 0, 0, 1256.0),
            _region_sel("f4", 0, 0, 201.0),
            _region_sel("f5", 0, 0, 1256.0),
        ]}),
    ]


def test_ruled_differs_from_default() -> None:
    """A ruled loft (straight transitions) produces a geometrically different wall than the
    default smooth blend — the ruled flag is honored end-to-end. Uses a 3-section circular
    vase where the smooth waist bulges and the ruled waist is a straight cone pair."""
    res_default = compile_document(_doc(_vase_features("f6", {})))
    res_ruled = compile_document(_doc(_vase_features("f6", {"ruled": True})))
    s_d = _status(res_default, "f6")
    s_r = _status(res_ruled, "f6")
    assert s_d["ok"], f"default loft failed: {s_d['error']}"
    assert s_r["ok"], f"ruled loft failed: {s_r['error']}"

    area_d = mass_props(res_default.bodies[0].shape)["area_mm2"]
    area_r = mass_props(res_ruled.bodies[0].shape)["area_mm2"]
    assert area_d > 0 and area_r > 0
    # The resolved flag is always distinct (threaded to the builder)...
    assert s_r["resolved"].get("_ruled") == "yes", f"ruled echo missing: {s_r['resolved']}"
    assert s_d["resolved"].get("_ruled") == "no", f"default echo wrong: {s_d['resolved']}"
    # ...and with curved 3-section profiles the geometry genuinely differs.
    assert abs(area_r - area_d) / area_d > 0.01, \
        f"ruled wall area {area_r:.1f} should differ from smooth {area_d:.1f}"
    # Both must stay open surfaces.
    assert _has_free_edges(res_default.bodies[0].shape)
    assert _has_free_edges(res_ruled.bodies[0].shape)
    print(f"[ruled] smooth area={area_d:.1f} ruled area={area_r:.1f} "
          f"(delta {100 * abs(area_r - area_d) / area_d:.1f}%)")


# --------------------------------------------------------------------------- test 3: never fuses

def test_surface_does_not_fuse_into_solid() -> None:
    """With a base solid present, surf_loft appends a SECOND (surface) body and leaves the
    solid's volume untouched — surfaces never merge into the running solid."""
    feats = [
        _feat("f1", "box", {"length": _dim(80), "width": _dim(80), "height": _dim(5),
                            "centered": False}, refs={"at": "origin"}),
    ] + _funnel_features("f5", {}, i0=2)
    doc = _doc(feats)
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"box failed: {_status(res, 'f1')['error']}"
    assert _status(res, "f5")["ok"], f"surf_loft failed: {_status(res, 'f5')['error']}"

    assert len(res.bodies) == 2, f"surf_loft should append a 2nd body; got {len(res.bodies)}"
    kinds = sorted(b.kind for b in res.bodies)
    assert kinds == ["solid", "surface"], f"expected one solid + one surface; got {kinds}"

    solid = next(b for b in res.bodies if b.kind == "solid")
    surf = next(b for b in res.bodies if b.kind == "surface")
    assert abs(solid.shape.volume - 80.0 * 80.0 * 5.0) / (80.0 * 80.0 * 5.0) < 0.01, \
        "base solid volume must be untouched by the surface loft"
    assert mass_props(surf.shape).get("is_surface") is True
    print(f"[no-fuse] bodies={kinds} solid_vol={solid.shape.volume:.0f}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_loft_is_open_surface()
    test_ruled_differs_from_default()
    test_surface_does_not_fuse_into_solid()
    print("\nSURF-LOFT TESTS PASSED")


if __name__ == "__main__":
    main()
