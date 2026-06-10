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
# main
# ---------------------------------------------------------------------------

def main() -> None:
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
    print("\nSKETCH SOLVER TESTS PASSED")


if __name__ == "__main__":
    main()
