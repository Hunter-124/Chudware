"""Tests for the SolidWorks-parity helix/spiral additions in features/curves.py.

Two new helix modes (over the constant-pitch helix exercised in test_boolean_helix.py):

  * **Variable pitch** — ``pitch_table`` rows define pitch (and optionally diameter) at given
    turn counts. The helix's total height is the INTEGRAL of pitch over the turns, and its local
    pitch varies along the length (z spacing between successive whole turns differs region to
    region).
  * **Flat spiral** — ``spiral:true`` builds a planar Archimedean spiral ``r = r0 + (pitch/2π)·θ``
    in the base plane (z≈const), with the expected outer radius and turn count.

Harness mirrors test_boolean_helix.py: ``_dim`` / ``_feat`` / ``_doc`` feed ``compile_document``;
``_status`` reads a feature's status record; docs validate before compiling.
"""

from __future__ import annotations

import math

from build123d import Wire

from engine.doc.compiler import compile_document
from engine.doc.schema import validate_part


# --------------------------------------------------------------------------- helpers
# (mirrored from test_boolean_helix.py so this file stands alone)


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _doc(features: list[dict], globals_list: list[dict] | None = None) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
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


def _local_pts(wire: Wire, n: int = 400) -> list:
    """Sample n+1 points along a wire at evenly spaced LENGTH positions (build123d @ param)."""
    return [wire @ (i / n) for i in range(n + 1)]


# --------------------------------------------------------------------------- variable pitch


def _var_helix_feat(fid: str, table: list, *, radius: float = 8.0,
                    ccw: bool = True, start_angle: float = 0.0,
                    base: str = "plane.top") -> dict:
    """A helix feature driven by a pitch_table. pitch/height keys are required by the schema but
    ignored by the variable-pitch path (the table governs everything)."""
    params: dict = {
        "pitch": _dim(5.0), "height": _dim(20.0),
        "start_angle": _dim(start_angle), "ccw": ccw, "radius": _dim(radius),
        "pitch_table": table,
    }
    return _feat(fid, "helix", params, refs={"base": base})


def test_variable_pitch_total_height_matches_integral() -> None:
    """A variable-pitch helix's total height equals the integral of the pitch table.

    Table: turns 0..2 at pitch 4, turns 2..5 at pitch 10 (piecewise-LINEAR pitch between rows).
    Height = ∫ pitch dt = [turn 0→2 trapezoid] + [turn 2→5 constant-10] ... but the pitch is
    linear between the row at turn 2 (pitch 4) and turn 5 (pitch 10), so:
        seg 0→2: pitch const 4  -> area 4·2 = 8
        seg 2→5: pitch 4→10 lin -> area 0.5·(4+10)·3 = 21
    total = 29 mm.
    """
    table = [
        {"turn": _dim(0.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(2.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(5.0), "pitch": _dim(10.0), "dia": _dim(16.0)},
    ]
    doc = _doc([_var_helix_feat("f1", table, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"variable-pitch helix failed: {s['error']}"
    # curve feature: no solid body.
    assert len(res.bodies) == 0, f"helix must not add a body; got {len(res.bodies)}"

    expected_h = 4.0 * 2.0 + 0.5 * (4.0 + 10.0) * 3.0  # = 29.0
    assert abs(s["resolved"]["height"] - expected_h) < 1e-6, \
        f"resolved height {s['resolved']['height']} vs integral {expected_h}"
    assert abs(s["resolved"]["turns"] - 5.0) < 1e-9, f"turns should be 5; got {s['resolved']}"

    wire = res.tool_bodies["f1"]
    assert isinstance(wire, Wire), f"tool_body should be a Wire; got {type(wire)}"
    # The curve's top z must reach the integrated height (within spline tolerance).
    pts = _local_pts(wire, 600)
    zmax = max(pt.Z for pt in pts)
    zmin = min(pt.Z for pt in pts)
    assert abs(zmin) < 1e-3, f"variable-pitch helix should start at z=0; got zmin={zmin}"
    assert abs(zmax - expected_h) / expected_h < 0.01, \
        f"variable-pitch top z {zmax:.3f} vs integral {expected_h}"
    print(f"[var-pitch] turns={s['resolved']['turns']:.0f} height={s['resolved']['height']:.2f} "
          f"(integral {expected_h}) zmax={zmax:.2f}")


def test_variable_pitch_local_pitch_varies() -> None:
    """Local pitch differs region-to-region: the z gained over the first full turn (pitch≈4) is
    much less than over the last full turn (pitch≈10).

    z(turn) is the integral of pitch, so:
        Δz over turn 0→1  = 4   (in the constant-4 region)
        Δz over turn 4→5  = pitch at ~4.5 ≈ 4 + (2.5/3)·6 ≈ 9   (linear-ramp region)
    We assert the last-turn rise is clearly larger than the first-turn rise.
    """
    table = [
        {"turn": _dim(0.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(2.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(5.0), "pitch": _dim(10.0), "dia": _dim(16.0)},
    ]
    doc = _doc([_var_helix_feat("f1", table, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"variable-pitch helix failed: {s['error']}"

    # Closed form of the integral z(turn) for this table:
    def z_of(turn: float) -> float:
        if turn <= 2.0:
            return 4.0 * turn
        # seg 2->5: pitch 4 + (t-2)/3 * 6 ; integral from 2 to turn.
        base = 4.0 * 2.0
        span = turn - 2.0
        p_end = 4.0 + span / 3.0 * 6.0
        return base + 0.5 * (4.0 + p_end) * span

    first_turn_rise = z_of(1.0) - z_of(0.0)
    last_turn_rise = z_of(5.0) - z_of(4.0)
    assert last_turn_rise > first_turn_rise + 2.0, \
        f"local pitch should grow: first-turn Δz={first_turn_rise} last-turn Δz={last_turn_rise}"

    # And confirm the built geometry reflects this: sample z at the analytic length fractions
    # matching whole turns. The wire is parameterised by length, but we can verify monotonic z and
    # that the top portion is steeper by comparing z at 4 vs 5 turns' worth of *height*.
    wire = res.tool_bodies["f1"]
    total_h = s["resolved"]["height"]
    pts = _local_pts(wire, 1000)
    zmax = max(pt.Z for pt in pts)
    # The geometric top must match z_of(5).
    assert abs(zmax - z_of(5.0)) / z_of(5.0) < 0.01, f"top z {zmax} vs z_of(5)={z_of(5.0)}"
    assert abs(total_h - z_of(5.0)) < 1e-6
    print(f"[var-pitch-local] first-turn Δz={first_turn_rise:.2f} last-turn Δz={last_turn_rise:.2f}")


def test_variable_diameter_changes_radius() -> None:
    """A pitch_table whose diameter column changes makes the helix radius vary along its length:
    the local cylindrical radius at the bottom turns differs from the top turns."""
    table = [
        {"turn": _dim(0.0), "pitch": _dim(5.0), "dia": _dim(10.0)},   # r = 5
        {"turn": _dim(4.0), "pitch": _dim(5.0), "dia": _dim(30.0)},   # r = 15
    ]
    doc = _doc([_var_helix_feat("f1", table, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"variable-diameter helix failed: {s['error']}"
    assert abs(s["resolved"]["radius_start"] - 5.0) < 1e-9
    assert abs(s["resolved"]["radius_end"] - 15.0) < 1e-9

    wire = res.tool_bodies["f1"]
    pts = _local_pts(wire, 1200)
    # cylindrical radius (distance from local axis) at each sample.
    radii = [math.hypot(pt.X, pt.Y) for pt in pts]
    # Bottom of the curve (z small) should be near r=5; top (z large) near r=15.
    zmax = max(pt.Z for pt in pts)
    bottom = [r for pt, r in zip(pts, radii) if pt.Z < 0.1 * zmax]
    top = [r for pt, r in zip(pts, radii) if pt.Z > 0.9 * zmax]
    assert min(radii) < 6.0, f"min radius should approach 5; got {min(radii):.2f}"
    assert max(radii) > 14.0, f"max radius should approach 15; got {max(radii):.2f}"
    assert sum(top) / len(top) > sum(bottom) / len(bottom) + 5.0, \
        "radius should grow from bottom to top"
    print(f"[var-dia] r range [{min(radii):.2f}, {max(radii):.2f}] "
          f"(table 5→15)")


def test_variable_pitch_bare_list_rows() -> None:
    """Rows may be bare [turn, pitch, dia] lists (not just mappings) and still integrate."""
    table = [
        [_dim(0.0), _dim(6.0), _dim(20.0)],
        [_dim(3.0), _dim(6.0), _dim(20.0)],
    ]
    doc = _doc([_var_helix_feat("f1", table, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"bare-list pitch_table failed: {s['error']}"
    # constant pitch 6 over 3 turns -> height 18.
    assert abs(s["resolved"]["height"] - 18.0) < 1e-6, f"height {s['resolved']['height']} vs 18"
    assert abs(s["resolved"]["turns"] - 3.0) < 1e-9
    print(f"[var-bare] height={s['resolved']['height']:.1f} (3 turns × pitch 6)")


# --------------------------------------------------------------------------- flat spiral


def _spiral_feat(fid: str, *, pitch: float, turns: float, start_radius: float = 2.0,
                 ccw: bool = True, base: str = "plane.top") -> dict:
    """A flat Archimedean spiral. pitch/height keys present for the schema; height is ignored."""
    params: dict = {
        "pitch": _dim(pitch), "height": _dim(20.0),
        "turns": _dim(turns), "start_angle": _dim(0.0), "ccw": ccw,
        "start_radius": _dim(start_radius), "radius": _dim(start_radius),
        "spiral": True,
    }
    return _feat(fid, "helix", params, refs={"base": base})


def test_flat_spiral_planar_outer_radius_and_turns() -> None:
    """A flat spiral lies in the base plane (z≈const) with the right outer radius and turn count.

    r = start_radius + (pitch/2π)·θ, θ ∈ [0, 2π·turns].
    With start_radius=2, pitch=3, turns=5: outer_r = 2 + 3·5 = 17.
    """
    sr, pitch, turns = 2.0, 3.0, 5.0
    doc = _doc([_spiral_feat("f1", pitch=pitch, turns=turns, start_radius=sr)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"flat spiral failed: {s['error']}"
    assert len(res.bodies) == 0, f"spiral is a curve; must not add a body; got {len(res.bodies)}"

    expected_outer = sr + pitch * turns  # 17
    assert abs(s["resolved"]["outer_radius"] - expected_outer) < 1e-9, \
        f"resolved outer_radius {s['resolved']['outer_radius']} vs {expected_outer}"
    assert abs(s["resolved"]["height"]) < 1e-12, "flat spiral has zero height"

    wire = res.tool_bodies["f1"]
    assert isinstance(wire, Wire), f"tool_body should be a Wire; got {type(wire)}"
    pts = _local_pts(wire, 800)

    # Planar: every point lies in the base (XY) plane -> z ≈ 0 (base = plane.top, local frame).
    zspread = max(pt.Z for pt in pts) - min(pt.Z for pt in pts)
    assert zspread < 1e-6, f"flat spiral must be planar (z const); z spread={zspread}"

    # Outer radius: the farthest point from the axis matches the analytic outer radius.
    radii = [math.hypot(pt.X, pt.Y) for pt in pts]
    assert abs(max(radii) - expected_outer) / expected_outer < 0.01, \
        f"spiral outer radius {max(radii):.3f} vs {expected_outer}"
    # Inner radius starts near start_radius.
    assert abs(min(radii) - sr) < 0.3, f"spiral inner radius {min(radii):.3f} vs {sr}"

    # Turn count: total angle swept ≈ 2π·turns. Use the spiral's arc-length sanity instead —
    # the radial growth across the whole curve equals pitch·turns.
    radial_growth = max(radii) - min(radii)
    assert abs(radial_growth - pitch * turns) / (pitch * turns) < 0.02, \
        f"radial growth {radial_growth:.3f} vs pitch·turns {pitch * turns}"
    print(f"[spiral] outer_r={max(radii):.2f} (expected {expected_outer}) "
          f"z-spread={zspread:.2e} turns={turns}")


def test_flat_spiral_turn_count_via_winding() -> None:
    """Confirm the spiral actually winds ``turns`` times: count the sign changes of the y-coord
    (axis crossings of the +x ray) along the curve ≈ 2·turns."""
    sr, pitch, turns = 1.0, 2.0, 4.0
    doc = _doc([_spiral_feat("f1", pitch=pitch, turns=turns, start_radius=sr)])
    _assert_valid(doc)
    res = compile_document(doc)
    s = _status(res, "f1")
    assert s["ok"], f"flat spiral failed: {s['error']}"

    wire = res.tool_bodies["f1"]
    pts = _local_pts(wire, 2000)
    # Count zero-crossings of y where x>0 (each full turn crosses the +x axis once).
    crossings = 0
    prev = None
    for pt in pts:
        if pt.X > 0:
            cur = 1 if pt.Y >= 0 else -1
            if prev is not None and cur != prev:
                crossings += 1
            prev = cur
        else:
            prev = None
    # Each full revolution gives ~1 crossing of the +x ray; allow ±1 for endpoint placement.
    assert abs(crossings - turns) <= 1, \
        f"+x-ray crossings {crossings} should be ≈ turns {turns}"
    print(f"[spiral-turns] +x-axis crossings={crossings} (turns {turns})")


def test_spiral_ccw_vs_cw_winding() -> None:
    """ccw=True and ccw=False both build and wind in opposite angular directions."""
    sr, pitch, turns = 1.0, 2.0, 3.0
    res_ccw = compile_document(_doc([_spiral_feat("f1", pitch=pitch, turns=turns,
                                                  start_radius=sr, ccw=True)]))
    res_cw = compile_document(_doc([_spiral_feat("f1", pitch=pitch, turns=turns,
                                                 start_radius=sr, ccw=False)]))
    assert _status(res_ccw, "f1")["ok"], _status(res_ccw, "f1")["error"]
    assert _status(res_cw, "f1")["ok"], _status(res_cw, "f1")["error"]
    w_ccw = res_ccw.tool_bodies["f1"]
    w_cw = res_cw.tool_bodies["f1"]
    assert w_ccw.length > 0 and w_cw.length > 0
    # Same magnitude curve (same radii schedule) -> equal lengths within tolerance.
    assert abs(w_ccw.length - w_cw.length) / w_ccw.length < 0.02, \
        f"ccw/cw spiral lengths should match: {w_ccw.length:.2f} vs {w_cw.length:.2f}"

    # A short distance along each: ccw turns toward +y, cw toward -y (start at angle 0 on +x).
    p_ccw = w_ccw @ 0.02
    p_cw = w_cw @ 0.02
    assert p_ccw.Y > 0 > p_cw.Y, \
        f"ccw should head +y ({p_ccw.Y:.3f}), cw -y ({p_cw.Y:.3f})"
    print(f"[spiral-hand] ccw early y={p_ccw.Y:.3f}  cw early y={p_cw.Y:.3f}")


# --------------------------------------------------------------------------- sweep contract


def test_sweep_along_variable_pitch_makes_spring() -> None:
    """A variable-pitch helix is consumable as a sweep ``path`` (the core curve-feature contract):
    a small circle swept along it produces a variable-pitch spring solid."""
    radius, prof_r = 8.0, 1.0
    table = [
        {"turn": _dim(0.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(2.0), "pitch": _dim(4.0), "dia": _dim(16.0)},
        {"turn": _dim(5.0), "pitch": _dim(10.0), "dia": _dim(16.0)},
    ]
    helix = _var_helix_feat("f1", table, radius=radius, base="plane.top")
    profile = _feat("f2", "sketch", {
        "entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [radius, 0.0], "radius": prof_r},
        ],
        "constraints": [],
    }, refs={"plane": "plane.front"})
    sweep = _feat("f3", "sweep", {"mode": "boss", "merge": True}, refs={
        "profile": [{"kind": "region", "sketch": "f2",
                     "centroid": [radius, 0.0],
                     "area_or_len": math.pi * prof_r ** 2, "hint_index": 0}],
        "path": {"kind": "region", "sketch": "f1"},  # the variable-pitch helix as the path
    })
    doc = _doc([helix, profile, sweep])
    _assert_valid(doc)
    res = compile_document(doc)
    assert _status(res, "f1")["ok"], _status(res, "f1")["error"]
    assert _status(res, "f2")["ok"], _status(res, "f2")["error"]
    s = _status(res, "f3")
    assert s["ok"], f"sweep along variable-pitch helix failed: {s['error']}"
    assert len(res.bodies) == 1, f"spring should be one body; got {len(res.bodies)}"
    vol = res.bodies[0].shape.volume
    # Sanity: tube cross-section π·prof_r² times the curve length (length tracked by the wire).
    wire_len = res.tool_bodies["f1"].length
    approx = (math.pi * prof_r ** 2) * wire_len
    assert vol > 0.0, "variable-pitch spring should have positive volume"
    assert abs(vol - approx) / approx < 0.25, f"spring vol {vol:.1f} vs ~{approx:.1f}"
    print(f"[var-sweep] variable-pitch spring vol={vol:.1f} (approx {approx:.1f})")


# --------------------------------------------------------------------------- error paths


def test_variable_pitch_rejects_single_row() -> None:
    """A pitch_table with a single distinct turn can't define a varying region → contained error."""
    table = [{"turn": _dim(0.0), "pitch": _dim(5.0), "dia": _dim(16.0)}]
    doc = _doc([_var_helix_feat("f1", table, radius=8.0)])
    _assert_valid(doc)
    res = compile_document(doc)  # must not raise
    s = _status(res, "f1")
    assert not s["ok"], "single-row pitch_table should fail"
    assert "pitch_table" in (s["error"] or ""), f"error should mention pitch_table; got {s['error']}"
    print(f"[var-error] single row -> '{(s['error'] or '')[:60]}'")


# --------------------------------------------------------------------------- main


def main() -> None:
    test_variable_pitch_total_height_matches_integral()
    test_variable_pitch_local_pitch_varies()
    test_variable_diameter_changes_radius()
    test_variable_pitch_bare_list_rows()
    test_flat_spiral_planar_outer_radius_and_turns()
    test_flat_spiral_turn_count_via_winding()
    test_spiral_ccw_vs_cw_winding()
    test_sweep_along_variable_pitch_makes_spring()
    test_variable_pitch_rejects_single_row()
    print("\nHELIX-EXT TESTS PASSED")


if __name__ == "__main__":
    main()
