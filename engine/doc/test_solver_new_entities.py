"""Guard: the W23 sketch entities (text, eq_curve, style_spline) must survive the
INTERACTIVE constraint solver, not just the no-solve compile path.

Before the W23d solver hardening, ``eq_curve`` and ``style_spline`` were unknown to
``_allocate_entity`` (sketch_solver.py) and hit its ``raise ValueError("Unknown entity
type")``. That call is NOT wrapped in a try/except in ``solve_sketch`` (only per-constraint
registration is), so ANY live sketch containing one of these entities would crash the moment
the solver ran — i.e. on the first drag, dimension, or added relation. ``text`` was already
handled; these tests pin all three plus the mixed case.
"""

from __future__ import annotations

import math

from engine.doc.sketch_solver import solve_sketch


def _params(entities, constraints=None):
    return {"entities": entities, "constraints": constraints or []}


_CIRCLE = {"xt": "5*cos(t)", "yt": "5*sin(t)", "t1": 0.0, "t2": 2 * math.pi}


def test_solver_accepts_text_entity() -> None:
    sol = solve_sketch(_params([
        {"id": "e1", "type": "text", "construction": False,
         "p": [10.0, 5.0], "text": "HI", "height": 8.0}]), {})
    assert sol is not None


def test_solver_accepts_eq_curve_entity() -> None:
    sol = solve_sketch(_params([
        {"id": "e1", "type": "eq_curve", "construction": False, **_CIRCLE}]), {})
    assert sol is not None


def test_solver_accepts_style_spline_entity() -> None:
    sol = solve_sketch(_params([
        {"id": "e1", "type": "style_spline", "construction": False,
         "points": [[0.0, 0.0], [10.0, 20.0], [30.0, 10.0], [40.0, 0.0]]}]), {})
    assert sol is not None


def test_solver_mixed_sketch_does_not_crash() -> None:
    """A realistic mixed sketch: a line + all three new entities solved together."""
    ents = [
        {"id": "L1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [50.0, 0.0]},
        {"id": "T1", "type": "text", "construction": False, "p": [5.0, 10.0], "text": "X", "height": 5.0},
        {"id": "Q1", "type": "eq_curve", "construction": False, **_CIRCLE},
        {"id": "S1", "type": "style_spline", "construction": False,
         "points": [[0.0, 30.0], [10.0, 40.0], [20.0, 30.0]]},
    ]
    sol = solve_sketch(_params(ents), {})
    assert sol is not None


def test_solver_fix_style_spline_pole() -> None:
    """A Fix relation on a style_spline control pole resolves (poles are draggable DOF)."""
    ents = [{"id": "S1", "type": "style_spline", "construction": False,
             "points": [[0.0, 0.0], [10.0, 20.0], [30.0, 0.0]]}]
    cons = [{"id": "c1", "type": "fix", "refs": ["S1.p0"]}]
    sol = solve_sketch(_params(ents, cons), {})
    assert sol is not None


def test_solver_coincident_to_eq_curve_endpoint() -> None:
    """Coincident of a point onto the eq_curve start (a fixed reference) must not crash."""
    ents = [
        {"id": "P1", "type": "point", "construction": False, "p": [1.0, 1.0]},
        {"id": "Q1", "type": "eq_curve", "construction": False,
         "xt": "5*cos(t)", "yt": "5*sin(t)", "t1": 0.0, "t2": math.pi},
    ]
    cons = [{"id": "c1", "type": "coincident", "refs": ["P1.p", "Q1.start"]}]
    sol = solve_sketch(_params(ents, cons), {})
    assert sol is not None


def test_solver_forward_tolerant_to_unknown_entity() -> None:
    """SYSTEMIC GUARD: a future/unknown sketch entity type must degrade to 0-DOF (treated as
    fixed), NOT crash the whole solve — so a missed _allocate_entity wiring can't take down every
    drag/dimension/relation in the sketch. A real line alongside it still solves normally."""
    ents = [
        {"id": "L1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [10.0, 0.0]},
        {"id": "Z1", "type": "totally_new_future_entity", "construction": False, "blah": 1},
    ]
    sol = solve_sketch(_params(ents), {})
    assert sol is not None
