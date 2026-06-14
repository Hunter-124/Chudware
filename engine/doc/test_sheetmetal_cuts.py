"""Sheet-metal CUT PROPAGATION + CORNER RELIEF tests (docs/SW_PARITY_SPEC §5 / GAPS P0).

Run:  & $py -m pytest engine/doc/test_sheetmetal_cuts.py -q   (from repo root)

Two SolidWorks-parity gaps are covered:

CUT PROPAGATION (``sm_flat_pattern`` must carry punched holes / slots into the flat blank):
1.  a flanged part with a BASE-region hole -> the hole appears in the flat pattern at the same flat
    (x, y) it was drilled, both in the flat SOLID (an inner wire on the broad face) and in the flat
    DXF geometry (a cut loop the exporter draws on the blank).
2.  a hole punched into the FLANGE WALL -> it DEVELOPS onto the flat strip: its flat position is
    ``BA + (distance down the wall)`` from the bend tangent, NOT its folded 3-D position. The flat
    solid loses material (the hole is really cut), and the developed loop lands on the strip.
3.  a slot (obround) propagates the same way (a multi-point loop, not just a circle).
4.  the no-cut flat pattern is byte-for-byte unchanged (regression guard: cut_count == 0).

CORNER RELIEF (adjacent edge flanges must not overlap at the shared corner):
5.  two ADJACENT (perpendicular) flanges sharing a corner -> a relief notch is generated; the body
    loses corner material (no overlap), a relief record is stored, and it develops into the flat.
6.  rectangular / obround / tear relief styles all generate a notch.
7.  two PARALLEL flanges (opposite edges) need NO relief (none generated) — the relief is only for
    adjacent-edge interference.
8.  ``corner_relief="none"`` disables the relief.

Harness mirrors test_sheetmetal_flat.py (manual compile loop + seeded records where the geometry
selectors cannot deterministically re-pick a regenerated edge — the same limitation the existing
two-flange flat test documents).
"""

from __future__ import annotations

import math

from build123d import GeomType, Plane, Solid, Vector

from engine.doc.compiler import compile_document, _resolve_params
from engine.doc.features import CompileContext, get_compiler
from engine.doc.features.base import Datum
from engine.doc.features.datum import reserved_datums
from engine.doc.features.sheetmetal import (
    bend_allowance,
    _apply_corner_reliefs,
    _append_relief_records,
    _adjacent_flanges,
    _shared_corner,
    _relief_record,
    _build_relief_tool,
    _relief_size,
)
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
        "id": "test-sheetmetal-cuts",
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


def _broad_inner_wire_count(shape, axis: Vector = Vector(0, 0, 1), min_area: float = 500.0) -> int:
    """Inner wires (holes) summed over the flat sheet's broad faces (one side worth)."""
    total = 0
    sides: dict[float, int] = {}
    for f in shape.faces():
        if f.geom_type != GeomType.PLANE:
            continue
        n = f.normal_at()
        n = n * (1.0 / max(n.length, 1e-12))
        if abs(abs(n.dot(axis)) - 1.0) < 1e-4 and f.area > min_area:
            z = round(f.center().dot(axis), 3)
            sides[z] = sides.get(z, 0) + len(f.inner_wires())
    # Both skins carry the same holes; report one side.
    return max(sides.values()) if sides else 0


def _loop_centroid_2d(loop: list[list[float]]) -> tuple[float, float]:
    n = max(len(loop), 1)
    return sum(p[0] for p in loop) / n, sum(p[1] for p in loop) / n


def _seed_bend(datums: dict, rec: dict) -> None:
    """Append a bend record, creating the container the compiler would otherwise create."""
    if "_sheet_metal_bends" not in datums:
        datums["_sheet_metal_bends"] = Datum(kind="_sheet_metal_bends", obj=[],
                                             feature_id="__sys__")
    datums["_sheet_metal_bends"].obj.append(rec)


def _compile_with_injection(doc: dict, before_fid: str, inject):
    """Run the compile loop manually, calling ``inject(bodies, datums, env)`` before *before_fid*.

    Mirrors the manual loop in test_sheetmetal_flat.py so a cut / relief can be staged onto the
    running body before the flat pattern runs (the face/edge selectors cannot deterministically
    re-pick a regenerated thin-sheet face — a documented harness limitation orthogonal to the
    cut/relief math under test)."""
    env: dict = {}
    bodies: list = []
    datums = reserved_datums()
    for feat in doc["features"]:
        if feat["id"] == before_fid:
            inject(bodies, datums, env)
        rp, _, _ = _resolve_params(feat.get("params", {}), env)
        fv = dict(feat)
        fv["params"] = rp
        ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
        out = get_compiler(feat["type"])(fv, ctx)
        bodies = out.bodies
    return bodies, datums


# --------------------------------------------------------------------------- 1: base-region hole


def test_base_region_hole_appears_in_flat_at_drilled_coordinate() -> None:
    """A hole in the base region propagates into the flat pattern at its drilled (x, y).

    The base region develops 1:1 (it is the fixed face), so a hole drilled at (30, 30) on the base
    must appear at (30, 30) on the flat blank — in the flat SOLID (inner wire) and as a cut loop the
    DXF export draws."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
               "position": "material_inside"}, refs={"edge": sel}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    def inject(bodies, datums, env):
        body = bodies[-1]
        # Through-hole at base (30, 30) (base spans y∈[0,40]; the bend is at y=0).
        tool = Solid.make_cylinder(4.0, 10.0, Plane(origin=Vector(30, 30, -1), z_dir=Vector(0, 0, 1)))
        bodies[-1] = body - tool

    bodies, datums = _compile_with_injection(doc, "f5", inject)
    flat = bodies[-1]
    payload = datums["_sheet_metal_flat"].obj["f5"]

    # --- the hole survives into the flat SOLID as an inner wire on the broad face ---
    assert _broad_inner_wire_count(flat) >= 1, "the base-region hole must remain in the flat solid"

    # --- and as a developed cut loop for the DXF export ---
    loops = payload["cut_loops"]
    assert len(loops) >= 1, "the flat pattern must record the hole as a cut loop for the flat DXF"

    # The loop centroid lands at the drilled coordinate (30, 30).
    cx, cy = _loop_centroid_2d(loops[0])
    assert abs(cx - 30.0) < 0.6 and abs(cy - 30.0) < 0.6, \
        f"base hole should develop at (30, 30); got ({cx:.2f}, {cy:.2f})"

    # The loop closes and is non-trivial (a real circle discretised to a polyline).
    assert len(loops[0]) >= 8, "a circular hole should discretise to a polyline loop"
    print(f"[cut-base] hole at flat ({cx:.2f},{cy:.2f}); flat inner wires="
          f"{_broad_inner_wire_count(flat)}; cut_count={len(loops)}")


# --------------------------------------------------------------------------- 1b: hole in the flat DXF


def test_base_region_hole_reaches_flat_dxf_geometry() -> None:
    """The propagated hole reaches the DXF/flat-export geometry (the manufacturing requirement)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)}, refs={"edge": sel}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    def inject(bodies, datums, env):
        tool = Solid.make_cylinder(4.0, 10.0, Plane(origin=Vector(25, 30, -1), z_dir=Vector(0, 0, 1)))
        bodies[-1] = bodies[-1] - tool

    _, datums = _compile_with_injection(doc, "f5", inject)
    payload = datums["_sheet_metal_flat"].obj["f5"]

    # The hole is in the payload the flat-DXF exporter consumes (cut_loops), projected to 2-D the
    # same way export2d.flat_pattern_layers does for the outline/bend lines.
    from engine.doc.export2d import _basis_from_normal, _to_2d
    u, v, _n = _basis_from_normal(payload["n_base"])
    cut_loops_2d = [[_to_2d(p, u, v) for p in lp] for lp in payload["cut_loops"]]
    assert cut_loops_2d, "the flat-DXF geometry must include the hole as a closed cut loop"
    # The 2-D loop is closed and bounds a real area (a hole, not a stray point).
    lp = cut_loops_2d[0]
    xs = [p[0] for p in lp]; ys = [p[1] for p in lp]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    assert extent > 1.0, f"the DXF cut loop should span the hole diameter; got {extent:.2f}"
    print(f"[cut-dxf] DXF cut loop span={extent:.2f} mm, {len(lp)} pts (drawn on the flat blank)")


# --------------------------------------------------------------------------- 2: wall hole develops


def test_wall_hole_develops_onto_flat_strip() -> None:
    """A hole punched through the FLANGE WALL develops onto the flat strip (not its folded position).

    For a 90° flange the wall hangs in -Z. A hole at wall-drop ``drop`` develops to flat distance
    ``BA + (drop - bend_drop)`` along the unfold direction from the bend tangent. The flat SOLID
    must actually lose material (a real cut), and the developed loop must sit on the strip, BEYOND
    the bend (i.e. past the base region)."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
               "position": "material_inside"}, refs={"edge": sel}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    full_flat_vol = None

    def inject(bodies, datums, env):
        nonlocal full_flat_vol
        full_flat_vol = bodies[-1].volume  # folded body volume before the wall cut
        # Drill a Y-axis hole through the wall (wall at y∈[0,2], z negative), mid-wall at z=-12.
        tool = Solid.make_cylinder(3.0, 10.0, Plane(origin=Vector(30, -5, -12), z_dir=Vector(0, 1, 0)))
        bodies[-1] = bodies[-1] - tool

    bodies, datums = _compile_with_injection(doc, "f5", inject)
    flat = bodies[-1]
    payload = datums["_sheet_metal_flat"].obj["f5"]

    # --- the wall hole was actually CUT from the flat solid (volume dropped) ---
    # Compare against an identical part with NO wall hole.
    doc_clean = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
               "position": "material_inside"}, refs={"edge": sel}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    clean_flat = compile_document(doc_clean).bodies[0].shape
    assert flat.volume < clean_flat.volume - 1.0, \
        f"the wall hole must remove material from the flat ({flat.volume:.2f} vs {clean_flat.volume:.2f})"

    # --- the hole shows on the flat broad face as an inner wire ---
    assert _broad_inner_wire_count(flat) >= 1, "the developed wall hole must be an inner wire"

    # --- and develops to a cut loop BEYOND the bend region (not at its folded y) ---
    loops = payload["cut_loops"]
    assert len(loops) >= 1, "the wall hole must develop to a flat cut loop"
    R, T, K = 2.0, 2.0, 0.45
    ba = bend_allowance(90.0, R, T, K)
    # tangent y for material_inside = +(R+T) = 4; length_dir = -Y. The hole (z=-12, drop=12,
    # bend_drop=(R+T)=4) develops to dev = BA + (12-4); flat y ≈ 4 - dev (negative, past the base).
    cx, cy = _loop_centroid_2d(loops[0])
    assert cy < 0.0, \
        f"the wall hole should develop onto the strip BEYOND the bend (y<0); got y={cy:.2f}"
    expected_y = 4.0 - (ba + (12.0 - 4.0))
    assert abs(cy - expected_y) < 2.0, \
        f"developed wall-hole y {cy:.2f} vs expected ~{expected_y:.2f} (BA={ba:.3f})"
    print(f"[cut-wall] wall hole develops to flat y={cy:.2f} (expected ~{expected_y:.2f}); "
          f"flat vol {flat.volume:.1f} < clean {clean_flat.volume:.1f}")


# --------------------------------------------------------------------------- 3: slot propagates


def test_base_region_slot_propagates() -> None:
    """An obround SLOT (not just a circle) in the base region propagates as a multi-point loop."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)}, refs={"edge": sel}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    def inject(bodies, datums, env):
        # A slot = two cylinders + a box bridge (an obround) at base (30, 28).
        c1 = Solid.make_cylinder(3.0, 10.0, Plane(origin=Vector(24, 28, -1), z_dir=Vector(0, 0, 1)))
        c2 = Solid.make_cylinder(3.0, 10.0, Plane(origin=Vector(36, 28, -1), z_dir=Vector(0, 0, 1)))
        box = Solid.make_box(12.0, 6.0, 10.0, Plane(origin=Vector(24, 25, -1)))
        slot = c1 + c2 + box
        bodies[-1] = bodies[-1] - slot

    bodies, datums = _compile_with_injection(doc, "f5", inject)
    flat = bodies[-1]
    payload = datums["_sheet_metal_flat"].obj["f5"]
    assert _broad_inner_wire_count(flat) >= 1, "the slot must remain in the flat solid"
    loops = payload["cut_loops"]
    assert len(loops) >= 1, "the slot must record a cut loop"
    # A slot is wider in X than tall in Y (an obround), and discretises to many points.
    lp = loops[0]
    xs = [p[0] for p in lp]; ys = [p[1] for p in lp]
    assert (max(xs) - min(xs)) > (max(ys) - min(ys)), "the slot should be wider than it is tall"
    assert len(lp) >= 8, "the slot loop should be a polyline, not a 4-point box"
    print(f"[cut-slot] slot loop span x={max(xs)-min(xs):.1f} y={max(ys)-min(ys):.1f}, {len(lp)} pts")


# --------------------------------------------------------------------------- 4: no-cut regression


def test_flat_pattern_without_cuts_is_unchanged() -> None:
    """With no cuts, the flat pattern is exactly as before (cut_count 0, no spurious loops).

    Regression guard: cut propagation must not alter the flat of a part that has no holes."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0)}, refs={"edge": sel}),
        _feat("f4", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)
    s4 = _status(res, "f4")
    assert s4["ok"], s4["error"]
    assert abs(s4["resolved"]["cut_count"] - 0.0) < 1e-9, "a no-cut part must record zero cut loops"
    # Developed length unchanged (the canonical 60.5553 along Y from the flat-pattern spec).
    flat = res.bodies[0].shape
    bb = flat.bounding_box()
    R, T, K, wall = 2.0, 2.0, 0.45, 20.0
    BA = (math.pi / 2.0) * (R + K * T)
    dev_len = (40.0 - (R + T)) + BA + wall
    assert abs((bb.max.Y - bb.min.Y) - dev_len) < 1e-2, \
        f"no-cut flat developed length must be unchanged; got {bb.max.Y-bb.min.Y:.4f} vs {dev_len:.4f}"
    # No inner wires (no holes).
    assert _broad_inner_wire_count(flat) == 0, "a no-cut flat must have no holes"
    print(f"[cut-none] cut_count=0; developed Y={bb.max.Y-bb.min.Y:.4f} (unchanged)")


# --------------------------------------------------------------------------- 5: corner relief generated


def test_corner_relief_generated_for_adjacent_flanges() -> None:
    """Two ADJACENT (perpendicular) flanges sharing a corner -> a relief notch is generated.

    The y=0 long-edge flange is built normally (f3). The x=0 short-edge flange is seeded as a clean
    bend record (the regenerated short edge cannot be re-picked deterministically — the documented
    selector limitation the two-flange flat test also works around). Adding the second flange must
    cut a corner relief: the body loses corner material (no overlap), a relief record is stored, and
    the notch develops into the flat blank."""
    base = _base_flange_doc(60.0, 40.0, 2.0)
    res0 = compile_document(_doc(base))
    sel_a = _pick_edge_selector(res0, (30.0, 0.0, 0.0), length=60.0)
    R, T, K = 2.0, 2.0, 0.45
    ba = bend_allowance(90.0, R, T, K)

    doc = _doc(base + [
        _feat("f3", "sm_edge_flange",
              {"angle": _dim(90.0), "length": _dim(15.0), "radius": _dim(2.0)}, refs={"edge": sel_a}),
        _feat("f5", "sm_flat_pattern", {}),
    ])
    _assert_valid(doc)

    captured: dict = {}

    def inject(bodies, datums, env):
        # Adjacent flange on the x=0 short edge: perpendicular bend line sharing corner (0, 0, 0).
        recB = {
            "feature": "f4", "angle": 90.0, "radius": R, "thickness": T, "k_factor": K,
            "neutral_radius": R + K * T, "bend_allowance": ba, "bend_deduction": 0.0,
            "wall_length": 15.0, "position": "material_inside", "flip": False,
            "bend_line": [[0.0, 0.0, 0.0], [0.0, 40.0, 0.0]],
            "e_dir": [0.0, 1.0, 0.0], "length_dir": [-1.0, 0.0, 0.0], "fold_dir": [0.0, 0.0, 1.0],
        }
        ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
        vol0 = bodies[-1].volume
        body2, reliefs = _apply_corner_reliefs(ctx, "f4", bodies[-1], recB, "rectangular")
        captured["vol0"] = vol0
        captured["vol1"] = body2.volume
        captured["reliefs"] = reliefs
        bodies[-1] = body2
        datums["_sheet_metal_bends"].obj.append(recB)
        _append_relief_records(ctx, reliefs)

    bodies, datums = _compile_with_injection(doc, "f5", inject)

    # --- a relief was generated and removed corner material (the overlap) ---
    assert len(captured["reliefs"]) == 1, "two adjacent flanges should generate exactly one relief"
    assert captured["vol1"] < captured["vol0"] - 1e-3, \
        f"the relief must remove corner material; {captured['vol0']:.3f} -> {captured['vol1']:.3f}"

    # --- the relief record carries the corner + a closed notch loop ---
    rec = captured["reliefs"][0]
    corner = rec["corner"]
    assert abs(corner[0]) < 1e-6 and abs(corner[1]) < 1e-6, \
        f"the relief sits at the shared corner (0, 0); got {corner}"
    assert len(rec["loop"]) >= 3, "the relief notch must be a closed loop"

    # --- and it develops into the flat blank as a cut loop near the corner ---
    payload = datums["_sheet_metal_flat"].obj["f5"]
    assert len(payload["cut_loops"]) >= 1, "the relief must develop into the flat blank"
    cx, cy = _loop_centroid_2d(payload["cut_loops"][0])
    # The notch straddles the corner (legs run into the part), so its centroid is near (size/2).
    assert cx >= -0.1 and cy >= -0.1 and cx < 6.0 and cy < 6.0, \
        f"the relief notch should sit at the corner; centroid ({cx:.2f}, {cy:.2f})"
    print(f"[relief] generated 1 notch at corner {corner[:2]}; body "
          f"{captured['vol0']:.1f}->{captured['vol1']:.1f}; flat notch ({cx:.2f},{cy:.2f})")


# --------------------------------------------------------------------------- 6: relief styles


def test_corner_relief_styles_all_generate_a_notch() -> None:
    """rectangular / obround / tear relief styles each build a valid cutting notch."""
    R, T = 2.0, 2.0
    recA = {"feature": "fa", "radius": R, "thickness": T,
            "bend_line": [[0.0, 0.0, 0.0], [60.0, 0.0, 0.0]],
            "e_dir": [1.0, 0.0, 0.0], "length_dir": [0.0, -1.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    recB = {"feature": "fb", "radius": R, "thickness": T,
            "bend_line": [[0.0, 0.0, 0.0], [0.0, 40.0, 0.0]],
            "e_dir": [0.0, 1.0, 0.0], "length_dir": [-1.0, 0.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    assert _adjacent_flanges(recA, recB), "perpendicular flanges sharing a corner are adjacent"
    corner = _shared_corner(recA, recB)
    assert corner is not None
    size = _relief_size(T, R)
    assert size >= T, "the relief should be at least the material thickness"
    n_base = Vector(0, 0, 1)
    for kind in ("rectangular", "obround", "tear"):
        rec = _relief_record(corner, recA, recB, size, kind)
        assert rec["kind"] == kind
        assert len(rec["loop"]) >= 3
        tool = _build_relief_tool(rec, n_base, T)
        assert tool.volume > 1.0, f"{kind} relief must build a non-empty cutting tool"
    print(f"[relief-styles] rectangular/obround/tear all build; relief size={size:.2f}")


# --------------------------------------------------------------------------- 7: parallel flanges need none


def test_parallel_flanges_generate_no_relief() -> None:
    """Two PARALLEL flanges (opposite edges) are NOT adjacent — no corner relief is generated."""
    R, T = 2.0, 2.0
    # Both on long edges (y=0 and y=40), bend lines parallel (along X), do NOT share a corner.
    recA = {"feature": "fa", "radius": R, "thickness": T,
            "bend_line": [[0.0, 0.0, 0.0], [60.0, 0.0, 0.0]],
            "e_dir": [1.0, 0.0, 0.0], "length_dir": [0.0, -1.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    recB = {"feature": "fb", "radius": R, "thickness": T,
            "bend_line": [[0.0, 40.0, 0.0], [60.0, 40.0, 0.0]],
            "e_dir": [1.0, 0.0, 0.0], "length_dir": [0.0, 1.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    assert not _adjacent_flanges(recA, recB), \
        "parallel flanges on opposite edges must NOT be flagged adjacent (no relief needed)"
    assert _shared_corner(recA, recB) is None, "parallel opposite-edge flanges share no corner"

    # End-to-end: a relief application between them is a no-op.
    base = _base_flange_doc(60.0, 40.0, 2.0)
    env: dict = {}
    bodies: list = compile_document(_doc(base)).bodies
    bodies = [bodies[0].shape]
    datums = reserved_datums()
    _seed_bend(datums, recA)
    ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
    body2, reliefs = _apply_corner_reliefs(ctx, "fb", bodies[-1], recB, "rectangular")
    assert reliefs == [], "parallel flanges must produce no corner relief"
    assert abs(body2.volume - bodies[-1].volume) < 1e-9, "no relief means the body is unchanged"
    print("[relief-parallel] opposite-edge flanges -> no relief (correct)")


# --------------------------------------------------------------------------- 8: relief can be disabled


def test_corner_relief_none_disables() -> None:
    """``corner_relief='none'`` skips relief generation even for adjacent flanges."""
    R, T, K = 2.0, 2.0, 0.45
    ba = bend_allowance(90.0, R, T, K)
    recA = {"feature": "fa", "angle": 90.0, "radius": R, "thickness": T, "k_factor": K,
            "bend_allowance": ba, "bend_line": [[0.0, 0.0, 0.0], [60.0, 0.0, 0.0]],
            "e_dir": [1.0, 0.0, 0.0], "length_dir": [0.0, -1.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    recB = {"feature": "fb", "angle": 90.0, "radius": R, "thickness": T, "k_factor": K,
            "bend_allowance": ba, "bend_line": [[0.0, 0.0, 0.0], [0.0, 40.0, 0.0]],
            "e_dir": [0.0, 1.0, 0.0], "length_dir": [-1.0, 0.0, 0.0], "fold_dir": [0.0, 0.0, 1.0]}
    base = _base_flange_doc(60.0, 40.0, 2.0)
    bodies = [compile_document(_doc(base)).bodies[0].shape]
    datums = reserved_datums()
    _seed_bend(datums, recA)
    ctx = CompileContext(equations_env={}, bodies=bodies, datums=datums)
    body2, reliefs = _apply_corner_reliefs(ctx, "fb", bodies[-1], recB, "none")
    assert reliefs == [], "corner_relief='none' must generate no relief"
    assert abs(body2.volume - bodies[-1].volume) < 1e-9, "disabled relief leaves the body untouched"
    print("[relief-none] corner_relief='none' -> no notch (disabled)")
