"""Tests for the shared numeric solver core (engine/doc/nsolve.py).

Plain main()-runner with asserts (house style, see engine/test_kernel_aliases.py),
NOT pytest. Run from repo root:

    & $py -m engine.doc.test_nsolve

The residual/Jacobian blocks here mimic what the sketch solver (C10) and mate
solver (C20) will register: each constraint is a small analytic block over a few
variables. NO finite differences — every jfunc is hand-derived. These doubles as
worked examples of the calling convention.
"""

from __future__ import annotations

import statistics
import time

import numpy as np

from engine.doc.nsolve import System


# -- reusable analytic constraint blocks (the kind C10/C20 will write) --------
#
# Convention: a 2-D point P occupies two consecutive variables (px, py). A block's
# var_idx lists exactly the variables it touches; rfunc/jfunc see x[var_idx] in
# that order.


def _coincident(sys, ia, ib, tag):
    """Two points equal: residual (ax-bx, ay-by). 4 vars: ax,ay,bx,by."""
    idx = [ia, ia + 1, ib, ib + 1]

    def r(x):
        return np.array([x[0] - x[2], x[1] - x[3]])

    def j(x):
        return np.array([[1.0, 0.0, -1.0, 0.0],
                         [0.0, 1.0, 0.0, -1.0]])

    sys.add_residual(r, idx, j, tag)


def _distance(sys, ia, ib, d, tag):
    """|P_a - P_b| = d. residual = dist - d. 4 vars."""
    idx = [ia, ia + 1, ib, ib + 1]

    def r(x):
        dx, dy = x[0] - x[2], x[1] - x[3]
        return np.array([np.hypot(dx, dy) - d])

    def j(x):
        dx, dy = x[0] - x[2], x[1] - x[3]
        L = np.hypot(dx, dy)
        if L < 1e-12:                      # guard the gradient at coincident pts
            return np.array([[0.0, 0.0, 0.0, 0.0]])
        return np.array([[dx / L, dy / L, -dx / L, -dy / L]])

    sys.add_residual(r, idx, j, tag)


def _fix_point(sys, ia, tx, ty, tag):
    """Pin a point: residual (ax-tx, ay-ty). 2 vars."""
    idx = [ia, ia + 1]

    def r(x):
        return np.array([x[0] - tx, x[1] - ty])

    def j(x):
        return np.array([[1.0, 0.0], [0.0, 1.0]])

    sys.add_residual(r, idx, j, tag)


def _point_on_line(sys, ip, ia, ib, tag):
    """Point P on the infinite line through A,B. residual = cross((B-A),(P-A)).
    6 vars: px,py,ax,ay,bx,by."""
    idx = [ip, ip + 1, ia, ia + 1, ib, ib + 1]

    def r(x):
        px, py, ax, ay, bx, by = x
        return np.array([(bx - ax) * (py - ay) - (by - ay) * (px - ax)])

    def j(x):
        px, py, ax, ay, bx, by = x
        # d/d[px,py,ax,ay,bx,by] of (bx-ax)(py-ay) - (by-ay)(px-ax)
        dpx = -(by - ay)
        dpy = (bx - ax)
        dax = -(py - ay) + (by - ay)      # from -(bx-ax)(py-ay)... expand carefully
        day = -(bx - ax) + (px - ax)
        dbx = (py - ay)
        dby = -(px - ax)
        return np.array([[dpx, dpy, dax, day, dbx, dby]])

    sys.add_residual(r, idx, j, tag)


# -- tests --------------------------------------------------------------------


def test_point_on_line_distance_and_warm_restart() -> None:
    """point-on-line + distance converges from a rough init; warm restart after a
    small perturbation takes <= 3 iterations."""
    sys = System()
    # A=(0,0) fixed, B=(10,0) fixed define a horizontal line; P starts off-line.
    A = sys.add_vars(2, [0.0, 0.0])
    B = sys.add_vars(2, [10.0, 0.0])
    P = sys.add_vars(2, [3.0, 4.0])        # rough init, off the line
    _fix_point(sys, int(A[0]), 0.0, 0.0, "fixA")
    _fix_point(sys, int(B[0]), 10.0, 0.0, "fixB")
    _point_on_line(sys, int(P[0]), int(A[0]), int(B[0]), "P_on_AB")
    _distance(sys, int(P[0]), int(A[0]), 7.0, "dist_PA_7")

    res = sys.solve()
    assert res.converged, f"cold solve failed: maxr={res.max_residual:.2e}"
    x = res.x
    px, py = x[int(P[0])], x[int(P[1])]
    assert abs(py) < 1e-7, f"P not on line y=0: py={py}"
    assert abs(np.hypot(px, py) - 7.0) < 1e-7, f"|PA| != 7: {np.hypot(px, py)}"

    # Warm restart: nudge the target distance constraint and re-solve from x.
    sys2 = System()
    A = sys2.add_vars(2, [0.0, 0.0])
    B = sys2.add_vars(2, [10.0, 0.0])
    P = sys2.add_vars(2, [px, py])
    _fix_point(sys2, int(A[0]), 0.0, 0.0, "fixA")
    _fix_point(sys2, int(B[0]), 10.0, 0.0, "fixB")
    _point_on_line(sys2, int(P[0]), int(A[0]), int(B[0]), "P_on_AB")
    _distance(sys2, int(P[0]), int(A[0]), 7.2, "dist_PA_72")    # small perturbation
    warm = sys2.solve(x0=sys2._x0(None))   # warm: start from last solved positions
    assert warm.converged, f"warm solve failed: maxr={warm.max_residual:.2e}"
    assert warm.iterations <= 3, f"warm restart took {warm.iterations} iters (>3)"
    print(f"[converge] cold {res.iterations} iters, warm restart {warm.iterations} iters")


def test_dof_free_fixed_and_rectangle() -> None:
    """DOF: free point in 2D = 2; fixed -> 0; rectangle-like 8-var/7-constraint -> 1."""
    # free point
    s1 = System()
    P = s1.add_vars(2, [1.0, 2.0])
    a1 = s1.analyze(s1._x0(None))
    assert a1.dof == 2, f"free point dof={a1.dof} (expected 2)"

    # fixed point
    s2 = System()
    P = s2.add_vars(2, [1.0, 2.0])
    _fix_point(s2, int(P[0]), 1.0, 2.0, "fix")
    a2 = s2.analyze(s2._x0(None))
    assert a2.dof == 0, f"fixed point dof={a2.dof} (expected 0)"

    # Rectangle-like system: 4 corners (8 vars). Constraints that leave exactly 1
    # DOF (independent rank 7): bottom edge horizontal, top edge horizontal, left
    # edge vertical, right edge vertical, + fix one corner (2) + one width dim.
    # That is 4 + 2 + 1 = 7 independent scalar constraints -> dof 1.
    s3 = System()
    P0 = s3.add_vars(2, [0.0, 0.0])    # bottom-left
    P1 = s3.add_vars(2, [10.0, 0.0])   # bottom-right
    P2 = s3.add_vars(2, [10.0, 6.0])   # top-right
    P3 = s3.add_vars(2, [0.0, 6.0])    # top-left
    i0, i1, i2, i3 = int(P0[0]), int(P1[0]), int(P2[0]), int(P3[0])

    def _horizontal(sys, ia, ib, tag):
        idx = [ia + 1, ib + 1]
        sys.add_residual(lambda x: np.array([x[0] - x[1]]),
                         idx, lambda x: np.array([[1.0, -1.0]]), tag)

    def _vertical(sys, ia, ib, tag):
        idx = [ia, ib]
        sys.add_residual(lambda x: np.array([x[0] - x[1]]),
                         idx, lambda x: np.array([[1.0, -1.0]]), tag)

    _horizontal(s3, i0, i1, "bottom_h")
    _horizontal(s3, i3, i2, "top_h")
    _vertical(s3, i0, i3, "left_v")
    _vertical(s3, i1, i2, "right_v")
    _fix_point(s3, i0, 0.0, 0.0, "fix_origin")        # 2 constraints
    _distance(s3, i0, i1, 10.0, "width")              # 1 dim
    a3 = s3.analyze(s3._x0(None))
    assert a3.rank == 7, f"rectangle rank={a3.rank} (expected 7)"
    assert a3.dof == 1, f"rectangle dof={a3.dof} (expected 1; one height DOF free)"
    print(f"[dof] free=2 fixed=0 rectangle dof={a3.dof} rank={a3.rank}")


def test_redundant_constraint() -> None:
    """Duplicate distance constraint -> its tag in redundant_tags, well_posed True."""
    s = System()
    A = s.add_vars(2, [0.0, 0.0])
    B = s.add_vars(2, [5.0, 0.0])
    _fix_point(s, int(A[0]), 0.0, 0.0, "fixA")
    _distance(s, int(A[0]), int(B[0]), 5.0, "dist1")
    _distance(s, int(A[0]), int(B[0]), 5.0, "dist2")   # identical -> redundant
    res = s.solve()
    a = s.analyze(res.x)
    assert a.well_posed, f"consistent redundancy should stay well_posed, conflicts={a.conflicting_tags}"
    flagged = set(a.redundant_tags)
    assert "dist2" in flagged and "dist1" not in flagged, \
        f"expected the later duplicate dist2 in redundant_tags, got {a.redundant_tags}"
    assert not a.conflicting_tags, f"unexpected conflicts: {a.conflicting_tags}"
    print(f"[redundant] redundant_tags={a.redundant_tags} well_posed={a.well_posed}")


def test_conflicting_constraints() -> None:
    """Contradictory distances (10 vs 12 on the same pair) -> conflicting_tags nonempty."""
    s = System()
    A = s.add_vars(2, [0.0, 0.0])
    B = s.add_vars(2, [10.0, 0.0])
    _fix_point(s, int(A[0]), 0.0, 0.0, "fixA")
    _distance(s, int(A[0]), int(B[0]), 10.0, "dist10")
    _distance(s, int(A[0]), int(B[0]), 12.0, "dist12")   # contradicts dist10
    res = s.solve()
    a = s.analyze(res.x)
    assert a.conflicting_tags, f"expected a conflict, got none (redundant={a.redundant_tags})"
    assert "dist12" in set(a.conflicting_tags), \
        f"expected the later contradictory dim dist12 to be flagged, got {a.conflicting_tags}"
    assert not a.well_posed, "system with contradiction must be not well_posed"
    print(f"[conflict] conflicting_tags={a.conflicting_tags} well_posed={a.well_posed}")


def test_soft_drag() -> None:
    """Soft target pulls a free endpoint; hard constraints win over the soft term."""
    s = System()
    A = s.add_vars(2, [0.0, 0.0])
    B = s.add_vars(2, [10.0, 0.0])    # free endpoint to drag
    _fix_point(s, int(A[0]), 0.0, 0.0, "fixA")
    _distance(s, int(A[0]), int(B[0]), 10.0, "len_10")   # hard: |AB| must stay 10
    # Drag B toward (20, 5): soft pull, low weight.
    s.soft_target(int(B[0]), 20.0, weight=0.01)
    s.soft_target(int(B[1]), 5.0, weight=0.01)
    res = s.solve(x0=s._x0(None))
    bx, by = res.x[int(B[0])], res.x[int(B[1])]
    L = np.hypot(bx, by)
    assert abs(L - 10.0) < 1e-4, f"hard length constraint lost: |AB|={L}"
    # B should move toward the target direction (positive x, positive y) but stay
    # on the radius-10 circle: it sits where the pull direction meets the circle.
    assert bx > 0 and by > 0, f"soft target did not pull B toward (20,5): B=({bx},{by})"
    # On the circle, the closest point to (20,5) is 10*(20,5)/|(20,5)|.
    tgt = np.array([20.0, 5.0]); expect = 10.0 * tgt / np.hypot(*tgt)
    assert np.allclose([bx, by], expect, atol=1e-3), \
        f"B={[bx,by]} not at circle point nearest target {expect.tolist()}"
    print(f"[soft] B=({bx:.4f},{by:.4f}) |AB|={L:.6f} (hard length held; soft pulled toward target)")


def test_time_budget_returns_best_so_far() -> None:
    """A tiny time budget makes solve return the best-so-far iterate (drag path),
    not converged, without raising."""
    N = 100
    sys = System()
    pts = [sys.add_vars(2, [float(k) * 0.3, 0.0]) for k in range(N)]   # bad init
    _fix_point(sys, int(pts[0][0]), 0.0, 0.0, "fix0")
    for k in range(N - 1):
        ia, ib = int(pts[k][0]), int(pts[k + 1][0])
        _distance(sys, ia, ib, 1.0, f"d{k}")
        sys.add_residual(lambda x: np.array([x[0] - x[1]]),
                         [ia + 1, ib + 1],
                         lambda x: np.array([[1.0, -1.0]]), f"h{k}")
    res = sys.solve(time_budget_ms=0.05)     # absurdly tight -> bails early
    assert not res.converged, "0.05 ms budget should not fully converge a 200-var solve"
    assert np.all(np.isfinite(res.x)), "best-so-far must be finite"
    # And with no budget it converges.
    full = sys.solve()
    assert full.converged, f"unbudgeted solve should converge: maxr={full.max_residual:.2e}"
    print(f"[budget] tight-budget converged={res.converged} maxr={res.max_residual:.2e}; "
          f"full converged in {full.iterations} iters")


def test_empty_and_degenerate() -> None:
    """Empty system and a system with no residuals don't crash."""
    s0 = System()
    r0 = s0.solve()
    assert r0.converged and r0.iterations == 0
    a0 = s0.analyze(np.zeros(0))
    assert a0.dof == 0 and a0.well_posed
    # vars but no constraints: all DOF free, trivially converged.
    s1 = System()
    s1.add_vars(4, [1.0, 2.0, 3.0, 4.0])
    r1 = s1.solve()
    assert r1.converged and r1.max_residual == 0.0
    a1 = s1.analyze(s1._x0(None))
    assert a1.dof == 4, f"4 free vars -> dof 4, got {a1.dof}"
    print("[degenerate] empty + constraint-free systems handled")


def test_performance_chain() -> None:
    """Synthetic chain of 100 points (~200 vars) with coincident+distance constraints:
    warm solve median over 20 runs. Print the number; assert < 50 ms (CI margin)."""
    N = 100

    def build():
        sys = System()
        pts = [sys.add_vars(2, [float(k), 0.0]) for k in range(N)]
        i0 = int(pts[0][0])
        _fix_point(sys, i0, 0.0, 0.0, "fix0")
        for k in range(N - 1):
            ia = int(pts[k][0]); ib = int(pts[k + 1][0])
            # each consecutive pair is unit-distance along the chain
            _distance(sys, ia, ib, 1.0, f"d{k}")
            # keep them collinear-ish via a horizontal relation on y
            sys.add_residual(lambda x: np.array([x[0] - x[1]]),
                             [ia + 1, ib + 1],
                             lambda x: np.array([[1.0, -1.0]]), f"h{k}")
        return sys

    # Cold solve once to get a solved configuration for warm starts.
    sys = build()
    cold = sys.solve()
    assert cold.converged, f"chain cold solve failed: maxr={cold.max_residual:.2e}"
    x_solved = cold.x.copy()
    nvars = sys.n_vars

    # Warm-solve timing: perturb slightly, warm-start from solved, measure.
    times_ms = []
    rng = np.random.default_rng(0)
    for _ in range(20):
        s = build()
        x0 = x_solved + rng.normal(0.0, 1e-3, size=x_solved.shape)   # tiny drag-like nudge
        t0 = time.perf_counter()
        r = s.solve(x0=x0)
        dt = (time.perf_counter() - t0) * 1e3
        assert r.converged, f"warm chain solve failed: maxr={r.max_residual:.2e}"
        times_ms.append(dt)

    median = statistics.median(times_ms)
    p_min, p_max = min(times_ms), max(times_ms)
    print(f"[perf] chain N={N} vars={nvars}: warm solve median={median:.3f} ms "
          f"(min={p_min:.3f}, max={p_max:.3f}, n=20)")
    assert median < 50.0, f"warm solve median {median:.3f} ms exceeds 50 ms CI margin"
    return median


def main() -> None:
    test_point_on_line_distance_and_warm_restart()
    test_dof_free_fixed_and_rectangle()
    test_redundant_constraint()
    test_conflicting_constraints()
    test_soft_drag()
    test_time_budget_returns_best_so_far()
    test_empty_and_degenerate()
    median = test_performance_chain()
    print(f"\nNSOLVE TESTS PASSED  (perf warm-solve median = {median:.3f} ms)")


if __name__ == "__main__":
    main()
