"""Sketch constraint solver for AI-CAD (docs/contracts/doc_schema.md §3).

Parameterization
----------------
Every sketch entity maps to a flat slice of a float64 variable vector x:

  point    : [x, y]                            (2 vars)
  line     : [p1x, p1y, p2x, p2y]             (4 vars)
  circle   : [cx, cy, r]                       (3 vars)
  arc      : [cx, cy, r, a0, a1]              (5 vars, a0/a1 in degrees)
  ellipse  : [cx, cy, rx, ry, rot]             (5 vars, rot in degrees)
  slot     : [p1x, p1y, p2x, p2y, w]          (5 vars)
  arc_slot : [cx, cy, r, a0, a1, w]           (6 vars; a0/a1 deg, arc-spine slot)
  spline   : [x0, y0, x1, y1, ..., xN, yN]    (2*(N+1) vars)

  ellipse_arc : [cx, cy, rx, ry, rot, a0, a1]  (7 vars; ellipse + trim angles, deg)
  parabola    : [vx, vy, fx, fy, t0, t1]       (6 vars; vertex, focus, param range)
  conic       : [p0x, p0y, p1x, p1y, apx, apy, rho]  (7 vars; SW conic)

Conic-family parameterizations (C28-A):
  ellipse_arc — same 5 base vars as ellipse plus a0/a1 trim angles in the
    UN-rotated ellipse frame (start/end derived like arc but with rx/ry/rot).
  parabola — own frame: origin = vertex, axis u = unit(focus - vertex) (opening
    direction), w = perp(u). focal length f = |focus - vertex|. A point at the
    perpendicular parameter t is P(t) = vertex + (t^2/(4f))*u + t*w. t0/t1 bound
    the drawn segment; start = P(t0), end = P(t1).
  conic — SolidWorks rho conic: endpoints p0/p1, tangent-intersection apex, and
    rho in (0,1). Renders as a rational quadratic Bezier P0,(apex,w),P1 with
    w = rho/(1-rho). start = p0, end = p1, apex handle = apex.

Derived handles ("e3.start", "e3.end" for arcs, etc.) map to expressions over
the entity's variables and expose chain-rule Jacobians for residual wiring.

All angular variables and dimension constraints are in DEGREES (CAD convention,
matching doc_schema.md §0 and equations.py).

Constraint → residual mapping
------------------------------
Each schema constraint type becomes one add_residual call, tagged with the
constraint's id string. Driving dims use the resolved dim value; driven:true dims
are excluded from the system and measured after solve.

See docs/contracts/doc_schema.md §3 for the full constraint table.

Public API (C18 sketch mode UI and C11 doc worker will call these)
------------------------------------------------------------------
  solve_sketch(sketch_params, equations_env, warm) -> SketchSolution
  drag(state, handle, target_xy) -> dict   (entity coords, no commit)

Both functions are pure dict-in / dict-out; no document or compiler imports.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from engine.doc.nsolve import System

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

EntityCoords = Dict[str, Any]   # entity id -> solved params dict


@dataclass
class SolveState:
    """Opaque warm-start state returned by solve_sketch, accepted by drag."""
    x: np.ndarray                        # last solved variable vector
    var_map: Dict[str, np.ndarray]       # entity id -> int64 variable indices
    n_vars: int                          # total variable count
    # Per-entity doc-geometry signature at the time this state was built (id -> json dump of the
    # entity params dict). solve_sketch warm-starts an entity from this state ONLY while its doc
    # geometry is unchanged — a sketch_trim/extend/edit_entity that rewrites an entity's params
    # under the SAME id (and often the same var count) must win over the stale warm vector.
    # Default empty = legacy whole-vector warm behavior for states without signatures.
    ent_sig: Dict[str, str] = field(default_factory=dict)


@dataclass
class SketchSolution:
    entities: EntityCoords               # entity id -> param dict (solved coords)
    statuses: Dict[str, str]             # entity id -> "under"|"full"|"over"
    dof: int
    redundant: List[str]                 # constraint ids
    conflicting: List[str]               # constraint ids
    converged: bool
    state: SolveState


# ---------------------------------------------------------------------------
# Degree/radian helpers (all solver math in radians; dims stored in degrees)
# ---------------------------------------------------------------------------

_DEG = math.pi / 180.0
_RAD = 180.0 / math.pi


def _d2r(d: float) -> float:
    return d * _DEG


def _r2d(r: float) -> float:
    return r * _RAD


# ---------------------------------------------------------------------------
# Parameterization: build variable layout from entity list
# ---------------------------------------------------------------------------

def _allocate_entity(sys: System, entity: dict) -> np.ndarray:
    """Allocate variables for one entity; return index array."""
    etype = entity["type"]
    eid = entity["id"]

    if etype == "point":
        px, py = entity["p"]
        return sys.add_vars(2, [px, py])

    elif etype == "line":
        p1x, p1y = entity["p1"]
        p2x, p2y = entity["p2"]
        return sys.add_vars(4, [p1x, p1y, p2x, p2y])

    elif etype == "circle":
        cx, cy = entity["center"]
        r = entity["radius"]
        return sys.add_vars(3, [cx, cy, r])

    elif etype == "arc":
        cx, cy = entity["center"]
        r = entity["radius"]
        a0 = entity["start"]   # degrees
        a1 = entity["end"]     # degrees
        return sys.add_vars(5, [cx, cy, r, a0, a1])

    elif etype == "ellipse":
        cx, cy = entity["center"]
        rx = entity["rx"]
        ry = entity["ry"]
        rot = entity["rot"]    # degrees
        return sys.add_vars(5, [cx, cy, rx, ry, rot])

    elif etype == "ellipse_arc":
        cx, cy = entity["center"]
        rx = entity["rx"]
        ry = entity["ry"]
        rot = entity.get("rot", 0.0)   # degrees
        a0 = entity["a0"]              # degrees, un-rotated ellipse frame
        a1 = entity["a1"]
        return sys.add_vars(7, [cx, cy, rx, ry, rot, a0, a1])

    elif etype == "parabola":
        vx, vy = entity["vertex"]
        fx, fy = entity["focus"]
        t0 = entity["t0"]
        t1 = entity["t1"]
        return sys.add_vars(6, [vx, vy, fx, fy, t0, t1])

    elif etype == "conic":
        p0x, p0y = entity["p0"]
        p1x, p1y = entity["p1"]
        apx, apy = entity["apex"]
        rho = entity.get("rho", 0.5)
        return sys.add_vars(7, [p0x, p0y, p1x, p1y, apx, apy, rho])

    elif etype == "slot":
        p1x, p1y = entity["p1"]
        p2x, p2y = entity["p2"]
        w = entity["width"]
        return sys.add_vars(5, [p1x, p1y, p2x, p2y, w])

    elif etype == "arc_slot":
        # arc-spine slot: [cx, cy, r, a0, a1, w]  (a0/a1 in degrees, CCW a0->a1)
        cx, cy = entity["center"]
        r = entity["radius"]
        a0 = entity["a0"]
        a1 = entity["a1"]
        w = entity["width"]
        return sys.add_vars(6, [cx, cy, r, a0, a1, w])

    elif etype == "spline":
        pts = entity["points"]
        init = []
        for px, py in pts:
            init += [px, py]
        return sys.add_vars(len(init), init)

    elif etype == "text":
        # text has no geometric constraints; allocate position only
        px, py = entity["p"]
        return sys.add_vars(2, [px, py])

    else:
        raise ValueError(f"Unknown entity type: {etype!r}")


# ---------------------------------------------------------------------------
# Handle resolution: handle string -> (var_indices, value_fn, jacobian_fn)
#
# value_fn(x_sub) -> 2-vector [hx, hy] (for point handles)
#   or scalar (for derived scalars, not needed here)
# jacobian_fn(x_sub) -> (2, k) array (chain rule)
#
# For handles that ARE direct variables (e.g. e1.p1 on a line), the value and
# Jacobian are trivially identity slices.
# ---------------------------------------------------------------------------

# --- Conic-family derived-point helpers (C28-A) ----------------------------
#
# Each takes the entity's relevant variable sub-slice (see _resolve_handle) and
# returns the world (x, y) of a handle plus its analytic Jacobian. Kept honest
# so coincident/fix on these handles flow correct partials into DOF accounting.


def _ellipse_arc_pt_val(xs):
    # xs = [cx, cy, rx, ry, rot, a]   (rot, a in degrees)
    cx, cy, rx, ry, rot, a = xs
    rr = rot * _DEG
    ar = a * _DEG
    lx = rx * math.cos(ar)
    ly = ry * math.sin(ar)
    cr, sr = math.cos(rr), math.sin(rr)
    return np.array([cx + cr * lx - sr * ly, cy + sr * lx + cr * ly])


def _ellipse_arc_pt_jac(xs):
    cx, cy, rx, ry, rot, a = xs
    rr = rot * _DEG
    ar = a * _DEG
    ca, sa = math.cos(ar), math.sin(ar)
    cr, sr = math.cos(rr), math.sin(rr)
    lx = rx * ca
    ly = ry * sa
    # d(local)/d[rx,ry,a]
    dlx_drx, dly_dry = ca, sa
    dlx_da = -rx * sa * _DEG
    dly_da = ry * ca * _DEG
    # world = c + R(rr) @ local; R = [[cr,-sr],[sr,cr]]
    # d/d[cx,cy]=I; d/drot = R'(rr)@local where R'=[[-sr,-cr],[cr,-sr]]*_DEG
    dwx_drot = (-sr * lx - cr * ly) * _DEG
    dwy_drot = (cr * lx - sr * ly) * _DEG
    # cols: cx, cy, rx, ry, rot, a
    return np.array([
        [1.0, 0.0, cr * dlx_drx, -sr * dly_dry, dwx_drot, cr * dlx_da - sr * dly_da],
        [0.0, 1.0, sr * dlx_drx,  cr * dly_dry, dwy_drot, sr * dlx_da + cr * dly_da],
    ])


def _parabola_pt_val(xs):
    # xs = [vx, vy, fx, fy, t]
    # P(t) = vertex + (t^2/(4f)) * u + t * w, u = unit(focus-vertex), w = perp(u)
    vx, vy, fx, fy, t = xs
    dx, dy = fx - vx, fy - vy
    f = math.hypot(dx, dy)
    if f < 1e-12:
        return np.array([vx, vy])
    ux, uy = dx / f, dy / f
    wx, wy = -uy, ux
    s = (t * t) / (4.0 * f)
    return np.array([vx + s * ux + t * wx, vy + s * uy + t * wy])


def _parabola_pt_jac(xs):
    # Numeric Jacobian (central differences) — robust and compact for the
    # parabola's nonlinear vertex/focus coupling. cols: vx,vy,fx,fy,t
    xs = np.asarray(xs, dtype=float)
    n = len(xs)
    J = np.zeros((2, n))
    for i in range(n):
        h = 1e-6 * max(1.0, abs(xs[i]))
        xp = xs.copy(); xp[i] += h
        xm = xs.copy(); xm[i] -= h
        J[:, i] = (_parabola_pt_val(xp) - _parabola_pt_val(xm)) / (2.0 * h)
    return J


# --- Conic-family curve evaluators (C32-B point_on / tangent) ---------------
#
# These map an entity's full variable slice + a curve parameter to the world
# point P and the (un-normalized) tangent dP/dparam. Used by point_on (closest-
# param projection) and tangent (param where curve tangent ∥ line). Range-free:
# point_on/tangent ignore the a0..a1 / t0..t1 trim, mirroring how arc/circle
# point_on uses the full circle today.


def _parabola_curve(xs, t):
    # xs = [vx, vy, fx, fy]; P(t) = vertex + (t^2/4f)*u + t*w; tangent dP/dt.
    vx, vy, fx, fy = xs[0], xs[1], xs[2], xs[3]
    dx, dy = fx - vx, fy - vy
    f = math.hypot(dx, dy)
    if f < 1e-12:
        return np.array([vx, vy]), np.array([0.0, 1.0])
    ux, uy = dx / f, dy / f
    wx, wy = -uy, ux
    s = (t * t) / (4.0 * f)
    P = np.array([vx + s * ux + t * wx, vy + s * uy + t * wy])
    dsd = (2.0 * t) / (4.0 * f)
    dP = np.array([dsd * ux + wx, dsd * uy + wy])   # dP/dt
    return P, dP


def _conic_curve(xs, u):
    # xs = [p0x, p0y, p1x, p1y, apx, apy, rho]; rational quadratic Bezier on u∈[0,1]
    #   P0,(apex,w),P1 with apex weight w = rho/(1-rho). P(u), tangent dP/du.
    p0 = np.array([xs[0], xs[1]])
    p1 = np.array([xs[2], xs[3]])
    ap = np.array([xs[4], xs[5]])
    rho = xs[6]
    rho = min(max(rho, 1e-6), 1.0 - 1e-6)
    w = rho / (1.0 - rho)
    b0 = (1.0 - u) ** 2
    b1 = 2.0 * (1.0 - u) * u * w
    b2 = u * u
    den = b0 + b1 + b2
    num = b0 * p0 + b1 * ap + b2 * p1
    P = num / den
    # derivative of P = num/den w.r.t. u, quotient rule
    db0 = -2.0 * (1.0 - u)
    db1 = 2.0 * w * (1.0 - 2.0 * u)
    db2 = 2.0 * u
    dnum = db0 * p0 + db1 * ap + db2 * p1
    dden = db0 + db1 + db2
    dP = (dnum * den - num * dden) / (den * den)
    return P, dP


def _ellipse_implicit(xs, P):
    # xs = [cx, cy, rx, ry, rot]; signed implicit value f(P) for the rotated
    # ellipse, scaled to length units so the residual is a true distance proxy:
    #   local q = R(-rot)·(P - c);  f = (sqrt((qx/rx)^2+(qy/ry)^2) - 1) * r_mean
    # Zero exactly on the ellipse; near-linear in distance for the solver.
    cx, cy, rx, ry, rot = xs[0], xs[1], xs[2], xs[3], xs[4]
    rr = rot * _DEG
    cr, sr = math.cos(rr), math.sin(rr)
    dxp, dyp = P[0] - cx, P[1] - cy
    qx = cr * dxp + sr * dyp
    qy = -sr * dxp + cr * dyp
    rx = rx if abs(rx) > 1e-9 else 1e-9
    ry = ry if abs(ry) > 1e-9 else 1e-9
    rad = math.hypot(qx / rx, qy / ry)
    r_mean = 0.5 * (abs(rx) + abs(ry))
    return (rad - 1.0) * r_mean


def _resolve_handle(
    handle: str,
    entities: dict,     # id -> entity dict
    var_map: dict,      # id -> np.ndarray of variable indices
) -> Tuple[np.ndarray, Any, Any]:
    """Return (var_idx, val_fn, jac_fn) for a point handle.

    val_fn(x_sub) -> ndarray([hx, hy])   (x_sub = x[var_idx])
    jac_fn(x_sub) -> ndarray((2, k))

    var_idx are the variables the handle DEPENDS ON (may be the full entity's
    vars for derived handles like arc.start).

    The virtual handle "origin" (also "origin.p") resolves to the fixed sketch
    origin (0, 0): no variables, constant value, zero-width Jacobian.
    """
    if "." in handle:
        eid, sub = handle.split(".", 1)
    else:
        eid = handle
        sub = None

    if eid == "origin" and eid not in entities:
        # Fixed sketch origin: contributes no variables; (2, 0) Jacobian.
        idx = np.zeros(0, dtype=np.int64)
        return (idx,
                lambda xs: np.array([0.0, 0.0]),
                lambda xs: np.zeros((2, 0)))

    ent = entities[eid]
    etype = ent["type"]
    vidx = var_map[eid]

    if etype == "point":
        # variables: [x, y]
        # sub can be None or "p"
        return (vidx,
                lambda xs: np.array([xs[0], xs[1]]),
                lambda xs: np.eye(2))

    elif etype == "line":
        # variables: [p1x, p1y, p2x, p2y]
        if sub in (None, "p1"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub == "p2":
            idx = vidx[[2, 3]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub == "mid":
            # Line midpoint ((p1+p2)/2): depends on all four line vars (SolidWorks midpoint
            # dimension target). d(mid)/d[p1x,p1y,p2x,p2y] = 0.5 on the matching component.
            return (vidx,
                    lambda xs: np.array([(xs[0] + xs[2]) * 0.5, (xs[1] + xs[3]) * 0.5]),
                    lambda xs: np.array([[0.5, 0.0, 0.5, 0.0],
                                         [0.0, 0.5, 0.0, 0.5]]))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for line {eid}")

    elif etype == "circle":
        # variables: [cx, cy, r]
        if sub in (None, "center"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for circle {eid}")

    elif etype == "arc":
        # variables: [cx, cy, r, a0, a1]
        if sub in (None, "center"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub == "start":
            # start point: (cx + r*cos(a0), cy + r*sin(a0)), angles in degrees
            # vars touched: [cx, cy, r, a0] -> indices [0,1,2,3] in full 5-var block
            idx = vidx[[0, 1, 2, 3]]
            def _arc_start_val(xs):
                cx, cy, r, a0 = xs
                a0r = a0 * _DEG
                return np.array([cx + r * math.cos(a0r), cy + r * math.sin(a0r)])
            def _arc_start_jac(xs):
                cx, cy, r, a0 = xs
                a0r = a0 * _DEG
                # d/d[cx, cy, r, a0]
                return np.array([
                    [1.0, 0.0, math.cos(a0r), -r * math.sin(a0r) * _DEG],
                    [0.0, 1.0, math.sin(a0r),  r * math.cos(a0r) * _DEG],
                ])
            return (idx, _arc_start_val, _arc_start_jac)
        elif sub == "end":
            # end point: (cx + r*cos(a1), cy + r*sin(a1))
            idx = vidx[[0, 1, 2, 4]]
            def _arc_end_val(xs):
                cx, cy, r, a1 = xs
                a1r = a1 * _DEG
                return np.array([cx + r * math.cos(a1r), cy + r * math.sin(a1r)])
            def _arc_end_jac(xs):
                cx, cy, r, a1 = xs
                a1r = a1 * _DEG
                return np.array([
                    [1.0, 0.0, math.cos(a1r), -r * math.sin(a1r) * _DEG],
                    [0.0, 1.0, math.sin(a1r),  r * math.cos(a1r) * _DEG],
                ])
            return (idx, _arc_end_val, _arc_end_jac)
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for arc {eid}")

    elif etype == "ellipse":
        # variables: [cx, cy, rx, ry, rot]
        if sub in (None, "center"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for ellipse {eid}")

    elif etype == "ellipse_arc":
        # variables: [cx, cy, rx, ry, rot, a0, a1]
        if sub in (None, "center"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub in ("start", "end"):
            # endpoint of a rotated ellipse at trim angle a (deg, un-rotated frame):
            #   local = (rx*cos a, ry*sin a); world = center + R(rot) @ local
            ai = 5 if sub == "start" else 6
            idx = vidx[[0, 1, 2, 3, 4, ai]]   # cx,cy,rx,ry,rot,a
            return (idx, _ellipse_arc_pt_val, _ellipse_arc_pt_jac)
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for ellipse_arc {eid}")

    elif etype == "parabola":
        # variables: [vx, vy, fx, fy, t0, t1]
        if sub in (None, "vertex"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub in ("start", "end"):
            ti = 4 if sub == "start" else 5
            idx = vidx[[0, 1, 2, 3, ti]]   # vx,vy,fx,fy,t
            return (idx, _parabola_pt_val, _parabola_pt_jac)
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for parabola {eid}")

    elif etype == "conic":
        # variables: [p0x, p0y, p1x, p1y, apx, apy, rho]
        if sub in (None, "start", "p0"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub in ("end", "p1"):
            idx = vidx[[2, 3]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub == "apex":
            idx = vidx[[4, 5]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for conic {eid}")

    elif etype == "slot":
        # variables: [p1x, p1y, p2x, p2y, w]
        if sub in (None, "p1"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub == "p2":
            idx = vidx[[2, 3]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for slot {eid}")

    elif etype == "arc_slot":
        # variables: [cx, cy, r, a0, a1, w]; spine endpoints derived like arc.
        if sub in (None, "center"):
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub in ("start", "end"):
            ai = 3 if sub == "start" else 4
            idx = vidx[[0, 1, 2, ai]]   # cx, cy, r, a
            def _as_pt_val(xs):
                cx, cy, r, a = xs
                ar = a * _DEG
                return np.array([cx + r * math.cos(ar), cy + r * math.sin(ar)])
            def _as_pt_jac(xs):
                cx, cy, r, a = xs
                ar = a * _DEG
                return np.array([
                    [1.0, 0.0, math.cos(ar), -r * math.sin(ar) * _DEG],
                    [0.0, 1.0, math.sin(ar),  r * math.cos(ar) * _DEG],
                ])
            return (idx, _as_pt_val, _as_pt_jac)
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for arc_slot {eid}")

    elif etype == "spline":
        # variables: [x0,y0, x1,y1, ...]
        if sub is None:
            # whole entity -> return first point as representative
            idx = vidx[[0, 1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        elif sub.startswith("p"):
            k = int(sub[1:])
            idx = vidx[[2*k, 2*k+1]]
            return (idx,
                    lambda xs: np.array([xs[0], xs[1]]),
                    lambda xs: np.eye(2))
        else:
            raise ValueError(f"Unknown sub-handle {sub!r} for spline {eid}")

    elif etype == "text":
        idx = vidx[[0, 1]]
        return (idx,
                lambda xs: np.array([xs[0], xs[1]]),
                lambda xs: np.eye(2))

    else:
        raise ValueError(f"Unknown entity type {etype!r} for handle {handle!r}")


# ---------------------------------------------------------------------------
# Constraint residual builders
# ---------------------------------------------------------------------------

def _resolve_dim(constraint: dict, equations_env: dict) -> float:
    """Extract the float value for a dimension constraint, resolving expr if set."""
    val_obj = constraint.get("value", {})
    expr = val_obj.get("expr")
    if expr is not None and expr in equations_env:
        return float(equations_env[expr])
    elif expr is not None:
        # Try evaluating the expression through the equations evaluator
        from engine.doc.equations import evaluate
        return evaluate(expr, equations_env)
    return float(val_obj.get("value", 0.0))


def _add_constraint(
    sys: System,
    constraint: dict,
    entities: dict,
    var_map: dict,
    equations_env: dict,
) -> None:
    """Register residual block(s) for one constraint.

    driven:true dimension constraints are skipped entirely.
    """
    cid = constraint["id"]
    ctype = constraint["type"]
    refs = constraint.get("refs", [])
    driven = constraint.get("driven", False)

    # Skip driven (reference) dimensions — they are measured after solve, not enforced.
    if driven:
        return

    if ctype == "coincident":
        _add_coincident(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "horizontal":
        ref = refs[0]
        if len(refs) >= 2:
            # two point handles (either may be the virtual "origin"): ya == yb
            _add_horizontal_2pts(sys, cid, refs[0], refs[1], entities, var_map)
        elif "." not in ref:
            # whole entity (line): enforce p1y == p2y
            eid = ref
            etype = entities[eid]["type"]
            if etype == "line":
                vidx = var_map[eid]
                # residual: p1y - p2y = 0
                # vars: [p1x, p1y, p2x, p2y] -> indices [1,3]
                idx = vidx[[1, 3]]
                sys.add_residual(
                    lambda xs: np.array([xs[0] - xs[1]]),
                    idx, lambda xs: np.array([[1.0, -1.0]]), cid)
            elif etype == "slot":
                vidx = var_map[eid]
                idx = vidx[[1, 3]]
                sys.add_residual(
                    lambda xs: np.array([xs[0] - xs[1]]),
                    idx, lambda xs: np.array([[1.0, -1.0]]), cid)
        else:
            # two point handles: enforce ya == yb
            _add_horizontal_2pts(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "vertical":
        ref = refs[0]
        if len(refs) >= 2:
            # two point handles (either may be the virtual "origin"): xa == xb
            _add_vertical_2pts(sys, cid, refs[0], refs[1], entities, var_map)
        elif "." not in ref:
            eid = ref
            etype = entities[eid]["type"]
            if etype == "line":
                vidx = var_map[eid]
                idx = vidx[[0, 2]]
                sys.add_residual(
                    lambda xs: np.array([xs[0] - xs[1]]),
                    idx, lambda xs: np.array([[1.0, -1.0]]), cid)
            elif etype == "slot":
                vidx = var_map[eid]
                idx = vidx[[0, 2]]
                sys.add_residual(
                    lambda xs: np.array([xs[0] - xs[1]]),
                    idx, lambda xs: np.array([[1.0, -1.0]]), cid)
        else:
            _add_vertical_2pts(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "parallel":
        _add_parallel(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "perpendicular":
        _add_perpendicular(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "tangent":
        _add_tangent(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "collinear":
        _add_collinear(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "equal":
        _add_equal(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "concentric":
        # Two circles/arcs share the same center
        eid0 = refs[0].split(".")[0]
        eid1 = refs[1].split(".")[0]
        # center of each: first two vars
        idx0 = var_map[eid0][[0, 1]]
        idx1 = var_map[eid1][[0, 1]]
        combined = np.concatenate([idx0, idx1])
        sys.add_residual(
            lambda xs: np.array([xs[0] - xs[2], xs[1] - xs[3]]),
            combined, lambda xs: np.array([[1, 0, -1, 0], [0, 1, 0, -1]], dtype=float), cid)

    elif ctype == "coradial":
        # Two circles/arcs share the same center AND the same radius.
        _add_coradial(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "midpoint":
        # refs[0] = point handle; refs[1] = line entity
        _add_midpoint(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "symmetric":
        # refs[0], refs[1] = points/entities; refs[2] = axis line
        _add_symmetric(sys, cid, refs[0], refs[1], refs[2], entities, var_map)

    elif ctype == "fix":
        _add_fix(sys, cid, refs[0], entities, var_map)

    elif ctype == "point_on":
        _add_point_on(sys, cid, refs[0], refs[1], entities, var_map)

    elif ctype == "distance":
        d = _resolve_dim(constraint, equations_env)
        axis = constraint.get("axis")
        _add_distance(sys, cid, refs[0], refs[1], d, entities, var_map, axis=axis,
                      cond=constraint.get("cond"))

    elif ctype == "angle":
        ang = _resolve_dim(constraint, equations_env)
        _add_angle(sys, cid, refs[0], refs[1], ang, entities, var_map)

    elif ctype == "radius":
        r = _resolve_dim(constraint, equations_env)
        eid = refs[0].split(".")[0]
        etype = entities[eid]["type"]
        if etype in ("circle", "arc"):
            ridx = var_map[eid][[2]]
            sys.add_residual(
                lambda xs, _r=r: np.array([xs[0] - _r]),
                ridx, lambda xs: np.array([[1.0]]), cid)
        elif etype in ("slot", "arc_slot"):
            # cap radius = w/2 -> constrain the width variable: w = 2r.
            widx = var_map[eid][[4 if etype == "slot" else 5]]
            sys.add_residual(
                lambda xs, _r=r: np.array([xs[0] - 2.0 * _r]),
                widx, lambda xs: np.array([[1.0]]), cid)

    elif ctype == "diameter":
        d = _resolve_dim(constraint, equations_env)
        r = d / 2.0
        eid = refs[0].split(".")[0]
        etype = entities[eid]["type"]
        if etype in ("circle", "arc"):
            ridx = var_map[eid][[2]]
            sys.add_residual(
                lambda xs, _r=r: np.array([xs[0] - _r]),
                ridx, lambda xs: np.array([[1.0]]), cid)
        elif etype in ("slot", "arc_slot"):
            # slot WIDTH == cap diameter == the width variable: w = d.
            widx = var_map[eid][[4 if etype == "slot" else 5]]
            sys.add_residual(
                lambda xs, _d=d: np.array([xs[0] - _d]),
                widx, lambda xs: np.array([[1.0]]), cid)

    else:
        # Unknown constraint type — skip silently (forward-tolerant)
        pass


# ---------------------------------------------------------------------------
# Individual constraint helpers
# ---------------------------------------------------------------------------

def _add_coincident(sys, cid, h0, h1, entities, var_map):
    """Two point handles must be equal."""
    idx0, vf0, jf0 = _resolve_handle(h0, entities, var_map)
    idx1, vf1, jf1 = _resolve_handle(h1, entities, var_map)
    # Combine indices; slice in rfunc/jfunc
    k0 = len(idx0)
    k1 = len(idx1)
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        p0 = vf0(xs[:k0])
        p1 = vf1(xs[k0:k0+k1])
        return p0 - p1  # shape (2,)

    def _j(xs):
        J0 = jf0(xs[:k0])        # (2, k0)
        J1 = jf1(xs[k0:k0+k1])  # (2, k1)
        return np.hstack([J0, -J1])  # (2, k0+k1)

    sys.add_residual(_r, combined, _j, cid)


def _add_horizontal_2pts(sys, cid, h0, h1, entities, var_map):
    """Two point handles: enforce ya == yb."""
    idx0, vf0, jf0 = _resolve_handle(h0, entities, var_map)
    idx1, vf1, jf1 = _resolve_handle(h1, entities, var_map)
    k0, k1 = len(idx0), len(idx1)
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        ya = vf0(xs[:k0])[1]
        yb = vf1(xs[k0:k0+k1])[1]
        return np.array([ya - yb])

    def _j(xs):
        Ja = jf0(xs[:k0])[1:2, :]   # row 1 (y) of Jacobian, shape (1, k0)
        Jb = jf1(xs[k0:k0+k1])[1:2, :]
        return np.hstack([Ja, -Jb])

    sys.add_residual(_r, combined, _j, cid)


def _add_vertical_2pts(sys, cid, h0, h1, entities, var_map):
    """Two point handles: enforce xa == xb."""
    idx0, vf0, jf0 = _resolve_handle(h0, entities, var_map)
    idx1, vf1, jf1 = _resolve_handle(h1, entities, var_map)
    k0, k1 = len(idx0), len(idx1)
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        xa = vf0(xs[:k0])[0]
        xb = vf1(xs[k0:k0+k1])[0]
        return np.array([xa - xb])

    def _j(xs):
        Ja = jf0(xs[:k0])[0:1, :]   # row 0 (x)
        Jb = jf1(xs[k0:k0+k1])[0:1, :]
        return np.hstack([Ja, -Jb])

    sys.add_residual(_r, combined, _j, cid)


def _line_direction_idx(eid, var_map):
    """Return var indices for [p1x, p1y, p2x, p2y] of a line entity."""
    return var_map[eid][[0, 1, 2, 3]]


def _add_parallel(sys, cid, h0, h1, entities, var_map):
    """Two lines parallel: cross((B-A), (D-C)) = 0, i.e. (bx-ax)(dy-cy) - (by-ay)(dx-cx) = 0."""
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    assert entities[eid0]["type"] == "line" and entities[eid1]["type"] == "line"
    idx0 = _line_direction_idx(eid0, var_map)  # [p1x,p1y,p2x,p2y]
    idx1 = _line_direction_idx(eid1, var_map)
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        return np.array([(bx-ax)*(dy-cy) - (by-ay)*(dx-cx)])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        dax = -(dy-cy)
        day = (dx-cx)
        dbx = (dy-cy)
        dby = -(dx-cx)
        dcx = (by-ay)
        dcy = -(bx-ax)
        ddx = -(by-ay)
        ddy = (bx-ax)
        return np.array([[dax, day, dbx, dby, dcx, dcy, ddx, ddy]])

    sys.add_residual(_r, combined, _j, cid)


def _add_perpendicular(sys, cid, h0, h1, entities, var_map):
    """Two lines perpendicular: dot((B-A),(D-C)) = 0."""
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    idx0 = _line_direction_idx(eid0, var_map)
    idx1 = _line_direction_idx(eid1, var_map)
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        return np.array([(bx-ax)*(dx-cx) + (by-ay)*(dy-cy)])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        dax = -(dx-cx)
        day = -(dy-cy)
        dbx = (dx-cx)
        dby = (dy-cy)
        dcx = -(bx-ax)
        dcy = -(by-ay)
        ddx = (bx-ax)
        ddy = (by-ay)
        return np.array([[dax, day, dbx, dby, dcx, dcy, ddx, ddy]])

    sys.add_residual(_r, combined, _j, cid)


def _add_collinear(sys, cid, h0, h1, entities, var_map):
    """Two lines lie on the same infinite line (2 residuals, removes 2 DOF).

    r1 = cross(dirA, dirB) = 0                  (directions parallel)
    r2 = cross(dirA, (C - A)) = 0               (endpoint C of line B on line A)

    where line A = (A=p1, B=p2) and line B = (C=p1, D=p2). r2 is the (un-normalized)
    signed area, vanishing exactly when C lies on the infinite line through A,B.
    Combined with r1 this pins both lines onto one infinite line. Matches the
    parallel/point_on residual conventions (raw cross products, analytic Jacobian).
    """
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    assert entities[eid0]["type"] == "line" and entities[eid1]["type"] == "line"
    idx0 = _line_direction_idx(eid0, var_map)  # [ax,ay,bx,by]
    idx1 = _line_direction_idx(eid1, var_map)  # [cx,cy,dx,dy]
    combined = np.concatenate([idx0, idx1])

    def _r(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        # r1: directions parallel
        r1 = (bx-ax)*(dy-cy) - (by-ay)*(dx-cx)
        # r2: endpoint C of line B on infinite line A
        r2 = (bx-ax)*(cy-ay) - (by-ay)*(cx-ax)
        return np.array([r1, r2])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        # r1 = (bx-ax)(dy-cy) - (by-ay)(dx-cx)
        r1 = [
            -(dy-cy),   # dax
            (dx-cx),    # day
            (dy-cy),    # dbx
            -(dx-cx),   # dby
            (by-ay),    # dcx
            -(bx-ax),   # dcy
            -(by-ay),   # ddx
            (bx-ax),    # ddy
        ]
        # r2 = (bx-ax)(cy-ay) - (by-ay)(cx-ax)
        r2 = [
            -(cy-ay) + (by-ay),   # dax
            -(bx-ax) + (cx-ax),   # day  (note: d/day of -(by-ay)(cx-ax) = +(cx-ax))
            (cy-ay),              # dbx
            -(cx-ax),             # dby
            -(by-ay),             # dcx
            (bx-ax),              # dcy
            0.0,                  # ddx
            0.0,                  # ddy
        ]
        return np.array([r1, r2])

    sys.add_residual(_r, combined, _j, cid)


def _add_coradial(sys, cid, h0, h1, entities, var_map):
    """Two circles/arcs share the same center AND the same radius (3 residuals).

    cx0 == cx1, cy0 == cy1, r0 == r1. Distinct SolidWorks relation (= concentric
    + equal-radius combined into one). Linear residuals, constant Jacobian.
    """
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    assert entities[eid0]["type"] in ("circle", "arc")
    assert entities[eid1]["type"] in ("circle", "arc")
    idx0 = var_map[eid0][[0, 1, 2]]  # cx0,cy0,r0
    idx1 = var_map[eid1][[0, 1, 2]]  # cx1,cy1,r1
    combined = np.concatenate([idx0, idx1])

    _Jc = np.array([
        [1.0, 0.0, 0.0, -1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, -1.0, 0.0],
        [0.0, 0.0, 1.0, 0.0, 0.0, -1.0],
    ])
    sys.add_residual(
        lambda xs: np.array([xs[0]-xs[3], xs[1]-xs[4], xs[2]-xs[5]]),
        combined, lambda xs: _Jc, cid)


def _entity_center_radius_idx(eid, entities, var_map):
    """Return (center_idx, radius_idx) for circle or arc entity."""
    etype = entities[eid]["type"]
    vidx = var_map[eid]
    if etype in ("circle", "arc"):
        return vidx[[0, 1]], vidx[2]
    raise ValueError(f"Entity {eid} of type {etype!r} has no center/radius")


def _add_tangent(sys, cid, h0, h1, entities, var_map):
    """Tangency between:
      line-circle: distance from line to circle center = radius
      line-arc: same as line-circle
      circle-circle (ext): |C1-C2| = r1+r2  or  |C1-C2| = |r1-r2| (internal)
        We implement external tangency (distance = sum of radii).
      arc-arc / arc-circle: same as circle-circle.
    """
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    t0 = entities[eid0]["type"]
    t1 = entities[eid1]["type"]

    curved = {"circle", "arc"}
    conics = {"ellipse_arc", "parabola", "conic"}
    if t0 == "line" and t1 in curved:
        _add_tangent_line_circle(sys, cid, eid0, eid1, entities, var_map)
    elif t0 in curved and t1 == "line":
        _add_tangent_line_circle(sys, cid, eid1, eid0, entities, var_map)
    elif t0 in curved and t1 in curved:
        _add_tangent_circle_circle(sys, cid, eid0, eid1, entities, var_map)
    elif t0 == "line" and t1 in conics:
        _add_tangent_line_conic(sys, cid, eid0, eid1, entities, var_map)
    elif t0 in conics and t1 == "line":
        _add_tangent_line_conic(sys, cid, eid1, eid0, entities, var_map)
    else:
        # line-line, and curve↔curve / conic↔conic tangent stay deferred; skip.
        pass


def _add_tangent_line_circle(sys, cid, line_eid, circ_eid, entities, var_map):
    """Distance from line to circle center = radius.

    line: [p1x, p1y, p2x, p2y]
    circle/arc: [cx, cy, r, ...]
    residual: dist(center, line) - r = 0
    dist(P, line AB) = |(B-A) x (A-P)| / |B-A|
    = |cross| / |BA|, where cross = (bx-ax)(ay-py) - (by-ay)(ax-px)
    """
    lidx = var_map[line_eid][[0, 1, 2, 3]]    # [ax,ay,bx,by]
    cidx = var_map[circ_eid][[0, 1, 2]]        # [cx,cy,r]
    combined = np.concatenate([lidx, cidx])

    def _r(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, r = xs[4], xs[5], xs[6]
        dxab = bx - ax
        dyab = by - ay
        L = math.hypot(dxab, dyab)
        if L < 1e-14:
            return np.array([r])
        cross = dxab * (ay - cy) - dyab * (ax - cx)
        dist = abs(cross) / L
        return np.array([dist - r])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, r = xs[4], xs[5], xs[6]
        dxab = bx - ax
        dyab = by - ay
        L2 = dxab**2 + dyab**2
        L = math.sqrt(L2) if L2 > 1e-28 else 1e-14
        cross = dxab * (ay - cy) - dyab * (ax - cx)
        sgn = 1.0 if cross >= 0 else -1.0

        # d(cross)/d[ax,ay,bx,by,cx,cy]
        # cross = (bx-ax)(ay-cy) - (by-ay)(ax-cx)
        d_cross_ax = -(ay - cy) + (by - ay)
        d_cross_ay = (bx - ax) - 0   # + (by-ay)*1 ... wait, let me redo
        # cross = dxab*(ay-cy) - dyab*(ax-cx)
        # d/dax = -1*(ay-cy) - (-1)*(ax-cx)*0 ... let me be careful:
        # dxab = bx-ax, dyab = by-ay
        # d(dxab)/dax = -1, d(dyab)/dax = 0
        # d(cross)/dax = -1*(ay-cy) - 0*(ax-cx) - dyab*(-1) = -(ay-cy) + dyab
        # d(cross)/day = dxab*(1) - 0 = dxab  ... wait: d(ay-cy)/day = 1
        # cross = dxab*(ay-cy) - dyab*(ax-cx)
        # dcross/day = dxab * 1
        # dcross/dax = d(dxab)/dax * (ay-cy) - d(dyab)/dax * (ax-cx) - dyab*d(ax-cx)/dax
        #            = -1*(ay-cy) - 0 - dyab*1 = -(ay-cy) - dyab
        # Let me just compute each:
        d_cross = {
            'ax': -(ay-cy) - dyab,   # wait: -(ay-cy) + 0 - dyab*1
            'ay': dxab,
            'bx': (ay-cy),
            'by': -(ax-cx),
            'cx': dyab,
            'cy': -dxab,
        }
        # Actually let me recompute carefully:
        # cross = (bx-ax)*(ay-cy) - (by-ay)*(ax-cx)
        # dcross/dax = -1*(ay-cy) - (-1)*(ax-cx) * but (ax-cx) has d/dax=1
        #            = -(ay-cy) - (by-ay)*(-1) ... no
        # (by-ay)*(ax-cx): d/dax = (by-ay)*1
        # so dcross/dax = -(ay-cy) - (by-ay) = -(ay-cy) - dyab
        # dcross/day = (bx-ax)*1 - (-1)*(ax-cx) = dxab + (ax-cx)
        # Actually: (by-ay) has d/day = -1
        # (by-ay)*(ax-cx): d/day = -1*(ax-cx) = -(ax-cx)
        # dcross/day = dxab - (-(ax-cx)) = dxab + (ax-cx)  ... hmm that doesn't seem right
        # Let me expand completely:
        # cross = bx*ay - bx*cy - ax*ay + ax*cy - by*ax + by*cx + ay*ax - ay*cx
        # Simplify: cross = bx*ay - bx*cy - ax*ay + ax*cy - by*ax + by*cx + ay*ax - ay*cx
        #                 = bx*ay - bx*cy + ax*cy - by*ax + by*cx - ay*cx
        # d/dax = -by + cy = cy - by
        # d/day = bx - cx = bx - cx
        # d/dbx = ay - cy
        # d/dby = -ax + cx = cx - ax
        # d/dcx = by - ay  ... wait: +by*cx -> d/dcx = by; -ay*cx -> d/dcx = -ay  => by-ay
        # d/dcy = -bx + ax = ax - bx
        d_cross2 = {
            'ax': cy - by,
            'ay': bx - cx,
            'bx': ay - cy,
            'by': cx - ax,
            'cx': by - ay,
            'cy': ax - bx,
        }

        # d(L)/d[ax,ay,bx,by]: L = sqrt(dxab^2 + dyab^2)
        d_L = {
            'ax': -dxab / L,
            'ay': -dyab / L,
            'bx': dxab / L,
            'by': dyab / L,
        }

        # dist = |cross| / L  => d(dist)/d[...] = (sgn * d_cross * L - |cross| * d_L) / L^2.
        # sgn applies ONLY to the d_cross term (chain rule through |cross|) — applying it to the
        # whole quotient negates the d_L term when cross < 0 (same bug as the point-line distance
        # dd(); see that comment).
        def _d_dist(var, is_line=True):
            dc = d_cross2.get(var, 0.0)
            dl = d_L.get(var, 0.0) if is_line else 0.0
            return (sgn * dc * L - abs(cross) * dl) / L2

        J = np.zeros((1, 7))
        J[0, 0] = _d_dist('ax')
        J[0, 1] = _d_dist('ay')
        J[0, 2] = _d_dist('bx')
        J[0, 3] = _d_dist('by')
        J[0, 4] = sgn * d_cross2.get('cx', 0.0) / L   # dcx
        J[0, 5] = sgn * d_cross2.get('cy', 0.0) / L   # dcy
        J[0, 6] = -1.0  # d(dist - r)/dr = -1
        return J

    sys.add_residual(_r, combined, _j, cid)


def _add_tangent_circle_circle(sys, cid, eid0, eid1, entities, var_map):
    """Circle/arc ↔ circle/arc tangency.

    Two arrangements satisfy tangency:
      external: |C0-C1| = r0 + r1   (circles touch, sit outside each other)
      internal: |C0-C1| = |r0 - r1| (one circle inside the other, touching)

    Branch selection (SolidWorks behavior): evaluate the CURRENT geometry and pick
    whichever branch the user already drew — the one whose residual is currently
    smaller. This preserves the drawn configuration and stops the solver flipping a
    nested pair inside-out (or vice versa). The chosen branch is frozen at
    constraint-build time (does not re-evaluate during the solve iterations).
    """
    idx0 = var_map[eid0][[0, 1, 2]]  # cx0,cy0,r0
    idx1 = var_map[eid1][[0, 1, 2]]  # cx1,cy1,r1
    combined = np.concatenate([idx0, idx1])

    # Pick internal vs external from the as-drawn geometry.
    e0, e1 = entities[eid0], entities[eid1]
    c0 = e0.get("center", [0.0, 0.0]); c1 = e1.get("center", [0.0, 0.0])
    r0_0 = float(e0.get("radius", 0.0)); r1_0 = float(e1.get("radius", 0.0))
    d0 = math.hypot(c0[0] - c1[0], c0[1] - c1[1])
    internal = abs(d0 - abs(r0_0 - r1_0)) < abs(d0 - (r0_0 + r1_0))

    def _r(xs):
        cx0, cy0, r0 = xs[0], xs[1], xs[2]
        cx1, cy1, r1 = xs[3], xs[4], xs[5]
        d = math.hypot(cx0 - cx1, cy0 - cy1)
        target = abs(r0 - r1) if internal else (r0 + r1)
        return np.array([d - target])

    def _j(xs):
        cx0, cy0, r0 = xs[0], xs[1], xs[2]
        cx1, cy1, r1 = xs[3], xs[4], xs[5]
        dx = cx0 - cx1; dy = cy0 - cy1
        d = math.hypot(dx, dy)
        if d < 1e-12:
            ux, uy = 0.0, 0.0
        else:
            ux, uy = dx/d, dy/d
        if internal:
            # d(|r0-r1|)/dr0 = sgn(r0-r1), /dr1 = -sgn(r0-r1)
            s = 1.0 if (r0 - r1) >= 0 else -1.0
            dr0, dr1 = -s, s
        else:
            dr0, dr1 = -1.0, -1.0
        return np.array([[ux, uy, dr0, -ux, -uy, dr1]])

    sys.add_residual(_r, combined, _j, cid)


def _add_tangent_line_conic(sys, cid, line_eid, conic_eid, entities, var_map):
    """Tangency line ↔ conic-family curve.

    Residual = signed distance from the line to the curve's nearest point.
    For ellipse_arc this is EXACT via the support function: the distance from the
    ellipse center to a line, minus the ellipse's support width in the line's
    normal direction, is zero exactly when the line is tangent:

        dist(center, line) - sqrt((a·cosφ)^2 + (b·sinφ)^2) = 0

    where φ is the line-normal direction measured in the ellipse's rotated frame
    and (a,b)=(rx,ry). (Range-free: ignores the a0/a1 trim — documented.)

    For parabola/conic there is no clean closed support form, so the residual is
    the NUMERIC closest-distance: project the curve's extreme point in the line-
    normal direction (param where curve tangent ∥ line) and measure its signed
    perpendicular distance to the line. Jacobian is central-difference (the
    parabola handles already use numeric partials).
    """
    etype = entities[conic_eid]["type"]
    lidx = var_map[line_eid][[0, 1, 2, 3]]   # ax,ay,bx,by

    if etype == "ellipse_arc":
        eidx = var_map[conic_eid][[0, 1, 2, 3, 4]]   # cx,cy,rx,ry,rot
        combined = np.concatenate([lidx, eidx])

        def _r(xs):
            return np.array([_ellipse_line_support(xs[:4], xs[4:9])])

        def _j(xs):
            return _numeric_jac_1(
                lambda v: np.array([_ellipse_line_support(v[:4], v[4:9])]),
                xs, 9)

        sys.add_residual(_r, combined, _j, cid)
        return

    if etype == "parabola":
        eidx = var_map[conic_eid][[0, 1, 2, 3]]   # vx,vy,fx,fy
        curve = _parabola_curve
        scan = lambda cv, n: np.linspace(-200.0, 200.0, n)
    else:   # conic
        eidx = var_map[conic_eid][[0, 1, 2, 3, 4, 5, 6]]
        curve = _conic_curve
        scan = lambda cv, n: np.linspace(0.0, 1.0, n)

    ke = len(eidx)
    combined = np.concatenate([lidx, eidx])

    def _support_resid(v):
        ax, ay, bx, by = v[0], v[1], v[2], v[3]
        cv = v[4:4+ke]
        dxab, dyab = bx - ax, by - ay
        L = math.hypot(dxab, dyab)
        if L < 1e-12:
            return np.array([0.0])
        nx, ny = -dyab / L, dxab / L   # unit line normal
        # param where curve tangent ∥ line (tangent·n = 0): scan then Newton.
        p = _extremum_param(curve, cv, nx, ny, scan)
        Q, _ = curve(cv, p)
        # signed distance of the extreme curve point from the line
        return np.array([(Q[0] - ax) * nx + (Q[1] - ay) * ny])

    def _r(xs):
        return _support_resid(xs)

    def _j(xs):
        return _numeric_jac_1(_support_resid, xs, 4 + ke)

    sys.add_residual(_r, combined, _j, cid)


def _ellipse_line_support(line, ell):
    """Exact line↔ellipse tangency residual via the support function.

    line = [ax,ay,bx,by]; ell = [cx,cy,rx,ry,rot]. Returns
      dist(center→line) - support, support = sqrt((rx·cosφ)^2+(ry·sinφ)^2),
    φ = angle of the line normal in the ellipse's rotated frame.
    """
    ax, ay, bx, by = line
    cx, cy, rx, ry, rot = ell
    dxab, dyab = bx - ax, by - ay
    L = math.hypot(dxab, dyab)
    if L < 1e-12:
        return 0.0
    nx, ny = -dyab / L, dxab / L          # unit line normal (world)
    rr = rot * _DEG
    cr, sr = math.cos(rr), math.sin(rr)
    # normal in the ellipse's un-rotated frame
    nlx = cr * nx + sr * ny
    nly = -sr * nx + cr * ny
    support = math.hypot(rx * nlx, ry * nly)
    dist = abs((cx - ax) * nx + (cy - ay) * ny)   # |center→line| along normal
    return dist - support


def _extremum_param(curve, cv, nx, ny, scan):
    """Param p where the curve tangent is parallel to a line (tangent·n = 0).

    Coarse scan for the extreme point in the +n direction, then Newton on
    g(p) = C'(p)·n. Returns the param at the point farthest along +n (the
    near-side tangency candidate), range-free.
    """
    pts = scan(cv, 41)
    best_p, best_proj = pts[0], -float("inf")
    for p in pts:
        Q, _ = curve(cv, p)
        proj = Q[0] * nx + Q[1] * ny
        if proj > best_proj:
            best_proj, best_p = proj, p
    p = best_p
    for _ in range(12):
        _, dQ = curve(cv, p)
        g = dQ[0] * nx + dQ[1] * ny
        h = 1e-4
        _, dQp = curve(cv, p + h)
        _, dQm = curve(cv, p - h)
        ddg = ((dQp[0] - dQm[0]) * nx + (dQp[1] - dQm[1]) * ny) / (2.0 * h)
        if abs(ddg) < 1e-14:
            break
        step = g / ddg
        p -= step
        if abs(step) < 1e-12:
            break
    return p


def _add_equal(sys, cid, h0, h1, entities, var_map):
    """Equal length (lines) or equal radius (circles/arcs)."""
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    t0 = entities[eid0]["type"]
    t1 = entities[eid1]["type"]

    if t0 == "line" and t1 == "line":
        # |p2-p1|_0 = |p2-p1|_1
        idx0 = var_map[eid0][[0, 1, 2, 3]]
        idx1 = var_map[eid1][[0, 1, 2, 3]]
        combined = np.concatenate([idx0, idx1])

        def _r(xs):
            ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
            cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
            L0 = math.hypot(bx-ax, by-ay)
            L1 = math.hypot(dx-cx, dy-cy)
            return np.array([L0 - L1])

        def _j(xs):
            ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
            cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
            L0 = math.hypot(bx-ax, by-ay)
            L1 = math.hypot(dx-cx, dy-cy)
            if L0 < 1e-12: L0 = 1e-12
            if L1 < 1e-12: L1 = 1e-12
            dL0_a = [-(bx-ax)/L0, -(by-ay)/L0, (bx-ax)/L0, (by-ay)/L0]
            dL1_b = [-(dx-cx)/L1, -(dy-cy)/L1, (dx-cx)/L1, (dy-cy)/L1]
            return np.array([dL0_a + [-v for v in dL1_b]])

        sys.add_residual(_r, combined, _j, cid)

    elif t0 in ("circle", "arc") and t1 in ("circle", "arc"):
        # r0 = r1
        ridx0 = var_map[eid0][[2]]
        ridx1 = var_map[eid1][[2]]
        combined = np.concatenate([ridx0, ridx1])
        sys.add_residual(
            lambda xs: np.array([xs[0] - xs[1]]),
            combined, lambda xs: np.array([[1.0, -1.0]]), cid)

    elif t0 == "slot" and t1 == "slot":
        # slot length p1->p2 equal
        idx0 = var_map[eid0][[0, 1, 2, 3]]
        idx1 = var_map[eid1][[0, 1, 2, 3]]
        combined = np.concatenate([idx0, idx1])
        _add_equal_length_4var(sys, cid, combined)

    # else: mixed types, skip


def _add_equal_length_4var(sys, cid, combined):
    def _r(xs):
        L0 = math.hypot(xs[2]-xs[0], xs[3]-xs[1])
        L1 = math.hypot(xs[6]-xs[4], xs[7]-xs[5])
        return np.array([L0 - L1])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        L0 = math.hypot(bx-ax, by-ay)
        L1 = math.hypot(dx-cx, dy-cy)
        if L0 < 1e-12: L0 = 1e-12
        if L1 < 1e-12: L1 = 1e-12
        return np.array([[-(bx-ax)/L0, -(by-ay)/L0, (bx-ax)/L0, (by-ay)/L0,
                           (dx-cx)/L1, (dy-cy)/L1, -(dx-cx)/L1, -(dy-cy)/L1]])

    sys.add_residual(_r, combined, _j, cid)


def _add_midpoint(sys, cid, point_handle, line_handle, entities, var_map):
    """Point P is at the midpoint of line entity."""
    pidx, pvf, pjf = _resolve_handle(point_handle, entities, var_map)
    eid = line_handle.split(".")[0]
    assert entities[eid]["type"] == "line"
    lidx = var_map[eid][[0, 1, 2, 3]]   # [ax,ay,bx,by]
    kp = len(pidx)
    combined = np.concatenate([pidx, lidx])

    def _r(xs):
        P = pvf(xs[:kp])
        ax, ay, bx, by = xs[kp], xs[kp+1], xs[kp+2], xs[kp+3]
        mx = (ax + bx) / 2.0
        my = (ay + by) / 2.0
        return np.array([P[0] - mx, P[1] - my])

    def _j(xs):
        Jp = pjf(xs[:kp])   # (2, kp)
        # d(P - mid)/d[ax,ay,bx,by] = [[-0.5,0,-0.5,0],[0,-0.5,0,-0.5]]
        Jl = np.array([[-0.5, 0.0, -0.5, 0.0],
                        [0.0, -0.5, 0.0, -0.5]])
        return np.hstack([Jp, Jl])

    sys.add_residual(_r, combined, _j, cid)


def _add_symmetric(sys, cid, h0, h1, axis_handle, entities, var_map):
    """Points h0 and h1 are symmetric about the axis line.

    The axis line's mirror: let the axis be defined by its p1/p2.
    For a point P to be symmetric to Q about line AB:
      1. midpoint M = (P+Q)/2 lies on line AB
      2. (P-Q) is perpendicular to (B-A)
    Two scalar residuals.
    """
    idx0, vf0, jf0 = _resolve_handle(h0, entities, var_map)
    idx1, vf1, jf1 = _resolve_handle(h1, entities, var_map)
    eid_ax = axis_handle.split(".")[0]
    assert entities[eid_ax]["type"] == "line"
    aidx = var_map[eid_ax][[0, 1, 2, 3]]   # [ax,ay,bx,by]
    k0, k1, ka = len(idx0), len(idx1), 4
    combined = np.concatenate([idx0, idx1, aidx])

    def _r(xs):
        P = vf0(xs[:k0])
        Q = vf1(xs[k0:k0+k1])
        ax, ay, bx, by = xs[k0+k1], xs[k0+k1+1], xs[k0+k1+2], xs[k0+k1+3]
        Mx = (P[0]+Q[0])/2.0; My = (P[1]+Q[1])/2.0  # midpoint
        dxab = bx-ax; dyab = by-ay
        # midpoint on line: cross product (B-A) x (M-A) = 0
        c1 = dxab*(My-ay) - dyab*(Mx-ax)
        # perpendicularity: dot(P-Q, B-A) = 0
        c2 = (P[0]-Q[0])*dxab + (P[1]-Q[1])*dyab
        return np.array([c1, c2])

    def _j(xs):
        P = vf0(xs[:k0])
        Q = vf1(xs[k0:k0+k1])
        ax, ay, bx, by = xs[k0+k1], xs[k0+k1+1], xs[k0+k1+2], xs[k0+k1+3]
        Jp0 = jf0(xs[:k0])    # (2, k0)
        Jq0 = jf1(xs[k0:k0+k1])  # (2, k1)
        dxab = bx-ax; dyab = by-ay
        Mx = (P[0]+Q[0])/2.0; My = (P[1]+Q[1])/2.0

        # c1 = dxab*(My-ay) - dyab*(Mx-ax)
        # dc1/dP = dxab*0.5*[0,1] - dyab*0.5*[1,0]  ... via Mx/My
        # dc1/dPx = -dyab*0.5
        # dc1/dPy = dxab*0.5
        # similarly dc1/dQ = same
        dc1_dP = np.array([-dyab*0.5, dxab*0.5])  # shape (2,)
        dc1_dQ = np.array([-dyab*0.5, dxab*0.5])

        # c2 = (Px-Qx)*dxab + (Py-Qy)*dyab
        # dc2/dPx = dxab, dc2/dPy = dyab
        dc2_dP = np.array([dxab, dyab])
        dc2_dQ = np.array([-dxab, -dyab])

        # dc1/d[ax,ay,bx,by]:
        # c1 = (bx-ax)*(My-ay) - (by-ay)*(Mx-ax)
        # dc1/dax = -1*(My-ay) + (by-ay)*1 = -(My-ay) + dyab ... wait:
        # Actually: expanded using same logic as parallel constraint
        # c1 = bx*My - bx*ay - ax*My + ax*ay - by*Mx + by*ax + ay*Mx - ay*ax
        # dc1/dax = -My + ay + by = -My + ay + by
        # dc1/day = -bx + ax - Mx + ax ... hmm let me just use the formula directly:
        # c1 = dxab*(My-ay) - dyab*(Mx-ax)
        # dc1/dax = -1*(My-ay) - (-1)*(Mx-ax)*... no:
        # d(dxab)/dax = -1, d(dyab)/dax = 0, d(Mx)/dax = 0, d(My)/dax = 0
        # dc1/dax = -1*(My-ay) - 0 = -(My-ay)
        # dc1/day = d(-dyab)/day... d(dyab)/day = -1 (since dy = by-ay)
        # dc1/day = dxab*(-1) - (-1)*(Mx-ax) - dyab*0 = -dxab + (Mx-ax)
        # Hmm, let me be even more careful:
        # c1 = (bx-ax)*(My-ay) - (by-ay)*(Mx-ax)
        # d/dax = (-1)*(My-ay) - 0 - (by-ay)*(-1) = -(My-ay) + (by-ay)
        # d/day = (bx-ax)*(-1) - (-1)*(Mx-ax) = -(bx-ax) + (Mx-ax)
        # d/dbx = 1*(My-ay) - 0 = My-ay
        # d/dby = 0 - 1*(Mx-ax) = -(Mx-ax)
        dc1_da = np.array([-(My-ay)+(by-ay), -(bx-ax)+(Mx-ax), My-ay, -(Mx-ax)])

        # dc2/d[ax,ay,bx,by]:
        # c2 = (Px-Qx)*dxab + (Py-Qy)*dyab
        # d/dax = (Px-Qx)*(-1) = -(Px-Qx)
        # d/day = (Py-Qy)*(-1) = -(Py-Qy)
        # d/dbx = (Px-Qx)
        # d/dby = (Py-Qy)
        dc2_da = np.array([-(P[0]-Q[0]), -(P[1]-Q[1]), P[0]-Q[0], P[1]-Q[1]])

        # Build J row by row
        # Row 0: c1; Row 1: c2
        # Columns: [P_vars (k0), Q_vars (k1), axis_vars (4)]
        J = np.zeros((2, k0 + k1 + 4))
        # c1 w.r.t. P
        J[0, :k0] = dc1_dP @ Jp0   # (2,) @ (2, k0) = (k0,)
        # c1 w.r.t. Q
        J[0, k0:k0+k1] = dc1_dQ @ Jq0
        # c1 w.r.t. axis
        J[0, k0+k1:] = dc1_da
        # c2 w.r.t. P
        J[1, :k0] = dc2_dP @ Jp0
        # c2 w.r.t. Q
        J[1, k0:k0+k1] = dc2_dQ @ Jq0
        # c2 w.r.t. axis
        J[1, k0+k1:] = dc2_da
        return J

    sys.add_residual(_r, combined, _j, cid)


def _add_fix(sys, cid, handle, entities, var_map):
    """Fix constraint (SolidWorks "Fix" relation).

    Bare entity-id ref (C50): freeze the WHOLE entity — line: p1 AND p2 (4
    residuals); circle: center + radius (3); arc: center + radius + both angles
    (5); point: its position (unchanged). Sub-handle refs ("e1.p2") keep the
    single-point behavior. The host uses bare-id fix to pin projected reference
    geometry (sketch_project_ref)."""
    eid = handle.split(".")[0]
    if eid == "origin" and eid not in entities:
        return  # the virtual origin is implicitly fixed — nothing to pin
    ent = entities[eid]
    etype = ent["type"]
    sub = handle.split(".")[1] if "." in handle else None

    if etype == "point":
        px, py = ent["p"]
        idx = var_map[eid]
        tx, ty = px, py
        sys.add_residual(
            lambda xs, _tx=tx, _ty=ty: np.array([xs[0] - _tx, xs[1] - _ty]),
            idx, lambda xs: np.eye(2), cid)

    elif etype == "line":
        if sub is None:
            # whole line: both endpoints (4 residuals)
            p1x, p1y = ent["p1"]
            p2x, p2y = ent["p2"]
            idx = var_map[eid][[0, 1, 2, 3]]
            tgt = np.array([p1x, p1y, p2x, p2y], dtype=float)
            sys.add_residual(
                lambda xs, _t=tgt: xs - _t,
                idx, lambda xs: np.eye(4), cid)
        elif sub == "p1":
            px, py = ent["p1"]
            idx = var_map[eid][[0, 1]]
            sys.add_residual(
                lambda xs, _tx=px, _ty=py: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)
        elif sub == "p2":
            px, py = ent["p2"]
            idx = var_map[eid][[2, 3]]
            sys.add_residual(
                lambda xs, _tx=px, _ty=py: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)

    elif etype == "circle":
        if sub is None:
            # whole circle: center + radius (3 residuals)
            cx, cy = ent["center"]
            idx = var_map[eid][[0, 1, 2]]
            tgt = np.array([cx, cy, ent["radius"]], dtype=float)
            sys.add_residual(
                lambda xs, _t=tgt: xs - _t,
                idx, lambda xs: np.eye(3), cid)
        elif sub == "center":
            cx, cy = ent["center"]
            idx = var_map[eid][[0, 1]]
            sys.add_residual(
                lambda xs, _tx=cx, _ty=cy: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)

    elif etype == "arc":
        if sub is None:
            # whole arc: center + radius + both angles (5 residuals)
            cx, cy = ent["center"]
            idx = var_map[eid][[0, 1, 2, 3, 4]]
            tgt = np.array([cx, cy, ent["radius"], ent["start"], ent["end"]],
                           dtype=float)
            sys.add_residual(
                lambda xs, _t=tgt: xs - _t,
                idx, lambda xs: np.eye(5), cid)
        elif sub == "center":
            cx, cy = ent["center"]
            idx = var_map[eid][[0, 1]]
            sys.add_residual(
                lambda xs, _tx=cx, _ty=cy: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)
        elif sub == "start":
            vidx, vf, jf = _resolve_handle(handle, entities, var_map)
            # Get the actual start point coordinates from entity
            cx, cy = ent["center"]
            r = ent["radius"]
            a0 = ent["start"]
            tx = cx + r * math.cos(a0 * _DEG)
            ty = cy + r * math.sin(a0 * _DEG)
            k = len(vidx)
            sys.add_residual(
                lambda xs, _tx=tx, _ty=ty, _vf=vf, _k=k: _vf(xs) - np.array([_tx, _ty]),
                vidx, lambda xs, _jf=jf: _jf(xs), cid)

    elif etype == "ellipse":
        if sub == "center" or sub is None:
            cx, cy = ent["center"]
            idx = var_map[eid][[0, 1]]
            sys.add_residual(
                lambda xs, _tx=cx, _ty=cy: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)

    elif etype == "slot":
        if sub == "p1" or sub is None:
            px, py = ent["p1"]
            idx = var_map[eid][[0, 1]]
            sys.add_residual(
                lambda xs, _tx=px, _ty=py: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)
        elif sub == "p2":
            px, py = ent["p2"]
            idx = var_map[eid][[2, 3]]
            sys.add_residual(
                lambda xs, _tx=px, _ty=py: np.array([xs[0] - _tx, xs[1] - _ty]),
                idx, lambda xs: np.eye(2), cid)

    elif etype in ("ellipse_arc", "parabola", "conic", "arc_slot"):
        # Generic fix on any resolvable handle (center/start/end/apex/vertex):
        # pin the derived point to its current world position via _resolve_handle.
        vidx, vf, jf = _resolve_handle(handle, entities, var_map)
        # Seed init slice from the entity's current variable values so the target
        # is the *current* world coords of that handle (matches arc.start above).
        cur = _entity_init_slice(ent)
        # Map the handle's vidx (global indices) back to local positions within
        # the entity block to read the seed values.
        base = var_map[eid][0]
        local = [int(i - base) for i in vidx]
        seed = np.array([cur[i] for i in local], dtype=float)
        tx, ty = vf(seed)
        sys.add_residual(
            lambda xs, _tx=tx, _ty=ty, _vf=vf: _vf(xs) - np.array([_tx, _ty]),
            vidx, lambda xs, _jf=jf: _jf(xs), cid)


def _entity_init_slice(ent: dict) -> list:
    """Return the initial variable values for an entity in allocation order.

    Used by _add_fix for conic-family handles so the pinned target equals the
    handle's current world position. Mirrors _allocate_entity's layout.
    """
    et = ent["type"]
    if et == "ellipse_arc":
        cx, cy = ent["center"]
        return [cx, cy, ent["rx"], ent["ry"], ent.get("rot", 0.0), ent["a0"], ent["a1"]]
    if et == "parabola":
        vx, vy = ent["vertex"]
        fx, fy = ent["focus"]
        return [vx, vy, fx, fy, ent["t0"], ent["t1"]]
    if et == "conic":
        p0x, p0y = ent["p0"]
        p1x, p1y = ent["p1"]
        apx, apy = ent["apex"]
        return [p0x, p0y, p1x, p1y, apx, apy, ent.get("rho", 0.5)]
    if et == "arc_slot":
        cx, cy = ent["center"]
        return [cx, cy, ent["radius"], ent["a0"], ent["a1"], ent["width"]]
    return []


def _add_point_on(sys, cid, point_handle, entity_handle, entities, var_map):
    """Point lies on entity (line, circle, or arc)."""
    pidx, pvf, pjf = _resolve_handle(point_handle, entities, var_map)
    eid = entity_handle.split(".")[0]
    ent = entities[eid]
    etype = ent["type"]
    kp = len(pidx)

    if etype == "line":
        lidx = var_map[eid][[0, 1, 2, 3]]  # [ax,ay,bx,by]
        combined = np.concatenate([pidx, lidx])

        def _r(xs):
            P = pvf(xs[:kp])
            ax, ay, bx, by = xs[kp], xs[kp+1], xs[kp+2], xs[kp+3]
            return np.array([(bx-ax)*(P[1]-ay) - (by-ay)*(P[0]-ax)])

        def _j(xs):
            P = pvf(xs[:kp])
            Jp = pjf(xs[:kp])   # (2, kp)
            ax, ay, bx, by = xs[kp], xs[kp+1], xs[kp+2], xs[kp+3]
            dxab = bx-ax; dyab = by-ay
            # d/dP: cross = dxab*(Py-ay) - dyab*(Px-ax)
            # dc/dPx = -dyab; dc/dPy = dxab
            dc_dP = np.array([-dyab, dxab])  # (2,)
            dc_dP_full = dc_dP @ Jp  # (kp,)
            # d/d[ax,ay,bx,by]:
            # Using same as point_on_line from test_nsolve:
            # dpx->-(by-ay), dpy->(bx-ax), dax->-(py-ay)+(by-ay), day->-(bx-ax)+(px-ax), dbx->(py-ay), dby->-(px-ax)
            Px, Py = P[0], P[1]
            dax = -(Py-ay) + (by-ay)   # = -(Py-ay) + dyab
            day = (Px-ax) - (bx-ax)    # = (Px-ax) - dxab
            dbx = Py-ay
            dby = -(Px-ax)
            dc_dl = np.array([dax, day, dbx, dby])
            J = np.zeros((1, kp + 4))
            J[0, :kp] = dc_dP_full
            J[0, kp:] = dc_dl
            return J

        sys.add_residual(_r, combined, _j, cid)

    elif etype in ("circle", "arc"):
        cidx = var_map[eid][[0, 1, 2]]  # [cx, cy, r]
        combined = np.concatenate([pidx, cidx])

        def _r(xs):
            P = pvf(xs[:kp])
            cx, cy, r = xs[kp], xs[kp+1], xs[kp+2]
            return np.array([math.hypot(P[0]-cx, P[1]-cy) - r])

        def _j(xs):
            P = pvf(xs[:kp])
            Jp = pjf(xs[:kp])
            cx, cy, r = xs[kp], xs[kp+1], xs[kp+2]
            dx = P[0]-cx; dy = P[1]-cy
            d = math.hypot(dx, dy)
            if d < 1e-12:
                return np.zeros((1, kp + 3))
            # d(|P-C| - r) / d[P] = (P-C)/d
            # d/dP_full = ((P-C)/d)^T @ Jp
            dP_dir = np.array([dx/d, dy/d])
            J = np.zeros((1, kp + 3))
            J[0, :kp] = dP_dir @ Jp
            J[0, kp] = -dx/d
            J[0, kp+1] = -dy/d
            J[0, kp+2] = -1.0
            return J

        sys.add_residual(_r, combined, _j, cid)

    elif etype == "ellipse_arc":
        # Implicit rotated-ellipse residual; ignores a0/a1 trim (full ellipse),
        # mirroring the arc/circle point_on convention above.
        eidx = var_map[eid][[0, 1, 2, 3, 4]]   # cx,cy,rx,ry,rot
        combined = np.concatenate([pidx, eidx])
        ke = 5

        def _r(xs):
            P = pvf(xs[:kp])
            return np.array([_ellipse_implicit(xs[kp:kp+ke], P)])

        def _j(xs):
            return _numeric_jac_1(lambda v: np.array(
                [_ellipse_implicit(v[kp:kp+ke], pvf(v[:kp]))]), xs, kp + ke)

        sys.add_residual(_r, combined, _j, cid)

    elif etype == "parabola":
        # Closest-param projection: residual = signed offset of P from the curve
        # along the curve normal at the projected param t*. Range-free (ignores
        # t0/t1), matching the arc convention.
        eidx = var_map[eid][[0, 1, 2, 3]]   # vx,vy,fx,fy
        combined = np.concatenate([pidx, eidx])
        ke = 4

        def _r(xs):
            P = pvf(xs[:kp])
            cv = xs[kp:kp+ke]
            t = _parabola_project(cv, P)
            Q, dQ = _parabola_curve(cv, t)
            return np.array([_signed_offset(P, Q, dQ)])

        def _j(xs):
            return _numeric_jac_1(lambda v: _r_proj_parabola(v, pvf, kp, ke),
                                  xs, kp + ke)

        sys.add_residual(_r, combined, _j, cid)

    elif etype == "conic":
        eidx = var_map[eid][[0, 1, 2, 3, 4, 5, 6]]   # p0,p1,apex,rho
        combined = np.concatenate([pidx, eidx])
        ke = 7

        def _r(xs):
            P = pvf(xs[:kp])
            cv = xs[kp:kp+ke]
            u = _conic_project(cv, P)
            Q, dQ = _conic_curve(cv, u)
            return np.array([_signed_offset(P, Q, dQ)])

        def _j(xs):
            return _numeric_jac_1(lambda v: _r_proj_conic(v, pvf, kp, ke),
                                  xs, kp + ke)

        sys.add_residual(_r, combined, _j, cid)

    # else: unsupported entity type for point_on — skip silently.


# --- point_on projection / numeric helpers (C32-B) -------------------------


def _signed_offset(P, Q, dQ):
    """Signed perpendicular offset of P from curve point Q with tangent dQ.

    = (P - Q) · n,  n = unit normal (perp of tangent). Zero iff P lies on the
    curve at Q; sign keeps the residual smooth across the curve for Newton.
    """
    L = math.hypot(dQ[0], dQ[1])
    if L < 1e-12:
        return math.hypot(P[0] - Q[0], P[1] - Q[1])
    nx, ny = -dQ[1] / L, dQ[0] / L
    return (P[0] - Q[0]) * nx + (P[1] - Q[1]) * ny


def _parabola_project(cv, P, t0=-200.0, t1=200.0):
    """Param t minimizing |P - parabola(t)|: coarse scan + Newton refine."""
    best_t, best_d = t0, float("inf")
    for k in range(81):
        t = t0 + (t1 - t0) * k / 80.0
        Q, _ = _parabola_curve(cv, t)
        d = (P[0] - Q[0]) ** 2 + (P[1] - Q[1]) ** 2
        if d < best_d:
            best_d, best_t = d, t
    return _newton_project(lambda t: _parabola_curve(cv, t), best_t, P)


def _conic_project(cv, P):
    """Param u∈[0,1] minimizing |P - conic(u)|: coarse scan + Newton refine."""
    best_u, best_d = 0.0, float("inf")
    for k in range(41):
        u = k / 40.0
        Q, _ = _conic_curve(cv, u)
        d = (P[0] - Q[0]) ** 2 + (P[1] - Q[1]) ** 2
        if d < best_d:
            best_d, best_u = d, u
    u = _newton_project(lambda u: _conic_curve(cv, u), best_u, P)
    return min(max(u, 0.0), 1.0)


def _newton_project(curve_fn, p0, P, iters=12):
    """Refine the closest-point param: root of g(p)=(C(p)-P)·C'(p)=0 (Newton)."""
    p = p0
    for _ in range(iters):
        Q, dQ = curve_fn(p)
        diff = np.array([Q[0] - P[0], Q[1] - P[1]])
        g = diff[0] * dQ[0] + diff[1] * dQ[1]
        # g' ~ |C'|^2 + diff·C'' ; approximate C'' numerically (cheap, stable)
        h = 1e-4
        _, dQp = curve_fn(p + h)
        _, dQm = curve_fn(p - h)
        ddQ = (dQp - dQm) / (2.0 * h)
        gp = dQ[0] ** 2 + dQ[1] ** 2 + diff[0] * ddQ[0] + diff[1] * ddQ[1]
        if abs(gp) < 1e-14:
            break
        step = g / gp
        p -= step
        if abs(step) < 1e-12:
            break
    return p


def _r_proj_parabola(v, pvf, kp, ke):
    P = pvf(v[:kp])
    cv = v[kp:kp+ke]
    t = _parabola_project(cv, P)
    Q, dQ = _parabola_curve(cv, t)
    return np.array([_signed_offset(P, Q, dQ)])


def _r_proj_conic(v, pvf, kp, ke):
    P = pvf(v[:kp])
    cv = v[kp:kp+ke]
    u = _conic_project(cv, P)
    Q, dQ = _conic_curve(cv, u)
    return np.array([_signed_offset(P, Q, dQ)])


def _numeric_jac_1(rfn, xs, n):
    """Central-difference Jacobian of a scalar residual rfn(x)->(1,) over n vars."""
    xs = np.asarray(xs, dtype=float)
    J = np.zeros((1, n))
    for i in range(n):
        h = 1e-6 * max(1.0, abs(xs[i]))
        xp = xs.copy(); xp[i] += h
        xm = xs.copy(); xm[i] -= h
        J[0, i] = (rfn(xp)[0] - rfn(xm)[0]) / (2.0 * h)
    return J


def _distance_cond_sign(h, c, entities) -> float:
    """Per-ref arc-condition sign for a distance constraint (C50, SolidWorks min/max).

    Returns -1.0 for "min" (near-rim gap), +1.0 for "max" (far rim), 0.0 otherwise.
    A condition is honored ONLY when the ref is a BARE circle/arc entity id (no
    ".sub"); any other ref kind is treated as "center" (sign 0)."""
    if c not in ("min", "max"):
        return 0.0
    if not isinstance(h, str) or "." in h:
        return 0.0
    ent = entities.get(h)
    if ent is None or ent.get("type") not in ("circle", "arc"):
        return 0.0
    return -1.0 if c == "min" else 1.0


def _add_distance(sys, cid, h0, h1, d, entities, var_map, axis=None, cond=None):
    """Distance constraint: point-point, or point-line (perpendicular foot distance).

    ``axis`` (point-point only): ``"h"`` constrains the horizontal gap |Δu|, ``"v"`` the vertical
    gap |Δv|; ``None`` is the aligned Euclidean distance (the historical behavior). A point-line
    distance ignores ``axis`` (the foot distance is already directional).

    ``cond`` (C50): optional 2-list aligned with [h0, h1], each "center"|"min"|"max".
    For a ref that is a BARE circle/arc entity id, "min"/"max" measure to the near/far
    rim instead of the center: residual = (D + Σ adj) - d with adj = -r ("min") or +r
    ("max"); the radius r is a solver VARIABLE (∂residual/∂r = ∓1). Every other ref
    kind ignores its condition ("center" — today's behavior). Absent cond == today.
    """
    # Determine kinds (entities.get: the virtual "origin" handle is not an entity)
    def _is_line(h):
        eid = h.split(".")[0]
        ent = entities.get(eid)
        return ent is not None and ent["type"] == "line" and "." not in h

    conds = list(cond) if isinstance(cond, (list, tuple)) else []
    conds = (conds + [None, None])[:2]
    s0 = _distance_cond_sign(h0, conds[0], entities)
    s1 = _distance_cond_sign(h1, conds[1], entities)

    if _is_line(h0) or _is_line(h1):
        # point-line distance (the "point" may be a bare circle/arc center)
        if _is_line(h0):
            line_h, pt_h, s_pt = h0, h1, s1
        else:
            line_h, pt_h, s_pt = h1, h0, s0
        eid_l = line_h.split(".")[0]
        lidx = var_map[eid_l][[0, 1, 2, 3]]
        pidx, pvf, pjf = _resolve_handle(pt_h, entities, var_map)
        kp = len(pidx)
        # min/max condition on a circle/arc ref: append its radius variable.
        rparts = [var_map[pt_h][[2]]] if s_pt else []
        combined = np.concatenate([pidx, lidx] + rparts)
        _d = float(d)

        def _r(xs):
            P = pvf(xs[:kp])
            ax, ay, bx, by = xs[kp], xs[kp+1], xs[kp+2], xs[kp+3]
            adj = s_pt * xs[kp+4] if s_pt else 0.0
            dxab = bx-ax; dyab = by-ay
            L = math.hypot(dxab, dyab)
            if L < 1e-14:
                return np.array([math.hypot(P[0]-ax, P[1]-ay) + adj - _d])
            cross = dxab*(P[1]-ay) - dyab*(P[0]-ax)
            return np.array([abs(cross)/L + adj - _d])

        def _j(xs):
            P = pvf(xs[:kp])
            Jp = pjf(xs[:kp])
            ax, ay, bx, by = xs[kp], xs[kp+1], xs[kp+2], xs[kp+3]
            dxab = bx-ax; dyab = by-ay
            L2 = dxab**2 + dyab**2
            L = math.sqrt(L2) if L2 > 1e-28 else 1e-14
            cross = dxab*(P[1]-ay) - dyab*(P[0]-ax)
            sgn = 1.0 if cross >= 0 else -1.0
            Px, Py = P[0], P[1]
            # d(cross)/dP: cross = dxab*(Py-ay) - dyab*(Px-ax)
            dc_dPx = -dyab; dc_dPy = dxab
            dc_dP = np.array([dc_dPx, dc_dPy])
            # d(dist)/dP = sgn * d(cross)/dP / L
            J = np.zeros((1, kp + 4 + (1 if s_pt else 0)))
            J[0, :kp] = sgn * (dc_dP @ Jp) / L
            # Analytic Jacobian for d(|cross|/L - _d) w.r.t. [ax,ay,bx,by]:
            # cross = (bx-ax)*(Py-ay) - (by-ay)*(Px-ax)
            # Expanded: bx*Py - bx*ay - ax*Py - by*Px + by*ax + ay*Px
            # d/dax = by-Py; d/day = Px-bx; d/dbx = Py-ay; d/dby = ax-Px
            # NOTE: this cross is the NEGATIVE of _add_tangent_line_circle's (which uses (A-C),
            # ours uses (P-A)) — its d_cross2 values must be negated here, not copied. The copied
            # (un-negated) partials made the Jacobian row rank-independent of geometrically
            # redundant dims (phantom DOF loss, no redundant tag) and broke Newton steps whenever
            # the LINE's variables were free (spurious non-convergence).
            dc_ax = by - Py;  dc_ay = Px - bx
            dc_bx = Py - ay;  dc_by = ax - Px
            d_L_ax = -dxab / L; d_L_ay = -dyab / L
            d_L_bx = dxab / L;  d_L_by = dyab / L
            # dist = |cross|/L; d(dist)/d[...] = (sgn*dc_[...]*L - abs(cross)*dL_[...])/L^2.
            # sgn applies ONLY to the dc term (chain rule through |cross|); multiplying the whole
            # quotient by sgn negates the dL term too, which is wrong whenever cross < 0 and the
            # line's own variables are free — one garbage column poisons the QR rank analysis
            # (missed redundancy => silent over-define) and Newton steps (false non-convergence).
            def dd(dc, dl):
                return (sgn * dc * L - abs(cross) * dl) / L2
            J[0, kp]   = dd(dc_ax, d_L_ax)
            J[0, kp+1] = dd(dc_ay, d_L_ay)
            J[0, kp+2] = dd(dc_bx, d_L_bx)
            J[0, kp+3] = dd(dc_by, d_L_by)
            if s_pt:
                J[0, kp+4] = s_pt   # ∂(D + s·r - d)/∂r
            return J

        sys.add_residual(_r, combined, _j, cid)

    else:
        # point-point distance (a bare circle/arc ref resolves to its center)
        idx0, vf0, jf0 = _resolve_handle(h0, entities, var_map)
        idx1, vf1, jf1 = _resolve_handle(h1, entities, var_map)
        k0, k1 = len(idx0), len(idx1)
        # min/max conditions append the circle/arc radius variables, in ref order.
        rparts = []
        sgns = []
        for h, s in ((h0, s0), (h1, s1)):
            if s:
                rparts.append(var_map[h][[2]])
                sgns.append(s)
        combined = np.concatenate([idx0, idx1] + rparts)
        kb = k0 + k1
        _d = float(d)
        ax = (axis or None)
        if isinstance(ax, str):
            ax = ax.lower()

        def _adj(xs):
            return sum(s * xs[kb + j] for j, s in enumerate(sgns))

        if ax in ("h", "v"):
            # Axis-aligned gap: residual = |Δcoord| - d (coord = u for "h", v for "v"). Smooth via a
            # signed residual whose sign tracks the current gap (avoids the abs() kink at the root;
            # mirrors how the angle residual uses a signed atan2 with a fixed sign per evaluation).
            comp = 0 if ax == "h" else 1   # which coordinate (x=u, y=v)

            def _r(xs):
                P0 = vf0(xs[:k0])
                P1 = vf1(xs[k0:k0+k1])
                return np.array([abs(P0[comp] - P1[comp]) + _adj(xs) - _d])

            def _j(xs):
                P0 = vf0(xs[:k0])
                P1 = vf1(xs[k0:k0+k1])
                J0 = jf0(xs[:k0])
                J1 = jf1(xs[k0:k0+k1])
                delta = P0[comp] - P1[comp]
                sgn = 1.0 if delta >= 0 else -1.0
                J = np.zeros((1, kb + len(sgns)))
                J[0, :k0] = sgn * J0[comp, :]
                J[0, k0:kb] = -sgn * J1[comp, :]
                for j, s in enumerate(sgns):
                    J[0, kb + j] = s
                return J

            sys.add_residual(_r, combined, _j, cid)
            return

        def _r(xs):
            P0 = vf0(xs[:k0])
            P1 = vf1(xs[k0:k0+k1])
            return np.array([math.hypot(P0[0]-P1[0], P0[1]-P1[1]) + _adj(xs) - _d])

        def _j(xs):
            P0 = vf0(xs[:k0])
            P1 = vf1(xs[k0:k0+k1])
            J0 = jf0(xs[:k0])     # (2, k0)
            J1 = jf1(xs[k0:k0+k1])  # (2, k1)
            dx = P0[0]-P1[0]; dy = P0[1]-P1[1]
            d = math.hypot(dx, dy)
            if d < 1e-12:
                return np.zeros((1, kb + len(sgns)))
            dir_ = np.array([dx/d, dy/d])  # (2,)
            J = np.zeros((1, kb + len(sgns)))
            J[0, :k0] = dir_ @ J0
            J[0, k0:kb] = -dir_ @ J1
            for j, s in enumerate(sgns):
                J[0, kb + j] = s   # ∂(D + s·r - d)/∂r
            return J

        sys.add_residual(_r, combined, _j, cid)


def _add_angle(sys, cid, h0, h1, ang_deg, entities, var_map):
    """Angle between two lines (the unsigned angle, in degrees).

    residual: atan2(cross, dot) - ang_rad
    where cross = (B-A) x (D-C), dot = (B-A).(D-C)
    """
    eid0 = h0.split(".")[0]
    eid1 = h1.split(".")[0]
    assert entities[eid0]["type"] == "line" and entities[eid1]["type"] == "line"
    idx0 = var_map[eid0][[0, 1, 2, 3]]
    idx1 = var_map[eid1][[0, 1, 2, 3]]
    combined = np.concatenate([idx0, idx1])
    ang_rad = ang_deg * _DEG

    def _r(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        ux, uy = bx-ax, by-ay
        vx, vy = dx-cx, dy-cy
        # Use the angle between the direction vectors:
        # residual = |dot/(|u||v|)| - cos(ang)  ... but that has issues near 0/180
        # Better: use sin/cos pair residuals? Or just atan2.
        # atan2(|cross|, dot) gives unsigned angle in [0, pi]
        # We want residual = atan2(|cross|, dot) - ang_rad
        cross = ux*vy - uy*vx
        dot = ux*vx + uy*vy
        theta = math.atan2(abs(cross), dot)
        return np.array([theta - ang_rad])

    def _j(xs):
        ax, ay, bx, by = xs[0], xs[1], xs[2], xs[3]
        cx, cy, dx, dy = xs[4], xs[5], xs[6], xs[7]
        ux, uy = bx-ax, by-ay
        vx, vy = dx-cx, dy-cy
        cross = ux*vy - uy*vx
        dot = ux*vx + uy*vy
        abs_cross = abs(cross)
        sgn_cross = 1.0 if cross >= 0 else -1.0
        denom = dot**2 + abs_cross**2   # = |u|^2 * |v|^2

        if denom < 1e-24:
            return np.zeros((1, 8))

        # d(atan2(|c|, d)) = (d * d(|c|) - |c| * d(d)) / denom
        # d(|c|)/d[...] = sgn_cross * d(cross)/d[...]
        # d(cross)/dux = vy, d(cross)/duy = -vx, d(cross)/dvx = -uy, d(cross)/dvy = ux
        dc_dux = vy; dc_duy = -vx; dc_dvx = -uy; dc_dvy = ux
        # d(dot)/dux = vx, d(dot)/duy = vy, d(dot)/dvx = ux, d(dot)/dvy = uy
        dd_dux = vx; dd_duy = vy; dd_dvx = ux; dd_dvy = uy

        def dtheta(dc_d_var, dd_d_var):
            return (dot * sgn_cross * dc_d_var - abs_cross * dd_d_var) / denom

        # Chain rule: dux/d[ax,ay,bx,by] = [-1,0,1,0]; duy = [0,-1,0,1]
        # dvx/d[cx,cy,dx,dy] = [-1,0,1,0]; dvy = [0,-1,0,1]
        J = np.zeros((1, 8))
        # u components: ux = bx-ax, uy = by-ay
        dt_dux = dtheta(dc_dux, dd_dux)
        dt_duy = dtheta(dc_duy, dd_duy)
        J[0, 0] = -dt_dux   # dax
        J[0, 1] = -dt_duy   # day
        J[0, 2] = dt_dux    # dbx
        J[0, 3] = dt_duy    # dby
        # v components: vx = dx-cx, vy = dy-cy
        dt_dvx = dtheta(dc_dvx, dd_dvx)
        dt_dvy = dtheta(dc_dvy, dd_dvy)
        J[0, 4] = -dt_dvx   # dcx
        J[0, 5] = -dt_dvy   # dcy
        J[0, 6] = dt_dvx    # ddx
        J[0, 7] = dt_dvy    # ddy
        return J

    sys.add_residual(_r, combined, _j, cid)


# ---------------------------------------------------------------------------
# Extract solved coordinates from variable vector
# ---------------------------------------------------------------------------

def _extract_entities(x: np.ndarray, entities_list: list, var_map: dict) -> EntityCoords:
    """Build entity coord dict from the solved variable vector."""
    result = {}
    for ent in entities_list:
        eid = ent["id"]
        etype = ent["type"]
        vidx = var_map[eid]
        vals = x[vidx]

        if etype == "point":
            result[eid] = {"type": "point", "p": [float(vals[0]), float(vals[1])]}

        elif etype == "line":
            result[eid] = {
                "type": "line",
                "p1": [float(vals[0]), float(vals[1])],
                "p2": [float(vals[2]), float(vals[3])],
            }

        elif etype == "circle":
            result[eid] = {
                "type": "circle",
                "center": [float(vals[0]), float(vals[1])],
                "radius": float(vals[2]),
            }

        elif etype == "arc":
            result[eid] = {
                "type": "arc",
                "center": [float(vals[0]), float(vals[1])],
                "radius": float(vals[2]),
                "start": float(vals[3]),
                "end": float(vals[4]),
            }

        elif etype == "ellipse":
            result[eid] = {
                "type": "ellipse",
                "center": [float(vals[0]), float(vals[1])],
                "rx": float(vals[2]),
                "ry": float(vals[3]),
                "rot": float(vals[4]),
            }

        elif etype == "ellipse_arc":
            result[eid] = {
                "type": "ellipse_arc",
                "center": [float(vals[0]), float(vals[1])],
                "rx": float(vals[2]),
                "ry": float(vals[3]),
                "rot": float(vals[4]),
                "a0": float(vals[5]),
                "a1": float(vals[6]),
            }

        elif etype == "parabola":
            result[eid] = {
                "type": "parabola",
                "vertex": [float(vals[0]), float(vals[1])],
                "focus": [float(vals[2]), float(vals[3])],
                "t0": float(vals[4]),
                "t1": float(vals[5]),
            }

        elif etype == "conic":
            result[eid] = {
                "type": "conic",
                "p0": [float(vals[0]), float(vals[1])],
                "p1": [float(vals[2]), float(vals[3])],
                "apex": [float(vals[4]), float(vals[5])],
                "rho": float(vals[6]),
            }

        elif etype == "slot":
            result[eid] = {
                "type": "slot",
                "p1": [float(vals[0]), float(vals[1])],
                "p2": [float(vals[2]), float(vals[3])],
                "width": float(vals[4]),
            }

        elif etype == "arc_slot":
            result[eid] = {
                "type": "arc_slot",
                "center": [float(vals[0]), float(vals[1])],
                "radius": float(vals[2]),
                "a0": float(vals[3]),
                "a1": float(vals[4]),
                "width": float(vals[5]),
            }

        elif etype == "spline":
            n = len(vidx) // 2
            pts = [[float(vals[2*i]), float(vals[2*i+1])] for i in range(n)]
            result[eid] = {"type": "spline", "points": pts}

        elif etype == "text":
            result[eid] = {"type": "text", "p": [float(vals[0]), float(vals[1])]}

    return result


# ---------------------------------------------------------------------------
# Per-entity status via nullspace analysis
# ---------------------------------------------------------------------------

def _compute_entity_statuses(
    x: np.ndarray,
    entities_list: list,
    var_map: dict,
    sys: System,
    analysis,
) -> Dict[str, str]:
    """Return "under"|"full"|"over" for each entity.

    Strategy:
    - If analysis.conflicting_tags is non-empty: any entity whose variable set
      intersects a conflicting constraint is "over"; others follow the normal path.
    - Otherwise compute the nullspace of the Jacobian. A variable is "determined"
      if it has near-zero projection onto the nullspace basis. An entity is "full"
      iff all its variables are determined; "under" if any are free.

    This is kept inside sketch_solver.py and uses the same QR machinery as
    nsolve.analyze (we call analyze and then compute the nullspace from J directly).
    """
    import scipy.linalg as sla

    statuses: Dict[str, str] = {}

    # Build dense Jacobian over real (non-soft) blocks
    real_blocks = [b for b in sys._blocks if not b.soft]
    n = sys.n_vars
    m_total = sum(b.m for b in real_blocks)

    if m_total == 0 or n == 0:
        for ent in entities_list:
            statuses[ent["id"]] = "under"
        return statuses

    J = np.zeros((m_total, n), dtype=np.float64)
    row = 0
    for b in real_blocks:
        jb = np.atleast_2d(np.asarray(b.jfunc(x[b.var_idx]), dtype=np.float64))
        J[row:row+b.m, b.var_idx] = jb
        row += b.m

    # SVD of J to get the nullspace
    _, s, Vt = np.linalg.svd(J, full_matrices=True)
    rank = int(np.sum(s > max(1e-7, 1e3 * np.finfo(float).eps * (s[0] if s.size else 1.0))))
    # Nullspace basis: rows of Vt[rank:] (each row is a nullspace direction)
    null_basis = Vt[rank:]  # shape (n_null, n)

    if null_basis.shape[0] == 0:
        # No nullspace: all variables determined
        null_proj = np.zeros(n)
    else:
        # Projection of each variable onto the nullspace:
        # proj[i] = ||null_basis[:, i]|| (norm of the i-th column of the nullspace basis)
        null_proj = np.linalg.norm(null_basis, axis=0)  # shape (n,)

    tol_null = 1e-6

    # Build set of conflicting variable indices (over-defined)
    conflicting_var_set: set = set()
    if analysis.conflicting_tags:
        ctag_set = set(analysis.conflicting_tags)
        for b in real_blocks:
            if b.tag in ctag_set:
                for i in b.var_idx:
                    conflicting_var_set.add(int(i))

    for ent in entities_list:
        eid = ent["id"]
        vidx = var_map[eid]
        var_ints = [int(i) for i in vidx]

        if conflicting_var_set and any(v in conflicting_var_set for v in var_ints):
            statuses[eid] = "over"
            continue

        # Check if all entity variables are determined (zero null-space projection)
        projections = null_proj[var_ints]
        if np.all(projections < tol_null):
            statuses[eid] = "full"
        else:
            statuses[eid] = "under"

    return statuses


# ---------------------------------------------------------------------------
# Dimensional measurement (driven dims + response annotations)
# ---------------------------------------------------------------------------

_DIMENSIONAL_TYPES = ("distance", "angle", "radius", "diameter")


def _handle_point(handle: str, entity_coords: dict):
    """World (x, y) of a point handle against SOLVED entity coords (EntityCoords from
    :func:`_extract_entities`). Returns None if it cannot be resolved (dangling)."""
    if "." in handle:
        eid, sub = handle.split(".", 1)
    else:
        eid, sub = handle, None
    ent = entity_coords.get(eid)
    if ent is None:
        if eid == "origin":
            return (0.0, 0.0)   # virtual fixed sketch origin
        return None
    et = ent.get("type")
    if et == "point":
        return tuple(ent["p"])
    if et == "line":
        if sub in (None, "p1"):
            return tuple(ent["p1"])
        if sub == "p2":
            return tuple(ent["p2"])
        if sub == "mid":
            (ax, ay), (bx, by) = tuple(ent["p1"]), tuple(ent["p2"])
            return ((ax + bx) * 0.5, (ay + by) * 0.5)
    if et in ("circle", "arc", "ellipse", "ellipse_arc", "arc_slot"):
        c = ent.get("center")
        if sub in (None, "center") and c is not None:
            return tuple(c)
        if et == "arc" and sub in ("start", "end"):
            cx, cy = ent["center"]
            r = ent["radius"]
            a = ent["start"] if sub == "start" else ent["end"]
            ar = a * _DEG
            return (cx + r * math.cos(ar), cy + r * math.sin(ar))
    if et == "slot":
        if sub in (None, "p1"):
            return tuple(ent["p1"])
        if sub == "p2":
            return tuple(ent["p2"])
    return None


def _line_endpoints(handle: str, entity_coords: dict):
    """(p1, p2) of a line entity referenced by a (possibly sub-less) handle, or None."""
    eid = handle.split(".")[0]
    ent = entity_coords.get(eid)
    if ent is None or ent.get("type") != "line":
        return None
    return tuple(ent["p1"]), tuple(ent["p2"])


def _measure_cond_adj(constraint: dict, refs: list, entity_coords: dict) -> float:
    """Σ adj of a distance constraint's arc-conditions against SOLVED coords (C50).

    Mirrors :func:`_distance_cond_sign`: only a BARE circle/arc entity-id ref honors
    its "min"/"max" condition (adj = -r / +r); everything else contributes 0. Keeps
    measure_constraint in semantic lockstep with the solver residual."""
    cond = constraint.get("cond")
    if not isinstance(cond, (list, tuple)):
        return 0.0
    adj = 0.0
    for h, c in zip(refs[:2], list(cond)[:2]):
        if c not in ("min", "max") or not isinstance(h, str) or "." in h:
            continue
        ent = entity_coords.get(h)
        if ent is None or ent.get("type") not in ("circle", "arc"):
            continue
        r = float(ent.get("radius", 0.0))
        adj += -r if c == "min" else r
    return adj


def measure_constraint(constraint: dict, entity_coords: dict):
    """Current solved measurement for a DIMENSIONAL constraint, in canonical units.

    distance → mm (point-point Euclidean, or |Δu|/|Δv| with axis, or perpendicular point-line);
    angle → degrees in [0, 180); radius/diameter → mm. Geometric relations (coincident, parallel,
    …) return None. Returns None when the refs cannot be resolved against ``entity_coords``.
    """
    ctype = constraint.get("type")
    refs = constraint.get("refs", []) or []
    if ctype not in _DIMENSIONAL_TYPES:
        return None

    if ctype == "radius":
        eid = refs[0].split(".")[0] if refs else None
        ent = entity_coords.get(eid) if eid else None
        if ent and ent.get("type") in ("circle", "arc"):
            return float(ent["radius"])
        if ent and ent.get("type") in ("slot", "arc_slot"):
            return float(ent["width"]) / 2.0   # cap radius
        return None

    if ctype == "diameter":
        eid = refs[0].split(".")[0] if refs else None
        ent = entity_coords.get(eid) if eid else None
        if ent and ent.get("type") in ("circle", "arc"):
            return float(ent["radius"]) * 2.0
        if ent and ent.get("type") in ("slot", "arc_slot"):
            return float(ent["width"])         # slot width = cap diameter
        return None

    if ctype == "angle":
        if len(refs) < 2:
            return None
        la = _line_endpoints(refs[0], entity_coords)
        lb = _line_endpoints(refs[1], entity_coords)
        if la is None or lb is None:
            return None
        ux, uy = la[1][0] - la[0][0], la[1][1] - la[0][1]
        vx, vy = lb[1][0] - lb[0][0], lb[1][1] - lb[0][1]
        cross = ux * vy - uy * vx
        dot = ux * vx + uy * vy
        theta = math.degrees(math.atan2(abs(cross), dot))
        # Report in [0, 180) — atan2(|cross|, dot) already lies in [0, 180].
        return float(theta % 180.0)

    if ctype == "distance":
        if len(refs) < 2:
            return None
        # point-line?
        def _is_line(h):
            eid = h.split(".")[0]
            ent = entity_coords.get(eid)
            return ent is not None and ent.get("type") == "line" and "." not in h

        adj = _measure_cond_adj(constraint, refs, entity_coords)
        if _is_line(refs[0]) or _is_line(refs[1]):
            line_h = refs[0] if _is_line(refs[0]) else refs[1]
            pt_h = refs[1] if _is_line(refs[0]) else refs[0]
            le = _line_endpoints(line_h, entity_coords)
            P = _handle_point(pt_h, entity_coords)
            if le is None or P is None:
                return None
            (ax, ay), (bx, by) = le
            dxab, dyab = bx - ax, by - ay
            L = math.hypot(dxab, dyab)
            if L < 1e-12:
                return float(math.hypot(P[0] - ax, P[1] - ay) + adj)
            cross = dxab * (P[1] - ay) - dyab * (P[0] - ax)
            return float(abs(cross) / L + adj)
        P0 = _handle_point(refs[0], entity_coords)
        P1 = _handle_point(refs[1], entity_coords)
        if P0 is None or P1 is None:
            return None
        axis = constraint.get("axis")
        if isinstance(axis, str) and axis.lower() == "h":
            return float(abs(P0[0] - P1[0]) + adj)
        if isinstance(axis, str) and axis.lower() == "v":
            return float(abs(P0[1] - P1[1]) + adj)
        return float(math.hypot(P0[0] - P1[0], P0[1] - P1[1]) + adj)

    return None


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def solve_sketch(
    sketch_params: dict,
    equations_env: dict,
    warm: Optional[SolveState] = None,
) -> SketchSolution:
    """Solve a sketch and return statuses + DOF analysis.

    Parameters
    ----------
    sketch_params:
        The dict at feature.params for a sketch feature (has "entities" and
        "constraints" keys per doc_schema.md §3).
    equations_env:
        Pre-resolved equations dict (str -> float), e.g. from
        equations.evaluate_globals(). Expression dims are looked up here.
    warm:
        Previous SolveState for warm-starting (pass None on first call).

    Returns
    -------
    SketchSolution with entity coords, per-entity status, DOF, redundant/
    conflicting constraint ids, convergence flag, and opaque SolveState.
    """
    entities_list = sketch_params.get("entities", [])
    constraints_list = sketch_params.get("constraints", [])

    # Build entity lookup
    entities = {e["id"]: e for e in entities_list}

    # Create the solver system
    sys = System()
    var_map: Dict[str, np.ndarray] = {}

    for ent in entities_list:
        var_map[ent["id"]] = _allocate_entity(sys, ent)

    # Register constraints
    for c in constraints_list:
        try:
            _add_constraint(sys, c, entities, var_map, equations_env)
        except Exception as exc:
            # Don't crash the whole solve on a single bad constraint; skip it
            # (matches "forward tolerant" pattern from doc_schema conventions)
            pass

    # Per-entity doc-geometry signatures for warm-start gating (see SolveState.ent_sig).
    cur_sig = {e["id"]: json.dumps(e, sort_keys=True, default=str) for e in entities_list}

    # Determine warm-start vector.
    # Warm values are applied PER ENTITY, and only while that entity's doc geometry is unchanged
    # since the warm state was built. A whole-vector warm start (the old behavior) silently
    # resurrected pre-edit geometry after sketch_trim/extend/edit_entity: the edited entity keeps
    # its id (and often the var count), so warm.x overrode the new doc coordinates and — with no
    # constraint pinning the freed points — the solver simply stayed there.
    x0 = None
    if warm is not None:
        if not getattr(warm, "ent_sig", None):
            # Legacy state without signatures: previous whole-vector behavior.
            if warm.n_vars == sys.n_vars:
                x0 = warm.x
        else:
            x0 = np.asarray(sys._init, dtype=np.float64).copy()
            for eid, idx in var_map.items():
                widx = warm.var_map.get(eid)
                if widx is None or len(widx) != len(idx):
                    continue
                if warm.ent_sig.get(eid) != cur_sig.get(eid):
                    continue   # doc geometry changed -> cold-start this entity
                x0[idx] = warm.x[widx]

    # Solve
    result = sys.solve(x0=x0, tol=1e-10, max_iter=50)
    x = result.x

    # Analyze
    analysis = sys.analyze(x)

    # Extract entity coordinates
    entity_coords = _extract_entities(x, entities_list, var_map)

    # Per-entity status
    statuses = _compute_entity_statuses(x, entities_list, var_map, sys, analysis)

    state = SolveState(x=x.copy(), var_map=var_map, n_vars=sys.n_vars, ent_sig=cur_sig)

    return SketchSolution(
        entities=entity_coords,
        statuses=statuses,
        dof=analysis.dof,
        redundant=[str(t) for t in analysis.redundant_tags],
        conflicting=[str(t) for t in analysis.conflicting_tags],
        converged=result.converged,
        state=state,
    )


def drag(
    state: SolveState,
    handle: str,
    target_xy: Tuple[float, float],
    sketch_params: dict,
    equations_env: dict,
    targets: list | None = None,
) -> Dict[str, Any]:
    """Constrained drag: pull handle(s) toward target_xy, constraints stay exact.

    Rebuilds the system from sketch_params (same topology, constraints), soft-
    targets the dragged point's x and y coordinates, warm-starts from state.x,
    and solves with a 10 ms time budget (best-so-far is returned if over budget).

    Single-point drag (the common case) passes ``handle`` + ``target_xy``.
    Whole-entity / profile / multi-select drags pass ``targets`` — a list of
    ``{"handle": str, "xy": [x, y]}`` (or ``(handle, (x, y))`` tuples). Every
    target is soft-targeted at weight 0.01, so a set of points sharing the same
    translation moves rigidly while constraints (coincidence, h/v, dims) still
    hold exactly. ``handle``/``target_xy`` are ignored when ``targets`` is given.

    Returns {"entities": EntityCoords, "statuses": {...}} — does NOT modify state
    and does NOT commit to the document (transient preview).
    """
    entities_list = sketch_params.get("entities", [])
    constraints_list = sketch_params.get("constraints", [])
    entities = {e["id"]: e for e in entities_list}

    sys = System()
    var_map: Dict[str, np.ndarray] = {}
    for ent in entities_list:
        var_map[ent["id"]] = _allocate_entity(sys, ent)

    for c in constraints_list:
        try:
            _add_constraint(sys, c, entities, var_map, equations_env)
        except Exception:
            pass

    # Normalize to a list of (handle, (tx, ty)). The legacy single-handle call
    # becomes a one-element list.
    target_pairs: list[tuple[str, tuple[float, float]]] = []
    if targets:
        for t in targets:
            if isinstance(t, dict):
                h = t.get("handle")
                xy = t.get("xy")
            else:
                h, xy = t
            if h is None or xy is None:
                continue
            target_pairs.append((h, (float(xy[0]), float(xy[1]))))
    elif handle is not None and target_xy is not None:
        target_pairs.append((handle, (float(target_xy[0]), float(target_xy[1]))))

    # Soft-target each resolvable handle's first two (x, y) variables. Derived
    # handles (arc start/end) expose extra vars (cx, cy, r, a); soft-targeting
    # their leading center vars translates the curve, which is the desired drag.
    any_target = False
    for h, (tx, ty) in target_pairs:
        try:
            hidx, _hvf, _hjf = _resolve_handle(h, entities, var_map)
        except (KeyError, ValueError):
            continue
        if len(hidx) == 0:
            continue   # zero-var virtual handle (origin) — nothing to drag
        sys.soft_target(int(hidx[0]), tx, weight=0.01)
        if len(hidx) > 1:
            sys.soft_target(int(hidx[1]), ty, weight=0.01)
        any_target = True

    if not any_target:
        # Nothing resolvable — return last known coords unchanged.
        entity_coords = _extract_entities(state.x, entities_list, var_map)
        return {"entities": entity_coords, "statuses": {}}

    # Use warm start from state if topology matches
    x0 = state.x if state.n_vars == sys.n_vars else None
    result = sys.solve(x0=x0, tol=1e-10, max_iter=50, time_budget_ms=10.0)
    x = result.x

    analysis = sys.analyze(x)
    entity_coords = _extract_entities(x, entities_list, var_map)
    tmp_state = SolveState(x=x, var_map=var_map, n_vars=sys.n_vars)
    statuses = _compute_entity_statuses(x, entities_list, var_map, sys, analysis)

    return {"entities": entity_coords, "statuses": statuses}
