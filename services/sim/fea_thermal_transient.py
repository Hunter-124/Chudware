"""Python oracle for the C++ transient thermal FEA kernel (core/sim/fea_thermal.cpp)
— the sim-stack differential-parity reference.

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
    """Exterior element faces whose three corners are in ``node_set``.

    A face shared by two tetrahedra is internal and must never receive a surface
    flux or convection film, even when all of its nodes happen to be in a merged
    boundary node set.
    """
    owners: dict[Tuple[int, int, int], Tuple[int, Tuple[int, int, int]]] = {}
    for el in tets:
        for f in _TET_FACES:
            cn = (int(el[f[0]]), int(el[f[1]]), int(el[f[2]]))
            key = tuple(sorted(cn))
            count, first = owners.get(key, (0, cn))
            owners[key] = (count + 1, first)
    return [cn for count, cn in owners.values()
            if count == 1 and all(i in node_set for i in cn)]


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
    try:
        pts = np.asarray(points, dtype=float)
        raw_tets = np.asarray(tets, dtype=float)
    except (TypeError, ValueError) as exc:
        return TransientResult(False, f"invalid mesh arrays: {exc}")
    if pts.ndim != 2 or pts.shape[1:] != (3,):
        return TransientResult(False, "points must have shape (n_nodes, 3)")
    if raw_tets.ndim != 2 or raw_tets.shape[1:] != (4,):
        return TransientResult(False, "tets must have shape (n_elements, 4)")
    if not np.all(np.isfinite(pts)) or not np.all(np.isfinite(raw_tets)):
        return TransientResult(False, "mesh coordinates/connectivity must be finite")
    if not np.all(raw_tets == np.floor(raw_tets)):
        return TransientResult(False, "tet connectivity must contain integer node indices")
    tets = raw_tets.astype(int)
    n = len(pts)
    if n == 0 or len(tets) == 0:
        return TransientResult(False, "empty mesh")
    if np.any(tets < 0) or np.any(tets >= n):
        return TransientResult(False, "tet connectivity contains an out-of-range node index")

    try:
        k_cond = float(k_cond)
        rho_cp = float(rho_cp)
        t_end = float(t_end)
        dt = float(dt)
        t_init = float(t_init)
    except (TypeError, ValueError) as exc:
        return TransientResult(False, f"invalid thermal scalar: {exc}")
    if not all(math.isfinite(v) for v in (k_cond, rho_cp, t_end, dt, t_init)):
        return TransientResult(False, "thermal inputs must be finite")
    if not (k_cond > 0.0 and rho_cp > 0.0 and dt > 0.0 and t_end > 0.0):
        return TransientResult(False, "k, rho_cp, dt, and t_end must be > 0")
    if len(fixed_nodes) != len(fixed_temps):
        return TransientResult(False, "fixed_nodes/fixed_temps length mismatch")
    if len(fixed_nodes) == 0 and len(fluxes) == 0 and len(convections) == 0:
        return TransientResult(False, "transient study needs a thermal boundary or heat source")

    def checked_nodes(values: Sequence[int], label: str, *,
                      reject_duplicates: bool = True) -> List[int]:
        out: List[int] = []
        for raw in values:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} contains a non-numeric node index") from exc
            if not math.isfinite(value) or value != math.floor(value):
                raise ValueError(f"{label} must contain finite integer node indices")
            nid = int(value)
            if nid < 0 or nid >= n:
                raise ValueError(f"{label} contains out-of-range node {nid}")
            out.append(nid)
        if reject_duplicates and len(set(out)) != len(out):
            raise ValueError(f"{label} contains duplicate node indices")
        return out

    try:
        fixed_raw = checked_nodes(fixed_nodes, "fixed_nodes", reject_duplicates=False)
        prescribed_raw = [float(v) for v in fixed_temps]
    except (TypeError, ValueError) as exc:
        return TransientResult(False, str(exc))
    if not all(math.isfinite(v) for v in prescribed_raw):
        return TransientResult(False, "fixed temperatures must be finite")
    fixed_values: dict[int, float] = {}
    for nid, temperature in zip(fixed_raw, prescribed_raw):
        if nid in fixed_values and fixed_values[nid] != temperature:
            return TransientResult(False, f"fixed node {nid} has conflicting temperatures")
        fixed_values[nid] = temperature
    fixed = list(fixed_values)
    prescribed = list(fixed_values.values())

    is_fixed = np.zeros(n, dtype=bool)
    pres = np.zeros(n)
    if fixed:
        is_fixed[fixed] = True
        pres[fixed] = prescribed
    free = np.where(~is_fixed)[0]
    nf = len(free)

    K = np.zeros((n, n))
    C = np.zeros((n, n))
    for elem_index, el in enumerate(tets):
        if len(set(int(i) for i in el)) != 4:
            return TransientResult(False, f"tet {elem_index} repeats a node")
        try:
            dN, vol = _tet4_grad(pts[el])
        except np.linalg.LinAlgError:
            dN, vol = None, 0.0
        if dN is None or not math.isfinite(vol) or vol <= 0.0:
            return TransientResult(False, f"tet {elem_index} is degenerate")
        Ke = k_cond * vol * (dN @ dN.T)
        K[np.ix_(el, el)] += Ke
        if lumped:
            C[el, el] += rho_cp * vol / 4.0
        else:
            Ce = rho_cp * vol / 20.0 * (np.ones((4, 4)) + np.eye(4))
            C[np.ix_(el, el)] += Ce
    if not np.all(np.isfinite(K)) or not np.all(np.isfinite(C)):
        return TransientResult(False, "assembled thermal matrices are non-finite")

    exterior = _surface_tris(tets, set(range(n)))
    if not exterior:
        return TransientResult(False, "mesh has no exterior faces")

    def selected_faces(values: Sequence[int], label: str) -> List[Tuple[int, int, int]]:
        nodes = checked_nodes(values, label, reject_duplicates=False)
        if not nodes:
            raise ValueError(f"{label} is empty")
        nset = set(nodes)
        faces = [cn for cn in exterior if all(i in nset for i in cn)]
        if not faces:
            raise ValueError(f"{label} selects no exterior mesh faces")
        return faces

    f_base = np.zeros(n)
    try:
        for index, cv in enumerate(convections):
            h = float(cv.h)
            t_amb = float(cv.t_amb)
            if not math.isfinite(h) or h <= 0.0:
                raise ValueError(f"convections[{index}].h must be finite and > 0")
            if not math.isfinite(t_amb):
                raise ValueError(f"convections[{index}].t_amb must be finite")
            for cn in selected_faces(cv.nodes, f"convections[{index}].nodes"):
                area = _tri_area(pts, cn)
                if not math.isfinite(area) or area <= 0.0:
                    raise ValueError(f"convections[{index}] contains a degenerate face")
                ms = (area / 12.0) * (np.ones((3, 3)) + np.eye(3))
                idx = list(cn)
                K[np.ix_(idx, idx)] += h * ms
                f_base[idx] += h * t_amb * area / 3.0

        flux_vecs = []
        flux_schedules = []
        for index, fl in enumerate(fluxes):
            q = float(fl.q)
            if not math.isfinite(q) or q == 0.0:
                raise ValueError(f"fluxes[{index}].q must be finite and non-zero")
            schedule = fl.schedule
            if len(schedule.times) != len(schedule.scales):
                raise ValueError(f"fluxes[{index}] schedule times/scales length mismatch")
            period = float(schedule.period)
            if not math.isfinite(period) or period < 0.0:
                raise ValueError(f"fluxes[{index}] schedule period must be finite and >= 0")
            times = [float(v) for v in schedule.times]
            scales = [float(v) for v in schedule.scales]
            if not all(math.isfinite(v) for v in times + scales):
                raise ValueError(f"fluxes[{index}] schedule values must be finite")
            if any(v < 0.0 for v in times):
                raise ValueError(f"fluxes[{index}] schedule times must be >= 0")
            if any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError(f"fluxes[{index}] schedule times must be strictly increasing")
            if period > 0.0 and times and times[-1] >= period:
                raise ValueError(f"fluxes[{index}] schedule times must be < period")
            if scales and not any(v != 0.0 for v in scales):
                raise ValueError(f"fluxes[{index}] schedule never applies a non-zero source")
            flux_schedules.append(Schedule(times=times, scales=scales, period=period))
            fv = np.zeros(n)
            for cn in selected_faces(fl.nodes, f"fluxes[{index}].nodes"):
                area = _tri_area(pts, cn)
                if not math.isfinite(area) or area <= 0.0:
                    raise ValueError(f"fluxes[{index}] contains a degenerate face")
                fv[list(cn)] += q * area / 3.0
            flux_vecs.append(fv)
    except (AttributeError, TypeError, ValueError) as exc:
        return TransientResult(False, str(exc))

    Kff = K[np.ix_(free, free)]
    Cff = C[np.ix_(free, free)]
    g_dir = -K[np.ix_(free, np.where(is_fixed)[0])] @ pres[is_fixed]
    fb = f_base[free] + g_dir

    T = np.full(n, t_init)
    T[is_fixed] = pres[is_fixed]
    env = T.copy()
    gmax, gmin, gmax_t = float(T.max()), float(T.min()), 0.0

    res = TransientResult(True)
    try:
        want = sorted(float(t) for t in frame_times)
    except (TypeError, ValueError) as exc:
        return TransientResult(False, f"invalid frame time: {exc}")
    if not all(math.isfinite(t) for t in want):
        return TransientResult(False, "frame times must be finite")
    wi = 0
    if wi < len(want) and want[wi] <= 0.0:
        res.frames.append((0.0, T.copy()))
        while wi < len(want) and want[wi] <= 0.0:
            wi += 1

    full_steps = int(math.floor(t_end / dt))
    n_steps = full_steps if math.isclose(full_steps * dt, t_end,
                                          rel_tol=1e-12, abs_tol=1e-15) else full_steps + 1
    res.n_steps = n_steps
    if nf == 0:
        res.frames.append((t_end, T.copy()))
        res.envelope_max, res.max_temp, res.min_temp = env, gmax, gmin
        return res

    full_inverse: Optional[np.ndarray] = None
    Tf = T[free].copy()
    t_prev = 0.0
    for step in range(1, n_steps + 1):
        t_next = min(step * dt, t_end)
        step_dt = t_next - t_prev
        try:
            if math.isclose(step_dt, dt, rel_tol=1e-12, abs_tol=1e-15):
                if full_inverse is None:
                    full_inverse = np.linalg.inv(Kff + Cff / dt)
                a_inv = full_inverse
            else:
                a_inv = np.linalg.inv(Kff + Cff / step_dt)
        except np.linalg.LinAlgError:
            return TransientResult(False, "thermal iteration matrix is singular")
        rhs = fb + (Cff @ Tf) / step_dt
        for schedule, fv in zip(flux_schedules, flux_vecs):
            scale = schedule.scale(t_next)
            if scale != 0.0:
                rhs += scale * fv[free]
        Tf = a_inv @ rhs
        if not np.all(np.isfinite(Tf)):
            return TransientResult(False, "thermal solve produced non-finite temperatures")
        T[free] = Tf
        env = np.maximum(env, T)
        maximum = float(T.max())
        if maximum > gmax:
            gmax, gmax_t = maximum, t_next
        gmin = min(gmin, float(T.min()))
        record = step == n_steps
        while wi < len(want) and want[wi] <= t_next + 1e-12:
            record = True
            wi += 1
        if record:
            res.frames.append((t_next, T.copy()))
        t_prev = t_next

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
