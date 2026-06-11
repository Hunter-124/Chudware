"""C32-B — tangent + point_on relations for the conic family.

Run (repo root, mock provider):
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" -m pytest \
        engine\\doc\\test_conic_relations.py -q

Covers the C28 conic-family entities (ellipse_arc, parabola, conic), which
before C32-B only honored coincident/fix (other relations were skipped silently):

  - line ↔ ellipse_arc tangent — EXACT support-function residual; converges to
    a true tangency (line touches the full ellipse at exactly one near-point)
  - point_on pulls a line endpoint onto a parabola (closest-param projection)
  - line ↔ conic tangent — NUMERIC residual; converges
  - point_on / tangent drop DOF by the expected amount (truthful accounting)
  - an unsupported pair (curve↔curve tangent on conics) still skips silently

The math is range-free for point_on/tangent on these types — it ignores the
a0/a1 (ellipse_arc) and t0/t1 (parabola) trim, mirroring how arc/circle
point_on uses the full circle today (see doc_schema.md §3.1).
"""

from __future__ import annotations

import math

import numpy as np

from engine.doc.sketch_solver import (
    solve_sketch,
    _ellipse_implicit,
    _parabola_curve,
    _parabola_project,
    _conic_curve,
    _conic_project,
    _signed_offset,
)


def _solve(entities, constraints):
    return solve_sketch({"entities": entities, "constraints": constraints}, {}, warm=None)


def _ellipse_arc(cx=0.0, cy=0.0, rx=30.0, ry=15.0, rot=0.0, a0=0.0, a1=360.0):
    return {"id": "e1", "type": "ellipse_arc", "construction": False,
            "center": [cx, cy], "rx": rx, "ry": ry, "rot": rot, "a0": a0, "a1": a1}


def _parabola(vx=0.0, vy=0.0, fx=0.0, fy=10.0, t0=-30.0, t1=30.0):
    return {"id": "e1", "type": "parabola", "construction": False,
            "vertex": [vx, vy], "focus": [fx, fy], "t0": t0, "t1": t1}


def _conic(p0=(-30.0, 0.0), p1=(30.0, 0.0), apex=(0.0, 25.0), rho=0.5):
    return {"id": "e1", "type": "conic", "construction": False,
            "p0": list(p0), "p1": list(p1), "apex": list(apex), "rho": rho}


def _line(eid, p1, p2):
    return {"id": eid, "type": "line", "construction": False,
            "p1": list(p1), "p2": list(p2)}


# ---------------------------------------------------------------------------
# line ↔ ellipse_arc tangent (EXACT support function)
# ---------------------------------------------------------------------------

def test_tangent_line_ellipse_arc_exact():
    # Fixed-shape ellipse (center fixed, a start point fixed -> rx pinned via x),
    # horizontal line pulled down onto it. The support residual is exact.
    ea = _ellipse_arc()
    ln = _line("e2", (-50.0, 25.0), (50.0, 25.0))
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": "fix", "refs": ["e1.start"]},   # locks (rx,0)
        {"id": "c3", "type": "horizontal", "refs": ["e2"]},
        {"id": "c4", "type": "tangent", "refs": ["e2", "e1"]},
    ]
    sol = _solve([ea, ln], cons)
    assert sol.converged, sol

    out_e = sol.entities["e1"]
    out_l = sol.entities["e2"]
    cx, cy = out_e["center"]
    rx, ry, rot = out_e["rx"], out_e["ry"], out_e["rot"]
    ax, ay = out_l["p1"]
    bx, by = out_l["p2"]

    # residual: |dist(center->line)| == support  (tangency)
    dxab, dyab = bx - ax, by - ay
    L = math.hypot(dxab, dyab)
    nx, ny = -dyab / L, dxab / L
    rr = rot * math.pi / 180.0
    cr, sr = math.cos(rr), math.sin(rr)
    nlx = cr * nx + sr * ny
    nly = -sr * nx + cr * ny
    support = math.hypot(rx * nlx, ry * nly)
    dist = abs((cx - ax) * nx + (cy - ay) * ny)
    assert abs(dist - support) < 1e-6, f"tangency residual {dist - support}"

    # the line must touch the FULL ellipse at exactly one near-point: sample the
    # implicit value of the closest ellipse point along the line — exactly one
    # sign-flat contact, no crossing into the interior.
    ell = np.array([cx, cy, rx, ry, rot])
    samples = [_ellipse_implicit(ell, np.array([ax + dxab * s, ay + dyab * s]))
               for s in np.linspace(0.0, 1.0, 201)]
    # tangent: the line stays on/outside the ellipse (implicit >= ~0 everywhere)
    assert min(samples) > -1e-4, f"line crosses into ellipse, min={min(samples)}"
    # and grazes it (some sample ~0)
    assert min(samples) < 1e-3, f"line does not touch ellipse, min={min(samples)}"


def test_tangent_line_ellipse_arc_drops_dof():
    # tangent is one scalar equation -> removes exactly 1 dof from a free system.
    ea = _ellipse_arc()
    ln = _line("e2", (-50.0, 25.0), (50.0, 25.0))
    free = _solve([ea, ln], [])
    assert free.dof == 7 + 4, free.dof          # ellipse_arc 7 + line 4
    tan = _solve([ea, ln], [{"id": "c1", "type": "tangent", "refs": ["e2", "e1"]}])
    assert tan.converged, tan
    assert tan.dof == free.dof - 1, f"tangent dof {tan.dof} (free {free.dof})"


# ---------------------------------------------------------------------------
# point_on a parabola (closest-param projection)
# ---------------------------------------------------------------------------

def test_point_on_parabola_pulls_endpoint():
    para = _parabola()
    ln = _line("e2", (15.0, 20.0), (40.0, 5.0))
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.vertex"]},
        {"id": "c2", "type": "fix", "refs": ["e2.p2"]},
        {"id": "c3", "type": "point_on", "refs": ["e2.p1", "e1"]},
    ]
    sol = _solve([para, ln], cons)
    assert sol.converged, sol

    out_p = sol.entities["e1"]
    P = np.array(sol.entities["e2"]["p1"])
    cv = np.array([0.0, 0.0, out_p["focus"][0], out_p["focus"][1]])
    t = _parabola_project(cv, P)
    Q, dQ = _parabola_curve(cv, t)
    assert abs(_signed_offset(P, Q, dQ)) < 1e-6, "endpoint not on parabola"


def test_point_on_parabola_drops_dof():
    para = _parabola()
    ln = _line("e2", (15.0, 20.0), (40.0, 5.0))
    free = _solve([para, ln], [])
    assert free.dof == 6 + 4, free.dof
    on = _solve([para, ln],
                [{"id": "c1", "type": "point_on", "refs": ["e2.p1", "e1"]}])
    assert on.converged, on
    assert on.dof == free.dof - 1, f"point_on dof {on.dof} (free {free.dof})"


def test_point_on_ellipse_arc_ignores_trim():
    # point_on uses the FULL rotated ellipse (range-free), like arc point_on.
    # Place a point off-curve, pull it on; the projected point's implicit ~0.
    ea = _ellipse_arc(a0=10.0, a1=80.0)   # narrow trim, but point_on uses full
    pt = {"id": "e2", "type": "point", "construction": False, "p": [40.0, 5.0]}
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.center"]},
        {"id": "c2", "type": "fix", "refs": ["e1.start"]},
        {"id": "c3", "type": "point_on", "refs": ["e2.p", "e1"]},
    ]
    sol = _solve([ea, pt], cons)
    assert sol.converged, sol
    out_e = sol.entities["e1"]
    P = np.array(sol.entities["e2"]["p"])
    ell = np.array([out_e["center"][0], out_e["center"][1],
                    out_e["rx"], out_e["ry"], out_e["rot"]])
    assert abs(_ellipse_implicit(ell, P)) < 1e-6, "point not on ellipse"


# ---------------------------------------------------------------------------
# line ↔ conic tangent (NUMERIC)
# ---------------------------------------------------------------------------

def test_tangent_line_conic_numeric():
    # Fully-pinned conic (both endpoints + apex fixed) with apex at y=25; a
    # horizontal line pulled onto it must graze the apex (the conic's +y extreme).
    con = _conic()
    ln = _line("e2", (-50.0, 30.0), (50.0, 30.0))
    cons = [
        {"id": "c1", "type": "fix", "refs": ["e1.start"]},
        {"id": "c2", "type": "fix", "refs": ["e1.end"]},
        {"id": "c3", "type": "fix", "refs": ["e1.apex"]},
        {"id": "c4", "type": "horizontal", "refs": ["e2"]},
        {"id": "c5", "type": "tangent", "refs": ["e2", "e1"]},
    ]
    sol = _solve([con, ln], cons)
    assert sol.converged, sol

    out_c = sol.entities["e1"]
    out_l = sol.entities["e2"]
    ax, ay = out_l["p1"]
    bx, by = out_l["p2"]
    dxab, dyab = bx - ax, by - ay
    L = math.hypot(dxab, dyab)
    nx, ny = -dyab / L, dxab / L
    cv = np.array([out_c["p0"][0], out_c["p0"][1], out_c["p1"][0], out_c["p1"][1],
                   out_c["apex"][0], out_c["apex"][1], out_c["rho"]])
    # closest curve point's signed distance to the line ~ 0 (tangency)
    from engine.doc.sketch_solver import _extremum_param
    p = _extremum_param(_conic_curve, cv, nx, ny, lambda c, n: np.linspace(0, 1, n))
    Q, _ = _conic_curve(cv, p)
    resid = (Q[0] - ax) * nx + (Q[1] - ay) * ny
    assert abs(resid) < 1e-5, f"conic tangency residual {resid}"


def test_tangent_line_conic_drops_dof():
    con = _conic()
    ln = _line("e2", (-50.0, 30.0), (50.0, 30.0))
    free = _solve([con, ln], [])
    assert free.dof == 7 + 4, free.dof
    tan = _solve([con, ln],
                 [{"id": "c1", "type": "tangent", "refs": ["e2", "e1"]}])
    assert tan.converged, tan
    assert tan.dof == free.dof - 1, f"conic tangent dof {tan.dof} (free {free.dof})"


# ---------------------------------------------------------------------------
# unsupported pair still skips silently
# ---------------------------------------------------------------------------

def test_unsupported_conic_curve_tangent_skips():
    # curve↔curve tangent on conics stays deferred: adds no residual, removes no
    # dof, and never raises. Two free conics: dof stays 14 with a tangent c.
    c0 = _conic()
    c1 = dict(_conic(), id="e2")
    free = _solve([c0, c1], [])
    assert free.dof == 14, free.dof
    sol = _solve([c0, c1],
                 [{"id": "c1", "type": "tangent", "refs": ["e1", "e2"]}])
    assert sol.converged, sol
    assert sol.dof == 14, f"unsupported tangent changed dof to {sol.dof}"


def test_unsupported_point_on_text_skips():
    # point_on against an entity type with no curve impl skips silently.
    txt = {"id": "e1", "type": "text", "construction": False,
           "p": [0.0, 0.0], "text": "x", "height": 5.0}
    pt = {"id": "e2", "type": "point", "construction": False, "p": [10.0, 10.0]}
    free = _solve([txt, pt], [])
    sol = _solve([txt, pt],
                 [{"id": "c1", "type": "point_on", "refs": ["e2.p", "e1"]}])
    assert sol.converged, sol
    assert sol.dof == free.dof, f"unsupported point_on changed dof to {sol.dof}"


def main() -> None:
    test_tangent_line_ellipse_arc_exact()
    test_tangent_line_ellipse_arc_drops_dof()
    test_point_on_parabola_pulls_endpoint()
    test_point_on_parabola_drops_dof()
    test_point_on_ellipse_arc_ignores_trim()
    test_tangent_line_conic_numeric()
    test_tangent_line_conic_drops_dof()
    test_unsupported_conic_curve_tangent_skips()
    test_unsupported_point_on_text_skips()
    print("ALL CONIC RELATION TESTS PASSED")


if __name__ == "__main__":
    main()
