r"""SW-parity extensions to the linear / circular / mirror pattern compilers.

Run:  & $py -m pytest engine\doc\test_pattern_ext.py -q   (from repo root)

Pytest-style (also importable as plain functions). The harness mirrors
engine/doc/test_extrude_ends.py (self-contained ``_dim`` / ``_feat`` / ``_doc`` / ``_status``)
and the box/hole/region helpers from engine/doc/test_pattern_mirror_draft.py.

Covers the options added in this wave (docs/SW_PARITY_SPEC.md §2 / docs/SW_PARITY_GAPS.md —
Linear/Circular Pattern, Mirror):

1.  Linear pattern 3×2 with one instance SKIPPED → 5 holes, not 6 (volume + instance count).
2.  Linear skip of a bare 1-D index in a single-direction row → one fewer hole.
3.  Linear VARY spacing pushes a later instance farther out (a hole appears past where the
    un-varied pattern would have stopped — bounding-box / removed-volume check).
4.  Linear VARY scale grows a later instance (more material removed than the un-varied grid).
5.  Circular BIDIRECTIONAL doubles the ring (instances on BOTH sides of the seed).
6.  Circular SKIP omits a ring instance.
7.  Mirror BODIES (refs.bodies) reflects a picked off-plane body, with merge=True (one fused
    body, doubled volume) and merge=False (a separate reflected body appended).
8.  Mirror whole-body MERGE toggle: merge True → 1 union body; merge False → 2 bodies.
9.  Un-set options leave the original pattern/mirror output unchanged (regression guard).
"""

from __future__ import annotations

import math

from build123d import CenterOf, GeomType

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


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


def _body_sel(owner: str) -> dict:
    return {"kind": "body", "owner_feature": owner}


# --------------------------------------------------------------------------- geometry helpers

_D = 8.0  # hole diameter


def _box(fid: str, L: float, W: float, H: float, centered: bool = True,
         refs: dict | None = None) -> dict:
    p = {"length": _dim(L), "width": _dim(W), "height": _dim(H), "centered": centered}
    return _feat(fid, "box", p, refs=refs or {})


def _top_face_sel(h: float) -> dict:
    return {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, h / 2.0],
            "axis": [0.0, 0.0, 1.0], "owner_feature": "f1", "hint_index": 0}


def _hole(fid: str, h: float, points: list, owner_face: str = "f1") -> dict:
    sel = {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, h / 2.0],
           "axis": [0.0, 0.0, 1.0], "owner_feature": owner_face, "hint_index": 0}
    return _feat(fid, "hole", {"style": "simple", "end": "through_all",
                               "standard": "custom", "diameter": _dim(_D)},
                 refs={"face": sel, "points": points})


def _cyl(depth: float, d: float = _D) -> float:
    return math.pi * (d / 2.0) ** 2 * depth


# --------------------------------------------------------------------------- 1: linear skip (grid)

def test_linear_pattern_skip_one_grid_cell() -> None:
    """A through hole patterned 3×2 with the (2,1) cell SKIPPED drills 5 holes, not 6."""
    H = 20.0
    base = _box("f1", 60.0, 40.0, H)
    hole = _hole("f2", H, [[-15.0, -7.0]])
    pat = _feat("f3", "linear_pattern", {
        "count1": 3, "spacing1": _dim(15.0),
        "count2": 2, "spacing2": _dim(14.0),
        "skip": [[2, 1]],  # omit the far corner instance
    }, refs={"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]})
    doc = _doc([base, hole, pat])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f2")["ok"], _status(res, "f2").get("error")
    s = _status(res, "f3")
    assert s["ok"], f"linear skip failed: {s.get('error')}"

    # 6 grid cells, minus the seed (already placed) → 5 *additional* tool applications; with one
    # cell skipped, 4 are placed. Total holes in the body = 5 (seed + 4).
    assert abs(s["resolved"].get("_instances_placed", -1) - 4.0) < 1e-9, \
        f"expected 4 placed instances (5 grid - seed - 1 skip); got {s['resolved'].get('_instances_placed')}"
    assert abs(s["resolved"].get("_instances_skipped", -1) - 1.0) < 1e-9, \
        f"expected 1 skipped instance; got {s['resolved'].get('_instances_skipped')}"

    vol = res.bodies[-1].shape.volume
    expected = 60.0 * 40.0 * H - 5.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"5-hole grid vol {vol:.1f} != {expected:.1f}"

    # And it must differ from the un-skipped 6-hole grid by exactly one cylinder.
    full = compile_document(_doc([base, hole, _feat("f3", "linear_pattern", {
        "count1": 3, "spacing1": _dim(15.0), "count2": 2, "spacing2": _dim(14.0),
    }, refs={"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]})]))
    vol_full = full.bodies[-1].shape.volume
    assert abs((vol - vol_full) - _cyl(H)) < 5.0, \
        f"skip should leave exactly one extra cylinder of material: {vol - vol_full:.1f} vs {_cyl(H):.1f}"


def test_linear_pattern_skip_string_form() -> None:
    """The PM form emits skip as a text string ("i:j" tokens); it must parse to the same cells as
    the list form. Skipping "2:1" on a 3×2 grid → 4 placed / 1 skipped, identical to [[2,1]]."""
    H = 20.0
    base = _box("f1", 60.0, 40.0, H)
    hole = _hole("f2", H, [[-15.0, -7.0]])
    pat = _feat("f3", "linear_pattern", {
        "count1": 3, "spacing1": _dim(15.0),
        "count2": 2, "spacing2": _dim(14.0),
        "skip": "2:1",  # string form (text field) — same cell as [[2, 1]]
    }, refs={"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]})
    doc = _doc([base, hole, pat])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f3")
    assert s["ok"], f"string skip failed: {s.get('error')}"
    assert abs(s["resolved"].get("_instances_placed", -1) - 4.0) < 1e-9, \
        f"string skip '2:1' should place 4; got {s['resolved'].get('_instances_placed')}"
    assert abs(s["resolved"].get("_instances_skipped", -1) - 1.0) < 1e-9, \
        f"string skip '2:1' should skip 1; got {s['resolved'].get('_instances_skipped')}"
    print("[pattern] linear skip string '2:1' == list [[2,1]] (4 placed, 1 skipped)")


# --------------------------------------------------------------------------- 2: linear skip (1-D index)

def test_linear_pattern_skip_bare_index_row() -> None:
    """A 1-D row of 4 holes with bare index 2 skipped → 3 holes."""
    H = 20.0
    base = _box("f1", 100.0, 40.0, H)
    hole = _hole("f2", H, [[-30.0, 0.0]])
    pat = _feat("f3", "linear_pattern", {
        "count1": 4, "spacing1": _dim(18.0), "skip": [2],  # bare linear index → cell (2,0)
    }, refs={"dir1": "axis.x", "features": ["f2"]})
    res = compile_document(_doc([base, hole, pat]))
    s = _status(res, "f3")
    assert s["ok"], f"1-D skip failed: {s.get('error')}"
    assert abs(s["resolved"].get("_instances_skipped", -1) - 1.0) < 1e-9
    vol = res.bodies[-1].shape.volume
    expected = 100.0 * 40.0 * H - 3.0 * _cyl(H)  # seed + 3 - 1 skip = 3 holes
    assert abs(vol - expected) < 5.0, f"3-hole row vol {vol:.1f} != {expected:.1f}"


# --------------------------------------------------------------------------- 3: vary spacing (position)

def test_linear_pattern_vary_spacing_pushes_later_instance() -> None:
    """vary.spacing1 accumulates the gap each step, so the LAST hole sits farther out than the
    un-varied pattern — proven by a hole landing past x where the un-varied row would have ended."""
    # H=30 (not 20): on a 200x40 plate a 20mm-thick centred box leaves the top and bottom faces
    # too close in the selector's bbox-diag-normalised centroid score (margin ~0.039 < 0.05), so
    # the hole's top-face pick goes dangling. A thicker plate separates the faces enough to resolve
    # cleanly; the hole layout this test exercises is purely in xy, so thickness is incidental.
    H = 30.0
    # Long plate; row starts near the left, marches +X.
    base = _box("f1", 200.0, 40.0, H)
    hole = _hole("f2", H, [[-80.0, 0.0]])
    # 4 holes, base spacing 15, vary +10/step:
    #   un-varied last (i=3): x = -80 + 3*15 = -35
    #   varied   last (i=3): x = -80 + (3*15 + (3*2/2)*10) = -80 + 75 = -5
    vary = _feat("f3", "linear_pattern", {
        "count1": 4, "spacing1": _dim(15.0),
        "vary": {"spacing1": _dim(10.0)},
    }, refs={"dir1": "axis.x", "features": ["f2"]})
    plain = _feat("f3", "linear_pattern", {
        "count1": 4, "spacing1": _dim(15.0),
    }, refs={"dir1": "axis.x", "features": ["f2"]})

    res_v = compile_document(_doc([base, hole, vary]))
    res_p = compile_document(_doc([base, hole, plain]))
    sv = _status(res_v, "f3")
    assert sv["ok"], f"vary spacing failed: {sv.get('error')}"
    assert sv["resolved"].get("_vary") == 1.0, "vary should be flagged in resolved"

    # The varied body's rightmost drilled hole is farther +X. Detect the rightmost hole edge by
    # the body's solid material: scan for the max-X open cylinder. Simpler and robust: the count
    # and total removed volume match (both rows = 4 holes), but the SPREAD differs — check the
    # x-extent of removed material via the highest-X point that is air at mid-height.
    # Easiest deterministic check: same hole COUNT (volume identical), different geometry.
    vol_v = res_v.bodies[-1].shape.volume
    vol_p = res_p.bodies[-1].shape.volume
    assert abs(vol_v - vol_p) < 5.0, \
        f"vary-spacing keeps the same 4 holes (same volume): {vol_v:.1f} vs {vol_p:.1f}"

    # Distinguishing assertion: build a probe that overlaps the VARIED last hole position (x=-5)
    # but NOT the un-varied one (x=-35). A small cut sketch at x=-5 must hit air in the varied
    # body (already drilled) and solid in the plain body. We test positions directly instead:
    # find the X of the farthest-right hole by inspecting which has a vertical through-cylinder.
    def _rightmost_hole_x(res) -> float:
        body = res.bodies[-1].shape
        xs = []
        for f in body.faces():
            try:
                if f.geom_type == GeomType.CYLINDER:
                    # Use the centre of MASS (= the cylinder axis), not the default GEOMETRY centre:
                    # a through-hole's cylindrical surface is only a partial patch, so its parametric
                    # GEOMETRY centre sits offset (~+4mm here) from the true axis, while the MASS /
                    # bounding-box centre lands exactly on the drilled axis.
                    xs.append(f.center(CenterOf.MASS).X)
            except Exception:  # noqa: BLE001
                continue
        return max(xs) if xs else float("-inf")

    rx_v = _rightmost_hole_x(res_v)
    rx_p = _rightmost_hole_x(res_p)
    assert rx_v > rx_p + 10.0, \
        f"varied row's last hole must sit farther +X: varied {rx_v:.1f} vs plain {rx_p:.1f}"
    # And it should be near the analytic -5 (varied) vs -35 (plain).
    assert abs(rx_v - (-5.0)) < 2.0, f"varied last hole x {rx_v:.1f} should be ~-5"
    assert abs(rx_p - (-35.0)) < 2.0, f"plain last hole x {rx_p:.1f} should be ~-35"


# --------------------------------------------------------------------------- 4: vary scale (size)

def test_linear_pattern_vary_scale_grows_later_instance() -> None:
    """vary.scale enlarges each successive instance about its own centre, so a varied row removes
    MORE material than the plain row (later holes are bigger)."""
    # H=30: a 20mm-thick 160x40 centred box leaves the hole's top-face pick ambiguous
    # (selector margin ~0.048 < 0.05); a thicker plate resolves it. Thickness is incidental to the
    # xy-only behaviour under test and stays consistent through every volume calc below.
    H = 30.0
    base = _box("f1", 160.0, 40.0, H)
    hole = _hole("f2", H, [[-60.0, 0.0]])
    # 4 holes; +0.4 scale/step → i=3 instance is 1+3*0.4 = 2.2× the seed hole diameter.
    vary = _feat("f3", "linear_pattern", {
        "count1": 4, "spacing1": _dim(30.0),
        "vary": {"scale": 0.4},
    }, refs={"dir1": "axis.x", "features": ["f2"]})
    plain = _feat("f3", "linear_pattern", {
        "count1": 4, "spacing1": _dim(30.0),
    }, refs={"dir1": "axis.x", "features": ["f2"]})

    res_v = compile_document(_doc([base, hole, vary]))
    res_p = compile_document(_doc([base, hole, plain]))
    sv = _status(res_v, "f3")
    assert sv["ok"], f"vary scale failed: {sv.get('error')}"

    vol_v = res_v.bodies[-1].shape.volume
    vol_p = res_p.bodies[-1].shape.volume
    # Scaled-up later holes remove strictly more material → smaller remaining volume.
    assert vol_v < vol_p - 50.0, \
        f"vary-scale must remove more material than the plain grid: varied {vol_v:.1f} vs plain {vol_p:.1f}"

    # Analytic: plain removes 4 equal cylinders; varied removes cylinders scaled by
    # (1, 1.4, 1.8, 2.2) in DIAMETER → area scales by the square.
    removed_p = 4.0 * _cyl(H)
    removed_v = sum(_cyl(H, _D * (1.0 + i * 0.4)) for i in range(4))
    full = 160.0 * 40.0 * H
    assert abs(vol_p - (full - removed_p)) < 5.0, f"plain removed vol mismatch: {vol_p:.1f}"
    assert abs(vol_v - (full - removed_v)) < 30.0, \
        f"varied removed vol {full - vol_v:.1f} vs analytic {removed_v:.1f}"


# --------------------------------------------------------------------------- 5: circular bidirectional

def test_circular_pattern_bidirectional_doubles_ring() -> None:
    """Bidirectional circular pattern lays instances on BOTH sides of the seed.

    equal_spacing=False so `angle` is the increment BETWEEN instances. count=3 → seed + 2 CCW
    holes (+30°, +60°); bidirectional count_b=3 → 2 more CW holes (-30°, -60°). Ring of 5 holes
    vs a one-sided ring of 3."""
    # H=30: the 120x120 centred plate's top/bottom faces are too symmetric for the hole's face
    # selector at 20mm thick (margin ~0.047 < 0.05); a thicker plate resolves the pick. Thickness
    # is incidental and used consistently in every volume comparison.
    H = 30.0
    base = _box("f1", 120.0, 120.0, H)
    hole = _hole("f2", H, [[40.0, 0.0]])  # seed at radius 40, on +X
    bidi = _feat("f3", "circular_pattern", {
        "count": 3, "angle": _dim(30.0), "equal_spacing": False,
        "bidirectional": True,
    }, refs={"axis": "axis.z", "features": ["f2"]})
    oneside = _feat("f3", "circular_pattern", {
        "count": 3, "angle": _dim(30.0), "equal_spacing": False,
    }, refs={"axis": "axis.z", "features": ["f2"]})

    res_b = compile_document(_doc([base, hole, bidi]))
    res_o = compile_document(_doc([base, hole, oneside]))
    sb = _status(res_b, "f3")
    assert sb["ok"], f"bidirectional circular failed: {sb.get('error')}"
    # 2 CCW + 2 CW additional instances placed (seed already in body).
    assert abs(sb["resolved"].get("_instances_placed", -1) - 4.0) < 1e-9, \
        f"expected 4 placed (2 CCW + 2 CW); got {sb['resolved'].get('_instances_placed')}"
    assert abs(sb["resolved"].get("count_b", -1) - 3.0) < 1e-9, "count_b should echo 3"

    vol_b = res_b.bodies[-1].shape.volume
    vol_o = res_o.bodies[-1].shape.volume
    full = 120.0 * 120.0 * H
    assert abs(vol_b - (full - 5.0 * _cyl(H))) < 5.0, \
        f"bidirectional ring should have 5 holes: vol {vol_b:.1f} vs {full - 5.0 * _cyl(H):.1f}"
    assert abs(vol_o - (full - 3.0 * _cyl(H))) < 5.0, \
        f"one-sided ring should have 3 holes: vol {vol_o:.1f}"
    # Bidirectional removes 2 more cylinders than one-sided.
    assert abs((vol_o - vol_b) - 2.0 * _cyl(H)) < 5.0, \
        f"bidirectional should drill 2 extra holes: delta {vol_o - vol_b:.1f} vs {2.0 * _cyl(H):.1f}"


# --------------------------------------------------------------------------- 6: circular skip

def test_circular_pattern_skip_ring_instance() -> None:
    """A 4-hole equal-spacing ring with instance index 2 (the 180° hole) skipped → 3 holes."""
    # H=30: same 120x120 face-selector symmetry as the bidirectional test — a thicker plate keeps
    # the hole's top-face pick unambiguous. Thickness is incidental to the ring layout under test.
    H = 30.0
    base = _box("f1", 120.0, 120.0, H)
    hole = _hole("f2", H, [[40.0, 0.0]])
    pat = _feat("f3", "circular_pattern", {
        "count": 4, "angle": _dim(360.0), "skip": [2],
    }, refs={"axis": "axis.z", "features": ["f2"]})
    res = compile_document(_doc([base, hole, pat]))
    s = _status(res, "f3")
    assert s["ok"], f"circular skip failed: {s.get('error')}"
    assert abs(s["resolved"].get("_instances_skipped", -1) - 1.0) < 1e-9
    assert abs(s["resolved"].get("_instances_placed", -1) - 2.0) < 1e-9, \
        f"4-hole ring - seed - 1 skip = 2 placed; got {s['resolved'].get('_instances_placed')}"
    vol = res.bodies[-1].shape.volume
    expected = 120.0 * 120.0 * H - 3.0 * _cyl(H)
    assert abs(vol - expected) < 5.0, f"3-hole ring (one skipped) vol {vol:.1f} != {expected:.1f}"


# --------------------------------------------------------------------------- 7: mirror bodies

def _two_body_plate() -> list[dict]:
    """f1 plate centred at origin; f3 a SEPARATE plate placed off the YZ plane at x∈[20,40].

    f2 = plane.right (YZ, normal +X) offset +20 → its origin sits at (20,0,0); the f3 box 'at'
    that plane (un-centred) spans x∈[20,40], well clear of the mirror plane x=0.

    ``merge: False`` on f3 is REQUIRED: the box feature otherwise unions every solid into the
    running body, so without it f1 and f3 would be one fused 8000 body and there would be nothing
    separate for ``refs.bodies`` to pick. With it, f3 is a genuine second disjoint body.
    """
    return [
        _box("f1", 20.0, 20.0, 10.0),                                   # x∈[-10,10]
        _feat("f2", "plane", {"method": "offset", "offset": _dim(20.0)},
              refs={"base": "plane.right"}),
        # x∈[20,40], a NEW separate body (merge:False keeps it off the f1 solid).
        _feat("f3", "box", {"length": _dim(20.0), "width": _dim(20.0), "height": _dim(10.0),
                            "centered": False, "merge": False}, refs={"at": "f2"}),
    ]


def test_mirror_bodies_merge_true_unions_reflection() -> None:
    """Mirror the picked off-plane body f3 across the Right (YZ) plane with merge=True →
    the reflection (x∈[-40,-20]) FUSES with the running body. Body count unchanged; the running
    body's volume grows by the reflected plate (it is disjoint, so just adds 4000 mm³)."""
    feats = _two_body_plate()
    feats.append(_feat("f4", "mirror", {"merge": True},
                       refs={"plane": "plane.right", "bodies": [_body_sel("f3")]}))
    doc = _doc(feats)
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"mirror bodies merge failed: {s.get('error')}"
    assert abs(s["resolved"].get("_bodies_mirrored", -1) - 1.0) < 1e-9
    assert s["resolved"].get("merge") == 1.0

    # f3 (4000) was a separate body; mirror unions its reflection (4000) into the running body
    # (bodies[-1] = f3). So f3's body becomes 8000, and f1's body (4000) is untouched.
    vols = sorted(b.shape.volume for b in res.bodies)
    assert len(res.bodies) == 2, f"merge=True should not grow the body list; got {len(res.bodies)}"
    assert abs(vols[0] - 4000.0) < 1.0, f"f1 plate should stay 4000; got {vols[0]:.1f}"
    assert abs(vols[1] - 8000.0) < 1.0, \
        f"mirrored f3 body should be 8000 (plate + reflection); got {vols[1]:.1f}"
    # The reflection occupies x∈[-40,-20]: the union body must reach that far -X.
    target = max(res.bodies, key=lambda b: b.shape.volume)
    bb = target.shape.bounding_box()
    assert bb.min.X < -19.0, f"merged body should span the reflection (x≈-40..-20); minX={bb.min.X:.1f}"


def test_mirror_bodies_merge_false_appends_separate_body() -> None:
    """Same pick, merge=False → the reflection is a NEW separate body (body count grows by 1)."""
    feats = _two_body_plate()
    feats.append(_feat("f4", "mirror", {"merge": False},
                       refs={"plane": "plane.right", "bodies": [_body_sel("f3")]}))
    doc = _doc(feats)
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f4")
    assert s["ok"], f"mirror bodies separate failed: {s.get('error')}"
    assert s["resolved"].get("merge") == 0.0
    assert len(res.bodies) == 3, \
        f"merge=False should append the reflection as a 3rd body; got {len(res.bodies)}"
    vols = sorted(round(b.shape.volume, 1) for b in res.bodies)
    assert vols == [4000.0, 4000.0, 4000.0], \
        f"three disjoint 4000 plates expected (f1, f3, reflection); got {vols}"
    # Exactly one body lives at x∈[-40,-20] (the lone reflection).
    refl = [b for b in res.bodies if b.shape.bounding_box().max.X < -10.0]
    assert len(refl) == 1, f"exactly one reflected body should sit at -X; got {len(refl)}"
    assert abs(refl[0].shape.volume - 4000.0) < 1.0


# --------------------------------------------------------------------------- 8: whole-body merge toggle

def test_mirror_whole_body_merge_toggle() -> None:
    """Whole-body mirror (no refs.features / refs.bodies): merge=True unions into one body,
    merge=False appends the reflection as a separate body. Geometry: an off-plane plate
    x∈[0,50] mirrored about the Right (YZ) plane → reflection x∈[-50,0]."""
    base = _box("f1", 50.0, 30.0, 10.0, centered=False)  # x∈[0,50]
    # merge default (True): one fused body, double volume.
    mir_merge = _feat("f2", "mirror", {}, refs={"plane": "plane.right"})
    res_m = compile_document(_doc([base, mir_merge]))
    s_m = _status(res_m, "f2")
    assert s_m["ok"], f"whole-body mirror merge failed: {s_m.get('error')}"
    assert len(res_m.bodies) == 1, f"merge default should leave 1 body; got {len(res_m.bodies)}"
    assert abs(res_m.bodies[-1].shape.volume - 2.0 * 50.0 * 30.0 * 10.0) < 5.0, \
        f"merged mirror vol {res_m.bodies[-1].shape.volume:.1f}"

    # merge=False: two separate bodies, each 15000.
    mir_sep = _feat("f2", "mirror", {"merge": False}, refs={"plane": "plane.right"})
    res_s = compile_document(_doc([base, mir_sep]))
    s_s = _status(res_s, "f2")
    assert s_s["ok"], f"whole-body mirror separate failed: {s_s.get('error')}"
    assert len(res_s.bodies) == 2, f"merge=False should yield 2 bodies; got {len(res_s.bodies)}"
    vols = sorted(b.shape.volume for b in res_s.bodies)
    assert abs(vols[0] - 15000.0) < 5.0 and abs(vols[1] - 15000.0) < 5.0, \
        f"two 15000 plates expected; got {[round(v, 1) for v in vols]}"


# --------------------------------------------------------------------------- 9: regression — defaults

def test_unset_options_match_original_output() -> None:
    """Patterns/mirror with NO new options must reproduce the original behaviour exactly:
    a plain 3×2 grid drills 6 holes; a plain feature-mirror drills 2."""
    H = 20.0
    base = _box("f1", 60.0, 40.0, H)
    hole = _hole("f2", H, [[-15.0, -7.0]])
    grid = _feat("f3", "linear_pattern", {
        "count1": 3, "spacing1": _dim(15.0), "count2": 2, "spacing2": _dim(14.0),
    }, refs={"dir1": "axis.x", "dir2": "axis.y", "features": ["f2"]})
    res = compile_document(_doc([base, hole, grid]))
    s = _status(res, "f3")
    assert s["ok"], f"plain grid failed: {s.get('error')}"
    assert abs(s["resolved"].get("_instances_skipped", -1) - 0.0) < 1e-9, "no skips expected"
    assert abs(s["resolved"].get("_instances_placed", -1) - 5.0) < 1e-9, \
        "6 grid cells minus the seed = 5 placed"
    vol = res.bodies[-1].shape.volume
    assert abs(vol - (60.0 * 40.0 * H - 6.0 * _cyl(H))) < 5.0, f"plain 6-hole grid vol {vol:.1f}"

    # Feature mirror unchanged.
    base2 = _box("f1", 60.0, 40.0, H)
    hole2 = _hole("f2", H, [[15.0, 0.0]])
    mir = _feat("f3", "mirror", {}, refs={"plane": "plane.right", "features": ["f2"]})
    res2 = compile_document(_doc([base2, hole2, mir]))
    s2 = _status(res2, "f3")
    assert s2["ok"], f"plain feature mirror failed: {s2.get('error')}"
    vol2 = res2.bodies[-1].shape.volume
    assert abs(vol2 - (60.0 * 40.0 * H - 2.0 * _cyl(H))) < 5.0, f"mirrored 2-hole vol {vol2:.1f}"


# --------------------------------------------------------------------------- main

def main() -> None:
    test_linear_pattern_skip_one_grid_cell()
    test_linear_pattern_skip_bare_index_row()
    test_linear_pattern_vary_spacing_pushes_later_instance()
    test_linear_pattern_vary_scale_grows_later_instance()
    test_circular_pattern_bidirectional_doubles_ring()
    test_circular_pattern_skip_ring_instance()
    test_mirror_bodies_merge_true_unions_reflection()
    test_mirror_bodies_merge_false_appends_separate_body()
    test_mirror_whole_body_merge_toggle()
    test_unset_options_match_original_output()
    print("\nPATTERN-EXT TESTS PASSED")


if __name__ == "__main__":
    main()
