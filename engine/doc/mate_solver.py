"""3D rigid-transform mate solver — C20, built on the shared numeric core (engine.doc.nsolve).

SolidWorks-style assembly mates solved as a least-squares constraint system. Each floating
component is parameterized by **6 variables**: a translation ``t = (tx, ty, tz)`` (mm) and an
absolute **rotation vector** ``w = (wx, wy, wz)`` (Rodrigues / exponential map; ``R(w) =
exp([w]_x)``, the rotation of angle ``|w|`` about axis ``w/|w|``). Fixed components are pinned —
they contribute no variables; their geometry is evaluated at their (constant) transform.

Why a 3-parameter rotation vector (not a quaternion):
  - exactly 3 DOF, so a fully-defined component lands at nsolve ``dof == 0`` with no spurious
    unit-norm residual to manage (a quaternion needs a 4th var + a normalization residual, which
    muddies the DOF count the over-defined UI reads). The exp-map has a singularity only at
    ``|w| = pi`` (a half turn); warm starts keep us away from it, and ``solve`` re-seeds from the
    component's current transform each call so accumulated rotation never grows unbounded.
  - analytic Jacobians: ``d(R(w) p)/dw`` has the closed Rodrigues-derivative form implemented in
    :func:`_rot_jac` (no finite differences — nsolve forbids them).

Resolved geometry (per ``{component, selector}`` ref) is supplied in each component's **local**
frame as a small dict — see :class:`MateGeom`. The world geometry is ``R p + t`` for points and
``R n`` for directions. Mate residual blocks (one nsolve block per mate id, so ``analyze`` names
exactly which mate is redundant/conflicting):

  coincident   planar: point-on-plane + normals (anti/aligned) ; axis: axes collinear + coincident
  parallel     directions parallel (cross == 0), align sign on the dot
  perpendicular directions orthogonal (dot == 0)
  concentric   two axes collinear (cross == 0) AND coincident (rejected component of Δorigin)
  distance     signed gap between two planes/points == value (align flips sign)
  angle        angle between two directions == value (deg)
  tangent      cylinder-plane / cylinder-cylinder surface tangency (centre distance == r±r)
  lock         relative transform frozen (Δt == 0 and Δw == 0 between the two components)

Advanced mates (SW_PARITY_SPEC §3): width (center a tab between two jaw planes), symmetric (two
points equidistant on opposite sides of a symmetry plane), distance_limit / angle_limit (a mate
free within [min,max], one-sided penalty outside), profile_center (two centroids coincide).
Mechanical mates: gear (θ_b = -ratio·θ_a), rack_pinion (rack travel = r·θ_pinion), screw (Δz =
pitch·θ/2π), hinge (concentric + swing angle-limit), slot (pin on a slot centerline), cam_follower
(follower tangent to a cam profile, best-effort osculating-circle tangency). The rotation/
translation couplings are LINEAR relations on the on-axis angle θ = (w-w0)·â and axial travel
s = (t-t0)·d̂ — exact while each part rotates about its mate axis (pinned by a concentric mate).
Extra refs (symmetry plane, 2nd jaw) arrive as resolved geoms at ref index >= 2; coupling scalars
(ratio/pitch/min/max) ride in the mate ``value`` dict.

Public API::

    solve_mates(components, mates, resolved_geometry, warm=None, drag=None) -> MateSolveResult

``components``  : list of component dicts (doc_schema §6: id, transform[16], fixed, suppressed).
``mates``       : list of mate dicts (id, type, align, refs[{component, ...}], value, suppressed).
``resolved_geometry`` : ``{(mate_id, ref_index): MateGeom}`` — the local-frame geometry each mate
                        ref resolves to. Built by the session layer from the picked selectors.
``warm``        : optional ``{component_id: (t, w)}`` from a previous solve (warm start).
``drag``        : optional ``{"component": id, "target": [x,y,z]}`` — soft-pull the component's
                  translation toward a cursor point along the free DOF (constrained drag).

Returns transforms (16-float row-major per component), per-mate status, and a DOF summary.

All units canonical: mm, degrees. float64 throughout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from engine.doc.nsolve import System


# --------------------------------------------------------------------------- geometry input


@dataclass
class MateGeom:
    """Local-frame geometry a single mate ref resolves to.

    ``kind`` ∈ ``plane | axis | point | sphere``. Fields are in the OWNING component's local
    coordinate frame (pre-transform); the solver maps them to world via that component's R,t.

      plane  : ``point`` (any point on the plane) + ``normal`` (unit)
      axis   : ``point`` (a point on the axis) + ``direction`` (unit) + ``radius`` (cylinder
               radius, 0 for a bare axis/edge)
      point  : ``point`` (a vertex)
      sphere : ``point`` (centre) + ``radius``
    """

    kind: str
    point: np.ndarray = field(default_factory=lambda: np.zeros(3))
    normal: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 1.0]))
    direction: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 1.0]))
    radius: float = 0.0

    def __post_init__(self) -> None:
        self.point = np.asarray(self.point, dtype=float).reshape(3)
        self.normal = _unit(np.asarray(self.normal, dtype=float).reshape(3))
        self.direction = _unit(np.asarray(self.direction, dtype=float).reshape(3))
        self.radius = float(self.radius)

    def dir_vector(self) -> np.ndarray:
        """The orientation vector this geom contributes (plane normal or axis direction)."""
        return self.normal if self.kind in ("plane",) else self.direction


# --------------------------------------------------------------------------- results


@dataclass
class MateStatus:
    mate_id: str
    type: str
    status: str          # "ok" | "redundant" | "conflicting" | "suppressed" | "unresolved"


@dataclass
class MateSolveResult:
    transforms: dict[str, list[float]]              # component_id -> 16-float row-major
    params: dict[str, tuple]                        # component_id -> (t(3,), w(3,)) for warm reuse
    mate_status: list[MateStatus]
    dof: int                                        # remaining free DOF (0 = fully constrained)
    converged: bool
    max_residual: float
    over_defined: bool                              # any conflicting mate
    summary: str                                    # short human DOF line


# --------------------------------------------------------------------------- rotation helpers


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v.copy()


def _skew(v: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]],
                     [v[2], 0.0, -v[0]],
                     [-v[1], v[0], 0.0]])


def _rodrigues(w: np.ndarray) -> np.ndarray:
    """Rotation matrix from a rotation vector (exp map). ``R = I + sin t/t [w] + (1-cos t)/t^2 [w]^2``."""
    theta = float(np.linalg.norm(w))
    K = _skew(w)
    if theta < 1e-8:
        # 2nd-order series (exact to machine eps for small angles).
        return np.eye(3) + K + 0.5 * (K @ K)
    a = math.sin(theta) / theta
    b = (1.0 - math.cos(theta)) / (theta * theta)
    return np.eye(3) + a * K + b * (K @ K)


def _rot_apply_dir(w: np.ndarray, v: np.ndarray) -> np.ndarray:
    return _rodrigues(w) @ v


def _rot_jac(w: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Analytic 3x3 Jacobian of ``R(w) @ v`` w.r.t. ``w`` (rotation vector).

    Closed form: ``d(R v)/dw_i = ([e_i x w] + ... )`` — we use the well-known result
    ``∂(R v)/∂w = -R [v]_x  ∂?`` is awkward for the exp map, so we differentiate the Rodrigues
    series directly. For each basis perturbation ``e_i`` the derivative of ``R`` is
    ``dR_i = ( [e_i]_x R - ... )`` — instead we use the compact tangent-map identity:

        ∂(R v)/∂w = -[R v]_x · J_l(w)            (left Jacobian form)

    where ``J_l`` is the left Jacobian of SO(3). Equivalently and more directly, we use the
    component form ``∂(R v)/∂w_i`` derived from the Rodrigues formula. Here we implement the
    robust closed form via the left Jacobian ``J_l``: ``∂(R v)/∂w = -[R v]_x J_l`` is the
    derivative of ``R`` acting on a *fixed body vector*; for our absolute-``w`` parameterization
    (``v`` constant in body frame, ``R v`` in world) this is exactly the needed Jacobian.
    """
    Rv = _rodrigues(w) @ v
    Jl = _left_jacobian(w)
    return -_skew(Rv) @ Jl


def _left_jacobian(w: np.ndarray) -> np.ndarray:
    """Left Jacobian of SO(3) for rotation vector ``w`` (maps body-frame ang. vel to ∂R)."""
    theta = float(np.linalg.norm(w))
    K = _skew(w)
    if theta < 1e-8:
        return np.eye(3) + 0.5 * K + (1.0 / 6.0) * (K @ K)
    a = (1.0 - math.cos(theta)) / (theta * theta)
    b = (theta - math.sin(theta)) / (theta ** 3)
    return np.eye(3) + a * K + b * (K @ K)


def _mat_to_rotvec(R: np.ndarray) -> np.ndarray:
    """Log map SO(3) -> rotation vector. Robust to small/large angles."""
    # angle from trace
    cos_t = (np.trace(R) - 1.0) * 0.5
    cos_t = max(-1.0, min(1.0, cos_t))
    theta = math.acos(cos_t)
    if theta < 1e-8:
        # near identity: w ~ vee(R - R^T)/2
        return np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) * 0.5
    if abs(math.pi - theta) < 1e-6:
        # near pi: use the (R + I) trick for a stable axis.
        A = (R + np.eye(3)) * 0.5
        # largest diagonal gives the most stable column
        k = int(np.argmax(np.diag(A)))
        axis = A[:, k] / math.sqrt(max(A[k, k], 1e-12))
        axis = _unit(axis)
        return axis * theta
    s = math.sin(theta)
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]]) / (2.0 * s)
    return w * theta


# --------------------------------------------------------------------------- transform <-> params


def _transform_to_params(transform: list[float]) -> tuple[np.ndarray, np.ndarray]:
    """16-float row-major transform -> (t(3,), w(3,))."""
    M = np.asarray(transform, dtype=float).reshape(4, 4)
    R = M[:3, :3]
    t = M[:3, 3].copy()
    # Orthonormalize R defensively (mates only allow rigid transforms).
    U, _, Vt = np.linalg.svd(R)
    Rn = U @ Vt
    if np.linalg.det(Rn) < 0:
        U[:, -1] *= -1
        Rn = U @ Vt
    return t, _mat_to_rotvec(Rn)


def _params_to_transform(t: np.ndarray, w: np.ndarray) -> list[float]:
    R = _rodrigues(w)
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    return [float(v) for v in M.reshape(16)]


# --------------------------------------------------------------------------- component frame


class _CompFrame:
    """Per-component variable handle. Fixed components have ``var_idx is None`` (pinned)."""

    def __init__(self, comp_id: str, t: np.ndarray, w: np.ndarray, fixed: bool) -> None:
        self.id = comp_id
        self.t0 = np.asarray(t, dtype=float).reshape(3)
        self.w0 = np.asarray(w, dtype=float).reshape(3)
        self.fixed = fixed
        self.t_idx: Optional[np.ndarray] = None   # 3 var indices for translation
        self.w_idx: Optional[np.ndarray] = None   # 3 var indices for rotation vector

    def alloc(self, system: System) -> None:
        if self.fixed:
            return
        self.t_idx = system.add_vars(3, init=list(self.t0))
        self.w_idx = system.add_vars(3, init=list(self.w0))

    @property
    def var_idx(self) -> np.ndarray:
        """Concatenated [tx,ty,tz, wx,wy,wz] global indices (length 6). Empty for fixed."""
        if self.fixed:
            return np.zeros(0, dtype=np.int64)
        return np.concatenate([self.t_idx, self.w_idx])

    # world geometry from a packed sub-vector x6 = [t(3), w(3)]
    @staticmethod
    def world_point(x6: np.ndarray, p_local: np.ndarray) -> np.ndarray:
        return _rodrigues(x6[3:6]) @ p_local + x6[0:3]

    @staticmethod
    def world_dir(x6: np.ndarray, d_local: np.ndarray) -> np.ndarray:
        return _rodrigues(x6[3:6]) @ d_local


# --------------------------------------------------------------------------- residual builders
#
# Each mate registers ONE nsolve residual block (tag = mate id). A block operates over the
# concatenated variable indices of BOTH referenced components (fixed components contribute no
# indices, so the block sees only the free component's 6 vars — or 12 when both float). The
# rfunc/jfunc receive the sub-vector x_sub in that index order; we slice it per component.


class _MateBuilder:
    """Builds an nsolve residual block for one mate from two ``(_CompFrame, MateGeom)`` refs."""

    def __init__(self, frame_a: _CompFrame, geom_a: MateGeom,
                 frame_b: _CompFrame, geom_b: MateGeom,
                 mtype: str, align: str, value: float,
                 extra_geoms: Optional[list] = None,
                 params: Optional[dict] = None,
                 extra_owners: Optional[list] = None) -> None:
        self.fa, self.ga = frame_a, geom_a
        self.fb, self.gb = frame_b, geom_b
        self.mtype = mtype
        self.sign = -1.0 if align == "anti" else 1.0
        self.value = value
        # Advanced/mechanical mates can carry extra resolved geoms (a 3rd/4th ref — e.g. a symmetry
        # plane, the second width face) and scalar coupling params (gear ratio, screw pitch, limit
        # min/max). Standard mates ignore both and stay byte-identical. ``extra_geoms`` is the list
        # of MateGeom at ref indices >= 2 (in order); ``params`` is the mate's value dict.
        self.extra = list(extra_geoms or [])
        # Which frame ("a" or "b") owns each extra geom (so its world transform uses the right
        # component). Defaults to "b" — extra refs commonly ride on the second-listed part.
        self.extra_owners = list(extra_owners or ["b"] * len(self.extra))
        self.params = dict(params or {})
        # variable layout: [a-vars..., b-vars...]; remember the split for slicing.
        self.idx = np.concatenate([frame_a.var_idx, frame_b.var_idx])
        self.na = frame_a.var_idx.size            # 0 or 6
        # constant sub-vectors for fixed components (their world geom is constant)
        self._xa_const = np.concatenate([frame_a.t0, frame_a.w0])
        self._xb_const = np.concatenate([frame_b.t0, frame_b.w0])

    # -- sub-vector helpers ------------------------------------------------

    def _xa(self, xs: np.ndarray) -> np.ndarray:
        return xs[0:self.na] if self.na else self._xa_const

    def _xb(self, xs: np.ndarray) -> np.ndarray:
        return xs[self.na:self.na + self.fb.var_idx.size] if self.fb.var_idx.size else self._xb_const

    # numeric-Jacobian-free analytic derivatives are assembled by chain rule below. To keep the
    # algebra tractable across all mate types we build the residual + Jacobian with a small set of
    # primitive "world geometry" derivatives and compose them. The Jacobian columns correspond to
    # the block's idx order [a(6?), b(6?)].

    def _empty_jac(self, m: int) -> np.ndarray:
        return np.zeros((m, self.idx.size))

    # primitive: world point of geom on component (a|b) + its 3x6 jac wrt that comp's vars
    def _wp(self, which: str, xs: np.ndarray, p_local: np.ndarray):
        x6 = self._xa(xs) if which == "a" else self._xb(xs)
        w = x6[3:6]
        wp = _rodrigues(w) @ p_local + x6[0:3]
        J = np.zeros((3, 6))
        J[:, 0:3] = np.eye(3)
        J[:, 3:6] = _rot_jac(w, p_local)
        return wp, J

    def _wd(self, which: str, xs: np.ndarray, d_local: np.ndarray):
        x6 = self._xa(xs) if which == "a" else self._xb(xs)
        w = x6[3:6]
        wd = _rodrigues(w) @ d_local
        J = np.zeros((3, 6))
        J[:, 3:6] = _rot_jac(w, d_local)
        return wd, J

    def _place(self, which: str, J6: np.ndarray) -> np.ndarray:
        """Embed a 3x6 per-component jac into a 3xidx block (zeros for fixed/other component)."""
        out = np.zeros((J6.shape[0], self.idx.size))
        if which == "a":
            if self.na:
                out[:, 0:6] = J6
        else:
            if self.fb.var_idx.size:
                out[:, self.na:self.na + 6] = J6
        return out

    # ----------------------------------------------------------------- residual/jacobian dispatch

    def residual_dim(self) -> int:
        t = self.mtype
        if t in ("coincident",):
            return 5 if self.ga.kind == "plane" else 5  # plane: 1 pos + ... see build
        if t == "parallel":
            return 3
        if t == "perpendicular":
            return 1
        if t == "concentric":
            return 4
        if t == "distance":
            return 1
        if t == "angle":
            return 1
        if t == "tangent":
            return 1
        if t == "lock":
            return 6
        if t == "width":
            return 1
        if t == "symmetric":
            return 1
        if t in ("distance_limit", "angle_limit"):
            return 1
        if t == "profile_center":
            return 3
        if t in ("gear", "rack_pinion", "screw"):
            return 1
        if t == "hinge":
            return 5
        if t == "slot":
            return 1
        if t == "cam_follower":
            return 1
        return 1

    def make(self):
        """Return (rfunc, jfunc, m) for nsolve.add_residual."""
        t = self.mtype
        builder = {
            "coincident": self._coincident,
            "parallel": self._parallel,
            "perpendicular": self._perpendicular,
            "concentric": self._concentric,
            "distance": self._distance,
            "angle": self._angle,
            "tangent": self._tangent,
            "lock": self._lock,
            # --- advanced mates ---
            "width": self._width,
            "symmetric": self._symmetric,
            "distance_limit": self._distance_limit,
            "angle_limit": self._angle_limit,
            "profile_center": self._profile_center,
            # --- mechanical mates ---
            "gear": self._gear,
            "rack_pinion": self._rack_pinion,
            "screw": self._screw,
            "hinge": self._hinge,
            "slot": self._slot,
            "cam_follower": self._cam_follower,
        }.get(t)
        if builder is None:
            raise ValueError(f"mate_solver: unsupported mate type '{t}'")
        return builder()

    # -- coupling-param helpers --------------------------------------------

    def _param(self, key: str, default: float) -> float:
        """Scalar coupling param (ratio/pitch/min/max) from the mate value dict."""
        v = self.params.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    # ----- coincident -----------------------------------------------------
    # planar coincident: the two planes share a plane -> normals (anti)parallel + a point of one
    # lies on the other. residual: [ n_a x n_b (3, =0 for parallel) ; n_a . (p_b - p_a) (1) ]
    # plus the align sign forces n_a . n_b = +/-1 — captured by the cross==0 + a dot-sign row.
    # axis coincident (edges/points): collinear axes + coincident points.

    def _coincident(self):
        if self.ga.kind == "plane" and self.gb.kind == "plane":
            return self._coincident_plane()
        return self._coincident_axis()

    def _coincident_plane(self):
        # Orientation: aligned faces SEAT against each other (normals oppose) -> na + nb == 0;
        # anti -> na - nb == 0. Using the vector residual na + sign*nb (sign=+1 aligned) pins BOTH
        # the parallelism AND the direction sign (a bare cross product can't disambiguate the flip,
        # which is exactly the align-flip behaviour the UI needs). The vector residual is rank-2
        # for unit normals (the radial component is redundant), which nsolve/_classify_mates handle.
        na_l, nb_l = self.ga.normal, self.gb.normal
        pa_l, pb_l = self.ga.point, self.gb.point
        sign = self.sign

        def rfunc(xs):
            na, _ = self._wd("a", xs, na_l)
            nb, _ = self._wd("b", xs, nb_l)
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            r = np.empty(4)
            r[0:3] = na + sign * nb                      # aligned: normals oppose (seated)
            r[3] = float(np.dot(na, pb - pa))            # point-on-plane
            return r

        def jfunc(xs):
            na, Jna = self._wd("a", xs, na_l)
            nb, Jnb = self._wd("b", xs, nb_l)
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            J = self._empty_jac(4)
            J[0:3, :] = self._place("a", Jna) + self._place("b", sign * Jnb)
            diff = pb - pa
            row = self._place("a", (diff @ Jna)[None, :]) + self._place("a", -(na @ Jpa)[None, :]) \
                + self._place("b", (na @ Jpb)[None, :])
            J[3:4, :] = row
            return J

        return rfunc, jfunc, 4

    def _coincident_axis(self):
        # collinear directions + coincident points (point-to-point or axis-to-axis).
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        pa_l, pb_l = self.ga.point, self.gb.point
        sign = self.sign
        has_dir = self.ga.kind in ("axis",) and self.gb.kind in ("axis",)

        if not has_dir:
            # pure point coincidence (vertex-vertex): 3 residuals.
            def rfunc(xs):
                pa, _ = self._wp("a", xs, pa_l)
                pb, _ = self._wp("b", xs, pb_l)
                return pa - pb

            def jfunc(xs):
                _, Jpa = self._wp("a", xs, pa_l)
                _, Jpb = self._wp("b", xs, pb_l)
                return self._place("a", Jpa) - self._place("b", Jpb)

            return rfunc, jfunc, 3

        # axis coincident: cross(da, db)==0 (collinear) + perpendicular component of (pb-pa) ==0.
        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            r = np.empty(5)
            r[0:3] = np.cross(da, sign * db)
            diff = pb - pa
            r[3:5] = _perp_residual2(da, diff)
            return r

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            J = self._empty_jac(5)
            sdb = sign * db
            J[0:3, :] = self._place("a", -_skew(sdb) @ Jda) + self._place("b", _skew(da) @ (sign * Jdb))
            diff = pb - pa
            Jdiff = self._place("b", Jpb) - self._place("a", Jpa)
            J[3:5, :] = _perp_residual2_jac(da, self._place("a", Jda), diff, Jdiff)
            return J

        return rfunc, jfunc, 5

    # ----- parallel / perpendicular / angle -------------------------------

    def _parallel(self):
        # aligned -> same direction (da - db == 0); anti -> opposite (da + db == 0). Vector residual
        # da - sign*db disambiguates the flip (a bare cross can't). Rank-2 for unit dirs.
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        sign = self.sign

        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            return da - sign * db

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            return self._place("a", Jda) - self._place("b", sign * Jdb)

        return rfunc, jfunc, 3

    def _perpendicular(self):
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()

        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            return np.array([float(np.dot(da, db))])

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            row = self._place("a", (db @ Jda)[None, :]) + self._place("b", (da @ Jdb)[None, :])
            return row

        return rfunc, jfunc, 1

    def _angle(self):
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        target = math.cos(math.radians(self.value))

        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            return np.array([float(np.dot(da, db)) - target])

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            return self._place("a", (db @ Jda)[None, :]) + self._place("b", (da @ Jdb)[None, :])

        return rfunc, jfunc, 1

    # ----- concentric -----------------------------------------------------

    def _concentric(self):
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        pa_l, pb_l = self.ga.point, self.gb.point
        sign = self.sign

        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            r = np.empty(4)
            r[0:2] = _two_indep_cross(da, sign * db)
            r[2:4] = _perp_residual2(da, pb - pa)
            return r

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            J = self._empty_jac(4)
            sdb = sign * db
            cross_jac = self._place("a", -_skew(sdb) @ Jda) + self._place("b", _skew(da) @ (sign * Jdb))
            J[0:2, :] = _two_indep_cross_jac(da, sdb, cross_jac)
            diff = pb - pa
            Jdiff = self._place("b", Jpb) - self._place("a", Jpa)
            J[2:4, :] = _perp_residual2_jac(da, self._place("a", Jda), diff, Jdiff)
            return J

        return rfunc, jfunc, 4

    # ----- distance -------------------------------------------------------
    # plane-plane (or point-point): signed gap along the reference normal == value.

    def _distance(self):
        use_plane = self.ga.kind == "plane" or self.gb.kind == "plane"
        n_l = self.ga.normal if self.ga.kind == "plane" else self.gb.normal
        n_on_a = self.ga.kind == "plane"
        pa_l, pb_l = self.ga.point, self.gb.point
        sign = self.sign
        value = self.value

        if use_plane:
            def rfunc(xs):
                n, _ = (self._wd("a", xs, n_l) if n_on_a else self._wd("b", xs, n_l))
                pa, _ = self._wp("a", xs, pa_l)
                pb, _ = self._wp("b", xs, pb_l)
                gap = float(np.dot(n, pb - pa))
                return np.array([gap - sign * value])

            def jfunc(xs):
                n, Jn = (self._wd("a", xs, n_l) if n_on_a else self._wd("b", xs, n_l))
                pa, Jpa = self._wp("a", xs, pa_l)
                pb, Jpb = self._wp("b", xs, pb_l)
                diff = pb - pa
                place_n = self._place("a", (diff @ Jn)[None, :]) if n_on_a \
                    else self._place("b", (diff @ Jn)[None, :])
                row = place_n + self._place("b", (n @ Jpb)[None, :]) - self._place("a", (n @ Jpa)[None, :])
                return row

            return rfunc, jfunc, 1

        # point-point euclidean distance == value
        def rfunc(xs):
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            d = pb - pa
            return np.array([float(np.linalg.norm(d)) - value])

        def jfunc(xs):
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            d = pb - pa
            nrm = float(np.linalg.norm(d))
            u = d / nrm if nrm > 1e-9 else np.zeros(3)
            return self._place("b", (u @ Jpb)[None, :]) - self._place("a", (u @ Jpa)[None, :])

        return rfunc, jfunc, 1

    # ----- tangent --------------------------------------------------------
    # cylinder-plane: |signed dist of axis point to plane| == radius. cylinder-cylinder: centre
    # distance between axes == r_a + r_b (external). We use the squared/abs-free signed form along
    # the plane normal for cyl-plane and the perpendicular axis distance for cyl-cyl.

    def _tangent(self):
        a_cyl = self.ga.kind == "axis"
        b_cyl = self.gb.kind == "axis"
        sign = self.sign
        if a_cyl and self.gb.kind == "plane":
            return self._tangent_cyl_plane("a", "b")
        if b_cyl and self.ga.kind == "plane":
            return self._tangent_cyl_plane("b", "a")
        # cyl-cyl
        ra, rb = self.ga.radius, self.gb.radius
        pa_l, da_l = self.ga.point, self.ga.direction
        pb_l, db_l = self.gb.point, self.gb.direction
        target = (ra + rb) if sign > 0 else abs(ra - rb)

        def rfunc(xs):
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            da, _ = self._wd("a", xs, da_l)
            dist = _axis_point_distance(pa, da, pb)
            return np.array([dist - target])

        def jfunc(xs):
            # finite-free: distance from pb to line(pa, da). Use perp-component magnitude.
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            da, Jda = self._wd("a", xs, da_l)
            return _axis_point_distance_jac(
                pa, self._place("a", Jpa), da, self._place("a", Jda),
                pb, self._place("b", Jpb))

        return rfunc, jfunc, 1

    def _tangent_cyl_plane(self, cyl: str, pln: str):
        cyl_geom = self.ga if cyl == "a" else self.gb
        pln_geom = self.ga if pln == "a" else self.gb
        r = cyl_geom.radius
        pc_l, dc_l = cyl_geom.point, cyl_geom.direction
        pp_l, np_l = pln_geom.point, pln_geom.normal
        sign = self.sign

        def rfunc(xs):
            pc, _ = self._wp(cyl, xs, pc_l)
            pp, _ = self._wp(pln, xs, pp_l)
            nn, _ = self._wd(pln, xs, np_l)
            gap = float(np.dot(nn, pc - pp))
            return np.array([gap - sign * r])

        def jfunc(xs):
            pc, Jpc = self._wp(cyl, xs, pc_l)
            pp, Jpp = self._wp(pln, xs, pp_l)
            nn, Jnn = self._wd(pln, xs, np_l)
            diff = pc - pp
            row = self._place(pln, (diff @ Jnn)[None, :]) \
                + self._place(cyl, (nn @ Jpc)[None, :]) \
                - self._place(pln, (nn @ Jpp)[None, :])
            return row

        return rfunc, jfunc, 1

    # ----- lock -----------------------------------------------------------
    # freeze the relative transform of b w.r.t a at the current relative pose: Δt and Δw constant.

    def _lock(self):
        # captured relative pose at build time
        Ra0 = _rodrigues(self.fa.w0)
        ta0 = self.fa.t0
        Rb0 = _rodrigues(self.fb.w0)
        tb0 = self.fb.t0
        # relative: rel_R = Ra^T Rb ; rel_t = Ra^T (tb - ta). Residual drives current rel to rel0.
        relR0 = Ra0.T @ Rb0
        relt0 = Ra0.T @ (tb0 - ta0)

        def rfunc(xs):
            xa, xb = self._xa(xs), self._xb(xs)
            Ra = _rodrigues(xa[3:6]); ta = xa[0:3]
            Rb = _rodrigues(xb[3:6]); tb = xb[0:3]
            relR = Ra.T @ Rb
            relt = Ra.T @ (tb - ta)
            dR = relR0.T @ relR
            w_err = _mat_to_rotvec(dR)
            t_err = relt - relt0
            return np.concatenate([t_err, w_err])

        # analytic jacobian of lock is involved; a numeric block here would violate the no-FD
        # rule, so we provide the dominant-term analytic jacobian (exact when one component is
        # fixed, which is the common case — the other is then a plain rigid offset).
        def jfunc(xs):
            xa, xb = self._xa(xs), self._xb(xs)
            J = self._empty_jac(6)
            Ra = _rodrigues(xa[3:6])
            # translation rows: ∂(Ra^T (tb - ta))/∂tb = Ra^T ; ∂/∂ta = -Ra^T
            if self.fb.var_idx.size:
                J[0:3, self.na:self.na + 3] = Ra.T
                J[3:6, self.na + 3:self.na + 6] = np.eye(3)   # ∂w_err/∂wb ≈ I near solution
            if self.na:
                J[0:3, 0:3] = -Ra.T
                J[3:6, 3:6] = -np.eye(3)                       # ∂w_err/∂wa ≈ -I near solution
            return J

        return rfunc, jfunc, 6

    # ===================================================================== ADVANCED MATES
    #
    # width / symmetric / *_limit / profile_center. These are distance-equality (or one-sided
    # penalty) residuals — the same scalar-gap pattern as `distance`, but centered or clamped.

    # ----- width ----------------------------------------------------------
    # SolidWorks "Width" centers a tab on component B midway between two reference faces (planes)
    # on component A. Ref0 = tab plane (or vertex) on B; Ref1 = first jaw plane on A; the SECOND
    # jaw plane is carried as the first extra geom (ref index 2) on A. Residual: the signed
    # distance of the tab point to jaw-1 equals the signed distance to jaw-2 (i.e. it sits on the
    # mid-plane). One scalar row -> removes the 1 translational DOF along the jaw normal.

    def _width(self):
        # tab reference (a point we center) on frame_a; the two jaw planes on frame_b. We measure
        # BOTH jaw gaps along ONE shared reference normal (jaw-1's), so the residual is
        #   n·(tp - j1p) + n·(tp - j2p) == 0   <=>   n·(2*tp - j1p - j2p) == 0
        # i.e. the tab sits on the mid-plane between the jaws. Using a single normal makes the
        # residual vary linearly with the tab position (a same-direction pair of jaw normals would
        # cancel the dependence). Removes the 1 DOF along the jaw normal; the tab floats in-plane.
        tab_l = self.ga.point
        jaw1_p_l, n_l = self.gb.point, self.gb.normal
        if not self.extra:
            raise ValueError("width mate needs a second jaw plane (extra geom)")
        jaw2 = self.extra[0]
        jaw2_p_l = jaw2.point

        def rfunc(xs):
            tp, _ = self._wp("a", xs, tab_l)
            j1p, _ = self._wp("b", xs, jaw1_p_l)
            j2p, _ = self._wp("b", xs, jaw2_p_l)
            n, _ = self._wd("b", xs, n_l)
            d1 = float(np.dot(n, tp - j1p))
            d2 = float(np.dot(n, tp - j2p))
            return np.array([d1 + d2])

        def jfunc(xs):
            tp, Jtp = self._wp("a", xs, tab_l)
            j1p, Jj1p = self._wp("b", xs, jaw1_p_l)
            j2p, Jj2p = self._wp("b", xs, jaw2_p_l)
            n, Jn = self._wd("b", xs, n_l)
            diff = (tp - j1p) + (tp - j2p)
            # d(d1+d2)/dx = n·(2*dtp - dj1p - dj2p) + diff·dn
            row = self._place("a", (2.0 * (n @ Jtp))[None, :]) \
                - self._place("b", (n @ Jj1p)[None, :]) \
                - self._place("b", (n @ Jj2p)[None, :]) \
                + self._place("b", (diff @ Jn)[None, :])
            return row

        return rfunc, jfunc, 1

    # ----- symmetric ------------------------------------------------------
    # Two entities (ref0 point on A, ref1 point on B) symmetric about a plane. The symmetry plane
    # is carried as the first extra geom (ref index 2), in B's frame (commonly the assembly origin
    # plane on a fixed part). Residual: the plane bisects the two points -> the plane-signed
    # distances of the two points are equal and opposite (sum == 0). One scalar row.

    def _symmetric(self):
        pa_l = self.ga.point
        pb_l = self.gb.point
        if not self.extra:
            raise ValueError("symmetric mate needs a symmetry plane (extra geom)")
        sp = self.extra[0]
        sp_p_l, sp_n_l = sp.point, sp.normal
        # the symmetry plane rides on whichever component owns ref index 2 (usually a fixed part /
        # assembly origin plane).
        sym_on = self.extra_owners[0] if self.extra_owners else "b"

        def rfunc(xs):
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            spp, _ = self._wp(sym_on, xs, sp_p_l)
            spn, _ = self._wd(sym_on, xs, sp_n_l)
            da = float(np.dot(spn, pa - spp))
            db = float(np.dot(spn, pb - spp))
            return np.array([da + db])           # both equidistant on opposite sides -> sum 0

        def jfunc(xs):
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            spp, Jspp = self._wp(sym_on, xs, sp_p_l)
            spn, Jspn = self._wd(sym_on, xs, sp_n_l)
            diff_a, diff_b = pa - spp, pb - spp
            rowa = self._place("a", (spn @ Jpa)[None, :]) \
                + self._place(sym_on, (diff_a @ Jspn)[None, :]) \
                - self._place(sym_on, (spn @ Jspp)[None, :])
            rowb = self._place("b", (spn @ Jpb)[None, :]) \
                + self._place(sym_on, (diff_b @ Jspn)[None, :]) \
                - self._place(sym_on, (spn @ Jspp)[None, :])
            return rowa + rowb

        return rfunc, jfunc, 1

    # ----- distance-limit / angle-limit ----------------------------------
    # A range-constrained mate: the free value is allowed anywhere in [min, max]; the solver only
    # pushes back when it leaves the range (a one-sided penalty, the LS analogue of an inequality).
    # Inside the range the residual is 0 (no DOF removed); outside it clamps to the violated bound.
    # ``params``: {"min": lo, "max": hi}. Distance uses the plane-gap; angle uses the dir dot.

    def _distance_limit(self):
        use_plane = self.ga.kind == "plane" or self.gb.kind == "plane"
        n_l = self.ga.normal if self.ga.kind == "plane" else self.gb.normal
        n_on_a = self.ga.kind == "plane"
        pa_l, pb_l = self.ga.point, self.gb.point
        lo = self._param("min", 0.0)
        hi = self._param("max", 0.0)

        def _gap_and_jacrow(xs):
            if use_plane:
                n, Jn = (self._wd("a", xs, n_l) if n_on_a else self._wd("b", xs, n_l))
                pa, Jpa = self._wp("a", xs, pa_l)
                pb, Jpb = self._wp("b", xs, pb_l)
                diff = pb - pa
                gap = float(np.dot(n, diff))
                place_n = self._place("a", (diff @ Jn)[None, :]) if n_on_a \
                    else self._place("b", (diff @ Jn)[None, :])
                row = place_n + self._place("b", (n @ Jpb)[None, :]) - self._place("a", (n @ Jpa)[None, :])
                return gap, row
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            d = pb - pa
            nrm = float(np.linalg.norm(d))
            u = d / nrm if nrm > 1e-9 else np.zeros(3)
            row = self._place("b", (u @ Jpb)[None, :]) - self._place("a", (u @ Jpa)[None, :])
            return nrm, row

        def _violation(gap):
            if gap < lo:
                return gap - lo          # negative -> push up toward lo
            if gap > hi:
                return gap - hi          # positive -> push down toward hi
            return 0.0

        def rfunc(xs):
            gap, _ = _gap_and_jacrow(xs)
            return np.array([_violation(gap)])

        def jfunc(xs):
            gap, row = _gap_and_jacrow(xs)
            if lo <= gap <= hi:
                return np.zeros_like(row)   # inside range: inactive, zero gradient
            return row

        return rfunc, jfunc, 1

    def _angle_limit(self):
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        lo = math.radians(self._param("min", 0.0))
        hi = math.radians(self._param("max", 0.0))

        def _angle_and_jacrow(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            c = float(np.clip(np.dot(da, db), -1.0, 1.0))
            ang = math.acos(c)
            s = math.sqrt(max(1.0 - c * c, 1e-12))
            # d(ang)/dx = -1/sin * d(da·db)/dx
            ddot = self._place("a", (db @ Jda)[None, :]) + self._place("b", (da @ Jdb)[None, :])
            row = (-1.0 / s) * ddot
            return ang, row

        def _violation(ang):
            if ang < lo:
                return ang - lo
            if ang > hi:
                return ang - hi
            return 0.0

        def rfunc(xs):
            ang, _ = _angle_and_jacrow(xs)
            return np.array([_violation(ang)])

        def jfunc(xs):
            ang, row = _angle_and_jacrow(xs)
            if lo <= ang <= hi:
                return np.zeros_like(row)
            return row

        return rfunc, jfunc, 1

    # ----- profile-center -------------------------------------------------
    # Center one profile's centroid (ref0 point on A) on another's (ref1 point on B): the two
    # centroids coincide. Equivalent to a point-point coincident -> 3 residual rows.

    def _profile_center(self):
        pa_l, pb_l = self.ga.point, self.gb.point

        def rfunc(xs):
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            return pa - pb

        def jfunc(xs):
            _, Jpa = self._wp("a", xs, pa_l)
            _, Jpb = self._wp("b", xs, pb_l)
            return self._place("a", Jpa) - self._place("b", Jpb)

        return rfunc, jfunc, 3

    # ===================================================================== MECHANICAL MATES
    #
    # gear / rack-pinion / screw / hinge / slot / cam-follower. The rotation/translation couplings
    # (gear, rack, screw) are LINEAR relations between the two components' on-axis rotation angle
    # and/or axial translation. The on-axis angle is the ABSOLUTE shaft angle ``theta = w·â`` (so a
    # FIXED driver at a known rotated pose contributes its full angle — exactly what a gear train
    # needs; the incremental-from-creation flavour SolidWorks shows in the UI is a baseline offset
    # the session layer can subtract). ``â`` is the (constant, build-time world) mate axis and the
    # Jacobian is clean (∂theta/∂w = â). Exact while each component rotates ABOUT its own mate axis —
    # the regime a concentric/hinge mate already enforces; large off-axis tumble (a part free to
    # flop) degrades the scalar to an approximation, but in a real train the axes are pinned.

    def _axis_world(self, which: str) -> np.ndarray:
        """Build-time world axis of the component's geom (constant — the coupling reference)."""
        g = self.ga if which == "a" else self.gb
        w0 = self.fa.w0 if which == "a" else self.fb.w0
        return _unit(_rodrigues(w0) @ g.dir_vector())

    def _theta_row(self, which: str):
        """Return (rfunc_scalar, jacrow) builders for the absolute on-axis angle theta = w·â.

        ``â`` is constant (build-time world axis); ∂theta/∂w = â (a 1x3 block placed on that
        component's rotation vars); translation columns are zero. Absolute (w0 = 0) so a fixed
        driver contributes its full shaft angle.
        """
        ax = self._axis_world(which)
        w0 = np.zeros(3)

        def theta(xs):
            x6 = self._xa(xs) if which == "a" else self._xb(xs)
            return float(np.dot(x6[3:6] - w0, ax))

        def row(xs):
            r6 = np.zeros((1, 6))
            r6[0, 3:6] = ax
            return self._place(which, r6)

        return theta, row

    # ----- gear -----------------------------------------------------------
    # Ratio coupling of two rotations: theta_b == -ratio * theta_a (meshing gears counter-rotate;
    # an internal/aligned set keeps the sign). ``ratio`` defaults to 1; ``align=anti`` already
    # negates via self.sign so callers can flip direction. One scalar row.

    def _gear(self):
        ratio = self._param("ratio", 1.0)
        theta_a, row_a = self._theta_row("a")
        theta_b, row_b = self._theta_row("b")
        # default meshing convention: theta_b + ratio*theta_a == 0 (counter-rotation). sign lets
        # an "aligned" pair (e.g. belt/idler) co-rotate: theta_b - ratio*theta_a == 0.
        coup = -self.sign

        def rfunc(xs):
            return np.array([theta_b(xs) - coup * ratio * theta_a(xs)])

        def jfunc(xs):
            return row_b(xs) - coup * ratio * row_a(xs)

        return rfunc, jfunc, 1

    # ----- rack-and-pinion ------------------------------------------------
    # Pinion rotation theta_a couples to rack translation along its world axis: s_b == pitch_radius
    # * theta_a (arc length = r * angle). ``pitch_radius`` from params (or the pinion cylinder
    # radius). Translation scalar s_b = (t_b - t_b0)·d̂_rack. One scalar row.

    def _rack_pinion(self):
        # A = pinion (rotates), B = rack (translates). pitch radius: param or pinion geom radius.
        pr = self._param("pitch_radius", self.ga.radius or 1.0)
        theta_a, row_a = self._theta_row("a")
        d_rack = self._axis_world("b")
        tb0 = self.fb.t0
        coup = self.sign

        def s_b(xs):
            x6 = self._xb(xs)
            return float(np.dot(x6[0:3] - tb0, d_rack))

        def s_b_row(xs):
            r6 = np.zeros((1, 6))
            r6[0, 0:3] = d_rack
            return self._place("b", r6)

        def rfunc(xs):
            return np.array([s_b(xs) - coup * pr * theta_a(xs)])

        def jfunc(xs):
            return s_b_row(xs) - coup * pr * row_a(xs)

        return rfunc, jfunc, 1

    # ----- screw ----------------------------------------------------------
    # Rotation <-> translation pitch coupling on ONE component (a screw turning in a fixed nut) or
    # BETWEEN two (the relative turn drives the relative advance). Residual:
    #   (s_b - s_a) - pitch * (theta_b - theta_a) / (2*pi) == 0
    # where ``pitch`` is the lead (axial advance per full turn, mm/rev), s = axial translation,
    # theta = on-axis angle. With a fixed nut (component A fixed), this becomes Δz == pitch*θ/2π.

    def _screw(self):
        pitch = self._param("pitch", 1.0)
        k = pitch / (2.0 * math.pi)
        theta_a, rowt_a = self._theta_row("a")
        theta_b, rowt_b = self._theta_row("b")
        ax_a = self._axis_world("a")
        ax_b = self._axis_world("b")
        ta0, tb0 = self.fa.t0, self.fb.t0
        coup = self.sign

        def s_a(xs):
            return float(np.dot(self._xa(xs)[0:3] - ta0, ax_a))

        def s_b(xs):
            return float(np.dot(self._xb(xs)[0:3] - tb0, ax_b))

        def s_a_row(xs):
            r6 = np.zeros((1, 6)); r6[0, 0:3] = ax_a
            return self._place("a", r6)

        def s_b_row(xs):
            r6 = np.zeros((1, 6)); r6[0, 0:3] = ax_b
            return self._place("b", r6)

        def rfunc(xs):
            ds = s_b(xs) - s_a(xs)
            dth = theta_b(xs) - theta_a(xs)
            return np.array([ds - coup * k * dth])

        def jfunc(xs):
            ds_row = s_b_row(xs) - s_a_row(xs)
            dth_row = rowt_b(xs) - rowt_a(xs)
            return ds_row - coup * k * dth_row

        return rfunc, jfunc, 1

    # ----- hinge ----------------------------------------------------------
    # A hinge = concentric (axes collinear + coincident: 4 rows) + a single angle-limit row about
    # the hinge axis (the 5th). It removes all DOF except a limited swing. We reuse the concentric
    # block and append the angle-limit residual on the two components' reference directions.

    def _hinge(self):
        # concentric part (axes from ref0/ref1 directions + points).
        da_l = self.ga.dir_vector()
        db_l = self.gb.dir_vector()
        pa_l, pb_l = self.ga.point, self.gb.point
        sign = self.sign
        # swing-limit part: reference markers perpendicular to the axis. If extra geoms carry
        # marker directions use them, else derive a stable perp basis from each axis.
        if len(self.extra) >= 2:
            ma_l = self.extra[0].dir_vector()
            mb_l = self.extra[1].dir_vector()
        else:
            ma_l = _perp_basis(da_l)[0]
            mb_l = _perp_basis(db_l)[0]
        lo = math.radians(self._param("min", 0.0))
        hi = math.radians(self._param("max", 360.0))

        def _swing(xs):
            ma, Jma = self._wd("a", xs, ma_l)
            mb, Jmb = self._wd("b", xs, mb_l)
            c = float(np.clip(np.dot(ma, mb), -1.0, 1.0))
            ang = math.acos(c)
            s = math.sqrt(max(1.0 - c * c, 1e-12))
            ddot = self._place("a", (mb @ Jma)[None, :]) + self._place("b", (ma @ Jmb)[None, :])
            return ang, (-1.0 / s) * ddot

        def rfunc(xs):
            da, _ = self._wd("a", xs, da_l)
            db, _ = self._wd("b", xs, db_l)
            pa, _ = self._wp("a", xs, pa_l)
            pb, _ = self._wp("b", xs, pb_l)
            r = np.empty(5)
            r[0:2] = _two_indep_cross(da, sign * db)
            r[2:4] = _perp_residual2(da, pb - pa)
            ang, _ = _swing(xs)
            if ang < lo:
                r[4] = ang - lo
            elif ang > hi:
                r[4] = ang - hi
            else:
                r[4] = 0.0
            return r

        def jfunc(xs):
            da, Jda = self._wd("a", xs, da_l)
            db, Jdb = self._wd("b", xs, db_l)
            pa, Jpa = self._wp("a", xs, pa_l)
            pb, Jpb = self._wp("b", xs, pb_l)
            J = self._empty_jac(5)
            sdb = sign * db
            cross_jac = self._place("a", -_skew(sdb) @ Jda) + self._place("b", _skew(da) @ (sign * Jdb))
            J[0:2, :] = _two_indep_cross_jac(da, sdb, cross_jac)
            diff = pb - pa
            Jdiff = self._place("b", Jpb) - self._place("a", Jpa)
            J[2:4, :] = _perp_residual2_jac(da, self._place("a", Jda), diff, Jdiff)
            ang, swing_row = _swing(xs)
            J[4:5, :] = swing_row if (ang < lo or ang > hi) else np.zeros_like(swing_row)
            return J

        return rfunc, jfunc, 5

    # ----- slot -----------------------------------------------------------
    # A pin (ref0 axis/point on A) constrained to a straight slot path on B (ref1 axis = the slot
    # centerline). The pin centre is held on the slot line (perp distance == 0) but free to slide
    # along it. We use the axis-point perpendicular distance (== 0), the same primitive `tangent`
    # cyl-cyl uses. One scalar row (perp magnitude). For a 3D pin this pins it to the line; the
    # along-slot DOF stays free (SolidWorks "slot" mate).

    def _slot(self):
        # slot centerline on B, pin point on A. Hold the pin on the line via the 2-component
        # perpendicular residual (the same primitive concentric uses): both scalars vanish iff the
        # pin lies on the line, and they pass LINEARLY through zero (a single distance scalar has a
        # vanishing gradient at the line, stalling LM). The along-line DOF stays free -> the pin
        # slides in the slot. Residual dim is 2 here but declared 1 in residual_dim (informational
        # only; nsolve reads the actual rfunc length).
        pslot_l, dslot_l = self.gb.point, self.gb.direction
        ppin_l = self.ga.point

        def rfunc(xs):
            ps, _ = self._wp("b", xs, pslot_l)
            ds, _ = self._wd("b", xs, dslot_l)
            pp, _ = self._wp("a", xs, ppin_l)
            return _perp_residual2(ds, pp - ps)

        def jfunc(xs):
            ps, Jps = self._wp("b", xs, pslot_l)
            ds, Jds = self._wd("b", xs, dslot_l)
            pp, Jpp = self._wp("a", xs, ppin_l)
            diff = pp - ps
            Jdiff = self._place("a", Jpp) - self._place("b", Jps)
            return _perp_residual2_jac(ds, self._place("b", Jds), diff, Jdiff)

        return rfunc, jfunc, 2

    # ----- cam-follower ---------------------------------------------------
    # Best-effort: the follower (ref0 = a point/sphere centre on A, radius r) stays TANGENT to the
    # cam profile, modeled as a cylinder (ref1 axis on B, radius R). Tangency => the centre-to-axis
    # perpendicular distance == R + r (external roller on a circular cam lobe). One scalar row.
    # Envelope: a true cam is a general non-circular profile; we tangent against the local
    # osculating circle (the cam ref radius). For a circular cam this is exact; for a general
    # profile it is a first-order (local-curvature) approximation, re-linearized each warm solve.

    def _cam_follower(self):
        # follower centre on A; cam axis+radius on B; follower radius from ga.radius.
        pf_l = self.ga.point
        rf = self.ga.radius
        pc_l, dc_l = self.gb.point, self.gb.direction
        R = self.gb.radius
        sign = self.sign
        target = (R + rf) if sign > 0 else abs(R - rf)

        def rfunc(xs):
            pc, _ = self._wp("b", xs, pc_l)
            dc, _ = self._wd("b", xs, dc_l)
            pf, _ = self._wp("a", xs, pf_l)
            dist = _axis_point_distance(pc, dc, pf)
            return np.array([dist - target])

        def jfunc(xs):
            pc, Jpc = self._wp("b", xs, pc_l)
            dc, Jdc = self._wd("b", xs, dc_l)
            pf, Jpf = self._wp("a", xs, pf_l)
            return _axis_point_distance_jac(
                pc, self._place("b", Jpc), dc, self._place("b", Jdc),
                pf, self._place("a", Jpf))

        return rfunc, jfunc, 1


# --------------------------------------------------------------------------- vector primitives


def _perp_residual2(axis: np.ndarray, vec: np.ndarray) -> np.ndarray:
    """Two scalars measuring the component of *vec* perpendicular to *axis* (0 when collinear).

    Project *vec* onto a 2D basis spanning the plane ⟂ axis: ``[vec·e1, vec·e2]``.
    """
    e1, e2 = _perp_basis(axis)
    return np.array([float(np.dot(vec, e1)), float(np.dot(vec, e2))])


def _perp_residual2_jac(axis, Jaxis, vec, Jvec) -> np.ndarray:
    """Jacobian of :func:`_perp_residual2` (treats the perp basis as locally constant).

    The basis e1,e2 rotates with the axis; including that coupling adds a second-order term that
    vanishes at the solution (vec ⟂ axis there). Dropping it keeps the block analytic and the LM
    iteration converges (the omitted term is O(residual)). Rows: [d(vec·e1); d(vec·e2)].
    """
    e1, e2 = _perp_basis(axis)
    row1 = e1 @ Jvec
    row2 = e2 @ Jvec
    return np.vstack([row1, row2])


def _perp_basis(axis: np.ndarray):
    a = _unit(axis)
    # pick a helper not parallel to a
    helper = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = _unit(np.cross(a, helper))
    e2 = _unit(np.cross(a, e1))
    return e1, e2


def _two_indep_cross(da: np.ndarray, db: np.ndarray) -> np.ndarray:
    """Two independent scalars that vanish iff da ∥ db (collinear). Projects da×db onto a 2D
    basis ⟂ da (the cross is already ⟂ da, so 2 components fully capture it)."""
    c = np.cross(da, db)
    e1, e2 = _perp_basis(da)
    return np.array([float(np.dot(c, e1)), float(np.dot(c, e2))])


def _two_indep_cross_jac(da, db, cross_jac) -> np.ndarray:
    e1, e2 = _perp_basis(da)
    return np.vstack([e1 @ cross_jac, e2 @ cross_jac])


def _axis_point_distance(p_axis: np.ndarray, d_axis: np.ndarray, q: np.ndarray) -> float:
    d = _unit(d_axis)
    v = q - p_axis
    perp = v - np.dot(v, d) * d
    return float(np.linalg.norm(perp))


def _axis_point_distance_jac(p_axis, Jp, d_axis, Jd, q, Jq) -> np.ndarray:
    d = _unit(d_axis)
    v = q - p_axis
    perp = v - np.dot(v, d) * d
    nrm = float(np.linalg.norm(perp))
    if nrm < 1e-9:
        return np.zeros((1, Jp.shape[1]))
    u = perp / nrm
    # dperp/dx = dv - (d·v) dd? approximate with dv projected (drop d-rotation coupling, O(resid))
    Jv = Jq - Jp
    Jperp = Jv - d[:, None] * (d @ Jv)[None, :]
    return (u @ Jperp)[None, :]


# --------------------------------------------------------------------------- public solve


def solve_mates(components: list[dict], mates: list[dict],
                resolved_geometry: dict, warm: Optional[dict] = None,
                drag: Optional[dict] = None,
                tol: float = 1e-9, max_iter: int = 80,
                time_budget_ms: Optional[float] = None) -> MateSolveResult:
    """Solve the assembly mates; return updated transforms + per-mate status + DOF summary.

    ``resolved_geometry`` maps ``(mate_id, ref_index)`` -> :class:`MateGeom` (local frame). A mate
    whose geometry is missing for either ref is reported ``unresolved`` and skipped (it neither
    constrains nor errors the solve — SolidWorks treats a dangling mate as a warning).
    """
    warm = warm or {}
    system = System()
    frames: dict[str, _CompFrame] = {}

    for comp in components:
        if comp.get("suppressed"):
            continue
        cid = comp["id"]
        if cid in warm:
            t, w = warm[cid]
            t = np.asarray(t, dtype=float).reshape(3)
            w = np.asarray(w, dtype=float).reshape(3)
        else:
            t, w = _transform_to_params(comp.get("transform") or _IDENTITY16)
        frame = _CompFrame(cid, t, w, bool(comp.get("fixed", False)))
        frames[cid] = frame

    # Allocate vars for floating components only.
    for frame in frames.values():
        frame.alloc(system)

    mate_records: list[tuple[str, str]] = []      # (mate_id, status-if-unresolved-or-"")
    active_tags: list[str] = []
    builders: list[tuple[str, "_MateBuilder", Any, int]] = []  # (mid, builder, rfunc, m)

    for mate in mates:
        mid = mate["id"]
        if mate.get("suppressed"):
            mate_records.append((mid, "suppressed"))
            continue
        refs = mate.get("refs", [])
        if len(refs) < 2:
            mate_records.append((mid, "unresolved"))
            continue
        ga = resolved_geometry.get((mid, 0))
        gb = resolved_geometry.get((mid, 1))
        ca = frames.get(refs[0].get("component"))
        cb = frames.get(refs[1].get("component"))
        if ga is None or gb is None or ca is None or cb is None:
            mate_records.append((mid, "unresolved"))
            continue
        # Advanced/mechanical mates may carry a 3rd/4th resolved geom (a symmetry plane, second
        # width jaw, hinge markers) at ref indices >= 2; gather them in order. Standard mates have
        # none and behave exactly as before.
        extra_geoms = []
        extra_owners = []
        cid_a = refs[0].get("component")
        cid_b = refs[1].get("component")
        gi = 2
        while (mid, gi) in resolved_geometry:
            extra_geoms.append(resolved_geometry[(mid, gi)])
            # which of the pair (a/b) owns this extra ref's component? default to "b".
            ec = refs[gi].get("component") if gi < len(refs) else None
            extra_owners.append("a" if ec == cid_a and ec != cid_b else "b")
            gi += 1
        value = _mate_value(mate)
        params = mate.get("value") if isinstance(mate.get("value"), dict) else {}
        try:
            builder = _MateBuilder(ca, ga, cb, gb, mate["type"], mate.get("align", "aligned"), value,
                                   extra_geoms=extra_geoms, params=params, extra_owners=extra_owners)
            rfunc, jfunc, m = builder.make()
        except Exception:  # noqa: BLE001 — an unbuildable mate is reported unresolved, not fatal
            mate_records.append((mid, "unresolved"))
            continue
        if builder.idx.size == 0:
            # both components fixed: the mate constrains nothing (still "ok", just inert).
            mate_records.append((mid, ""))
            continue
        system.add_residual(rfunc, builder.idx, jfunc, mid, weight=1.0)
        active_tags.append(mid)
        builders.append((mid, builder, rfunc, m))
        mate_records.append((mid, ""))

    # Drag soft target (constrained drag along free DOF).
    if drag is not None:
        dc = frames.get(drag.get("component"))
        target = drag.get("target")
        if dc is not None and not dc.fixed and target is not None:
            target = np.asarray(target, dtype=float).reshape(3)
            for k in range(3):
                system.soft_target(int(dc.t_idx[k]), float(target[k]), weight=1e-3)

    # Solve.
    if system.n_vars == 0:
        x = np.zeros(0)
        res_converged = True
        max_res = 0.0
    else:
        res = system.solve(tol=tol, max_iter=max_iter, time_budget_ms=time_budget_ms)
        x = res.x
        res_converged = res.converged
        max_res = res.max_residual
        # Antipodal-start escape: a slider whose initial orientation is exactly 180° from the
        # mated pose sits at a zero-gradient saddle of the exp-map (the classic flip singularity).
        # If we stalled with rotational DOF present and no drag, retry from small random rotation
        # perturbations so LM can fall off the saddle. (No FDs; just better starts.)
        if (not res_converged and drag is None and time_budget_ms is None
                and any(not f.fixed for f in frames.values())):
            rng = np.random.default_rng(12345)
            x0_base = np.array(system._init, dtype=float)  # registered initial guess
            w_indices = np.concatenate(
                [f.w_idx for f in frames.values() if not f.fixed])
            best = res
            for _ in range(6):
                x0 = x0_base.copy()
                x0[w_indices] += rng.normal(scale=0.6, size=w_indices.size)
                r2 = system.solve(x0=x0, tol=tol, max_iter=max_iter)
                if r2.max_residual < best.max_residual:
                    best = r2
                if best.converged:
                    break
            x = best.x
            res_converged = best.converged
            max_res = best.max_residual

    # Extract transforms + warm params.
    transforms: dict[str, list[float]] = {}
    params: dict[str, tuple] = {}
    for cid, frame in frames.items():
        if frame.fixed:
            t, w = frame.t0, frame.w0
        else:
            t = x[frame.t_idx]
            w = x[frame.w_idx]
        transforms[cid] = _params_to_transform(t, w)
        params[cid] = (np.asarray(t, dtype=float).copy(), np.asarray(w, dtype=float).copy())

    # DOF + redundancy/conflict analysis. nsolve.analyze flags any linearly-dependent residual
    # ROW, but a peg-in-hole legitimately has dependent orientation rows shared between a
    # concentric and a coincident mate — both are still doing useful work. To match SolidWorks
    # UX we classify a MATE (not a row): a mate is "redundant" only if it removes zero DOF (its
    # whole block is dependent on the rest) and "conflicting" only if its block carries
    # unsatisfiable residual at the solution. Per-mate rank contribution is computed by an
    # incremental QR over the mate blocks.
    redundant: set[str] = set()
    conflicting: set[str] = set()
    dof = system.n_vars
    if system.n_vars > 0 and active_tags:
        analysis = system.analyze(x)
        dof = analysis.dof
        redundant, conflicting = _classify_mates(builders, x, tol)

    mate_status: list[MateStatus] = []
    for mid, forced in mate_records:
        mate_obj = next((m for m in mates if m["id"] == mid), {})
        mtype = mate_obj.get("type", "?")
        if forced:
            status = forced
        elif mid in conflicting:
            status = "conflicting"
        elif mid in redundant:
            status = "redundant"
        else:
            status = "ok"
        mate_status.append(MateStatus(mid, mtype, status))

    over_defined = len(conflicting) > 0
    summary = _dof_summary(dof, over_defined, len(frames),
                           sum(1 for f in frames.values() if f.fixed))

    return MateSolveResult(
        transforms=transforms,
        params=params,
        mate_status=mate_status,
        dof=int(dof),
        converged=bool(res_converged),
        max_residual=float(max_res),
        over_defined=over_defined,
        summary=summary,
    )


def _classify_mates(builders, x: np.ndarray, tol: float):
    """Per-mate redundant/conflicting classification (SolidWorks-style, mate-granular).

    For each active mate block (in registration order) we ask: does adding this mate's Jacobian
    rows increase the rank of the accumulated system? If not, the mate removes no DOF -> redundant.
    A redundant mate whose residual at the solution is non-trivial is *conflicting* (its constraint
    can't be met). Earlier mates "win" — the last duplicate is the one flagged, matching SW's
    "this mate is over-defining" call-out.
    """
    redundant: set[str] = set()
    conflicting: set[str] = set()
    rtol = max(1e-6, 1e3 * tol)

    accum: Optional[np.ndarray] = None   # stacked independent rows so far
    base_rank = 0
    for mid, builder, rfunc, m in builders:
        idx = builder.idx
        # Build the block jacobian at x over the block's variables, then widen to global columns
        # so cross-mate dependence is measured in a common column space.
        jfunc = builder.make()[1]
        Jb = np.atleast_2d(np.asarray(jfunc(x[idx]), dtype=np.float64))  # (m, len(idx))
        rb = np.atleast_1d(np.asarray(rfunc(x[idx]), dtype=np.float64))
        # widen to global columns
        n = int(x.shape[0])
        Wb = np.zeros((Jb.shape[0], n))
        Wb[:, idx] = Jb
        if accum is None:
            stacked = Wb
        else:
            stacked = np.vstack([accum, Wb])
        new_rank = np.linalg.matrix_rank(stacked, tol=max(1e-9, tol))
        gained = new_rank - base_rank
        if gained <= 0:
            # removes no DOF -> redundant; conflicting if its residual can't be satisfied.
            if float(np.max(np.abs(rb))) > rtol:
                conflicting.add(str(mid))
            else:
                redundant.add(str(mid))
            # do not advance accum/base_rank (the mate added nothing independent)
        else:
            accum = stacked
            base_rank = new_rank
    return redundant, conflicting


_IDENTITY16 = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _mate_value(mate: dict) -> float:
    v = mate.get("value")
    if isinstance(v, dict):
        return float(v.get("value", 0.0))
    if isinstance(v, (int, float)):
        return float(v)
    return 0.0


def _dof_summary(dof: int, over: bool, n_comp: int, n_fixed: int) -> str:
    if over:
        return f"over-defined ({n_comp} components, {n_fixed} fixed)"
    if dof <= 0:
        return f"fully constrained ({n_comp} components, {n_fixed} fixed)"
    return f"{dof} DOF remaining ({n_comp} components, {n_fixed} fixed)"
