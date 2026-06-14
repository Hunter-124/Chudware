"""Sheet-metal HEM + JOG tests — fold an edge back on itself / step the flange to a parallel offset.

Run:  & $py -m pytest engine/doc/test_sheetmetal_hem_jog.py -q   (from repo root)

Both features build on the SAME bend infrastructure as the base/edge flange and store per-bend
records (BA = θ·(R+K·T)) so the flat pattern develops them. The SolidWorks-relevant checks:

HEM (sm_hem):
1.  closed hem folds the return wall ANTIPARALLEL to the parent (a 180° fold), with a tiny gap
    (≈ thickness) between the two skins; thickness preserved; one bend record with BA stored.
2.  open hem leaves a visible gap larger than the closed hem (a real radius, not ~T/2).
3.  teardrop hem curls past 180°.
4.  flat pattern develops a hemmed part (no crash, planar single-thickness sheet).
5.  error cases: no sheet / before a base flange; bad (curved) edge.

JOG (sm_jog):
6.  the end flange is PARALLEL to the base but offset by ≈ ``offset`` (two opposing bends);
    two bend records stored.
7.  flat pattern develops a jogged part (two bends).
8.  error cases: no sheet; radius 0; angle out of range.

Harness mirrors test_sheetmetal.py / test_sheetmetal_flat.py.
"""

from __future__ import annotations

import math

from build123d import GeomType

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc.selectors import describe_edges, make_selector
from engine.doc.features.sheetmetal import bend_allowance


# --------------------------------------------------------------------------- harness


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-sheetmetal-hemjog",
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


def _rect_sketch(fid: str, w: float, h: float, plane: str = "plane.top") -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [w, 0.0]},
            {"id": "e2", "type": "line", "construction": False, "p1": [w, 0.0], "p2": [w, h]},
            {"id": "e3", "type": "line", "construction": False, "p1": [w, h], "p2": [0.0, h]},
            {"id": "e4", "type": "line", "construction": False, "p1": [0.0, h], "p2": [0.0, 0.0]},
        ],
        "constraints": [],
    }, refs={"plane": plane})


def _region_sel(sid: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sid, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _base_flange_doc(w: float = 60.0, h: float = 40.0, thickness: float = 2.0) -> list[dict]:
    return [
        _rect_sketch("f1", w, h),
        _feat("f2", "sm_base_flange", {"thickness": _dim(thickness)},
              refs={"profile": [_region_sel("f1", w / 2.0, h / 2.0, w * h)]}),
    ]


def _pick_edge_selector(res, target_mid: tuple[float, float, float],
                        length: float | None = None, owner: str = "f2") -> dict:
    body = res.bodies[0]
    infos = describe_edges(body.shape.wrapped)
    cands = [i for i in infos if i.surf == "line"]
    if length is not None:
        cands = [i for i in cands if abs((i.measure or 0.0) - length) < 1e-3] or cands
    best = min(cands, key=lambda i: sum(
        (i.centroid[k] - target_mid[k]) ** 2 for k in range(3)))
    sel = make_selector(best)
    sel["owner_feature"] = owner
    return sel


def _z_broad_faces(shape, min_area: float = 400.0):
    """Planar faces whose normal is ±Z (the parent + folded broad faces), area-filtered."""
    return [f for f in shape.faces()
            if f.geom_type == GeomType.PLANE and abs(f.normal_at().Z) > 0.99
            and f.area > min_area]


def _is_planar_single_thickness(shape, thickness: float) -> bool:
    planes = [f for f in shape.faces() if f.geom_type == GeomType.PLANE]
    if not planes:
        return False
    biggest = max(planes, key=lambda f: f.area)
    n = biggest.normal_at()
    n = n * (1.0 / max(n.length, 1e-12))
    broad = [f for f in planes
             if abs(abs(f.normal_at().dot(n)) - 1.0) < 1e-4 and f.area > 0.25 * biggest.area]
    centers = [f.center().dot(n) for f in broad]
    return abs((max(centers) - min(centers)) - thickness) < 1e-3


# --------------------------------------------------------------------------- 1: closed hem


def test_hem_closed_folds_back_antiparallel() -> None:
    """A closed hem folds the return wall flat back against the sheet (180° fold, tiny gap).

    The base sheet's broad faces are at Z=0/Z=2 with ±Z normals. A closed hem (default radius ≈ T/2)
    folds a return wall BELOW the sheet whose broad faces are also ±Z (antiparallel to the parent),
    standing off by a gap of roughly the material thickness. Thickness is preserved; one bend record
    with BA = θ·(R+K·T) at θ = 180° is stored.
    """
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sheet_vol = res0.bodies[0].shape.volume
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [_feat("f3", "sm_hem", {"type": "closed"}, refs={"edge": sel})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"closed hem failed: {s['error']}"
    body = res.bodies[0].shape

    # The hem adds material.
    assert body.volume > sheet_vol + 100.0, "hem should add the return-wall material"

    # The return wall lies BELOW the base (Z<0) with a ±Z normal → antiparallel to the parent face.
    zf = _z_broad_faces(body)
    ret = [f for f in zf if f.center().Z < -1e-6]
    assert ret, "closed hem should produce a return wall below the base sheet"
    # The wall is (anti)parallel to the parent top face (+Z) — the return broad faces are ±Z.
    assert all(abs(f.normal_at().Z) > 0.99 for f in ret), \
        "the closed-hem return wall must be parallel/antiparallel to the parent face"

    # Tiny gap: the TOP skin of the return wall sits within ~thickness of the base bottom (Z=0).
    top_ret = max(f.center().Z for f in ret)
    gap = 0.0 - top_ret
    assert gap <= 2.0 * 2.0 + 1e-6, f"closed-hem gap should be ~thickness; got {gap:.3f}"

    # Thickness preserved: the return wall's two skins are 2 mm apart.
    ret_zs = sorted(f.center().Z for f in ret)
    assert abs((ret_zs[-1] - ret_zs[0]) - 2.0) < 1e-3, \
        f"return-wall thickness must stay 2 mm; got {ret_zs[-1]-ret_zs[0]:.3f}"

    # Bend record: one bend, θ=180°, BA = π·(R+K·T).
    R = s["resolved"]["radius"]
    ba_expected = bend_allowance(180.0, R, 2.0, 0.45)
    assert abs(s["resolved"]["bend_allowance"] - ba_expected) < 1e-6, \
        f"hem BA {s['resolved']['bend_allowance']} vs {ba_expected}"
    assert abs(s["resolved"]["angle"] - 180.0) < 1e-9
    print(f"[hem-closed] return wall antiparallel, gap={gap:.2f}, BA={ba_expected:.4f}")


# --------------------------------------------------------------------------- 2: open hem gap


def test_hem_open_leaves_larger_gap_than_closed() -> None:
    """An open hem (real radius) stands the return wall off by a bigger gap than a closed hem."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    gaps: dict[str, float] = {}
    for htype, radius in (("closed", None), ("open", 3.0)):
        params: dict = {"type": htype}
        if radius is not None:
            params["radius"] = _dim(radius)
        doc = _doc(base + [_feat("f3", "sm_hem", params, refs={"edge": sel})])
        _assert_valid(doc)
        res = compile_document(doc)
        s = _status(res, "f3")
        assert s["ok"], f"{htype} hem failed: {s['error']}"
        bb = res.bodies[0].shape.bounding_box()
        # How far the hem reaches below the base (the fold depth) tracks the open-hem standoff.
        gaps[htype] = -bb.min.Z

    assert gaps["open"] > gaps["closed"] + 0.5, \
        f"open hem (radius 3) should stand off more than the closed hem; got {gaps}"
    print(f"[hem-open] fold depth closed={gaps['closed']:.2f} < open={gaps['open']:.2f}")


# --------------------------------------------------------------------------- 3: teardrop hem


def test_hem_teardrop_curls_past_180() -> None:
    """A teardrop hem builds with a bend sweeping past 180° (a near-closed curl)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [_feat("f3", "sm_hem", {"type": "teardrop", "radius": _dim(2.0)},
                             refs={"edge": sel})])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"teardrop hem failed: {s['error']}"
    assert s["resolved"]["angle"] > 180.0, "teardrop hem should curl past 180°"
    # BA reflects the >180° sweep (longer than the open hem's 180° BA at the same radius).
    ba_open = bend_allowance(180.0, 2.0, 2.0, 0.45)
    assert s["resolved"]["bend_allowance"] > ba_open, \
        "teardrop BA should exceed the 180° open-hem BA at the same radius"
    print(f"[hem-teardrop] angle={s['resolved']['angle']}° BA={s['resolved']['bend_allowance']:.4f}")


# --------------------------------------------------------------------------- 4: hem flat pattern


def test_hem_flat_pattern_develops() -> None:
    """A hemmed part flattens into a planar single-thickness blank (one bend developed)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_hem", {"type": "open", "radius": _dim(2.0), "length": _dim(8.0)},
              refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f3")["ok"], _status(res, "f3")["error"]
    s4 = _status(res, "f4")
    assert s4["ok"], f"hem flat pattern failed: {s4['error']}"
    flat = res.bodies[0].shape
    assert _is_planar_single_thickness(flat, 2.0), "hem flat must be a single 2 mm sheet"
    assert abs(s4["resolved"]["bend_count"] - 1.0) < 1e-9, "hem develops one bend"
    # Plausible developed length: base (40) minus setback + BA(180°) + return wall, well over 40.
    bb = flat.bounding_box()
    assert (bb.max.Y - bb.min.Y) > 40.0, "flat develops longer than the base alone"
    print(f"[hem-flat] planar; Y-extent={bb.max.Y-bb.min.Y:.2f}; vol={flat.volume:.1f}")


# --------------------------------------------------------------------------- 5: hem errors


def test_hem_before_base_flange_errors() -> None:
    """A hem on a non-sheet-metal body errors cleanly."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "sm_hem", {"type": "closed"}, refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "hem on a non-sheet part should fail"
    assert "base flange" in s["error"].lower() or "sheet metal" in s["error"].lower(), \
        f"error should mention the missing base flange; got: {s['error']}"
    print(f"[hem-err-nobase] -> '{s['error'][:60]}'")


def test_hem_curved_edge_rejected() -> None:
    """A non-straight (circular) bend edge is rejected by the shared straightness guard."""
    from build123d import Edge, Plane, Vector
    from engine.doc.features.sheetmetal import _edge_dir_and_endpoints
    arc = Edge.make_circle(8.0, Plane(origin=Vector(0, 0, 0)), start_angle=0.0, end_angle=90.0)
    raised = False
    try:
        _edge_dir_and_endpoints(arc)
    except Exception as exc:  # noqa: BLE001
        raised = True
        assert "straight" in str(exc).lower() or "line" in str(exc).lower()
    assert raised, "the straightness guard must reject a circular edge"
    print("[hem-err-curved] straightness guard rejects a curved edge")


# --------------------------------------------------------------------------- 6: jog offset


def test_jog_end_flange_parallel_and_offset() -> None:
    """A jog steps the end flange to a PARALLEL plane offset by ≈ offset (two opposing bends).

    Base broad faces are at Z=0/Z=2 (mid-plane Z=1). With angle=90, offset=10 the end flange's broad
    faces are also ±Z (parallel to the base) but offset so the end mid-plane is ~offset away. Two
    bend records are stored.
    """
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sheet_vol = res0.bodies[0].shape.volume
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_jog",
              {"angle": _dim(90.0), "offset": _dim(10.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"jog failed: {s['error']}"
    body = res.bodies[0].shape
    assert body.volume > sheet_vol + 100.0, "jog should add material"

    zf = _z_broad_faces(body)
    # The base broad faces (centred near the sheet middle, Z≈0/2) and the END flange broad faces
    # (the lowest/offset pair). Group by Z.
    end = [f for f in zf if f.center().Z < -2.0]
    assert len(end) >= 2, "the end flange should have two ±Z (parallel) broad faces below the base"
    assert all(abs(f.normal_at().Z) > 0.99 for f in end), \
        "the end flange must be PARALLEL to the base (±Z normals)"

    # Offset: base mid-plane Z=1; end-flange mid-plane = mean of its two skin Zs ≈ 1 - offset.
    end_zs = sorted(f.center().Z for f in end)
    end_mid = (end_zs[0] + end_zs[-1]) / 2.0
    base_mid = 1.0
    measured_offset = abs(base_mid - end_mid)
    assert abs(measured_offset - 10.0) < 0.6, \
        f"end-flange offset should be ≈ 10 mm; got {measured_offset:.3f}"
    # The end flange thickness is preserved.
    assert abs((end_zs[-1] - end_zs[0]) - 2.0) < 1e-3, "end-flange thickness must stay 2 mm"

    # Echoed achieved offset matches.
    assert abs(s["resolved"]["offset"] - 10.0) < 0.6, \
        f"resolved offset {s['resolved']['offset']} vs 10"
    # Bend allowance for one of the two 90° bends.
    ba90 = bend_allowance(90.0, 2.0, 2.0, 0.45)
    assert abs(s["resolved"]["bend_allowance"] - ba90) < 1e-3
    print(f"[jog] end flange parallel; offset={measured_offset:.3f}; BA(90°)={ba90:.4f}")


def test_jog_stores_two_bend_records() -> None:
    """The jog stores TWO bend records (the up bend + the back bend) for the flat pattern."""
    from engine.doc.compiler import _resolve_params
    from engine.doc.features import CompileContext, get_compiler
    from engine.doc.features.datum import reserved_datums

    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_jog",
              {"angle": _dim(90.0), "offset": _dim(10.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)

    env: dict = {}
    bodies: list = []
    datums = reserved_datums()
    for feat in doc["features"]:
        rp, _, _ = _resolve_params(feat.get("params", {}), env)
        fv = dict(feat)
        fv["params"] = rp
        out = get_compiler(feat["type"])(
            fv, CompileContext(equations_env=env, bodies=bodies, datums=datums))
        bodies = out.bodies

    recs = [r for r in datums["_sheet_metal_bends"].obj if r["feature"] == "f3"]
    assert len(recs) == 2, f"a jog should store two bend records; got {len(recs)}"
    for r in recs:
        assert abs(r["angle"] - 90.0) < 1e-9
        assert abs(r["bend_allowance"] - bend_allowance(90.0, 2.0, 2.0, 0.45)) < 1e-6
    print(f"[jog-records] two bend records stored, each 90° BA={recs[0]['bend_allowance']:.4f}")


# --------------------------------------------------------------------------- 7: jog flat pattern


def test_jog_flat_pattern_develops_two_bends() -> None:
    """A jogged part flattens into a planar blank developing BOTH bends."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_jog",
              {"angle": _dim(90.0), "offset": _dim(10.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f3")["ok"], _status(res, "f3")["error"]
    s4 = _status(res, "f4")
    assert s4["ok"], f"jog flat pattern failed: {s4['error']}"
    flat = res.bodies[0].shape
    assert _is_planar_single_thickness(flat, 2.0), "jog flat must be a single 2 mm sheet"
    assert abs(s4["resolved"]["bend_count"] - 2.0) < 1e-9, "jog develops two bends"
    bb = flat.bounding_box()
    assert (bb.max.Y - bb.min.Y) > 40.0, "flat develops longer than the base alone"
    print(f"[jog-flat] planar; 2 bends; Y-extent={bb.max.Y-bb.min.Y:.2f}; vol={flat.volume:.1f}")


# --------------------------------------------------------------------------- 8: jog errors


def test_jog_before_base_flange_errors() -> None:
    """A jog on a non-sheet-metal body errors cleanly."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "sm_jog", {"angle": _dim(90.0), "offset": _dim(10.0), "length": _dim(20.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "jog on a non-sheet part should fail"
    assert "base flange" in s["error"].lower() or "sheet metal" in s["error"].lower(), \
        f"error should mention the missing base flange; got: {s['error']}"
    print(f"[jog-err-nobase] -> '{s['error'][:60]}'")


def test_jog_radius_zero_and_bad_angle_error() -> None:
    """Jog rejects radius 0 and an angle outside (0, 90]."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_jog", {"angle": _dim(90.0), "offset": _dim(10.0), "radius": _dim(0.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    s = _status(compile_document(doc), "f3")
    assert not s["ok"] and "radius" in s["error"].lower(), f"radius 0 should fail; got {s['error']}"

    doc2 = _doc(base + [
        _feat("f3", "sm_jog", {"angle": _dim(120.0), "offset": _dim(10.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc2)
    s2 = _status(compile_document(doc2), "f3")
    assert not s2["ok"] and "angle" in s2["error"].lower(), \
        f"angle 120 should fail (must be in (0,90]); got {s2['error']}"
    print(f"[jog-err] radius0 -> '{s['error'][:40]}'; angle120 -> '{s2['error'][:40]}'")
