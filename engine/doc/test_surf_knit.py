r"""Surfacing — knit-surface feature (``surf_knit``): sew ≥2 surface bodies into one shell.

Run:  & $py -m pytest engine\doc\test_surf_knit.py -q   (from repo root)

``surf_knit`` sews the selected surface/face bodies edge-to-edge. If the result is watertight it
is promoted to a SOLID (``kind=="solid"``, real enclosed volume); otherwise it stays an open
Shell (``kind=="surface"``, an area report).

Covers:
1. The six planar walls of a box (built with ``surf_planar`` on the three reserved planes plus
   three offset datum planes) knit into a single CLOSED body → ``kind=="solid"`` with the exact
   box volume (mass_props volume > 0). The six input surface bodies are consumed (1 body left).
2. Two coplanar open patches that share one edge knit into a single LARGER OPEN shell →
   ``kind=="surface"`` (volume ≈ 0, area = the sum of the patch areas), and they actually join
   (one shell, the shared edge is internal — not two loose faces).
3. A tube wall (``surf_extrude``, 4 side faces) + two ``surf_planar`` end caps knit into the same
   closed box solid — the mixed-source path to a solid.
4. Knit with fewer than two resolvable surfaces → a clean contained error (ok:false).

Body selectors reference the geometry-owning feature whose tool_body matches the surface body —
for a ``surf_planar`` face that is the driving SKETCH (its bounding face is registered as the
sketch's tool_body with an identical signature). Harness mirrors engine/doc/test_extrude_ends.py
and test_surf_extrude.py.
"""

from __future__ import annotations

# Import the feature modules so their @register decorators run.
from engine.doc.features import surf_knit  # noqa: F401  (registers surf_knit)
from engine.doc.features import surf_planar  # noqa: F401  (planar faces / box walls)
from engine.doc.features import surf_extrude  # noqa: F401  (tube side wall)
from engine.doc.features import extrude  # noqa: F401  (surf_* reuse its resolvers)
from engine.doc.features import boolean  # noqa: F401  (surf_knit reuses its body matcher)

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part

from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)


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


def _body_sel(owner: str) -> dict:
    return {"kind": "body", "owner_feature": owner}


def _is_solid(shape) -> bool:
    return shape.wrapped.ShapeType() == TopAbs_SOLID


def _count(shape, topo_type: int) -> int:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape.wrapped, topo_type, m)
    return m.Extent()


def _edge_face_counts(shape) -> tuple[int, int]:
    """Return (shared_edge_count, free_edge_count) — edges with 2 / exactly 1 adjacent face."""
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
    shared = sum(1 for i in range(1, m.Extent() + 1) if m.FindFromIndex(i).Extent() == 2)
    free = sum(1 for i in range(1, m.Extent() + 1) if m.FindFromIndex(i).Extent() == 1)
    return shared, free


# --------------------------------------------------------------------------- box-of-6-walls builder

def _box_walls_features(L: float, W: float, H: float) -> list[dict]:
    """Six ``surf_planar`` walls of an axis-aligned box at the origin [0,L]x[0,W]x[0,H].

    Walls (each on its own plane, distinct 3D centroid so the body matcher is unambiguous):
      - bottom z=0 / top z=H        on plane.top (XY) and its +H offset   — sketch (x,y)
      - front y=0 / back y=W         on plane.front (XZ) and its -W offset  — sketch (x,z)
      - left x=0 / right x=L         on plane.right (YZ) and its +L offset  — sketch (y,z)

    (plane.front's offset normal points along -Y, so the back wall uses a -W offset to land at
    y=+W — matching the datum-plane convention.) Feature ids match the required ``f<N>`` pattern;
    the six driving sketches are f1,f4,f6,f8,f10,f12 (the body selectors reference these — a
    surf_planar wall's bounding face equals its sketch's tool_body, an exact signature match).
    """
    return [
        # bottom z=0
        _rect_sketch("f1", 0, 0, L, W, "plane.top"),
        _feat("f2", "surf_planar", {}, refs={"profile": [_region_sel("f1", L / 2, W / 2, L * W)]}),
        # top z=H
        _feat("f3", "plane", {"method": "offset", "offset": _dim(H)}, refs={"base": "plane.top"}),
        _rect_sketch("f4", 0, 0, L, W, "f3"),
        _feat("f5", "surf_planar", {}, refs={"profile": [_region_sel("f4", L / 2, W / 2, L * W)]}),
        # front y=0
        _rect_sketch("f6", 0, 0, L, H, "plane.front"),
        _feat("f7", "surf_planar", {}, refs={"profile": [_region_sel("f6", L / 2, H / 2, L * H)]}),
        # back y=W (offset -W along plane.front's -Y normal)
        _feat("f8", "plane", {"method": "offset", "offset": _dim(-W)}, refs={"base": "plane.front"}),
        _rect_sketch("f9", 0, 0, L, H, "f8"),
        _feat("f10", "surf_planar", {}, refs={"profile": [_region_sel("f9", L / 2, H / 2, L * H)]}),
        # left x=0
        _rect_sketch("f11", 0, 0, W, H, "plane.right"),
        _feat("f12", "surf_planar", {}, refs={"profile": [_region_sel("f11", W / 2, H / 2, W * H)]}),
        # right x=L
        _feat("f13", "plane", {"method": "offset", "offset": _dim(L)}, refs={"base": "plane.right"}),
        _rect_sketch("f14", 0, 0, W, H, "f13"),
        _feat("f15", "surf_planar", {}, refs={"profile": [_region_sel("f14", W / 2, H / 2, W * H)]}),
    ]


_BOX_WALL_SKETCHES = ["f1", "f4", "f6", "f9", "f11", "f14"]


# --------------------------------------------------------------------------- test 1: closed box → solid

def test_knit_six_planar_walls_into_solid() -> None:
    """Six ``surf_planar`` walls of a 20x30x16 box knit into ONE watertight body → a SOLID with
    the exact box volume (= L*W*H = 9600 mm^3). The six input surface bodies are consumed."""
    L, W, H = 20.0, 30.0, 16.0
    feats = _box_walls_features(L, W, H)
    feats.append(_feat("f16", "surf_knit", {},
                       refs={"surfaces": [_body_sel(s) for s in _BOX_WALL_SKETCHES]}))
    doc = _doc(feats)
    _assert_valid(doc)
    res = compile_document(doc)

    # Every wall + the knit must compile cleanly.
    for s in res.feature_status:
        assert s["ok"], f"{s['id']} failed: {s['error']}"
    s = _status(res, "f16")
    assert s["ok"], f"surf_knit failed: {s['error']}"

    # The six inputs are consumed; only the knit body remains.
    assert len(res.bodies) == 1, f"expected 1 body after knit; got {len(res.bodies)}"
    bo = res.bodies[0]

    # Watertight → promoted to a SOLID.
    assert bo.kind == "solid", f"closed knit should be a solid; got kind={bo.kind!r}"
    assert _is_solid(bo.shape), "knit body must be a TopoDS solid"
    assert _count(bo.shape, TopAbs_SOLID) == 1, "knit should yield exactly one solid"

    # mass_props reports a REAL enclosed volume = the box volume.
    mp = mass_props(bo.shape)
    assert not mp.get("is_surface"), "a solid knit must not report is_surface"
    expected = L * W * H  # 9600
    assert mp["volume_mm3"] > 0.0, f"solid must have positive volume; got {mp['volume_mm3']}"
    assert abs(mp["volume_mm3"] - expected) / expected < 0.01, \
        f"knit solid volume {mp['volume_mm3']:.1f} vs expected box {expected}"

    # A closed solid has NO free boundary edges (every edge is shared by 2 faces).
    _shared, free = _edge_face_counts(bo.shape)
    assert free == 0, f"watertight knit must have no free edges; got {free}"

    # The feature echoes its closed/volume telemetry.
    assert s["resolved"].get("_closed", 0.0) == 1.0, "resolved._closed should flag a solid"
    assert abs(s["resolved"].get("volume", 0.0) - expected) / expected < 0.01
    print(f"[knit-box-solid] kind={bo.kind} vol={mp['volume_mm3']:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 2: open patches → shell

def test_knit_two_open_patches_into_surface() -> None:
    """Two coplanar 20x30 patches sharing the edge at x=20 knit into a SINGLE larger OPEN shell
    → kind=='surface' (volume ≈ 0, area = 1200). They genuinely JOIN: one shell, the shared
    edge becomes internal (adjacent to both faces), and free boundary edges remain."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 30, "plane.top"),
        _feat("f2", "surf_planar", {}, refs={"profile": [_region_sel("f1", 10, 15, 600.0)]}),
        _rect_sketch("f3", 20, 0, 20, 30, "plane.top"),
        _feat("f4", "surf_planar", {}, refs={"profile": [_region_sel("f3", 30, 15, 600.0)]}),
        _feat("f5", "surf_knit", {}, refs={"surfaces": [_body_sel("f1"), _body_sel("f3")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f5")
    assert s["ok"], f"surf_knit (open) failed: {s['error']}"

    assert len(res.bodies) == 1, f"expected 1 knit body; got {len(res.bodies)}"
    bo = res.bodies[0]

    # Open → stays a surface body.
    assert bo.kind == "surface", f"open knit should be a surface; got kind={bo.kind!r}"
    assert not _is_solid(bo.shape), "open knit must NOT be a solid"

    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"open knit mass_props should flag is_surface; got {mp}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"open shell volume should be ~0; got {mp['volume_mm3']}"
    expected_area = 600.0 + 600.0
    assert abs(mp["area_mm2"] - expected_area) / expected_area < 0.01, \
        f"merged-patch area {mp['area_mm2']:.1f} vs expected {expected_area}"

    # The patches actually JOINED: a single shell of 2 faces with one shared (internal) edge.
    assert _count(bo.shape, TopAbs_SHELL) == 1, "the two patches must sew into ONE shell"
    assert _count(bo.shape, TopAbs_FACE) == 2, "the merged shell should have both faces"
    shared, free = _edge_face_counts(bo.shape)
    assert shared >= 1, "the patches must share an internal edge (proof they actually knit)"
    assert free > 0, "an open shell must still have free boundary edges"

    assert s["resolved"].get("_closed", 1.0) == 0.0, "resolved._closed should be 0 for a surface"
    print(f"[knit-open-surface] kind={bo.kind} area={mp['area_mm2']:.1f} "
          f"shared_edges={shared} free_edges={free}")


# --------------------------------------------------------------------------- test 3: tube + caps → solid

def test_knit_tube_wall_plus_caps_into_solid() -> None:
    """Mixed-source closed box: a ``surf_extrude`` tube wall (4 side faces, the 20x30 profile
    swept up H=16) + a bottom ``surf_planar`` cap (z=0) + a top cap (z=H) knit into the same
    box solid (volume 9600). Exercises the surf_extrude → solid path."""
    L, W, H = 20.0, 30.0, 16.0
    doc = _doc([
        # tube side walls (open, no caps)
        _rect_sketch("f1", 0, 0, L, W, "plane.top"),
        _feat("f2", "surf_extrude", {"end": "blind", "depth": _dim(H)},
              refs={"profile": [_region_sel("f1", L / 2, W / 2, L * W)]}),
        # bottom cap at z=0
        _rect_sketch("f3", 0, 0, L, W, "plane.top"),
        _feat("f4", "surf_planar", {}, refs={"profile": [_region_sel("f3", L / 2, W / 2, L * W)]}),
        # top cap at z=H
        _feat("f5", "plane", {"method": "offset", "offset": _dim(H)}, refs={"base": "plane.top"}),
        _rect_sketch("f6", 0, 0, L, W, "f5"),
        _feat("f7", "surf_planar", {}, refs={"profile": [_region_sel("f6", L / 2, W / 2, L * W)]}),
        # knit tube + the two caps. The tube is referenced by the surf_extrude feature itself
        # (its 4-face wall is the unique 4-face body — resolved via the topo-owner fallback); the
        # caps by their driving sketches (exact face-signature match).
        _feat("f8", "surf_knit", {},
              refs={"surfaces": [_body_sel("f2"), _body_sel("f3"), _body_sel("f6")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f8")
    assert s["ok"], f"surf_knit (tube+caps) failed: {s['error']}"

    assert len(res.bodies) == 1, f"expected 1 knit body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "solid", f"tube+caps should close into a solid; got kind={bo.kind!r}"
    assert _is_solid(bo.shape)

    mp = mass_props(bo.shape)
    expected = L * W * H  # 9600
    assert abs(mp["volume_mm3"] - expected) / expected < 0.01, \
        f"tube+caps solid volume {mp['volume_mm3']:.1f} vs expected {expected}"
    _shared, free = _edge_face_counts(bo.shape)
    assert free == 0, f"closed solid must have no free edges; got {free}"
    print(f"[knit-tube-caps-solid] kind={bo.kind} vol={mp['volume_mm3']:.1f} (expected {expected})")


# --------------------------------------------------------------------------- test 4: too-few error

def test_knit_requires_two_surfaces() -> None:
    """A knit that resolves fewer than two surface bodies → a clean contained error (ok:false),
    never a crash."""
    doc = _doc([
        _rect_sketch("f1", 0, 0, 20, 30, "plane.top"),
        _feat("f2", "surf_planar", {}, refs={"profile": [_region_sel("f1", 10, 15, 600.0)]}),
        # Only one surface selected → not enough to knit.
        _feat("f3", "surf_knit", {}, refs={"surfaces": [_body_sel("f1")]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "knit with one surface should fail"
    assert "at least 2" in s["error"], f"expected a clear 'at least 2' error; got: {s['error']}"
    # The single surface body is untouched (the failed feature continues on the last good state).
    assert len(res.bodies) == 1
    print(f"[knit-too-few] clean error: {s['error'][:60]}")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_knit_six_planar_walls_into_solid()
    test_knit_two_open_patches_into_surface()
    test_knit_tube_wall_plus_caps_into_solid()
    test_knit_requires_two_surfaces()
    print("\nSURF-KNIT TESTS PASSED")


if __name__ == "__main__":
    main()
