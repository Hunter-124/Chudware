"""Curve↔curve tangent constraints in the 2D sketch solver (SW-parity, GAPS P0).

Before this, tangency was solved only for line↔circle / circle↔circle / line↔conic.
This covers the remaining entity-pair combinations the spec calls out — arc↔spline,
spline↔spline, arc↔arc, circle↔arc, line↔arc/spline, style_spline pairs — driven by
a single scalar residual (signed gap at the curves' mutual closest point; parallel
tangents follow automatically at a closest-approach point).

Harness mirrors engine/doc/test_nsolve.py / test_conic_relations.py: build a sketch
(entities + constraints), solve via solve_sketch, assert convergence AND that the
tangency condition (curves touch + tangent directions parallel at the touch point)
holds within tolerance. Runs under pytest OR as a plain main() runner (house style).

Run (repo root, dev venv):
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" -X utf8 -m pytest \
        engine/doc/test_sketch_tangent.py -q
"""

from __future__ import annotations

import math

import numpy as np

from engine.doc.sketch_solver import (
    solve_sketch,
    _circle_curve,
    _spline_curve,
    _style_spline_curve,
    _mutual_closest,
    _curve_descriptor,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _solve(entities, constraints):
    return solve_sketch({"entities": entities, "constraints": constraints}, {}, warm=None)


def _circle(eid, cx, cy, r):
    return {"id": eid, "type": "circle", "construction": False,
            "center": [cx, cy], "radius": r}


def _arc(eid, cx, cy, r, a0=0.0, a1=180.0):
    return {"id": eid, "type": "arc", "construction": False,
            "center": [cx, cy], "radius": r, "start": a0, "end": a1}


def _spline(eid, pts):
    return {"id": eid, "type": "spline", "construction": False,
            "points": [list(p) for p in pts]}


def _style_spline(eid, pts):
    return {"id": eid, "type": "style_spline", "construction": False,
            "points": [list(p) for p in pts]}


def _line(eid, p1, p2):
    return {"id": eid, "type": "line", "construction": False,
            "p1": list(p1), "p2": list(p2)}


def _radius(cid, eid, r):
    return {"id": cid, "type": "radius", "refs": [eid],
            "value": {"value": r, "expr": None}}


# ---------------------------------------------------------------------------
# Tangency verification helpers (independent re-derivation, not the solver's)
# ---------------------------------------------------------------------------

def _cv_and_scan(ent_out, etype):
    """Flat curve-var slice + a scan() for a solved entity, from _curve_descriptor."""
    if etype in ("circle", "arc"):
        cv = np.array([ent_out["center"][0], ent_out["center"][1], ent_out["radius"]])
    elif etype in ("spline", "style_spline"):
        cv = np.array([c for p in ent_out["points"] for c in p])
    else:
        raise AssertionError(f"verify helper has no slice for {etype}")
    npts = len(cv) // 2
    if etype == "circle" or etype == "arc":
        scan = lambda c, n: np.linspace(0.0, 2.0 * math.pi, n)
    elif etype == "spline":
        scan = lambda c, n: np.linspace(0.0, float(max(npts - 1, 1)), n)
    else:   # style_spline
        scan = lambda c, n: np.linspace(0.0, 1.0, n)
    return cv, scan


def _curve_fn(etype):
    return {"circle": _circle_curve, "arc": _circle_curve,
            "spline": _spline_curve, "style_spline": _style_spline_curve}[etype]


def _assert_tangent(sol, eid0, t0, eid1, t1, touch_tol=1e-4, par_tol=1e-4):
    """Assert the two solved curves touch AND their tangents are parallel there."""
    o0 = sol.entities[eid0]
    o1 = sol.entities[eid1]
    cv0, scan0 = _cv_and_scan(o0, t0)
    cv1, scan1 = _cv_and_scan(o1, t1)
    f0, f1 = _curve_fn(t0), _curve_fn(t1)
    p, q = _mutual_closest(f0, cv0, scan0, f1, cv1, scan1)
    P0, dP0 = f0(cv0, p)
    Q1, dP1 = f1(cv1, q)
    gap = math.hypot(P0[0] - Q1[0], P0[1] - Q1[1])
    assert gap < touch_tol, f"curves do not touch: gap={gap:.2e}"
    L0 = math.hypot(dP0[0], dP0[1])
    L1 = math.hypot(dP1[0], dP1[1])
    cross = (dP0[0] * dP1[1] - dP0[1] * dP1[0]) / (L0 * L1)
    assert abs(cross) < par_tol, f"tangents not parallel at touch: cross={cross:.2e}"
    return gap, cross


# ---------------------------------------------------------------------------
# arc / circle ↔ spline
# ---------------------------------------------------------------------------

def test_tangent_circle_spline_converges():
    # Fixed circle r=10 at origin; a 3-point spline with pinned ends arching above.
    # The free middle point is driven down so the spline grazes the circle.
    circ = _circle("e1", 0.0, 0.0, 10.0)
    spl = _spline("e2", [(-20.0, 14.0), (0.0, 12.0), (20.0, 14.0)])
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1"]},
        {"id": "c2", "type": "fix", "refs": ["e2.p0"]},
        {"id": "c3", "type": "fix", "refs": ["e2.p2"]},
        {"id": "c4", "type": "tangent", "refs": ["e2", "e1"]},
    ]
    sol = _solve([circ, spl], cons)
    assert sol.converged, sol
    gap, cross = _assert_tangent(sol, "e1", "circle", "e2", "spline")
    # the middle point should land on the circle (its y ~ +10 above the center)
    mid = sol.entities["e2"]["points"][1]
    assert abs(math.hypot(mid[0], mid[1]) - 10.0) < 1e-3, f"midpoint off-circle: {mid}"


def test_tangent_arc_spline_converges():
    # Same idea but a (range-free) ARC instead of a full circle.
    arc = _arc("e1", 0.0, 0.0, 8.0, a0=0.0, a1=180.0)
    spl = _spline("e2", [(-15.0, 13.0), (0.0, 10.5), (15.0, 13.0)])
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": _radius("x", "e1", 8.0)["type"], "refs": ["e1"],
         "value": {"value": 8.0, "expr": None}},
        {"id": "c3", "type": "fix", "refs": ["e2.p0"]},
        {"id": "c4", "type": "fix", "refs": ["e2.p2"]},
        {"id": "c5", "type": "tangent", "refs": ["e1", "e2"]},
    ]
    sol = _solve([arc, spl], cons)
    assert sol.converged, sol
    _assert_tangent(sol, "e1", "arc", "e2", "spline")


def test_tangent_spline_drops_one_dof():
    # tangent is ONE scalar equation -> removes exactly 1 dof from a free system.
    circ = _circle("e1", 0.0, 0.0, 10.0)
    spl = _spline("e2", [(-20.0, 14.0), (0.0, 12.0), (20.0, 14.0)])
    free = _solve([circ, spl], [])
    assert free.dof == 3 + 6, free.dof          # circle 3 + 3-pt spline 6
    tan = _solve([circ, spl],
                 [{"id": "c1", "type": "tangent", "refs": ["e2", "e1"]}])
    assert tan.converged, tan
    assert tan.dof == free.dof - 1, f"tangent dof {tan.dof} (free {free.dof})"


# ---------------------------------------------------------------------------
# spline ↔ spline
# ---------------------------------------------------------------------------

def test_tangent_spline_spline_converges():
    # A fixed lower hump; an upper hump with pinned ends pulled down until it kisses.
    s1 = _spline("e1", [(-20.0, 0.0), (0.0, 10.0), (20.0, 0.0)])
    s2 = _spline("e2", [(-20.0, 20.0), (0.0, 16.0), (20.0, 20.0)])
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.p0"]},
        {"id": "c2", "type": "fix", "refs": ["e1.p1"]},
        {"id": "c3", "type": "fix", "refs": ["e1.p2"]},
        {"id": "c4", "type": "fix", "refs": ["e2.p0"]},
        {"id": "c5", "type": "fix", "refs": ["e2.p2"]},
        {"id": "c6", "type": "tangent", "refs": ["e1", "e2"]},
    ]
    sol = _solve([s1, s2], cons)
    assert sol.converged, sol
    _assert_tangent(sol, "e1", "spline", "e2", "spline")


def test_tangent_spline_spline_drops_one_dof():
    s1 = _spline("e1", [(-20.0, 0.0), (0.0, 10.0), (20.0, 0.0)])
    s2 = _spline("e2", [(-20.0, 20.0), (0.0, 16.0), (20.0, 20.0)])
    free = _solve([s1, s2], [])
    assert free.dof == 6 + 6, free.dof
    tan = _solve([s1, s2],
                 [{"id": "c1", "type": "tangent", "refs": ["e1", "e2"]}])
    assert tan.converged, tan
    assert tan.dof == free.dof - 1, f"spline-spline tangent dof {tan.dof}"


def test_tangent_style_spline_arc_converges():
    # style_spline (control-pole curve) ↔ arc: pole-curve pulled tangent to an arc.
    arc = _arc("e1", 0.0, 0.0, 10.0, a0=0.0, a1=180.0)
    ss = _style_spline("e2", [(-18.0, 16.0), (0.0, 22.0), (18.0, 16.0)])
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": "radius", "refs": ["e1"],
         "value": {"value": 10.0, "expr": None}},
        {"id": "c3", "type": "fix", "refs": ["e2.p0"]},
        {"id": "c4", "type": "fix", "refs": ["e2.p2"]},
        {"id": "c5", "type": "tangent", "refs": ["e1", "e2"]},
    ]
    sol = _solve([arc, ss], cons)
    assert sol.converged, sol
    _assert_tangent(sol, "e1", "arc", "e2", "style_spline", touch_tol=1e-3, par_tol=1e-3)


# ---------------------------------------------------------------------------
# arc ↔ arc, circle ↔ arc (dedicated circle-circle analytic branch — verify intact)
# ---------------------------------------------------------------------------

def test_tangent_arc_arc_external():
    # Two arcs, centers fixed 25 apart, r0 pinned to 10; external tangency forces
    # r0 + r1 == 25  ->  r1 == 15.
    a0 = _arc("e1", 0.0, 0.0, 10.0, a0=0.0, a1=180.0)
    a1 = _arc("e2", 25.0, 0.0, 10.0, a0=90.0, a1=270.0)
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": "fix", "refs": ["e2.center"]},
        {"id": "c3", "type": "radius", "refs": ["e1"],
         "value": {"value": 10.0, "expr": None}},
        {"id": "c4", "type": "tangent", "refs": ["e1", "e2"]},
    ]
    sol = _solve([a0, a1], cons)
    assert sol.converged, sol
    r0 = sol.entities["e1"]["radius"]
    r1 = sol.entities["e2"]["radius"]
    assert abs((r0 + r1) - 25.0) < 1e-6, f"external tangency r0+r1={r0 + r1} (want 25)"
    assert abs(r0 - 10.0) < 1e-6 and abs(r1 - 15.0) < 1e-6, (r0, r1)


def test_tangent_circle_arc_internal():
    # A small arc nested inside a big circle (internal tangency): big circle r=20 at
    # origin (fixed), small arc center at (12,0) fixed, big radius pinned; internal
    # tangency forces |C0-C1| == |r0 - r1|  ->  12 == |20 - r1|  ->  r1 == 8.
    circ = _circle("e1", 0.0, 0.0, 20.0)
    arc = _arc("e2", 12.0, 0.0, 6.0, a0=0.0, a1=360.0)
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": "fix", "refs": ["e2.center"]},
        {"id": "c3", "type": "radius", "refs": ["e1"],
         "value": {"value": 20.0, "expr": None}},
        {"id": "c4", "type": "tangent", "refs": ["e1", "e2"]},
    ]
    sol = _solve([circ, arc], cons)
    assert sol.converged, sol
    r1 = sol.entities["e2"]["radius"]
    assert abs(r1 - 8.0) < 1e-6, f"internal tangency r1={r1} (want 8)"


# ---------------------------------------------------------------------------
# line ↔ spline
# ---------------------------------------------------------------------------

def test_tangent_line_spline_converges():
    # A horizontal line pulled down onto the top of a fixed spline hump: at tangency
    # the line grazes the hump's apex (the curve's +y extreme).
    spl = _spline("e1", [(-20.0, 0.0), (0.0, 12.0), (20.0, 0.0)])
    ln = _line("e2", (-30.0, 18.0), (30.0, 18.0))
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.p0"]},
        {"id": "c2", "type": "fix", "refs": ["e1.p1"]},
        {"id": "c3", "type": "fix", "refs": ["e1.p2"]},
        {"id": "c4", "type": "horizontal", "refs": ["e2"]},
        {"id": "c5", "type": "tangent", "refs": ["e2", "e1"]},
    ]
    sol = _solve([spl, ln], cons)
    assert sol.converged, sol
    # the line's y must reach the spline apex y (=12 at the fixed middle point)
    ay = sol.entities["e2"]["p1"][1]
    by = sol.entities["e2"]["p2"][1]
    assert abs(ay - by) < 1e-7, "line not horizontal"
    assert abs(ay - 12.0) < 1e-4, f"line not grazing spline apex y=12: ay={ay}"


def test_tangent_line_spline_drops_one_dof():
    spl = _spline("e1", [(-20.0, 0.0), (0.0, 12.0), (20.0, 0.0)])
    ln = _line("e2", (-30.0, 18.0), (30.0, 18.0))
    free = _solve([spl, ln], [])
    assert free.dof == 6 + 4, free.dof
    tan = _solve([spl, ln],
                 [{"id": "c1", "type": "tangent", "refs": ["e2", "e1"]}])
    assert tan.converged, tan
    assert tan.dof == free.dof - 1, f"line-spline tangent dof {tan.dof}"


# ---------------------------------------------------------------------------
# regression: an unsupported pair still skips silently (no residual, no crash)
# ---------------------------------------------------------------------------

def test_tangent_line_line_still_skips():
    # line↔line tangent is ill-defined: it must add no residual, remove no dof, and
    # never raise (forward-tolerant), exactly as before.
    l0 = _line("e1", (0.0, 0.0), (10.0, 0.0))
    l1 = _line("e2", (0.0, 5.0), (10.0, 7.0))
    free = _solve([l0, l1], [])
    sol = _solve([l0, l1],
                 [{"id": "c1", "type": "tangent", "refs": ["e1", "e2"]}])
    assert sol.converged, sol
    assert sol.dof == free.dof, f"line-line tangent changed dof to {sol.dof}"


def test_curve_descriptor_covers_new_types():
    # _allocate_entity / _resolve_handle / _extract_entities stay in sync: every
    # type the descriptor recognizes must round-trip through a solve without raising.
    import numpy as _np
    for ent in (
        _circle("e1", 0.0, 0.0, 5.0),
        _arc("e1", 0.0, 0.0, 5.0),
        _spline("e1", [(0.0, 0.0), (5.0, 5.0), (10.0, 0.0)]),
        _style_spline("e1", [(0.0, 0.0), (5.0, 5.0), (10.0, 0.0)]),
    ):
        sol = _solve([ent], [])
        assert sol.converged
        vidx = sol.state.var_map["e1"]
        desc = _curve_descriptor(ent["type"], vidx)
        assert desc is not None, ent["type"]


# ---------------------------------------------------------------------------
# main() runner (house style, parallels test_nsolve.py)
# ---------------------------------------------------------------------------

def main() -> None:
    test_tangent_circle_spline_converges()
    test_tangent_arc_spline_converges()
    test_tangent_spline_drops_one_dof()
    test_tangent_spline_spline_converges()
    test_tangent_spline_spline_drops_one_dof()
    test_tangent_style_spline_arc_converges()
    test_tangent_arc_arc_external()
    test_tangent_circle_arc_internal()
    test_tangent_line_spline_converges()
    test_tangent_line_spline_drops_one_dof()
    test_tangent_line_line_still_skips()
    test_curve_descriptor_covers_new_types()
    print("ALL SKETCH TANGENT TESTS PASSED")


if __name__ == "__main__":
    main()
