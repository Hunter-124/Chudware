"""Steady-state 1-D pipe-network hydraulic solver for :mod:`engine.piping`.

Public entry points:

* :func:`solve(doc, *, boundary, fluid=None, pumps=None, ...) -> dict` — turn an
  ``aicad.piping/1`` document (``schema.py``) into a connected hydraulic network and solve
  for **node heads / pressures** and **link flows** at steady state.
* :func:`pressure_drop_report(doc, result=None, ...) -> dict` — a per-segment pressure-drop
  report with an explicit **K-factor breakdown** (friction dP + each fitting's K).
* :func:`load_pump_curve(csv, *, speed_ratio=1.0, ...) -> dict` — import a user pump-curve
  CSV and apply pump-affinity-law scaling (``Q∝N``, ``H∝N²``).

Design mirrors the native MNA circuit simulator (``engine/sim/circuit.py``): GPL-clean,
numpy/scipy only, contract-shaped soft failures (``{"ok": False, "reason": ..., "hint": ...}``)
instead of tracebacks. The network solve is the direct hydraulic analogue of nodal analysis —
nodal heads play the role of node voltages, the pipe ``dQ/dH`` conductance plays the role of the
MNA conductance stamp, and a fixed-head (reservoir / known-pressure) node is the ground / voltage
source. Component pressure drop (Darcy-Weisbach, Colebrook friction, fitting K-factors) comes from
the MIT-licensed ``fluids`` library; when ``fluids`` is absent every entry point degrades
contract-shaped, exactly like ``catalog.nearest_pipe``.

Units: the piping document is **mm** internally (contract §1); this module converts to SI
(metres, m³/s, Pa) for the physics and reports SI + convenient engineering units (kPa, m³/h,
m/s). Hydraulic head ``H = z + p/(ρg)`` carries elevation, so gravity is honoured via each node's
``z`` coordinate.
"""

from __future__ import annotations

import csv
import io
import math
import os
from typing import Any, Optional

from engine.piping import catalog, schema

G = 9.80665  # m/s²

# Water @ 20 °C — the default fluid when the caller gives none.
WATER = {"name": "water", "rho": 998.2, "mu": 1.002e-3}

# Absolute roughness ε (m) by material family (advisory; drives friction only).
_ROUGHNESS_M = {
    "A106-B": 4.6e-5, "A53-B": 4.6e-5, "A312-304": 1.5e-5, "A312-316": 1.5e-5,
    "A333-6": 4.6e-5, "copper": 1.5e-6, "PVC": 1.5e-6, "CPVC": 1.5e-6,
}
_DEFAULT_ROUGHNESS_M = 4.6e-5  # commercial steel

# Default fitting K-factors used when a fluids-based geometric K is not applicable.
# Documented, defensible round numbers (Crane TP-410 order of magnitude); the report always
# records which method produced each K.
_DEFAULT_K = {"tee_run": 0.6, "tee_branch": 1.3, "flange": 0.0, "reducer": 0.10}

_MIN_ABS_Q = 1e-9      # m³/s flow floor for friction Reynolds (avoid div-by-zero)
_Q_JAC_FLOOR = 1e-5    # m³/s |Q| floor in the dQ/dH Jacobian conductance only (never in the
                       # residual): keeps the square-law Newton step from being strangled at a
                       # cold zero-flow start. Affects convergence rate, not the solution.
_SNAP_TOL_MM = 1.0     # endpoints within this distance collapse to one node


# ── contract-shaped error helper (matches catalog.py) ─────────────────────────

def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def have_fluids() -> bool:
    try:
        import fluids  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ── geometry helpers ──────────────────────────────────────────────────────────

def _dist(a, b) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _polyline_length_mm(path) -> float:
    return sum(_dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def _point_on_polyline(pt, path, tol) -> bool:
    """True if ``pt`` lies within ``tol`` mm of any segment of ``path``."""
    for i in range(len(path) - 1):
        if _seg_dist(pt, path[i], path[i + 1]) <= tol:
            return True
    return False


def _seg_dist(p, a, b) -> float:
    p = [float(c) for c in p]; a = [float(c) for c in a]; b = [float(c) for c in b]
    ab = [b[i] - a[i] for i in range(3)]
    ap = [p[i] - a[i] for i in range(3)]
    denom = sum(c * c for c in ab)
    t = 0.0 if denom == 0 else max(0.0, min(1.0, sum(ap[i] * ab[i] for i in range(3)) / denom))
    proj = [a[i] + t * ab[i] for i in range(3)]
    return _dist(p, proj)


# ── network assembly ──────────────────────────────────────────────────────────

class _Node:
    __slots__ = ("id", "xyz", "idx", "fixed", "head", "demand")

    def __init__(self, id, xyz, idx):
        self.id = id
        self.xyz = xyz            # mm
        self.idx = idx
        self.fixed = False
        self.head = None          # m (set for fixed nodes)
        self.demand = 0.0         # m³/s leaving the system at this node


class _Link:
    __slots__ = ("id", "a", "b", "length_m", "d_m", "area", "eps", "kfit",
                 "kbreak", "pump", "flow", "fd", "spec_err")

    def __init__(self, id, a, b):
        self.id = id
        self.a = a                # from-node idx
        self.b = b                # to-node idx
        self.length_m = 0.0
        self.d_m = 0.0            # inner diameter (m)
        self.area = 0.0
        self.eps = _DEFAULT_ROUGHNESS_M
        self.kfit = 0.0          # summed fitting K
        self.kbreak: list[dict] = []   # per-fitting K breakdown
        self.pump = None         # optional pump dict (head-vs-flow)
        self.flow = 1e-4         # m³/s current iterate (a->b positive)
        self.fd = 0.02
        self.spec_err = None


def _snap_nodes(runs, tol) -> tuple[list[_Node], dict[int, tuple[int, int]]]:
    """Collapse coincident run endpoints into shared nodes.

    Returns (nodes, run_endpoints) where run_endpoints[run_index] = (node_idx_a, node_idx_b).
    """
    nodes: list[_Node] = []

    def _get(xyz) -> int:
        for nd in nodes:
            if _dist(nd.xyz, xyz) <= tol:
                return nd.idx
        idx = len(nodes)
        nodes.append(_Node(f"n{idx}", [float(c) for c in xyz], idx))
        return idx

    run_ep: dict[int, tuple[int, int]] = {}
    for ri, run in enumerate(runs):
        path = run["path"]
        run_ep[ri] = (_get(path[0]), _get(path[-1]))
    return nodes, run_ep


def _fitting_K(ft: dict, d_m: float, fd: float) -> tuple[float, str]:
    """K-factor for a fitting on a pipe of inner-dia ``d_m`` (m) at friction ``fd``.

    Geometric K from ``fluids`` for elbows/reducers; documented defaults otherwise. Returns
    (K, method).
    """
    ftype = ft.get("type")
    params = ft.get("params") or {}
    try:
        from fluids import fittings as ff
    except Exception:  # noqa: BLE001
        ff = None

    if ftype == "elbow" and ff is not None:
        angle = params.get("angle", 90) or 90
        radius = params.get("radius", "LR") or "LR"
        bd = 1.5 if str(radius).upper() == "LR" else 1.0
        try:
            return float(ff.bend_rounded(Di=d_m, angle=float(angle), fd=fd,
                                         bend_diameters=bd)), f"bend_rounded({radius},{angle}deg)"
        except Exception:  # noqa: BLE001
            pass
    if ftype == "reducer" and ff is not None:
        try:
            nps2 = ft.get("nps2")
            d2 = catalog.nearest_pipe(nps=nps2, schedule=ft.get("schedule", "40"))
            if not isinstance(d2, dict):
                d_small = d2.inner_dia / 1000.0
                d1, d0 = max(d_m, d_small), min(d_m, d_small)
                # contraction_conical needs a cone length; use the catalog reducer length.
                dims = catalog.fitting_dims("reducer", ft.get("nps"), nps2=nps2)
                L = (dims.get("length") or 0.0) / 1000.0 if isinstance(dims, dict) else 0.0
                if L > 0:
                    K = float(ff.contraction_conical(Di1=d1, Di2=d0, l=L, fd=fd))
                    return K, "contraction_conical"
        except Exception:  # noqa: BLE001
            pass
        return _DEFAULT_K["reducer"], "default(reducer)"
    if ftype == "tee":
        branch = params.get("branch_nps")
        if branch is not None and float(branch) < float(ft.get("nps", 0) or 0):
            return _DEFAULT_K["tee_branch"], "default(tee_branch)"
        return _DEFAULT_K["tee_run"], "default(tee_run)"
    if ftype == "flange":
        return _DEFAULT_K["flange"], "default(flange)"
    if ftype == "elbow":
        # fluids unavailable — order-of-magnitude default (LR 90° ≈ 0.3).
        return 0.3, "default(elbow)"
    return 0.0, "default(none)"


def _build_network(doc, fluid, tol) -> dict:
    """Assemble nodes + links from a piping doc. Returns a context dict or contract-shaped err."""
    runs = [r for r in doc.get("runs", []) if not r.get("suppressed")]
    if not runs:
        return _err("no (unsuppressed) pipe runs to solve",
                    hint="add at least one run to the piping document")

    nodes, run_ep = _snap_nodes(runs, tol)
    links: list[_Link] = []
    warnings: list[str] = []
    doc_material = doc.get("material")

    for ri, run in enumerate(runs):
        a, b = run_ep[ri]
        lk = _Link(run.get("id", f"r{ri}"), a, b)
        lk.length_m = _polyline_length_mm(run["path"]) / 1000.0
        spec = catalog.nearest_pipe(nps=run.get("nps"), schedule=run.get("schedule", "40"))
        if isinstance(spec, dict):  # soft-fail from catalog
            lk.spec_err = spec.get("reason")
            warnings.append(f"{lk.id}: {spec.get('reason')}")
            # fall back to nps inches as bore so the network stays solvable
            bore_mm = float(run.get("nps") or 1.0) * 25.4
            lk.d_m = bore_mm / 1000.0
        else:
            lk.d_m = spec.inner_dia / 1000.0
        lk.area = math.pi * lk.d_m ** 2 / 4.0
        mat = run.get("material") or doc_material
        lk.eps = _ROUGHNESS_M.get(mat, _DEFAULT_ROUGHNESS_M)
        if lk.length_m <= 0:
            warnings.append(f"{lk.id}: zero-length run skipped")
            continue
        links.append(lk)

    if not links:
        return _err("no solvable links (all runs zero-length or unresolved)",
                    hint="check run paths and nps/schedule")

    # Attach fittings to the run(s) they lie on (K-factor loss elements).
    for ft in doc.get("fittings", []):
        if ft.get("suppressed"):
            continue
        origin = ft.get("origin")
        placed = False
        for lk, run in zip(links, [r for r in runs if _polyline_length_mm(r["path"]) > 0]):
            if _point_on_polyline(origin, run["path"], tol):
                K, method = _fitting_K(ft, lk.d_m, lk.fd)
                lk.kfit += K
                lk.kbreak.append({"fitting": ft.get("id"), "type": ft.get("type"),
                                  "K": round(K, 4), "method": method})
                placed = True
                break
        if not placed:
            warnings.append(f"fitting {ft.get('id')} at {origin} not on any run — K ignored")

    return {"nodes": nodes, "links": links, "warnings": warnings, "fluid": fluid}


# ── boundary-condition resolution ─────────────────────────────────────────────

def _resolve_node(ref: dict, nodes, links_by_id, tol) -> Optional[int]:
    """Resolve a BC node reference to a node index.

    Accepts ``{"node": "n0"}``, ``{"at": [x,y,z]}`` (nearest within tol), or
    ``{"run": "r1", "end": "start"|"end"}``.
    """
    if "node" in ref:
        for nd in nodes:
            if nd.id == ref["node"]:
                return nd.idx
        return None
    if "at" in ref:
        best, bd = None, tol
        for nd in nodes:
            d = _dist(nd.xyz, ref["at"])
            if d <= bd:
                best, bd = nd.idx, d
        return best
    if "run" in ref:
        lk = links_by_id.get(ref["run"])
        if lk is None:
            return None
        return lk.a if ref.get("end", "start") == "start" else lk.b
    return None


def _apply_boundary(ctx, boundary, tol) -> Optional[dict]:
    """Mutate ctx nodes with fixed heads / demands. Returns a contract-shaped err or None."""
    nodes = ctx["nodes"]
    links_by_id = {lk.id: lk for lk in ctx["links"]}
    rho = ctx["fluid"]["rho"]
    bcs = boundary if isinstance(boundary, list) else boundary.get("nodes", [])
    n_fixed = 0
    for bc in bcs:
        idx = _resolve_node(bc, nodes, links_by_id, tol)
        if idx is None:
            return _err(f"boundary reference {bc!r} did not resolve to a node",
                        hint="use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}")
        nd = nodes[idx]
        z = nd.xyz[2] / 1000.0
        if "head" in bc:
            nd.fixed, nd.head, n_fixed = True, float(bc["head"]), n_fixed + 1
        elif "pressure" in bc:  # kPa gauge -> head
            nd.fixed = True
            nd.head = z + float(bc["pressure"]) * 1000.0 / (rho * G)
            n_fixed += 1
        elif "demand" in bc:    # m³/h leaving system (+) / entering (-)
            nd.demand += float(bc["demand"]) / 3600.0
        elif "demand_lps" in bc:
            nd.demand += float(bc["demand_lps"]) / 1000.0
    if n_fixed == 0:
        return _err("network under-constrained: no fixed-head/pressure node",
                    hint="give at least one boundary with 'pressure' (kPa) or 'head' (m)")
    return None


# ── pump curves ───────────────────────────────────────────────────────────────

def load_pump_curve(csv_source: str, *, speed_ratio: float = 1.0,
                    flow_col: str = "flow", head_col: str = "head") -> dict:
    """Import a pump-curve CSV and apply affinity-law scaling for ``speed_ratio`` = N/N_rated.

    CSV: a header row with a flow column (m³/h) and a head column (m). ``csv_source`` may be a
    path or the CSV text itself. Affinity laws: ``Q' = Q·r``, ``H' = H·r²``. Returns a pump dict
    ``{"ok": True, "points": [[Q_m3h, H_m], ...], "shutoff_head", "max_flow", "speed_ratio"}`` or
    contract-shaped ``{"ok": False, ...}``.
    """
    try:
        if os.path.isfile(csv_source):
            with open(csv_source, newline="") as fh:
                text = fh.read()
        else:
            text = csv_source
    except Exception as exc:  # noqa: BLE001
        return _err(f"cannot read pump curve: {exc}")

    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        return _err("pump curve CSV is empty", hint="need a header row with flow,head columns")
    fmap = {c.strip().lower(): c for c in rdr.fieldnames}
    fk = fmap.get(flow_col.lower()) or fmap.get("q") or fmap.get("flow_m3h")
    hk = fmap.get(head_col.lower()) or fmap.get("h") or fmap.get("head_m")
    if not fk or not hk:
        return _err(f"pump curve needs '{flow_col}' and '{head_col}' columns",
                    hint=f"found columns: {rdr.fieldnames}")
    r = float(speed_ratio)
    pts: list[list[float]] = []
    for row in rdr:
        try:
            q = float(row[fk]) * r
            h = float(row[hk]) * r * r
        except (TypeError, ValueError):
            continue
        pts.append([q, h])
    if len(pts) < 2:
        return _err("pump curve needs >= 2 valid points", hint="check numeric flow/head cells")
    pts.sort(key=lambda p: p[0])
    return {"ok": True, "points": pts, "shutoff_head": pts[0][1],
            "max_flow": pts[-1][0], "speed_ratio": r}


def _pump_head_and_slope(pump: dict, flow_m3s: float) -> tuple[float, float]:
    """Pump head (m) and its slope d(head)/dQ (m per m³/s) at a flow magnitude (m³/s).

    Piecewise-linear over the imported curve; flat (zero-slope) extrapolation past both ends.
    """
    q = max(0.0, flow_m3s) * 3600.0  # m³/h
    pts = pump["points"]
    if q <= pts[0][0]:
        return pts[0][1], 0.0
    if q >= pts[-1][0]:
        return pts[-1][1], 0.0
    for i in range(len(pts) - 1):
        q0, h0 = pts[i]
        q1, h1 = pts[i + 1]
        if q0 <= q <= q1 and q1 > q0:
            slope_per_m3h = (h1 - h0) / (q1 - q0)
            head = h0 + (q - q0) * slope_per_m3h
            return head, slope_per_m3h * 3600.0  # -> m per m³/s
    return pts[-1][1], 0.0


def _pump_head(pump: dict, flow_m3s: float) -> float:
    return _pump_head_and_slope(pump, flow_m3s)[0]


def _attach_pumps(ctx, pumps) -> Optional[dict]:
    links_by_id = {lk.id: lk for lk in ctx["links"]}
    for spec in (pumps or []):
        run_id = spec.get("run")
        lk = links_by_id.get(run_id)
        if lk is None:
            return _err(f"pump references unknown run {run_id!r}",
                        hint=f"runs available: {list(links_by_id)}")
        curve = spec.get("curve")
        pump = curve if isinstance(curve, dict) and curve.get("ok") else \
            load_pump_curve(curve, speed_ratio=spec.get("speed_ratio", 1.0)) if curve else None
        if pump is None or not pump.get("ok"):
            return pump if isinstance(pump, dict) else _err("pump missing 'curve'")
        # direction: 'a_to_b' (default) means head added along the run's from->to sense.
        pump = dict(pump)
        pump["sign"] = -1.0 if spec.get("direction") == "b_to_a" else 1.0
        lk.pump = pump
    return None


# ── friction + solve ──────────────────────────────────────────────────────────

def _friction_factor(lk: _Link, q_abs: float, mu: float, rho: float) -> float:
    from fluids import friction_factor, Reynolds
    v = q_abs / lk.area
    Re = Reynolds(V=max(v, 1e-9), D=lk.d_m, rho=rho, mu=mu)
    if Re < 2100:
        return max(64.0 / max(Re, 1e-6), 1e-4)
    return float(friction_factor(Re=Re, eD=lk.eps / lk.d_m))


def _resistance(lk: _Link) -> float:
    """r such that friction+fitting headloss h = r·Q² (m), using the current fd + fitting K."""
    c_pipe = lk.fd * lk.length_m / (2.0 * G * lk.d_m * lk.area ** 2)
    c_fit = lk.kfit / (2.0 * G * lk.area ** 2)
    return c_pipe + c_fit


def _link_flow(lk: _Link, dH: float) -> tuple[float, float]:
    """Flow ``q`` (m³/s, a→b positive) and conductance ``p = dq/d(ΔH)`` for a head difference.

    Solves the monotonic head–flow relation ``r·q·|q| − sign·H_pump(|q|) = ΔH`` for ``q`` (a small
    inner Newton when a pump curve is present, closed form otherwise) and returns the local
    conductance for the outer nodal Newton. Folding the pump-curve slope into ``p`` is what keeps
    the outer solve from oscillating on a steep pump curve.
    """
    r = _resistance(lk)
    if r <= 0:
        return 0.0, 0.0
    if lk.pump is None:
        q = math.copysign(math.sqrt(abs(dH) / r), dH)
        return q, 1.0 / (2.0 * r * max(abs(q), _Q_JAC_FLOOR))
    sign = lk.pump["sign"]
    q = lk.flow or 1e-4
    for _ in range(40):
        hp, dhpdq = _pump_head_and_slope(lk.pump, abs(q))
        sgn_q = 1.0 if q >= 0 else -1.0
        f = r * q * abs(q) - sign * hp - dH
        dfdq = 2.0 * r * abs(q) - sign * dhpdq * sgn_q
        if dfdq == 0:
            break
        step = f / dfdq
        q -= step
        if abs(step) < 1e-12:
            break
    _, dhpdq = _pump_head_and_slope(lk.pump, abs(q))
    dfdq = 2.0 * r * max(abs(q), _Q_JAC_FLOOR) - sign * dhpdq * (1.0 if q >= 0 else -1.0)
    return q, 1.0 / max(dfdq, 1e-9)


def solve(doc: dict, *, boundary, fluid: Optional[dict] = None,
          pumps: Optional[list] = None, tol_mm: float = _SNAP_TOL_MM,
          max_iter: int = 100, tol_head: float = 1e-6) -> dict:
    """Solve an ``aicad.piping/1`` network for node heads/pressures + link flows.

    ``boundary`` is a list of node BCs (``{'node'|'at'|'run', ...}`` + one of ``head`` (m),
    ``pressure`` (kPa gauge), ``demand`` (m³/h) or ``demand_lps``); at least one fixed
    head/pressure node is required. ``fluid`` = ``{'rho','mu'}`` (default water @20 °C).
    ``pumps`` is an optional list of ``{'run', 'curve', 'speed_ratio', 'direction'}``.

    Returns a contract-shaped result: on success ``{"ok": True, "nodes": [...], "links": [...],
    "converged": bool, "iterations": int, ...}``; on failure ``{"ok": False, "reason", "hint"}``.
    Never raises for a modelling/data problem.
    """
    if schema.validate(doc):
        return _err("piping document failed schema validation",
                    hint="run engine.piping.schema.validate(doc) and fix the errors",
                    errors=schema.validate(doc))
    if not have_fluids():
        return _err("fluids library not installed",
                    hint="pip install fluids (MIT) to enable the hydraulic solve")

    fl = dict(WATER); fl.update(fluid or {})
    rho, mu = fl["rho"], fl["mu"]

    ctx = _build_network(doc, fl, tol_mm)
    if not ctx.get("nodes"):
        return ctx  # contract-shaped err
    err = _apply_boundary(ctx, boundary, tol_mm)
    if err:
        return err
    err = _attach_pumps(ctx, pumps)
    if err:
        return err

    nodes, links = ctx["nodes"], ctx["links"]
    free = [nd.idx for nd in nodes if not nd.fixed]
    pos = {idx: k for k, idx in enumerate(free)}   # node idx -> reduced row
    nfree = len(free)

    # initial heads: fixed at BC, free at mean fixed head
    fixed_heads = [nd.head for nd in nodes if nd.fixed]
    h0 = sum(fixed_heads) / len(fixed_heads)
    H = [nd.head if nd.fixed else h0 for nd in nodes]

    import numpy as np

    # Newton–Raphson on the nodal mass-balance residual e(H) = inflow − outflow − demand.
    # Each link flow Q solves r·Q·|Q| − H_pump(|Q|) = ΔH (:func:`_link_flow`); the Jacobian is the
    # (negated) weighted graph Laplacian with edge weight p = dQ/dH — the direct hydraulic analogue
    # of the MNA conductance stamp (circuit.py). Friction factor (hence r) is refreshed from the
    # current flows each iteration, and a backtracking line search on ‖e‖∞ makes the square-law
    # solve globally convergent from a cold (zero-flow) start.
    def _residual(Hv) -> np.ndarray:
        e = np.zeros(nfree)
        for lk in links:
            q, _ = _link_flow(lk, Hv[lk.a] - Hv[lk.b])
            lk.flow = q
            if lk.a in pos:
                e[pos[lk.a]] -= q
            if lk.b in pos:
                e[pos[lk.b]] += q
        for nd in nodes:
            if nd.idx in pos:
                e[pos[nd.idx]] -= nd.demand
        return e

    converged = False
    it = 0
    if nfree == 0:
        converged = True
    for it in range(1, max_iter + 1) if nfree else []:
        for lk in links:
            lk.fd = _friction_factor(lk, max(abs(lk.flow), _MIN_ABS_Q), mu, rho)

        J = np.zeros((nfree, nfree))
        e = np.zeros(nfree)
        for lk in links:
            q, p = _link_flow(lk, H[lk.a] - H[lk.b])
            lk.flow = q
            a, b = lk.a, lk.b
            # residual: node a loses q (outflow), node b gains q (inflow)
            if a in pos:
                e[pos[a]] -= q
                J[pos[a], pos[a]] -= p
                if b in pos:
                    J[pos[a], pos[b]] += p
            if b in pos:
                e[pos[b]] += q
                J[pos[b], pos[b]] -= p
                if a in pos:
                    J[pos[b], pos[a]] += p
        for nd in nodes:
            if nd.idx in pos:
                e[pos[nd.idx]] -= nd.demand   # demand leaves the system

        enorm = float(np.max(np.abs(e)))
        if enorm < tol_head:
            converged = True
            break
        try:
            dH = np.linalg.solve(J, -e)
        except np.linalg.LinAlgError:
            return _err("singular hydraulic system (disconnected network?)",
                        hint="check that every node connects to a fixed-head node",
                        iterations=it)

        # backtracking line search: accept the largest fraction of the Newton step that reduces
        # the mass-imbalance residual (guards the square-law cold start against overshoot).
        base = list(H)
        factor = 1.0
        for _ls in range(30):
            for idx in free:
                H[idx] = base[idx] + factor * float(dH[pos[idx]])
            if float(np.max(np.abs(_residual(H)))) < enorm or factor < 1e-6:
                break
            factor *= 0.5
        # convergence is decided solely by the mass-balance residual at the top of the loop —
        # never by a small step (a stalled line search must not masquerade as converged).

    # final flows + per-link diagnostics from converged heads
    max_imbalance = _finalize_flows(nodes, links, H, mu, rho)

    return _assemble_result(ctx, nodes, links, H, fl, converged, it, max_imbalance)


def _finalize_flows(nodes, links, H, mu, rho) -> float:
    # Iterate the flow<->friction fixed point so link flows are consistent with their Reynolds-
    # dependent friction factor at the converged heads (also solves the all-fixed-node case,
    # where the Newton loop over free heads does not run).
    for _ in range(60):
        dmax = 0.0
        for lk in links:
            lk.fd = _friction_factor(lk, max(abs(lk.flow), _MIN_ABS_Q), mu, rho)
            q, _ = _link_flow(lk, H[lk.a] - H[lk.b])
            dmax = max(dmax, abs(q - lk.flow))
            lk.flow = q
        if dmax < 1e-10:
            break
    # node mass-balance residual (m³/s)
    imbalance = {nd.idx: (-nd.demand if not nd.fixed else 0.0) for nd in nodes}
    for lk in links:
        imbalance[lk.b] = imbalance.get(lk.b, 0.0) + lk.flow
        imbalance[lk.a] = imbalance.get(lk.a, 0.0) - lk.flow
    free_imb = [abs(v) for nd, v in imbalance.items()
                if not nodes[nd].fixed]
    return max(free_imb) if free_imb else 0.0


def _assemble_result(ctx, nodes, links, H, fl, converged, it, max_imbalance) -> dict:
    rho = fl["rho"]
    node_out = []
    for nd in nodes:
        z = nd.xyz[2] / 1000.0
        head = H[nd.idx]
        node_out.append({
            "id": nd.id, "xyz": nd.xyz, "fixed": nd.fixed,
            "head_m": round(head, 4),
            "pressure_kpa": round(rho * G * (head - z) / 1000.0, 3),
            "demand_m3h": round(nd.demand * 3600.0, 4),
        })
    link_out = []
    for lk in links:
        q = lk.flow
        v = q / lk.area if lk.area else 0.0
        r = _resistance(lk)
        headloss = r * q * abs(q)
        from fluids import Reynolds
        Re = Reynolds(V=abs(v), D=lk.d_m, rho=rho, mu=fl["mu"]) if lk.area else 0.0
        link_out.append({
            "id": lk.id, "from": nodes[lk.a].id, "to": nodes[lk.b].id,
            "length_mm": round(lk.length_m * 1000.0, 2),
            "id_mm": round(lk.d_m * 1000.0, 3),
            "flow_m3h": round(q * 3600.0, 4),
            "velocity_ms": round(v, 4),
            "reynolds": round(float(Re), 1),
            "friction_factor": round(lk.fd, 5),
            "headloss_m": round(headloss, 4),
            "dp_kpa": round(rho * G * headloss / 1000.0, 3),
            "k_total": round(lk.kfit, 4),
            "pump_head_m": round(lk.pump["sign"] * _pump_head(lk.pump, abs(q)), 3) if lk.pump else 0.0,
        })
    warnings = ctx["warnings"]
    hint = None
    next_actions = []
    if not converged:
        hint = f"solver did not converge in {it} iterations (max node imbalance " \
               f"{max_imbalance * 3600.0:.3g} m³/h) — check connectivity / boundary conditions"
        next_actions.append("verify every run endpoint meets another run or a boundary node")
    else:
        next_actions.append("call piping_report for the per-segment K-factor pressure-drop table")
    return {
        "ok": True,
        "converged": converged,
        "iterations": it,
        "max_imbalance_m3h": round(max_imbalance * 3600.0, 6),
        "fluid": {"name": fl.get("name", "custom"), "rho": rho, "mu": fl["mu"]},
        "nodes": node_out,
        "links": link_out,
        "warnings": warnings,
        "hint": hint,
        "next_actions": next_actions,
    }


# ── per-segment pressure-drop report (K-factor breakdown) ─────────────────────

def pressure_drop_report(doc: dict, result: Optional[dict] = None, *,
                         boundary=None, fluid: Optional[dict] = None,
                         pumps: Optional[list] = None, **solve_kw) -> dict:
    """Per-segment pressure-drop report with an explicit K-factor breakdown.

    If ``result`` (a prior :func:`solve` output) is not given, a solve is run first (``boundary``
    then required). Each segment lists friction dP, per-fitting K + dP, and the total. Returns a
    contract-shaped dict.
    """
    if result is None:
        if boundary is None:
            return _err("pressure_drop_report needs a solved 'result' or a 'boundary' to solve",
                        hint="pass boundary=[{'node':'n0','pressure':<kPa>}, ...]")
        result = solve(doc, boundary=boundary, fluid=fluid, pumps=pumps, **solve_kw)
    if not result.get("ok"):
        return result

    if not have_fluids():
        return _err("fluids library not installed", hint="pip install fluids (MIT)")

    fl = dict(WATER); fl.update(fluid or {})
    rho, mu = fl["rho"], fl["mu"]
    tol = solve_kw.get("tol_mm", _SNAP_TOL_MM)
    ctx = _build_network(doc, fl, tol)
    if not ctx.get("nodes"):
        return ctx
    link_res = {lk["id"]: lk for lk in result["links"]}

    segments = []
    total_dp = 0.0
    for lk in ctx["links"]:
        lr = link_res.get(lk.id)
        if lr is None:
            continue
        q = lr["flow_m3h"] / 3600.0
        v = q / lk.area if lk.area else 0.0
        lk.fd = _friction_factor(lk, max(abs(q), _MIN_ABS_Q), mu, rho)
        # friction component
        from fluids import K_from_f, dP_from_K
        k_fric = K_from_f(fd=lk.fd, L=lk.length_m, D=lk.d_m) if lk.d_m else 0.0
        dp_fric = dP_from_K(K=k_fric, rho=rho, V=abs(v))
        breakdown = [{"item": "pipe friction", "K": round(float(k_fric), 4),
                      "dp_kpa": round(dp_fric / 1000.0, 4),
                      "method": f"Colebrook fd={lk.fd:.5f}, L/D={lk.length_m / lk.d_m:.1f}"}]
        seg_dp = dp_fric
        # recompute per-fitting K at the segment's fd, then its dP
        for fb in lk.kbreak:
            dp_k = dP_from_K(K=fb["K"], rho=rho, V=abs(v))
            breakdown.append({"item": fb["type"], "fitting": fb["fitting"],
                              "K": fb["K"], "dp_kpa": round(dp_k / 1000.0, 4),
                              "method": fb["method"]})
            seg_dp += dp_k
        total_dp += seg_dp
        segments.append({
            "id": lk.id, "from": lr["from"], "to": lr["to"],
            "length_mm": lr["length_mm"], "id_mm": lr["id_mm"],
            "flow_m3h": lr["flow_m3h"], "velocity_ms": lr["velocity_ms"],
            "k_friction": round(float(k_fric), 4),
            "k_fittings": round(lk.kfit, 4),
            "dp_kpa": round(seg_dp / 1000.0, 4),
            "breakdown": breakdown,
        })

    return {
        "ok": True,
        "fluid": {"name": fl.get("name", "custom"), "rho": rho, "mu": mu},
        "segments": segments,
        "total_dp_kpa": round(total_dp / 1000.0, 4),
        "converged": result.get("converged"),
        "warnings": ctx["warnings"],
        "hint": None if result.get("converged")
        else "underlying solve did not converge — pressures/flows are approximate",
        "next_actions": ["deliver the pressure-drop table or size a pump for total_dp_kpa"],
    }
