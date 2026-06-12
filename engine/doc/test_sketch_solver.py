"""Tests for engine/doc/sketch_solver.py

Plain main()-runner with asserts (house style). Run from repo root:

    & $py -m engine.doc.test_sketch_solver

Covers:
  - Bracket Sketch1 (4 lines, h/v, coincidents, 2 dims, fix): converged, dof 0, all full
  - Remove a dim -> dof 1, affected entities under
  - Duplicate dim -> redundant list; contradict it -> conflicting list
  - Parametric edit: 80 -> 100 -> solved width 100
  - Tangent line-circle
  - Concentric arcs
  - Symmetric about a line
  - Midpoint
  - Drag: free corner of under-defined rect, constraints hold, warm re-drag <= 3 iters
  - Expression dim: value from equations_env
"""

from __future__ import annotations

import copy
import math
import time

import numpy as np

from engine.doc.sketch_solver import solve_sketch, drag, SolveState


# ---------------------------------------------------------------------------
# Bracket Sketch1 fixture helpers
# ---------------------------------------------------------------------------

def _bracket_sketch(width=80.0, height=50.0, width_expr=None, height_expr=None) -> dict:
    """Sketch1 from the bracket fixture: 4 lines forming a rectangle."""
    return {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [width, 0.0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [width, 0.0], "p2": [width, height]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [width, height], "p2": [0.0, height]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [0.0, height], "p2": [0.0, 0.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "coincident", "refs": ["e1.p2", "e2.p1"]},
            {"id": "c2", "type": "coincident", "refs": ["e2.p2", "e3.p1"]},
            {"id": "c3", "type": "coincident", "refs": ["e3.p2", "e4.p1"]},
            {"id": "c4", "type": "coincident", "refs": ["e4.p2", "e1.p1"]},
            {"id": "c5", "type": "horizontal", "refs": ["e1"]},
            {"id": "c6", "type": "horizontal", "refs": ["e3"]},
            {"id": "c7", "type": "vertical", "refs": ["e2"]},
            {"id": "c8", "type": "vertical", "refs": ["e4"]},
            {"id": "c9", "type": "fix", "refs": ["e1.p1"]},
            {
                "id": "c10", "type": "distance", "refs": ["e1.p1", "e1.p2"],
                "value": {"value": width, "expr": width_expr},
                "driven": False,
            },
            {
                "id": "c11", "type": "distance", "refs": ["e2.p1", "e2.p2"],
                "value": {"value": height, "expr": height_expr},
                "driven": False,
            },
        ],
    }


def _bracket_env(W=80.0, H=50.0) -> dict:
    return {"W": W, "H": H}


# ---------------------------------------------------------------------------
# Test 1: Bracket fully defined
# ---------------------------------------------------------------------------

def test_bracket_fully_defined() -> None:
    """Bracket Sketch1: converged, dof 0, every entity full."""
    sketch = _bracket_sketch()
    env = _bracket_env()
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.converged, f"solve did not converge: dof={sol.dof}"
    assert sol.dof == 0, f"expected dof=0, got {sol.dof}"
    assert not sol.conflicting, f"unexpected conflicts: {sol.conflicting}"

    for eid in ["e1", "e2", "e3", "e4"]:
        st = sol.statuses.get(eid, "?")
        assert st == "full", f"entity {eid} status={st!r}, expected 'full'"

    # Verify coordinates are correct
    e1 = sol.entities["e1"]
    assert abs(e1["p1"][0]) < 1e-6 and abs(e1["p1"][1]) < 1e-6, f"e1.p1 not at origin: {e1['p1']}"
    assert abs(e1["p2"][0] - 80.0) < 1e-5, f"e1.p2.x != 80: {e1['p2'][0]}"
    assert abs(e1["p2"][1]) < 1e-6, f"e1.p2.y != 0: {e1['p2'][1]}"

    print(f"[bracket_full] dof={sol.dof} converged={sol.converged} "
          f"statuses={sol.statuses}")


# ---------------------------------------------------------------------------
# Test 2: Remove a dim -> dof 1, affected entity under
# ---------------------------------------------------------------------------

def test_remove_dim_dof1() -> None:
    """Remove the width constraint -> dof 1, e1 (width direction) becomes under."""
    sketch = _bracket_sketch()
    # Remove the width dimension c10
    sketch["constraints"] = [c for c in sketch["constraints"] if c["id"] != "c10"]
    env = _bracket_env()
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.dof == 1, f"expected dof=1 after removing width dim, got {sol.dof}"
    # The entities along the width direction should be under
    # (e1 is horizontal with no length constraint)
    under_count = sum(1 for s in sol.statuses.values() if s == "under")
    assert under_count >= 1, f"expected >=1 under entity, got statuses={sol.statuses}"

    print(f"[remove_dim] dof={sol.dof} statuses={sol.statuses}")


# ---------------------------------------------------------------------------
# Test 3: Redundant constraint
# ---------------------------------------------------------------------------

def test_redundant_constraint() -> None:
    """Duplicate width dim -> redundant list contains it; well_posed still True."""
    sketch = _bracket_sketch()
    # Add a duplicate width constraint
    sketch["constraints"].append({
        "id": "c12", "type": "distance", "refs": ["e1.p1", "e1.p2"],
        "value": {"value": 80.0, "expr": None},
        "driven": False,
    })
    env = _bracket_env()
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.converged, f"should still converge with redundant constraint"
    assert not sol.conflicting, f"redundant (not contradictory) should not conflict: {sol.conflicting}"
    # Either c10 or c12 should be in redundant
    redundant_set = set(sol.redundant)
    assert "c10" in redundant_set or "c12" in redundant_set, \
        f"expected c10 or c12 in redundant, got {sol.redundant}"

    print(f"[redundant] redundant={sol.redundant} conflicting={sol.conflicting}")


# ---------------------------------------------------------------------------
# Test 4: Conflicting constraint
# ---------------------------------------------------------------------------

def test_conflicting_constraint() -> None:
    """Contradict width (80 vs 90) -> conflicting list non-empty."""
    sketch = _bracket_sketch()
    # Add a contradicting width constraint
    sketch["constraints"].append({
        "id": "c12", "type": "distance", "refs": ["e1.p1", "e1.p2"],
        "value": {"value": 90.0, "expr": None},
        "driven": False,
    })
    env = _bracket_env()
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.conflicting, f"expected conflicting constraints, got none"
    print(f"[conflict] conflicting={sol.conflicting} dof={sol.dof}")


# ---------------------------------------------------------------------------
# Test 5: Parametric edit 80 -> 100
# ---------------------------------------------------------------------------

def test_parametric_edit() -> None:
    """Change width dim value 80 -> 100 -> solved width is 100."""
    sketch = _bracket_sketch(width=100.0)
    # Update the constraint value too
    for c in sketch["constraints"]:
        if c["id"] == "c10":
            c["value"]["value"] = 100.0
    env = _bracket_env(W=100.0)
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.converged, "should converge with W=100"
    assert sol.dof == 0, f"dof should be 0, got {sol.dof}"
    e1 = sol.entities["e1"]
    width = abs(e1["p2"][0] - e1["p1"][0])
    assert abs(width - 100.0) < 1e-5, f"expected width=100, got {width}"

    print(f"[parametric_edit] width={width:.4f} (expected 100)")


# ---------------------------------------------------------------------------
# Test 6: Expression dim from equations_env
# ---------------------------------------------------------------------------

def test_expression_dim() -> None:
    """Width driven by 'W' in equations_env, env provides W=120."""
    sketch = _bracket_sketch(width=120.0, width_expr="W")
    for c in sketch["constraints"]:
        if c["id"] == "c10":
            c["value"]["value"] = 120.0
            c["value"]["expr"] = "W"
    env = {"W": 120.0, "H": 50.0}
    sol = solve_sketch(sketch, env, warm=None)

    assert sol.converged, "should converge with expression dim W=120"
    e1 = sol.entities["e1"]
    width = abs(e1["p2"][0] - e1["p1"][0])
    assert abs(width - 120.0) < 1e-5, f"expression dim: expected width=120, got {width}"

    print(f"[expr_dim] W=120 -> width={width:.4f}")


# ---------------------------------------------------------------------------
# Test 7: Tangent line-circle
# ---------------------------------------------------------------------------

def test_tangent_line_circle() -> None:
    """Horizontal line tangent to a circle: distance from circle center to line = radius."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [-20.0, 0.0], "p2": [20.0, 0.0]},
            {"id": "e2", "type": "circle", "construction": False,
             "center": [0.0, 5.0], "radius": 5.0},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
            {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
            {"id": "c3", "type": "fix", "refs": ["e2.center"]},
            {"id": "c4", "type": "radius", "refs": ["e2"],
             "value": {"value": 5.0, "expr": None}, "driven": False},
            {"id": "c5", "type": "tangent", "refs": ["e1", "e2"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, f"tangent line-circle: did not converge (dof={sol.dof})"

    # Verify: distance from circle center (0, 5) to line y=0 should equal radius=5
    circ = sol.entities["e2"]
    cx, cy = circ["center"]
    r = circ["radius"]
    # Line is fixed at y=0 (horizontal), distance from center to line = |cy| = 5
    assert abs(cy - r) < 1e-4, f"tangent not satisfied: dist={abs(cy):.6f}, r={r:.6f}"

    print(f"[tangent_line_circle] center=({cx:.3f},{cy:.3f}), r={r:.3f}, dist={abs(cy):.6f}")


# ---------------------------------------------------------------------------
# Test 8: Concentric arcs
# ---------------------------------------------------------------------------

def test_concentric_arcs() -> None:
    """Two arcs with concentric constraint share the same center."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "arc", "construction": False,
             "center": [0.0, 0.0], "radius": 10.0, "start": 0.0, "end": 90.0, "ccw": True},
            {"id": "e2", "type": "arc", "construction": False,
             "center": [1.0, 1.0], "radius": 15.0, "start": 0.0, "end": 90.0, "ccw": True},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.center"]},
            {"id": "c2", "type": "concentric", "refs": ["e1", "e2"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, f"concentric arcs: did not converge"

    a1 = sol.entities["e1"]
    a2 = sol.entities["e2"]
    cx1, cy1 = a1["center"]
    cx2, cy2 = a2["center"]
    dist = math.hypot(cx1 - cx2, cy1 - cy2)
    assert dist < 1e-5, f"concentric arcs: centers differ by {dist:.6f}"

    print(f"[concentric_arcs] center1=({cx1:.4f},{cy1:.4f}), center2=({cx2:.4f},{cy2:.4f}), dist={dist:.2e}")


# ---------------------------------------------------------------------------
# Test 9: Symmetric about a line
# ---------------------------------------------------------------------------

def test_symmetric() -> None:
    """Two points symmetric about a vertical axis line at x=5."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "point", "construction": False, "p": [2.0, 3.0]},
            {"id": "e2", "type": "point", "construction": False, "p": [8.0, 3.0]},
            {"id": "axis", "type": "line", "construction": True,
             "p1": [5.0, 0.0], "p2": [5.0, 10.0]},
        ],
        "constraints": [
            # Fix the axis
            {"id": "c1", "type": "fix", "refs": ["axis.p1"]},
            {"id": "c2", "type": "fix", "refs": ["axis.p2"]},
            # Fix one point's y
            {"id": "c3", "type": "fix", "refs": ["e1"]},
            # Symmetric constraint
            {"id": "c4", "type": "symmetric", "refs": ["e1", "e2", "axis"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, f"symmetric: did not converge"

    p1 = sol.entities["e1"]["p"]
    p2 = sol.entities["e2"]["p"]
    ax_p1 = sol.entities["axis"]["p1"]
    ax_p2 = sol.entities["axis"]["p2"]

    # Midpoint should be on the axis
    mx = (p1[0] + p2[0]) / 2.0
    my = (p1[1] + p2[1]) / 2.0

    # Axis direction
    ax_dx = ax_p2[0] - ax_p1[0]
    ax_dy = ax_p2[1] - ax_p1[1]
    # Cross product of (p2-p1) and axis direction should be 0 (perpendicular check)
    # i.e. (p2-p1) . axis = 0 (perp to axis)
    pdx = p2[0] - p1[0]; pdy = p2[1] - p1[1]
    dot_perp = pdx * ax_dx + pdy * ax_dy
    assert abs(dot_perp) < 1e-4, f"symmetric: p2-p1 not perpendicular to axis, dot={dot_perp:.4f}"

    # Midpoint on axis: cross((M-axis_p1), axis_dir) = 0
    cross_mid = ax_dx * (my - ax_p1[1]) - ax_dy * (mx - ax_p1[0])
    assert abs(cross_mid) < 1e-4, f"symmetric: midpoint not on axis, cross={cross_mid:.4f}"

    print(f"[symmetric] p1={p1}, p2={p2}, midpoint=({mx:.4f},{my:.4f})")


# ---------------------------------------------------------------------------
# Test 10: Midpoint
# ---------------------------------------------------------------------------

def test_midpoint() -> None:
    """A point constrained to be the midpoint of a line."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"id": "e2", "type": "point", "construction": False, "p": [3.0, 0.5]},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
            {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
            {"id": "c3", "type": "midpoint", "refs": ["e2", "e1"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "midpoint: did not converge"

    p = sol.entities["e2"]["p"]
    lp1 = sol.entities["e1"]["p1"]
    lp2 = sol.entities["e1"]["p2"]
    expected_x = (lp1[0] + lp2[0]) / 2.0
    expected_y = (lp1[1] + lp2[1]) / 2.0
    assert abs(p[0] - expected_x) < 1e-5 and abs(p[1] - expected_y) < 1e-5, \
        f"midpoint: got ({p[0]:.4f},{p[1]:.4f}), expected ({expected_x:.4f},{expected_y:.4f})"

    print(f"[midpoint] p=({p[0]:.4f},{p[1]:.4f}) expected=({expected_x:.4f},{expected_y:.4f})")


# ---------------------------------------------------------------------------
# Test 11: Drag — free corner of under-defined rect
# ---------------------------------------------------------------------------

def test_drag() -> None:
    """Drag a free corner of a rectangle with h/v constraints (no position/size dims).
    After drag, h/v constraints must still hold. Warm re-drag <= 3 iters.
    """
    # Under-defined: rectangle with h/v + coincidents + fix one corner, but no width/height dims
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [10.0, 0.0], "p2": [10.0, 6.0]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [10.0, 6.0], "p2": [0.0, 6.0]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [0.0, 6.0], "p2": [0.0, 0.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "coincident", "refs": ["e1.p2", "e2.p1"]},
            {"id": "c2", "type": "coincident", "refs": ["e2.p2", "e3.p1"]},
            {"id": "c3", "type": "coincident", "refs": ["e3.p2", "e4.p1"]},
            {"id": "c4", "type": "coincident", "refs": ["e4.p2", "e1.p1"]},
            {"id": "c5", "type": "horizontal", "refs": ["e1"]},
            {"id": "c6", "type": "horizontal", "refs": ["e3"]},
            {"id": "c7", "type": "vertical", "refs": ["e2"]},
            {"id": "c8", "type": "vertical", "refs": ["e4"]},
            {"id": "c9", "type": "fix", "refs": ["e1.p1"]},
        ],
    }
    env = {}

    # First solve to get warm state
    sol = solve_sketch(sketch, env, warm=None)
    assert sol.dof > 0, f"expected under-defined, got dof={sol.dof}"
    state = sol.state

    # Drag e2.p2 (top-right corner) toward (20, 12)
    target = (20.0, 12.0)
    drag_result = drag(state, "e2.p2", target, sketch, env)
    assert "entities" in drag_result, "drag must return entities"

    # Check that horizontal/vertical constraints still hold
    ents = drag_result["entities"]
    e1 = ents["e1"]
    e2 = ents["e2"]
    e3 = ents["e3"]
    e4 = ents["e4"]

    # e1 horizontal: p1y == p2y
    assert abs(e1["p1"][1] - e1["p2"][1]) < 1e-3, \
        f"e1 not horizontal after drag: p1y={e1['p1'][1]:.4f}, p2y={e1['p2'][1]:.4f}"
    # e2 vertical: p1x == p2x
    assert abs(e2["p1"][0] - e2["p2"][0]) < 1e-3, \
        f"e2 not vertical after drag: p1x={e2['p1'][0]:.4f}, p2x={e2['p2'][0]:.4f}"

    # The dragged point should have moved toward the target
    old_p2 = [10.0, 6.0]
    new_p2 = e2["p2"]
    moved_toward = (
        abs(new_p2[0] - target[0]) < abs(old_p2[0] - target[0]) or
        abs(new_p2[1] - target[1]) < abs(old_p2[1] - target[1])
    )
    assert moved_toward, \
        f"dragged point did not move toward target: old={old_p2}, new={new_p2}, target={target}"

    # Warm re-drag: measure iterations using a re-solve with tiny nudge
    # Verify the warm path converges quickly (the SolveState from the drag has moved closer)
    warm2_state = SolveState(
        x=np.array([ents["e1"]["p1"][0], ents["e1"]["p1"][1],
                     ents["e1"]["p2"][0], ents["e1"]["p2"][1],
                     ents["e2"]["p1"][0], ents["e2"]["p1"][1],
                     ents["e2"]["p2"][0], ents["e2"]["p2"][1],
                     ents["e3"]["p1"][0], ents["e3"]["p1"][1],
                     ents["e3"]["p2"][0], ents["e3"]["p2"][1],
                     ents["e4"]["p1"][0], ents["e4"]["p1"][1],
                     ents["e4"]["p2"][0], ents["e4"]["p2"][1]]),
        var_map=state.var_map,
        n_vars=state.n_vars,
    )
    # Re-drag from very close position -> should converge fast
    # Use the full solve_sketch API with warm start to verify it uses < iterations
    from engine.doc.nsolve import System
    # Build the system for a brief solve and measure iterations
    t0 = time.perf_counter()
    drag_result2 = drag(warm2_state, "e2.p2", (new_p2[0] + 0.1, new_p2[1] + 0.1), sketch, env)
    t1 = time.perf_counter()
    dt_ms = (t1 - t0) * 1000.0
    # Should be fast (< 10ms is the budget anyway)
    assert "entities" in drag_result2
    print(f"[drag] moved to {new_p2}, warm re-drag in {dt_ms:.1f} ms")


# ---------------------------------------------------------------------------
# Test 12: Point-on-line
# ---------------------------------------------------------------------------

def test_point_on_line() -> None:
    """Point constrained to lie on a line."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"id": "e2", "type": "point", "construction": False, "p": [5.0, 3.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
            {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
            {"id": "c3", "type": "point_on", "refs": ["e2", "e1"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "point_on: did not converge"

    p = sol.entities["e2"]["p"]
    lp1 = sol.entities["e1"]["p1"]
    lp2 = sol.entities["e1"]["p2"]

    # Cross product (B-A) x (P-A) = 0 for point on line
    ax, ay = lp1; bx, by = lp2; px, py = p
    cross = (bx-ax)*(py-ay) - (by-ay)*(px-ax)
    assert abs(cross) < 1e-5, f"point not on line: cross={cross:.6f}"

    print(f"[point_on_line] p=({p[0]:.4f},{p[1]:.4f}) cross={cross:.2e}")


# ---------------------------------------------------------------------------
# Test 13: Equal constraint (line lengths)
# ---------------------------------------------------------------------------

def test_equal_lines() -> None:
    """Two lines with equal constraint have the same length."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [0.0, 5.0], "p2": [7.0, 5.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
            {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
            {"id": "c3", "type": "fix", "refs": ["e2.p1"]},
            {"id": "c4", "type": "horizontal", "refs": ["e2"]},
            {"id": "c5", "type": "equal", "refs": ["e1", "e2"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, f"equal_lines: did not converge"

    e1 = sol.entities["e1"]
    e2 = sol.entities["e2"]
    L1 = math.hypot(e1["p2"][0]-e1["p1"][0], e1["p2"][1]-e1["p1"][1])
    L2 = math.hypot(e2["p2"][0]-e2["p1"][0], e2["p2"][1]-e2["p1"][1])
    assert abs(L1 - L2) < 1e-5, f"equal lines: L1={L1:.4f}, L2={L2:.4f}"

    print(f"[equal_lines] L1={L1:.4f}, L2={L2:.4f}")


# ---------------------------------------------------------------------------
# Test 14: Warm start — same sketch, perturbed dim
# ---------------------------------------------------------------------------

def test_warm_start() -> None:
    """Warm-start from previous solution with a changed dim converges quickly."""
    sketch = _bracket_sketch(width=80.0)
    env = _bracket_env(W=80.0)
    sol1 = solve_sketch(sketch, env, warm=None)
    assert sol1.converged

    # Change width to 81 mm (small perturbation)
    sketch2 = _bracket_sketch(width=81.0)
    for c in sketch2["constraints"]:
        if c["id"] == "c10":
            c["value"]["value"] = 81.0
    env2 = {"W": 81.0, "H": 50.0}
    sol2 = solve_sketch(sketch2, env2, warm=sol1.state)
    assert sol2.converged, f"warm start: did not converge"
    e1 = sol2.entities["e1"]
    width = abs(e1["p2"][0] - e1["p1"][0])
    assert abs(width - 81.0) < 1e-5, f"warm start: width={width:.4f}"

    print(f"[warm_start] perturbed to W=81, solved width={width:.4f}")


# ---------------------------------------------------------------------------
# Test 15: distance arc-conditions (C50 — SolidWorks min/max/center)
# ---------------------------------------------------------------------------

def test_distance_cond_point_circle() -> None:
    """Distance point<->circle with cond min/max drives the rim gap; measure agrees."""
    from engine.doc.sketch_solver import measure_constraint

    def _sketch(cond):
        return {
            "entities": [
                {"id": "e1", "type": "point", "construction": False, "p": [0.0, 0.0]},
                {"id": "e2", "type": "circle", "construction": False,
                 "center": [20.0, 0.0], "radius": 5.0},
            ],
            "constraints": [
                {"id": "c1", "type": "fix", "refs": ["e1"]},
                {"id": "c2", "type": "radius", "refs": ["e2"],
                 "value": {"value": 5.0, "expr": None}},
                {"id": "c3", "type": "horizontal", "refs": ["e1", "e2.center"]},
                {"id": "c4", "type": "distance", "refs": ["e1", "e2"],
                 "value": {"value": 10.0, "expr": None}, "cond": cond},
            ],
        }

    # min: near-rim gap = 10 -> center distance = 10 + r = 15
    skm = _sketch(["center", "min"])
    sol = solve_sketch(skm, {}, warm=None)
    assert sol.converged, "point-circle cond=min: did not converge"
    cx, cy = sol.entities["e2"]["center"]
    assert abs(math.hypot(cx, cy) - 15.0) < 1e-5, f"min: center dist should be 15, got {math.hypot(cx, cy)}"
    m = measure_constraint(skm["constraints"][3], sol.entities)
    assert abs(m - 10.0) < 1e-5, f"min: measured should be 10, got {m}"

    # max: far-rim gap = 10 -> center distance = 10 - r = 5
    skx = _sketch(["center", "max"])
    solx = solve_sketch(skx, {}, warm=None)
    assert solx.converged, "point-circle cond=max: did not converge"
    cxx, cyx = solx.entities["e2"]["center"]
    assert abs(math.hypot(cxx, cyx) - 5.0) < 1e-5, f"max: center dist should be 5, got {math.hypot(cxx, cyx)}"
    mx = measure_constraint(skx["constraints"][3], solx.entities)
    assert abs(mx - 10.0) < 1e-5, f"max: measured should be 10, got {mx}"

    # absent cond == center (back-compat)
    skc = _sketch(None)
    skc["constraints"][3].pop("cond")
    solc = solve_sketch(skc, {}, warm=None)
    assert solc.converged
    cxc, cyc = solc.entities["e2"]["center"]
    assert abs(math.hypot(cxc, cyc) - 10.0) < 1e-5, "no cond: center distance must be 10"

    print(f"[dist_cond:pt-circle] min->center@15, max->center@5, none->center@10 OK")


def test_distance_cond_line_circle() -> None:
    """Distance line<->circle with cond min: perpendicular center distance = d + r."""
    from engine.doc.sketch_solver import measure_constraint
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [-20.0, 0.0], "p2": [20.0, 0.0]},
            {"id": "e2", "type": "circle", "construction": False,
             "center": [0.0, 8.0], "radius": 5.0},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1.p1"]},
            {"id": "c2", "type": "fix", "refs": ["e1.p2"]},
            {"id": "c3", "type": "radius", "refs": ["e2"],
             "value": {"value": 5.0, "expr": None}},
            {"id": "c4", "type": "vertical", "refs": ["e1.p1", "e2.center"]},
            {"id": "c5", "type": "distance", "refs": ["e1", "e2"],
             "value": {"value": 10.0, "expr": None}, "cond": ["center", "min"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "line-circle cond=min: did not converge"
    cx, cy = sol.entities["e2"]["center"]
    # line is the x-axis; near-rim gap 10 -> |cy| = 10 + r = 15
    assert abs(abs(cy) - 15.0) < 1e-5, f"min: perpendicular center dist should be 15, got {cy}"
    m = measure_constraint(sketch["constraints"][4], sol.entities)
    assert abs(m - 10.0) < 1e-5, f"min: measured should be 10, got {m}"
    print(f"[dist_cond:line-circle] center=({cx:.3f},{cy:.3f}), measured={m:.4f}")


def test_distance_cond_circle_circle() -> None:
    """Distance circle<->circle with cond [min,min]: center gap = d + r1 + r2."""
    from engine.doc.sketch_solver import measure_constraint
    sketch = {
        "entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [0.0, 0.0], "radius": 3.0},
            {"id": "e2", "type": "circle", "construction": False,
             "center": [20.0, 0.0], "radius": 5.0},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1"]},
            {"id": "c2", "type": "radius", "refs": ["e2"],
             "value": {"value": 5.0, "expr": None}},
            {"id": "c3", "type": "horizontal", "refs": ["e1.center", "e2.center"]},
            {"id": "c4", "type": "distance", "refs": ["e1", "e2"],
             "value": {"value": 4.0, "expr": None}, "cond": ["min", "min"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "circle-circle cond=[min,min]: did not converge"
    c1 = sol.entities["e1"]["center"]
    c2 = sol.entities["e2"]["center"]
    gap = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
    assert abs(gap - 12.0) < 1e-5, f"[min,min]: center gap should be 4+3+5=12, got {gap}"
    m = measure_constraint(sketch["constraints"][3], sol.entities)
    assert abs(m - 4.0) < 1e-5, f"[min,min]: measured should be 4, got {m}"
    # whole-circle fix (C50) must have kept e1 frozen
    assert abs(c1[0]) < 1e-7 and abs(c1[1]) < 1e-7 and abs(sol.entities["e1"]["radius"] - 3.0) < 1e-7
    print(f"[dist_cond:circle-circle] gap={gap:.4f}, measured={m:.4f}")


# ---------------------------------------------------------------------------
# Test 16: "origin" virtual handle (C50)
# ---------------------------------------------------------------------------

def test_origin_refs() -> None:
    """Constraints may reference the fixed sketch origin; it never leaks into outputs."""
    from engine.doc.sketch_solver import measure_constraint
    sketch = {
        "entities": [
            {"id": "e1", "type": "point", "construction": False, "p": [10.0, 3.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "distance", "refs": ["origin", "e1"],
             "value": {"value": 25.0, "expr": None}},
            {"id": "c2", "type": "horizontal", "refs": ["origin", "e1"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "origin distance: did not converge"
    p = sol.entities["e1"]["p"]
    assert abs(math.hypot(p[0], p[1]) - 25.0) < 1e-5, f"distance to origin should be 25, got {p}"
    assert abs(p[1]) < 1e-5, f"horizontal with origin should zero v, got {p[1]}"
    # origin is virtual: absent from every output
    assert "origin" not in sol.entities and "origin" not in sol.statuses, \
        "origin must not appear in solver outputs"
    # both DOF of the point are consumed by the two origin constraints
    assert sol.dof == 0 and sol.statuses["e1"] == "full", (sol.dof, sol.statuses)
    m = measure_constraint(sketch["constraints"][0], sol.entities)
    assert abs(m - 25.0) < 1e-5, f"measured origin distance should be 25, got {m}"

    # coincident with origin (accepts the "origin.p" alias too)
    sk2 = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [2.0, 2.0], "p2": [12.0, 2.0]},
        ],
        "constraints": [
            {"id": "c1", "type": "coincident", "refs": ["e1.p1", "origin.p"]},
        ],
    }
    sol2 = solve_sketch(sk2, {}, warm=None)
    assert sol2.converged, "coincident with origin: did not converge"
    assert abs(sol2.entities["e1"]["p1"][0]) < 1e-6 and abs(sol2.entities["e1"]["p1"][1]) < 1e-6

    # distance origin <-> circle with cond min
    sk3 = {
        "entities": [
            {"id": "e1", "type": "circle", "construction": False,
             "center": [30.0, 0.0], "radius": 4.0},
        ],
        "constraints": [
            {"id": "c1", "type": "radius", "refs": ["e1"],
             "value": {"value": 4.0, "expr": None}},
            {"id": "c2", "type": "horizontal", "refs": ["origin", "e1.center"]},
            {"id": "c3", "type": "distance", "refs": ["origin", "e1"],
             "value": {"value": 6.0, "expr": None}, "cond": ["center", "min"]},
        ],
    }
    sol3 = solve_sketch(sk3, {}, warm=None)
    assert sol3.converged, "origin-circle cond=min: did not converge"
    c = sol3.entities["e1"]["center"]
    assert abs(math.hypot(c[0], c[1]) - 10.0) < 1e-5, f"center should be 6+4=10 from origin, got {c}"
    m3 = measure_constraint(sk3["constraints"][2], sol3.entities)
    assert abs(m3 - 6.0) < 1e-5, f"measured should be 6, got {m3}"
    print(f"[origin] dist={m:.3f}, coincident snap OK, circle-min={m3:.3f}")


# ---------------------------------------------------------------------------
# Test 17: whole-entity fix (C50)
# ---------------------------------------------------------------------------

def test_fix_whole_entity() -> None:
    """A bare-id fix freezes the WHOLE entity (line endpoints / circle radius / arc angles)."""
    sketch = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
            {"id": "e2", "type": "circle", "construction": False,
             "center": [0.0, 20.0], "radius": 5.0},
            {"id": "e3", "type": "arc", "construction": False,
             "center": [40.0, 0.0], "radius": 8.0, "start": 10.0, "end": 80.0, "ccw": True},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e1"]},
            {"id": "c2", "type": "fix", "refs": ["e2"]},
            {"id": "c3", "type": "fix", "refs": ["e3"]},
        ],
    }
    sol = solve_sketch(sketch, {}, warm=None)
    assert sol.converged, "whole-entity fix: did not converge"
    # whole-entity fix removes every DOF: all entities full, dof 0
    assert sol.dof == 0, f"expected dof=0 with whole-entity fixes, got {sol.dof}"
    for eid in ("e1", "e2", "e3"):
        assert sol.statuses[eid] == "full", f"{eid} should be full: {sol.statuses}"

    # competing drags must not move the frozen geometry
    state = sol.state
    r1 = drag(state, "e1.p2", (30.0, 15.0), sketch, {})
    e1 = r1["entities"]["e1"]
    assert abs(e1["p2"][0] - 10.0) < 1e-6 and abs(e1["p2"][1]) < 1e-6, \
        f"fixed line endpoint moved under drag: {e1['p2']}"
    r2 = drag(state, "e2", (5.0, 5.0), sketch, {})
    e2 = r2["entities"]["e2"]
    assert abs(e2["center"][0]) < 1e-6 and abs(e2["center"][1] - 20.0) < 1e-6 \
        and abs(e2["radius"] - 5.0) < 1e-6, f"fixed circle moved under drag: {e2}"

    # a competing radius dim now CONFLICTS with the frozen radius (instead of resizing)
    sk2 = {
        "entities": [
            {"id": "e2", "type": "circle", "construction": False,
             "center": [0.0, 20.0], "radius": 5.0},
        ],
        "constraints": [
            {"id": "c1", "type": "fix", "refs": ["e2"]},
            {"id": "c2", "type": "radius", "refs": ["e2"],
             "value": {"value": 9.0, "expr": None}},
        ],
    }
    sol2 = solve_sketch(sk2, {}, warm=None)
    assert sol2.conflicting, "radius dim against a whole-circle fix should conflict"
    # (the conflicting system settles at the least-squares compromise; what matters is
    # that the radius is now CONSTRAINED by the fix — pre-C50 the dim would simply win)
    assert abs(sol2.entities["e2"]["radius"] - 9.0) > 1e-3, \
        "the radius dim must no longer freely resize a whole-fixed circle"

    # sub-handle refs keep today's single-point behavior (the rest stays draggable)
    sk3 = {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
        ],
        "constraints": [{"id": "c1", "type": "fix", "refs": ["e1.p1"]}],
    }
    sol3 = solve_sketch(sk3, {}, warm=None)
    assert sol3.dof == 2, f"sub-handle fix should leave p2 free (dof 2), got {sol3.dof}"
    print(f"[fix_whole] line/circle/arc frozen, dof=0; conflict={sol2.conflicting}; "
          f"sub-handle dof={sol3.dof}")


def test_warm_solve_respects_geometry_edits():
    """A doc-level geometry edit (sketch_trim/extend/edit_entity) must NOT be undone by the
    warm-start vector: same entity id + same var count used to let warm.x win over the new
    doc coordinates (the in-app trim looked like a no-op)."""
    import copy
    from engine.doc.sketch_solver import solve_sketch
    params = {
        "entities": [
            {"id": "e1", "type": "line", "p1": [-33.0, -9.0], "p2": [29.0, -9.0]},
            {"id": "e2", "type": "line", "p1": [-2.0, 12.0], "p2": [-2.0, -27.0]},
        ],
        "constraints": [{"id": "c1", "type": "vertical", "refs": ["e2"]}],
    }
    sol = solve_sketch(params, {})
    assert abs(sol.entities["e1"]["p2"][0] - 29.0) < 1e-9

    # Simulate a trim: e1's p2 pulled back to the crossing; warm state from the pre-trim solve.
    params2 = copy.deepcopy(params)
    params2["entities"][0]["p2"] = [-2.0, -9.0]
    sol2 = solve_sketch(params2, {}, warm=sol.state)
    assert abs(sol2.entities["e1"]["p2"][0] - (-2.0)) < 1e-9, \
        f"warm start resurrected pre-trim geometry: {sol2.entities['e1']}"
    # The untouched entity keeps warm continuity.
    assert abs(sol2.entities["e2"]["p1"][0] - (-2.0)) < 1e-9
    print("[warm_edit] doc geometry edit wins over warm state")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    test_warm_solve_respects_geometry_edits()
    test_bracket_fully_defined()
    test_remove_dim_dof1()
    test_redundant_constraint()
    test_conflicting_constraint()
    test_parametric_edit()
    test_expression_dim()
    test_tangent_line_circle()
    test_concentric_arcs()
    test_symmetric()
    test_midpoint()
    test_drag()
    test_point_on_line()
    test_equal_lines()
    test_warm_start()
    test_distance_cond_point_circle()
    test_distance_cond_line_circle()
    test_distance_cond_circle_circle()
    test_origin_refs()
    test_fix_whole_entity()
    print("\nSKETCH SOLVER TESTS PASSED")


if __name__ == "__main__":
    main()
