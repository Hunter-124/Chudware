r"""Ruled SURFACE feature (``surf_ruled``) — the straight-rule strip between two rails.

Run:  & $py -X utf8 -m pytest engine\doc\test_surf_ruled.py -q   (from repo root)

Covers the surfacing ruled compiler (``surf_ruled.py``):
1. Two parallel straight rails (a line at z=0 and a line at z=10, both spanning x[0,20]) ruled
   into a FLAT quad strip — kind=="surface", AREA mass-props (is_surface, area≈200 = 20×10,
   volume≈0, mass≈0), and the sheet is OPEN (free edges along both rails). The two single-edge
   rails drive the exact ``BRepFill.Face_s`` path.
2. A TWISTED rail pair (far end of the top rail shifted +y) rules into a non-planar quad whose
   area exceeds the flat strip's — proving the surface genuinely follows both rails.
3. A surface never fuses into a running solid: with a base box present, surf_ruled appends a
   SECOND body and leaves the solid's volume untouched.

Harness mirrors engine/doc/test_extrude_ends.py.
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.evaluate import check, mass_props
from engine.doc.schema import validate_part
from engine.doc.features import surf_ruled  # noqa: F401  (registers the compiler)


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


def _line_sketch(fid: str, x1: float, y1: float, x2: float, y2: float, plane_ref) -> dict:
    """A single-segment open sketch (one line) — resolves to a SINGLE-edge rail wire."""
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x1, y1], "p2": [x2, y2]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region_sel(sketch_id: str, cx: float, cy: float, length: float) -> dict:
    """A sketch-region ref (resolved by loft._resolve_curve_wire to the raw edge chain)."""
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": length, "hint_index": 0}


def _has_free_edges(shape) -> bool:
    """True when the body is an OPEN sheet — check()'s free-edge census fires."""
    return any(i.get("kind") == "free_edges" for i in check(shape).get("issues", []))


# A pair of parallel rails: line A on plane.top (z=0) and line B on a datum plane at z=H,
# both spanning x[0, span] with the top rail's FAR end offset +y by ``skew`` (0 = flat strip).
def _ruled_features(ruled_id: str, *, span: float = 20.0, height: float = 10.0,
                    skew: float = 0.0, i0: int = 1) -> list[dict]:
    fa, fpl, fb = f"f{i0}", f"f{i0 + 1}", f"f{i0 + 2}"
    return [
        _line_sketch(fa, 0, 0, span, 0, "plane.top"),                 # rail A at z=0
        _feat(fpl, "plane", {"method": "offset", "offset": _dim(height)},
              refs={"base": "plane.top"}),                            # z=H plane
        _line_sketch(fb, 0, 0, span, skew, fpl),                      # rail B at z=H (skewed)
        _feat(ruled_id, "surf_ruled", {}, refs={
            "edge_a": _region_sel(fa, span / 2.0, 0.0, span),
            "edge_b": _region_sel(fb, span / 2.0, skew / 2.0, span),
        }),
    ]


# --------------------------------------------------------------------------- test 1: flat strip

def test_surf_ruled_is_open_flat_surface() -> None:
    """Two parallel straight rails ruled into a FLAT 20×10 quad strip (no caps)."""
    doc = _doc(_ruled_features("f4", span=20.0, height=10.0, skew=0.0))
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"surf_ruled failed: {s['error']}"

    bos = res.bodies
    assert len(bos) == 1, f"expected 1 surface body; got {len(bos)}"
    bo = bos[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"
    # The single-edge rail pair drives the exact BRepFill.Face_s path.
    assert s["resolved"].get("_ruled_kind") == "face", \
        f"single-edge rails should use the ruled-face path; got {s['resolved']}"

    # AREA report (no enclosed volume) — the defining surface property.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # Analytic ruled-strip area: a flat 20 (x-span) × 10 (z gap) rectangle = 200 mm².
    expected = 20.0 * 10.0
    assert abs(mp["area_mm2"] - expected) / expected < 0.01, \
        f"flat ruled-strip area {mp['area_mm2']:.3f} vs expected {expected}"

    # The sheet must be OPEN: both rails are un-capped free edges.
    assert _has_free_edges(bo.shape), "ruled surface must be an OPEN sheet (free rail edges)"
    print(f"[surf_ruled-flat] kind={bo.kind} area={mp['area_mm2']:.2f} (expect {expected}) open=yes")


# --------------------------------------------------------------------------- test 2: twisted strip

def test_surf_ruled_twisted_strip() -> None:
    """A twisted rail pair (top rail's far end shifted +y by 10) rules into a non-planar quad
    whose area exceeds the flat 200 mm² strip — the rulings genuinely track both rails."""
    doc = _doc(_ruled_features("f4", span=20.0, height=10.0, skew=10.0))
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"twisted surf_ruled failed: {s['error']}"

    bo = res.bodies[0]
    assert bo.kind == "surface"
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True
    assert abs(mp["volume_mm3"]) < 1e-6, f"twisted surface volume should be ~0; got {mp['volume_mm3']}"

    flat = 20.0 * 10.0  # 200 mm²
    area = mp["area_mm2"]
    # A twisted quad bows out of plane → strictly more area than the flat strip.
    assert area > flat * 1.02, \
        f"twisted strip area {area:.2f} should exceed the flat {flat} mm²"
    # Bounded above: the surface stays within a reasonable envelope of the rail geometry.
    assert area < flat * 2.0, f"twisted strip area {area:.2f} unexpectedly large"
    assert _has_free_edges(bo.shape), "twisted ruled surface must stay an OPEN sheet"
    print(f"[surf_ruled-twist] area={area:.2f} (flat {flat}, must exceed)")


# --------------------------------------------------------------------------- test 3: never fuses

def test_surface_does_not_fuse_into_solid() -> None:
    """With a base solid present, surf_ruled appends a SECOND (surface) body and leaves the
    solid's volume untouched — surfaces never merge into the running solid."""
    feats = [
        _feat("f1", "box", {"length": _dim(60), "width": _dim(60), "height": _dim(5),
                            "centered": False}, refs={"at": "origin"}),
    ] + _ruled_features("f5", span=20.0, height=10.0, skew=0.0, i0=2)
    doc = _doc(feats)
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], f"box failed: {_status(res, 'f1')['error']}"
    assert _status(res, "f5")["ok"], f"surf_ruled failed: {_status(res, 'f5')['error']}"

    assert len(res.bodies) == 2, f"surf_ruled should append a 2nd body; got {len(res.bodies)}"
    kinds = sorted(b.kind for b in res.bodies)
    assert kinds == ["solid", "surface"], f"expected one solid + one surface; got {kinds}"

    solid = next(b for b in res.bodies if b.kind == "solid")
    surf = next(b for b in res.bodies if b.kind == "surface")
    base_vol = 60.0 * 60.0 * 5.0
    assert abs(solid.shape.volume - base_vol) / base_vol < 0.01, \
        "base solid volume must be untouched by the ruled surface"
    assert mass_props(surf.shape).get("is_surface") is True
    print(f"[no-fuse] bodies={kinds} solid_vol={solid.shape.volume:.0f}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_ruled_is_open_flat_surface()
    test_surf_ruled_twisted_strip()
    test_surface_does_not_fuse_into_solid()
    print("\nSURF-RULED TESTS PASSED")


if __name__ == "__main__":
    main()
