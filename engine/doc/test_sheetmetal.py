"""Sheet-metal feature tests — base flange + edge flange with real bend geometry.

Run:  & $py -m pytest engine/doc/test_sheetmetal.py -q   (from repo root)

Covers (feature_params.md §Sheet Metal):
1.  base flange: 60x40 region, thickness 2 -> 60x40x2 = 4800 mm³ sheet; stores thickness/k.
2.  edge flange 90° length 20 radius 2 off a long edge -> volume increases by wall + bend region;
    the flange wall is perpendicular (a broad face normal flips ~90° vs the sheet) and thickness
    stays 2 mm; the stored bend allowance BA ≈ (π/2)(2 + 0.45·2) = 4.555 mm.
3.  edge flange at 45°.
4.  two edge flanges on different edges.
5.  position material_inside / material_outside / bend_outside differ in the outside envelope.
6.  k-factor inheritance from the base flange.
7.  bend record stored for the flat pattern (radius/angle/k/neutral_radius/BA/BD).
8.  error cases: open profile, bad (curved) edge, radius 0, edge flange before a base flange.

Harness mirrors test_features1.py (_doc + compile_document, make_selector for edge picks).
"""

from __future__ import annotations

import math

from build123d import GeomType

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part
from engine.doc.selectors import describe_edges, make_selector


# --------------------------------------------------------------------------- harness


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict], globals_list: list[dict] | None = None) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-sheetmetal",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": globals_list or []},
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


def _rect_sketch(fid: str, w: float, h: float, plane: str = "plane.top",
                 x0: float = 0.0, y0: float = 0.0) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": [x0, y0], "p2": [x0 + w, y0]},
            {"id": "e2", "type": "line", "construction": False, "p1": [x0 + w, y0], "p2": [x0 + w, y0 + h]},
            {"id": "e3", "type": "line", "construction": False, "p1": [x0 + w, y0 + h], "p2": [x0, y0 + h]},
            {"id": "e4", "type": "line", "construction": False, "p1": [x0, y0 + h], "p2": [x0, y0]},
        ],
        "constraints": [],
    }, refs={"plane": plane})


def _region_sel(sid: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sid, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


def _base_flange_doc(w: float = 60.0, h: float = 40.0, thickness: float = 2.0,
                     k_factor: float | None = None) -> list[dict]:
    """A base-flange part: w×h sheet of the given thickness."""
    params: dict = {"thickness": _dim(thickness)}
    if k_factor is not None:
        params["k_factor"] = _dim(k_factor)
    return [
        _rect_sketch("f1", w, h),
        _feat("f2", "sm_base_flange", params,
              refs={"profile": [_region_sel("f1", w / 2.0, h / 2.0, w * h)]}),
    ]


def _pick_edge_selector(res, target_mid: tuple[float, float, float],
                        length: float | None = None, owner: str = "f2") -> dict:
    """Build an edge selector for the line edge nearest *target_mid* (optionally length-matched)."""
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


# --------------------------------------------------------------------------- 1: base flange


def test_base_flange_volume_and_context() -> None:
    """60x40 region thickness 2 -> 4800 mm³ sheet; resolved echoes thickness/k_factor."""
    doc = _doc(_base_flange_doc(60.0, 40.0, 2.0))
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert s["ok"], f"base flange failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    expected = 60.0 * 40.0 * 2.0
    assert abs(vol - expected) / expected < 0.001, f"base flange vol {vol} vs {expected}"
    # Sheet thickness in Z is exactly 2.
    bb = res.bodies[0].shape.bounding_box()
    assert abs((bb.max.Z - bb.min.Z) - 2.0) < 1e-6, "base sheet thickness must be 2 mm"
    # Part-level context echoed.
    assert abs(s["resolved"]["thickness"] - 2.0) < 1e-9
    assert abs(s["resolved"]["k_factor"] - 0.45) < 1e-9
    assert abs(s["resolved"]["default_bend_radius"] - 2.0) < 1e-9
    print(f"[sm-base] 60x40x2 sheet vol={vol:.1f}; k={s['resolved']['k_factor']}")


# --------------------------------------------------------------------------- 2: edge flange 90°


def test_edge_flange_90_geometry_and_ba() -> None:
    """90° flange length 20 radius 2 off a long edge: wall perpendicular, thickness 2, BA≈4.555."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sheet_vol = res0.bodies[0].shape.volume
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange", {
            "angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
            "position": "material_inside", "flip": False,
        }, refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"edge flange failed: {s['error']}"

    body = res.bodies[0].shape
    vol = body.volume
    # Volume increased by ~ wall(20×60×2=2400) + bend region (quarter annulus ×60) minus the
    # material_inside overlap into the corner. Must be strictly greater than the sheet alone, and
    # in the right ballpark (wall ≈ 2400).
    assert vol > sheet_vol + 2000.0, f"flange should add ~wall+bend material; {vol} vs {sheet_vol}"

    # Bend allowance BA = (π/2)(R + K·T) = (π/2)(2 + 0.45·2) = 4.5553 mm.
    ba_expected = (math.pi / 2.0) * (2.0 + 0.45 * 2.0)
    assert abs(s["resolved"]["bend_allowance"] - ba_expected) < 1e-3, \
        f"BA {s['resolved']['bend_allowance']} vs expected {ba_expected}"
    assert abs(ba_expected - 4.5553) < 1e-3

    # Thickness preserved: the wall's two broad faces (area ~1200, normals ±Y) sit 2 mm apart.
    planes = [f for f in body.faces() if f.geom_type == GeomType.PLANE and f.area > 1000.0]
    y_faces = [f for f in planes if abs(f.normal_at().Y) > 0.99]
    assert len(y_faces) >= 2, "wall should have two large faces with ±Y normals"
    ys = sorted(f.center().Y for f in y_faces)
    assert abs((ys[-1] - ys[0]) - 2.0) < 1e-4, f"wall thickness must stay 2 mm; got {ys[-1]-ys[0]}"

    # Perpendicularity: the sheet top face normal is +Z; the wall broad faces are ±Y => 90° flip.
    top = max((f for f in body.faces()
               if f.geom_type == GeomType.PLANE and abs(f.normal_at().Z) > 0.99),
              key=lambda f: f.area)
    wall_n = y_faces[0].normal_at()
    dot = abs(top.normal_at().X * wall_n.X + top.normal_at().Y * wall_n.Y
              + top.normal_at().Z * wall_n.Z)
    assert dot < 1e-4, f"wall normal should be perpendicular to sheet (dot={dot})"

    print(f"[sm-edge90] vol {sheet_vol:.0f}->{vol:.0f}; BA={s['resolved']['bend_allowance']:.4f}; "
          f"wall thickness=2; perp dot={dot:.2e}")


def test_edge_flange_neutral_radius_and_deduction() -> None:
    """Stored neutral_radius = R + K·T and bend_deduction are correct for the 90° flange."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], s["error"]
    nr = s["resolved"]["neutral_radius"]
    assert abs(nr - (2.0 + 0.45 * 2.0)) < 1e-9, f"neutral_radius {nr} vs 2.9"
    # BD = 2·(R+T)·tan(45°) - BA = 2·4·1 - 4.5553 = 3.4447 mm.
    bd_expected = 2.0 * (2.0 + 2.0) * math.tan(math.radians(45.0)) - (math.pi / 2.0) * 2.9
    assert abs(s["resolved"]["bend_deduction"] - bd_expected) < 1e-3, \
        f"BD {s['resolved']['bend_deduction']} vs {bd_expected}"
    print(f"[sm-bend-data] neutral_radius={nr:.3f}, BD={s['resolved']['bend_deduction']:.4f}")


# --------------------------------------------------------------------------- 3: 45° flange


def test_edge_flange_45_degrees() -> None:
    """45° edge flange builds; BA = (π/4)(R+K·T); the wall is NOT perpendicular."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sheet_vol = res0.bodies[0].shape.volume
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(45.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"45° flange failed: {s['error']}"
    vol = res.bodies[0].shape.volume
    assert vol > sheet_vol + 1000.0, "45° flange should still add material"
    ba_expected = math.radians(45.0) * (2.0 + 0.45 * 2.0)
    assert abs(s["resolved"]["bend_allowance"] - ba_expected) < 1e-3, \
        f"45° BA {s['resolved']['bend_allowance']} vs {ba_expected}"
    print(f"[sm-edge45] vol {sheet_vol:.0f}->{vol:.0f}; BA={s['resolved']['bend_allowance']:.4f} "
          f"(angle/2 of the 90° BA)")


# --------------------------------------------------------------------------- 4: multiple flanges


def test_two_edge_flanges_different_edges() -> None:
    """Two flanges on opposite long edges both build and each adds material."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sheet_vol = res0.bodies[0].shape.volume
    sel_a = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc1 = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(15.0), "radius": _dim(2.0)},
              refs={"edge": sel_a}),
    ])
    _assert_valid(doc1)
    res1 = compile_document(doc1)
    assert _status(res1, "f3")["ok"], _status(res1, "f3")["error"]
    vol1 = res1.bodies[0].shape.volume

    # Pick the opposite long edge (y=40 side) on the post-f3 body and add a second flange.
    sel_b = _pick_edge_selector(res1, (30.0, 40.0, 0.0), length=60.0, owner="f2")
    doc2 = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(15.0), "radius": _dim(2.0)},
              refs={"edge": sel_a}),
        _feat("f4", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(15.0), "radius": _dim(2.0)},
              refs={"edge": sel_b}),
    ])
    _assert_valid(doc2)
    res2 = compile_document(doc2)
    s3 = _status(res2, "f3")
    s4 = _status(res2, "f4")
    assert s3["ok"], f"first flange failed: {s3['error']}"
    assert s4["ok"], f"second flange failed: {s4['error']}"
    vol2 = res2.bodies[0].shape.volume
    assert vol2 > vol1 > sheet_vol, \
        f"each flange should add material: sheet {sheet_vol:.0f} < one {vol1:.0f} < two {vol2:.0f}"
    print(f"[sm-multi] sheet {sheet_vol:.0f} -> +1 flange {vol1:.0f} -> +2 flanges {vol2:.0f}")


# --------------------------------------------------------------------------- 5: position variants


def test_position_variants_change_envelope() -> None:
    """material_inside / material_outside / bend_outside give different outside envelopes."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    envelopes: dict[str, float] = {}
    for pos in ("material_inside", "material_outside", "bend_outside"):
        doc = _doc(base + [
            _feat("f3", "sm_edge_flange", {
                "angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
                "position": pos,
            }, refs={"edge": sel}),
        ])
        _assert_valid(doc)
        res = compile_document(doc)
        s = _status(res, "f3")
        assert s["ok"], f"position {pos} failed: {s['error']}"
        bb = res.bodies[0].shape.bounding_box()
        # The flange grows the sheet beyond y=0 (the picked edge plane) by different amounts.
        envelopes[pos] = bb.max.Y - bb.min.Y

    # material_inside keeps the outside flush (least Y growth); bend_outside bulges out the most.
    assert envelopes["material_inside"] < envelopes["material_outside"] < envelopes["bend_outside"], \
        f"position envelopes should order inside<outside<bend_outside; got {envelopes}"
    print(f"[sm-position] Y-extent inside={envelopes['material_inside']:.1f} "
          f"outside={envelopes['material_outside']:.1f} bend_outside={envelopes['bend_outside']:.1f}")


# --------------------------------------------------------------------------- 6: k-factor inherit


def test_k_factor_inherited_from_base() -> None:
    """An edge flange inherits the base flange's k-factor unless overridden."""
    base = _base_flange_doc(60.0, 40.0, 2.0, k_factor=0.33)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], s["error"]
    assert abs(s["resolved"]["k_factor"] - 0.33) < 1e-9, \
        f"edge flange should inherit k=0.33; got {s['resolved']['k_factor']}"
    ba_expected = (math.pi / 2.0) * (2.0 + 0.33 * 2.0)
    assert abs(s["resolved"]["bend_allowance"] - ba_expected) < 1e-3
    print(f"[sm-kfactor] inherited k=0.33 -> BA={s['resolved']['bend_allowance']:.4f}")


# --------------------------------------------------------------------------- 7: error cases


def test_edge_flange_before_base_flange_errors() -> None:
    """An edge flange with no sheet body errors cleanly (no crash)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    # A box (NOT a sheet-metal body) then an edge flange -> no sheet-metal context.
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "edge flange on a non-sheet part should fail"
    assert "base flange" in s["error"].lower() or "sheet metal" in s["error"].lower(), \
        f"error should mention the missing base flange; got: {s['error']}"
    print(f"[sm-err-nobase] -> '{s['error'][:60]}'")


def test_base_flange_open_profile_errors() -> None:
    """An open (non-closing) sketch yields no region -> clean base-flange error."""
    open_sketch = _feat("f1", "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [60.0, 0.0]},
            {"id": "e2", "type": "line", "construction": False, "p1": [60.0, 0.0], "p2": [60.0, 40.0]},
            # third side missing -> open chain, no closed region.
        ],
        "constraints": [],
    }, refs={"plane": "plane.top"})
    doc = _doc([
        open_sketch,
        _feat("f2", "sm_base_flange", {"thickness": _dim(2.0)},
              refs={"profile": [_region_sel("f1", 30.0, 20.0, 2400.0)]}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "open profile should fail the base flange"
    print(f"[sm-err-open] -> '{s['error'][:60]}'")


def test_edge_flange_radius_zero_errors() -> None:
    """Inner bend radius 0 is rejected cleanly."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(0.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "radius 0 should fail"
    assert "radius" in s["error"].lower(), f"error should mention radius; got {s['error']}"
    print(f"[sm-err-r0] -> '{s['error'][:60]}'")


def test_edge_flange_curved_edge_rejected() -> None:
    """A non-straight (circular) edge is rejected as a bend line.

    Two layers of coverage:
    1. End to end: a hole's circular edge selected as the bend line fails the feature (here the
       symmetric circular edges dangle in the selector, which is itself a clean rejection).
    2. The straightness GUARD itself: ``_edge_dir_and_endpoints`` raises on a real circular Edge,
       proving the curved-edge math rejects curves regardless of how the edge was picked.
    """
    from build123d import Edge, Plane, Vector
    from engine.doc.features.sheetmetal import _edge_dir_and_endpoints

    # Layer 2 — direct guard on a true circular arc edge (90° of a radius-8 circle).
    arc = Edge.make_circle(8.0, Plane(origin=Vector(0, 0, 0)), start_angle=0.0, end_angle=90.0)
    raised = False
    try:
        _edge_dir_and_endpoints(arc)
    except Exception as exc:  # noqa: BLE001 — FeatureError
        raised = True
        assert "straight" in str(exc).lower() or "line" in str(exc).lower(), \
            f"guard should cite the straight-line requirement; got: {exc}"
    assert raised, "the straightness guard must reject a circular edge"

    # Layer 1 — end to end: select a hole's circular edge and confirm the feature fails cleanly.
    sketch = _feat("f1", "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [60.0, 0.0]},
            {"id": "e2", "type": "line", "construction": False, "p1": [60.0, 0.0], "p2": [60.0, 40.0]},
            {"id": "e3", "type": "line", "construction": False, "p1": [60.0, 40.0], "p2": [0.0, 40.0]},
            {"id": "e4", "type": "line", "construction": False, "p1": [0.0, 40.0], "p2": [0.0, 0.0]},
            {"id": "e5", "type": "circle", "construction": False, "center": [30.0, 20.0], "radius": 8.0},
        ],
        "constraints": [],
    }, refs={"plane": "plane.top"})
    base = [
        sketch,
        _feat("f2", "sm_base_flange", {"thickness": _dim(2.0)},
              refs={"profile": [_region_sel("f1", 30.0, 20.0, 2400.0)]}),
    ]
    res0 = compile_document(_doc(base))
    assert _status(res0, "f2")["ok"], _status(res0, "f2")["error"]
    body = res0.bodies[0]
    infos = describe_edges(body.shape.wrapped)
    circ = next((i for i in infos if i.surf == "circle"), None)
    assert circ is not None, "washer should have a circular edge"
    sel = make_selector(circ)
    sel["owner_feature"] = "f2"
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)},
              refs={"edge": sel}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "a curved bend edge should be rejected"
    print(f"[sm-err-curved] guard raised; end-to-end -> '{s['error'][:60]}'")
