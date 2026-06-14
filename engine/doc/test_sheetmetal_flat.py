"""Sheet-metal FLAT PATTERN (unfold) tests — develop a folded part into its flat blank.

Run:  & $py -m pytest engine/doc/test_sheetmetal_flat.py -q   (from repo root)

The developed length / bend-allowance correctness is what a SolidWorks user verifies first, so the
tests compute the expected developed length FROM FIRST PRINCIPLES and assert the produced flat
matches it (the bend region develops to BA, NOT the outer 90° corner length).

Covered:
1.  90° edge flange (R=2, K=0.45, wall=20) on a 60×40×2 base -> flat is PLANAR (single thickness,
    all on one plane), developed length along the flange direction = base_to_bend + BA + wall, with
    BA = 4.5553; flat area = base + wall + bend-development; flat volume = area·thickness; one bend
    line. The developed length uses BA, not the outer corner.
2.  45° flange develops with its (smaller) BA.
3.  two edge flanges -> two bend lines, flat develops both.
4.  a flat (no-bend) sheet errors cleanly.
5.  flat pattern stores bend lines + outline + flat_area/volume for the DXF wave.

Harness mirrors test_sheetmetal.py.
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


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-sheetmetal-flat",
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


def _is_planar_single_thickness(shape, thickness: float) -> tuple[bool, float, "Vector"]:
    """A flat sheet has exactly two large broad faces with opposite normals, ``thickness`` apart.

    Returns (ok, measured_thickness, broad_normal)."""
    planes = [f for f in shape.faces() if f.geom_type == GeomType.PLANE]
    if not planes:
        return False, 0.0, None
    # The broad faces are the largest; group by normal axis.
    biggest = max(planes, key=lambda f: f.area)
    n = biggest.normal_at()
    n = n * (1.0 / max(n.length, 1e-12))
    broad = [f for f in planes
             if abs(abs(f.normal_at().dot(n)) - 1.0) < 1e-4 and f.area > 0.25 * biggest.area]
    centers = [f.center().dot(n) for f in broad]
    meas = max(centers) - min(centers)
    return abs(meas - thickness) < 1e-4, meas, n


# --------------------------------------------------------------------------- 1: 90° single bend


def test_flat_pattern_90_developed_length_uses_BA() -> None:
    """60×40×2 + 90° flange (R=2, wall=20) -> planar flat; developed length = base_to_bend+BA+wall.

    Expected from first principles (material_inside):
      setback x0 = R + T = 4  -> base_to_bend = 40 - 4 = 36
      BA = (π/2)(R + K·T) = (π/2)(2 + 0.45·2) = 4.5553   (NOT the outer corner (R+T)·π/2 = 6.283)
      wall = 20
      developed length along the flange (Y) axis = 36 + 4.5553 + 20 = 60.5553 mm.
    """
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange", {
            "angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
            "position": "material_inside", "flip": False,
        }, refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)

    s3 = _status(res, "f3")
    assert s3["ok"], f"edge flange failed: {s3['error']}"
    s4 = _status(res, "f4")
    assert s4["ok"], f"flat pattern failed: {s4['error']}"

    flat = res.bodies[0].shape

    # --- flat is planar: a single sheet of thickness 2, all broad faces coplanar pairs ---
    ok, meas, n = _is_planar_single_thickness(flat, 2.0)
    assert ok, f"flat must be a single 2 mm sheet; measured thickness {meas}"
    bb = flat.bounding_box()
    # The flat lies in the base plane (Z in [0,2]) — no folded material below/above.
    assert abs((bb.max.Z - bb.min.Z) - 2.0) < 1e-4, \
        f"flat must be 2 mm thick in Z (the base plane); got {bb.max.Z - bb.min.Z}"

    # --- developed length along the flange direction (Y) computed from first principles ---
    R, T, K, wall = 2.0, 2.0, 0.45, 20.0
    BA = (math.pi / 2.0) * (R + K * T)
    assert abs(BA - 4.5553) < 1e-3, f"BA {BA}"
    setback = R + T  # material_inside
    base_to_bend = 40.0 - setback  # 36
    dev_len_expected = base_to_bend + BA + wall  # 60.5553

    # The flat extends along Y from the far base edge (y=40) to the flange tip.
    y_extent = bb.max.Y - bb.min.Y
    assert abs(y_extent - dev_len_expected) < 1e-3, \
        f"developed Y length {y_extent:.4f} vs expected {dev_len_expected:.4f} (BA={BA:.4f})"

    # Cross-check: the developed length must NOT be the outer-corner convention.
    outer_corner = base_to_bend + (R + T) * (math.pi / 2.0) + wall  # 62.28...
    assert abs(y_extent - outer_corner) > 1.0, \
        "developed length must use BA, not the outer 90° corner length"

    # --- flat area = base + wall + bend-development; flat volume = area·thickness ---
    # Developed area along Y over a 60 mm wide blank: 60 * dev_len_expected.
    area_expected = 60.0 * dev_len_expected
    assert abs(s4["resolved"]["flat_area"] - area_expected) < 1e-2, \
        f"flat_area {s4['resolved']['flat_area']:.3f} vs {area_expected:.3f}"
    vol_expected = area_expected * 2.0
    assert abs(s4["resolved"]["flat_volume"] - vol_expected) < 1e-1, \
        f"flat_volume {s4['resolved']['flat_volume']:.3f} vs {vol_expected:.3f}"
    assert abs(flat.volume - vol_expected) < 1e-1, \
        f"flat body volume {flat.volume:.3f} vs area·T {vol_expected:.3f}"

    # --- exactly one bend line for a single bend ---
    assert abs(s4["resolved"]["bend_count"] - 1.0) < 1e-9

    print(f"[flat-90] developed Y={y_extent:.4f} (=base36 + BA{BA:.4f} + wall20); "
          f"area={s4['resolved']['flat_area']:.1f}; vol={flat.volume:.1f}")


# --------------------------------------------------------------------------- 2: 45° bend develops with BA


def test_flat_pattern_45_uses_smaller_BA() -> None:
    """A 45° flange develops with BA = (π/4)(R+K·T); the bend region is shorter than the 90° one."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(45.0), "length": _dim(20.0), "radius": _dim(2.0),
               "position": "material_inside"},
              refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f3")["ok"], _status(res, "f3")["error"]
    s4 = _status(res, "f4")
    assert s4["ok"], s4["error"]

    flat = res.bodies[0].shape
    ok, meas, _ = _is_planar_single_thickness(flat, 2.0)
    assert ok, f"45° flat must still be a single 2 mm sheet; measured {meas}"

    R, T, K, wall = 2.0, 2.0, 0.45, 20.0
    BA45 = math.radians(45.0) * (R + K * T)
    setback = R + T
    dev_len_expected = (40.0 - setback) + BA45 + wall

    bb = flat.bounding_box()
    y_extent = bb.max.Y - bb.min.Y
    assert abs(y_extent - dev_len_expected) < 1e-3, \
        f"45° developed Y {y_extent:.4f} vs {dev_len_expected:.4f} (BA45={BA45:.4f})"
    # 45° BA is exactly half the 90° BA.
    BA90 = math.radians(90.0) * (R + K * T)
    assert abs(BA45 - BA90 / 2.0) < 1e-9
    print(f"[flat-45] developed Y={y_extent:.4f}; BA45={BA45:.4f} (= half of BA90 {BA90:.4f})")


# --------------------------------------------------------------------------- 3: two bends


def test_flat_pattern_two_flanges_two_bend_lines() -> None:
    """Two 90° flanges on OPPOSITE long edges develop into one flat with two bend lines.

    The real edge-flange on the y=0 edge is built normally (f3). For the SECOND, opposite-edge
    flange we seed its bend record directly: the geometry selectors in this harness resolve the
    y=40 long edge ambiguously to the y=0 edge after the first bend regenerates topology (a known
    selector-persistence limitation, orthogonal to the flat-pattern math). Seeding a clean record
    on the y=40 edge exercises the multi-bend development path deterministically — exactly the
    records the edge-flange compiler stores, with a distinct bend line and the +Y unfold direction.
    """
    from engine.doc.compiler import _resolve_params
    from engine.doc.features import CompileContext, get_compiler
    from engine.doc.features.datum import reserved_datums
    from engine.doc.features.sheetmetal import bend_allowance

    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel_a = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(15.0), "radius": _dim(2.0),
               "position": "material_inside"},
              refs={"edge": sel_a}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    R, T, K, wall = 2.0, 2.0, 0.45, 15.0
    ba = bend_allowance(90.0, R, T, K)

    # Manual compile loop so we can inject the opposite-edge bend record before the flat pattern.
    env: dict = {}
    bodies: list = []
    datums = reserved_datums()
    for feat in doc["features"]:
        if feat["id"] == "f4":
            rec = {
                "feature": "f3b", "angle": 90.0, "radius": R, "thickness": T, "k_factor": K,
                "neutral_radius": R + K * T, "bend_allowance": ba, "bend_deduction": 0.0,
                "wall_length": wall, "position": "material_inside", "flip": True,
                "bend_line": [[0.0, 40.0, 0.0], [60.0, 40.0, 0.0]],
                "e_dir": [1.0, 0.0, 0.0], "length_dir": [0.0, 1.0, 0.0],
                "fold_dir": [0.0, 0.0, 1.0],
            }
            datums["_sheet_metal_bends"].obj.append(rec)
        rp, _, _ = _resolve_params(feat.get("params", {}), env)
        fv = dict(feat)
        fv["params"] = rp
        out = get_compiler(feat["type"])(
            fv, CompileContext(equations_env=env, bodies=bodies, datums=datums))
        bodies = out.bodies

    flat = bodies[-1]
    payload = datums["_sheet_metal_flat"].obj["f4"]
    assert len(payload["bend_lines"]) == 2, "two bends -> two bend lines"

    ok, meas, _ = _is_planar_single_thickness(flat, 2.0)
    assert ok, f"two-bend flat must be a single 2 mm sheet; measured {meas}"

    # Two opposite 90° flanges grow the Y extent on BOTH sides:
    # 40 (base) -> 40 + 2·(BA + wall − setback).
    setback = R + T
    per_side = ba + wall - setback
    y_extent_expected = 40.0 + 2.0 * per_side
    bb = flat.bounding_box()
    y_extent = bb.max.Y - bb.min.Y
    assert abs(y_extent - y_extent_expected) < 1e-2, \
        f"two-bend developed Y {y_extent:.4f} vs {y_extent_expected:.4f}"
    # Developed area = full blank: 60 wide × developed length.
    assert abs(payload["flat_area"] - 60.0 * y_extent_expected) < 1e-1
    print(f"[flat-2bend] bend_lines=2; developed Y={y_extent:.4f} vs {y_extent_expected:.4f}; "
          f"area={payload['flat_area']:.1f}")


# --------------------------------------------------------------------------- 4: no-bend errors cleanly


def test_flat_pattern_no_bends_errors_cleanly() -> None:
    """A flat (no-bend) base sheet has no bends to unfold -> contained FeatureError (no crash)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    doc = _doc(base + [
        _feat("f3", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert not s["ok"], "a no-bend sheet's flat pattern is itself -> should error, not crash"
    assert "no bends" in s["error"].lower() or "flat" in s["error"].lower(), \
        f"error should mention there are no bends; got: {s['error']}"
    # The body is unchanged (the base sheet survives the contained error).
    assert abs(res.bodies[0].shape.volume - 60.0 * 40.0 * 2.0) < 1e-3
    print(f"[flat-nobend] -> '{s['error'][:70]}'")


def test_flat_pattern_no_sheet_metal_errors_cleanly() -> None:
    """A flat pattern on a non-sheet-metal body errors cleanly."""
    doc = _doc([
        _feat("f1", "box", {"length": _dim(20), "width": _dim(20), "height": _dim(20)}),
        _feat("f2", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f2")
    assert not s["ok"], "flat pattern on a plain box should fail"
    assert "sheet metal" in s["error"].lower(), f"got: {s['error']}"
    print(f"[flat-nosm] -> '{s['error'][:70]}'")


# --------------------------------------------------------------------------- 5: stored artifacts


def test_flat_pattern_stores_bend_lines_and_outline() -> None:
    """The flat pattern stores bend lines + an outline polyline for the later flat-DXF export."""
    from engine.doc.features import CompileContext, get_compiler
    from engine.doc.features.datum import reserved_datums
    from engine.doc.compiler import _resolve_params

    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
               "position": "material_inside"},
              refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    # Run the compile loop manually to capture the ctx datums the flat pattern stores.
    env: dict = {}
    bodies: list = []
    datums = reserved_datums()
    for feat in doc["features"]:
        rp, _, _ = _resolve_params(feat.get("params", {}), env)
        fv = dict(feat)
        fv["params"] = rp
        comp = get_compiler(feat["type"])
        ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
        out = comp(fv, ctx)
        bodies = out.bodies

    payload = datums["_sheet_metal_flat"].obj["f4"]
    assert len(payload["bend_lines"]) == 1, "one bend -> one bend line record"
    bl = payload["bend_lines"][0]
    assert abs(bl["bend_allowance"] - (math.pi / 2.0) * 2.9) < 1e-6
    # bend lines are 3-D segments (two endpoints each).
    assert len(bl["line_start"]) == 2 and len(bl["line_start"][0]) == 3
    assert len(bl["line_end"]) == 2
    # outline is a non-trivial polyline (>= 4 corners for the flat blank boundary).
    assert len(payload["outline"]) >= 4, f"outline should bound the flat sheet; got {payload['outline']}"
    assert payload["flat_area"] > 0 and payload["flat_volume"] > 0
    # No multi-bend (single-level flange).
    assert payload["multibend"] == []
    print(f"[flat-artifacts] bend_lines={len(payload['bend_lines'])}, "
          f"outline_pts={len(payload['outline'])}, area={payload['flat_area']:.1f}")
