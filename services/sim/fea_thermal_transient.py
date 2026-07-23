"""Python oracle for the C++ transient thermal FEA kernel (core/sim/fea_thermal.cpp,
T-FIN18) — the sim-stack differential-parity reference.

Scope (kept deliberately minimal, like the lbm.py oracle): tet4 elements, isotropic
conductivity, Dirichlet fixed temperatures, per-load surface heat flux with a
piecewise/periodic schedule, convection (Robin) films, lumped (row-sum) or consistent
rho*cp capacitance, implicit backward Euler at a fixed step:

    (K + C/dt) T^{n+1} = (C/dt) T^n + f(t^{n+1})

Same unit convention as the kernel: everything in the mesh length unit L (mm in
Chudware) — k in W/(L*K), q in W/L^2, h in W/(L^2*K), rho_cp VOLUMETRIC in J/(L^3*K).
Dense numpy linear algebra (oracle meshes are small); the iteration matrix is
factored once (numpy.linalg LU via scipy-free ``lu_factor`` equivalent: we simply
pre-invert — acceptable at oracle scale) and reused across steps, mirroring the
C++ reuse of the SparseLU/Cholesky factorization.

Schedule semantics (must stay in lockstep with thermal_schedule_scale in C++):
sample-and-hold — scale[i] holds on [times[i], times[i+1]); the last scale holds to
the end of the period (period > 0 wraps via fmod) or to t_end; an EMPTY schedule
means always-on (scale 1). This is the direct consumption shape for
aicad.powerprofile/1 per-rail duty cycles: duty d over period p =
times=[0, d*p], scales=[1, 0], period=p.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

# Tet corner faces, matching core/sim/fea_thermal.cpp kTetFaces (corners only — tet4).
_TET_FACES = ((0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3))


@dataclass
class Schedule:
    """Piecewise sample-and-hold scale s(t); empty = always on."""
    times: List[float] = field(default_factory=list)
    scales: List[float] = field(default_factory=list)
    period: float = 0.0

    def scale(self, t: float) -> float:
        if not self.times:
            return 1.0
        tt = t
        if self.period > 0.0:
            tt = math.fmod(t, self.period)
            if tt < 0.0:
                tt += self.period
        s = self.scales[0]
        for bt, bs in zip(self.times, self.scales):
            if bt <= tt + 1e-12:
                s = bs
            else:
                break
        return s


@dataclass
class FluxLoad:
    nodes: Sequence[int]          # mesh nodes on the loaded face(s)
    q: float                      # W/L^2, >0 into the body
    schedule: Schedule = field(default_factory=Schedule)


@dataclass
class Convection:
    nodes: Sequence[int]
    h: float                      # W/(L^2*K)
    t_amb: float


@dataclass
class TransientResult:
    ok: bool
    error: str = ""
    frames: List[Tuple[float, np.ndarray]] = field(default_factory=list)
    envelope_max: Optional[np.ndarray] = None
    max_temp: float = 0.0
    max_temp_time: float = 0.0
    min_temp: float = 0.0
    n_steps: int = 0


def _tet4_grad(ep: np.ndarray):
    """Constant shape-function gradients (4x3) + volume for one tet (rows of T^-1)."""
    T = np.ones((4, 4))
    T[:, 1:] = ep
    det = np.linalg.det(T)
    vol = abs(det) / 6.0
    if vol <= 0.0:
        return None, 0.0
    Tinv = np.linalg.inv(T)
    return Tinv[1:4, :].T, vol  # (4,3): dN[i,c]


def _surface_tris(tets: np.ndarray, node_set: set) -> List[Tuple[int, int, int]]:
    """Element faces whose 3 corners are all inside node_set (once per element-face,
    identical enumeration to the C++ surface_pass)."""
    out = []
    for el in tets:
        for f in _TET_FACES:
            cn = (el[f[0]], el[f[1]], el[f[2]])
            if cn[0] in node_set and cn[1] in node_set and cn[2] in node_set:
                out.append(cn)
    return out


def _tri_area(pts: np.ndarray, cn) -> float:
    a, b, c = pts[cn[0]], pts[cn[1]], pts[cn[2]]
    return 0.5 * float(np.linalg.norm(np.cross(b - a, c - a)))


def solve_transient(points: np.ndarray, tets: np.ndarray, k_cond: float,
                    fixed_nodes: Sequence[int], fixed_temps: Sequence[float],
                    fluxes: Sequence[FluxLoad], convections: Sequence[Convection],
                    rho_cp: float, t_end: float, dt: float, t_init: float = 0.0,
                    frame_times: Sequence[float] = (), lumped: bool = True
                    ) -> TransientResult:
    """Backward-Euler transient conduction; see the module docstring."""
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    tets = np.asarray(tets, dtype=int).reshape(-1, 4)
    n = len(pts)
    if n == 0 or len(tets) == 0:
        return TransientResult(False, "empty mesh")
    if not (k_cond > 0.0 and rho_cp > 0.0 and dt > 0.0 and t_end >= dt):
        return TransientResult(False, "k, rho_cp, dt must be > 0 and t_end >= dt")
    if len(fixed_nodes) != len(fixed_temps):
        return TransientResult(False, "fixed_nodes/fixed_temps length mismatch")

    is_fixed = np.zeros(n, dtype=bool)
    pres = np.zeros(n)
    for nid, tv in zip(fixed_nodes, fixed_temps):
        is_fixed[nid] = True
        pres[nid] = tv
    free = np.where(~is_fixed)[0]
    red = -np.ones(n, dtype=int)
    red[free] = np.arange(len(free))
    nf = len(free)

    K = np.zeros((n, n))
    C = np.zeros((n, n))
    for el in tets:
        dN, vol = _tet4_grad(pts[el])
        if dN is None:
            continue
        Ke = k_cond * vol * (dN @ dN.T)
        K[np.ix_(el, el)] += Ke
        if lumped:
            C[el, el] += rho_cp * vol / 4.0            # row-sum lumping
        else:
            Ce = rho_cp * vol / 20.0 * (np.ones((4, 4)) + np.eye(4))
            C[np.ix_(el, el)] += Ce

    f_base = np.zeros(n)
    # Convection: K += h * intg(Ni Nj) dS  (tri3 consistent surface mass A/12*[2,1,1;...]),
    # f += h*T_amb * A/3 per corner.
    for cv in convections:
        if cv.h <= 0.0 or not len(cv.nodes):
            continue
        nset = set(int(i) for i in cv.nodes)
        for cn in _surface_tris(tets, nset):
            A = _tri_area(pts, cn)
            if A < 1e-12:
                continue
            Ms = (A / 12.0) * (np.ones((3, 3)) + np.eye(3))
            idx = list(cn)
            K[np.ix_(idx, idx)] += cv.h * Ms
            for i in idx:
                f_base[i] += cv.h * cv.t_amb * A / 3.0

    # Per-flux consistent load vectors (q * A/3 per corner), one per scheduled load.
    flux_vecs = []
    for fl in fluxes:
        fv = np.zeros(n)
        if len(fl.nodes) and fl.q != 0.0:
            nset = set(int(i) for i in fl.nodes)
            for cn in _surface_tris(tets, nset):
                A = _tri_area(pts, cn)
                if A < 1e-12:
                    continue
                for i in cn:
                    fv[i] += fl.q * A / 3.0
        flux_vecs.append(fv)

    # Reduce: free-free blocks; Dirichlet columns move to the RHS (constant in time).
    Kff = K[np.ix_(free, free)]
    Cff = C[np.ix_(free, free)]
    g_dir = -K[np.ix_(free, np.where(is_fixed)[0])] @ pres[is_fixed]
    fb = f_base[free] + g_dir

    T = np.full(n, float(t_init))
    T[is_fixed] = pres[is_fixed]
    env = T.copy()
    gmax, gmin, gmax_t = float(T.max()), float(T.min()), 0.0

    res = TransientResult(True)
    want = sorted(float(t) for t in frame_times)
    wi = 0
    if wi < len(want) and want[wi] <= 0.0:
        res.frames.append((0.0, T.copy()))
        while wi < len(want) and want[wi] <= 0.0:
            wi += 1

    n_steps = int(math.ceil(t_end / dt - 1e-9))
    res.n_steps = n_steps
    if nf == 0:
        res.frames.append((t_end, T.copy()))
        res.envelope_max, res.max_temp, res.min_temp = env, gmax, gmin
        return res

    A_it = Kff + Cff / dt
    A_inv = np.linalg.inv(A_it)  # factored once, reused every step (dense oracle scale)
    Tf = T[free].copy()
    for step in range(1, n_steps + 1):
        t_next = min(step * dt, t_end)
        rhs = fb + (Cff @ Tf) / dt
        for fl, fv in zip(fluxes, flux_vecs):
            s = fl.schedule.scale(t_next)
            if s != 0.0:
                rhs = rhs + s * fv[free]
        Tf = A_inv @ rhs
        T[free] = Tf
        env = np.maximum(env, T)
        m = float(T.max())
        if m > gmax:
            gmax, gmax_t = m, t_next
        gmin = min(gmin, float(T.min()))
        record = step == n_steps
        while wi < len(want) and want[wi] <= t_next + 1e-12:
            record = True
            wi += 1
        if record:
            res.frames.append((t_next, T.copy()))

    res.envelope_max = env
    res.max_temp, res.min_temp, res.max_temp_time = gmax, gmin, gmax_t
    return res


def box_mesh(Lx: float, Ly: float, Lz: float, nx: int, ny: int, nz: int):
    """Kuhn 6-tet box mesh, identical decomposition to core/tests/test_thermal.cpp —
    returns (points (N,3), tets (M,4), x0_nodes, xL_nodes)."""
    def nid(i, j, k):
        return (i * (ny + 1) + j) * (nz + 1) + k

    pts, x0, xL = [], [], []
    for i in range(nx + 1):
        for j in range(ny + 1):
            for k in range(nz + 1):
                pts.append((Lx * i / nx, Ly * j / ny, Lz * k / nz))
                if i == 0:
                    x0.append(nid(i, j, k))
                if i == nx:
                    xL.append(nid(i, j, k))
    paths = (((0, 0, 0), (1, 0, 0), (1, 1, 0), (1, 1, 1)),
             ((0, 0, 0), (1, 0, 0), (1, 0, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 1, 1)),
             ((0, 0, 0), (0, 1, 0), (0, 1, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 0, 1), (0, 1, 1), (1, 1, 1)))
    tets = []
    for ci in range(nx):
        for cj in range(ny):
            for ck in range(nz):
                for path in paths:
                    tets.append([nid(ci + d[0], cj + d[1], ck + d[2]) for d in path])
    return np.array(pts), np.array(tets, dtype=int), x0, xL
