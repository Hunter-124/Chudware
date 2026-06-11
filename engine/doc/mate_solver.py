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
                 mtype: str, align: str, value: float) -> None:
        self.fa, self.ga = frame_a, geom_a
        self.fb, self.gb = frame_b, geom_b
        self.mtype = mtype
        self.sign = -1.0 if align == "anti" else 1.0
        self.value = value
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
        }.get(t)
        if builder is None:
            raise ValueError(f"mate_solver: unsupported mate type '{t}'")
        return builder()

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
        if len(refs) != 2:
            mate_records.append((mid, "unresolved"))
            continue
        ga = resolved_geometry.get((mid, 0))
        gb = resolved_geometry.get((mid, 1))
        ca = frames.get(refs[0].get("component"))
        cb = frames.get(refs[1].get("component"))
        if ga is None or gb is None or ca is None or cb is None:
            mate_records.append((mid, "unresolved"))
            continue
        value = _mate_value(mate)
        try:
            builder = _MateBuilder(ca, ga, cb, gb, mate["type"], mate.get("align", "aligned"), value)
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
