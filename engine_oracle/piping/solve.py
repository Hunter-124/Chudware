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

from engine.piping import catalog, properties, schema

G = 9.80665  # m/s²
R_UNIVERSAL = properties.R_UNIVERSAL  # J/(mol·K)
P_ATM = properties.P_ATM              # Pa

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
          pumps: Optional[list] = None, network_type: str = "liquid",
          tol_mm: float = _SNAP_TOL_MM,
          max_iter: int = 100, tol_head: float = 1e-6) -> dict:
    """Solve an ``aicad.piping/1`` network for node heads/pressures + link flows.

    ``boundary`` is a list of node BCs (``{'node'|'at'|'run', ...}`` + one of ``head`` (m),
    ``pressure`` (kPa gauge), ``demand`` (m³/h) or ``demand_lps``); at least one fixed
    head/pressure node is required. ``fluid`` selects the working fluid — ``None`` (water
    @20 °C), an explicit ``{'rho','mu'}`` override, or a sourced lookup
    ``{'name':'water'|'air'|'methane'|'natural_gas'|'nitrogen'|'carbon_dioxide', 'T':K, 'P':Pa}``
    resolved through :mod:`engine.piping.properties`. ``pumps`` is an optional list of
    ``{'run', 'curve', 'speed_ratio', 'direction'}``.

    ``network_type`` selects the physics: ``"liquid"`` (incompressible head solve, the default)
    or ``"gas"`` (isothermal-compressible mass-balance solve on absolute-pressure² — see
    :func:`solve_gas`; requires a named gas ``fluid`` so molar mass / temperature are known).

    Returns a contract-shaped result: on success ``{"ok": True, "nodes": [...], "links": [...],
    "converged": bool, "iterations": int, ...}``; on failure ``{"ok": False, "reason", "hint"}``.
    Never raises for a modelling/data problem.
    """
    if str(network_type).lower() in ("gas", "compressible"):
        return solve_gas(doc, boundary=boundary, fluid=fluid, tol_mm=tol_mm,
                         max_iter=max_iter)
    if schema.validate(doc):
        return _err("piping document failed schema validation",
                    hint="run engine.piping.schema.validate(doc) and fix the errors",
                    errors=schema.validate(doc))
    if not have_fluids():
        return _err("fluids library not installed",
                    hint="pip install fluids (MIT) to enable the hydraulic solve")

    fl = properties.resolve_solver_fluid(fluid)
    if not fl.get("ok", True) and "reason" in fl:
        return fl
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
        "fluid": {"name": fl.get("name", "custom"), "rho": rho, "mu": fl["mu"],
                  "source": fl.get("source"), "phase": fl.get("phase", "liquid")},
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


# ── compressible (gas) network solve ──────────────────────────────────────────
#
# Isothermal-compressible analogue of the incompressible head solve. The natural nodal
# potential for steady isothermal gas flow is **absolute pressure squared** φ = P² (Pa²):
# integrating the isothermal momentum + Darcy friction equation along a pipe gives, per link,
#
#     ṁ² · (f·L/D + ΣK) = (A² · M / (R·T·Z)) · (P_a² − P_b²)                        (Weymouth form)
#
# so mass flow ṁ ∝ √(Δφ) exactly as the incompressible link flow Q ∝ √(ΔH). Conserving **mass**
# (not volume — volume is not conserved when density changes with pressure) at every node yields a
# Newton solve on φ that is the direct compressible sibling of the head solve above, with the same
# graph-Laplacian Jacobian, Reynolds-refreshed friction and backtracking line search. The small
# ``2·ln(P_a/P_b)`` momentum term is neglected (Weymouth assumption; it is < a few % of f·L/D for
# the L/D ratios of real pipe runs) and the gas is treated as ideal (Z = 1) — both documented.

_PHI_JAC_FLOOR_FRAC = 1e-9   # |Δφ| floor (fraction of the potential scale) in the Jacobian only.


def _gas_friction_factor(lk: _Link, mdot_abs: float, mu: float) -> float:
    """Darcy friction factor from the mass-flux Reynolds number Re = ṁ·D/(A·μ)."""
    from fluids import friction_factor
    if lk.area <= 0:
        return 0.02
    Re = mdot_abs * lk.d_m / (lk.area * mu)
    if Re < 2100:
        return max(64.0 / max(Re, 1e-6), 1e-4)
    return float(friction_factor(Re=max(Re, 1.0), eD=lk.eps / lk.d_m))


def _gas_link_flow(lk: _Link, dphi: float, coef_geom: float,
                   phi_floor: float) -> tuple[float, float]:
    """Mass flow ṁ (kg/s, a→b positive) and conductance dṁ/dφ for a potential drop Δφ = φ_a−φ_b.

    ``coef_geom`` = A²·M/(R·T·Z) for the link; the resistance is ``f·L/D + ΣK``.
    """
    ktot = lk.fd * lk.length_m / lk.d_m + lk.kfit
    if ktot <= 0 or coef_geom <= 0:
        return 0.0, 0.0
    coef = coef_geom / ktot                       # ṁ² = coef·|Δφ|
    mdot = math.copysign(math.sqrt(coef * abs(dphi)), dphi)
    p = 0.5 * math.sqrt(coef / max(abs(dphi), phi_floor))
    return mdot, p


def _apply_gas_boundary(ctx, boundary, tol, rho_std) -> Optional[dict]:
    """Fixed absolute-pressure² nodes + mass demands for the gas solve. Reuses ``_Node.head`` to
    store φ = P_abs² (Pa²) and ``_Node.demand`` to store the mass demand (kg/s leaving)."""
    nodes = ctx["nodes"]
    links_by_id = {lk.id: lk for lk in ctx["links"]}
    bcs = boundary if isinstance(boundary, list) else boundary.get("nodes", [])
    n_fixed = 0
    for bc in bcs:
        idx = _resolve_node(bc, nodes, links_by_id, tol)
        if idx is None:
            return _err(f"boundary reference {bc!r} did not resolve to a node",
                        hint="use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}")
        nd = nodes[idx]
        if "pressure_abs" in bc:      # kPa absolute
            p_abs = float(bc["pressure_abs"]) * 1000.0
            nd.fixed, nd.head, n_fixed = True, p_abs * p_abs, n_fixed + 1
        elif "pressure" in bc:        # kPa gauge -> absolute
            p_abs = float(bc["pressure"]) * 1000.0 + P_ATM
            nd.fixed, nd.head, n_fixed = True, p_abs * p_abs, n_fixed + 1
        elif "mass_demand" in bc:     # kg/h leaving (+) / entering (−)
            nd.demand += float(bc["mass_demand"]) / 3600.0
        elif "std_demand" in bc or "demand" in bc:   # Nm³/h (standard volume) leaving (+)
            q_std = float(bc.get("std_demand", bc.get("demand")))
            nd.demand += q_std * rho_std / 3600.0
    if n_fixed == 0:
        return _err("gas network under-constrained: no fixed-pressure node",
                    hint="give at least one boundary with 'pressure' (kPa gauge) or 'pressure_abs'")
    return None


def solve_gas(doc: dict, *, boundary, fluid: Optional[dict] = None,
              tol_mm: float = _SNAP_TOL_MM, max_iter: int = 100,
              tol_mass: float = 1e-9) -> dict:
    """Isothermal-compressible pipe-network solve (see the module note above).

    ``fluid`` must resolve to a **named gas** (``{'name':'natural_gas'|'methane'|'air'|
    'nitrogen'|'carbon_dioxide', 'T':K, 'P':Pa}``) so molar mass + temperature are known.
    ``boundary`` entries fix pressure (``pressure`` kPa gauge / ``pressure_abs`` kPa) or set a
    demand (``mass_demand`` kg/h, or ``std_demand``/``demand`` in Nm³/h at 15 °C, 1 atm). At
    least one fixed-pressure node is required. Returns node absolute/gauge pressures and per-link
    mass flow, standard volumetric flow, mean velocity, Reynolds and pressure drop — contract-
    shaped on any modelling failure.
    """
    if schema.validate(doc):
        return _err("piping document failed schema validation",
                    hint="run engine.piping.schema.validate(doc) and fix the errors",
                    errors=schema.validate(doc))
    if not have_fluids():
        return _err("fluids library not installed",
                    hint="pip install fluids (MIT) to enable the hydraulic solve")

    fl = properties.resolve_solver_fluid(fluid)
    if not fl.get("ok", True) and "reason" in fl:
        return fl
    if fl.get("phase") != "gas" or not fl.get("molar_mass"):
        return _err("gas network solve needs a named compressible fluid with a molar mass",
                    hint="pass fluid={'name':'natural_gas','T':288.15,'P':<line Pa>} "
                         "(also air/methane/nitrogen/carbon_dioxide)")
    M = fl["molar_mass"]
    T = fl.get("T_K") or properties.STD_T
    mu = fl["mu"]
    rho_std = properties.STD_P * M / (R_UNIVERSAL * properties.STD_T)

    ctx = _build_network(doc, fl, tol_mm)
    if not ctx.get("nodes"):
        return ctx
    err = _apply_gas_boundary(ctx, boundary, tol_mm, rho_std)
    if err:
        return err

    nodes, links = ctx["nodes"], ctx["links"]
    for lk in links:
        lk.flow = 1e-3  # kg/s seed (reuse .flow slot for mass flow in gas mode)

    free = [nd.idx for nd in nodes if not nd.fixed]
    pos = {idx: k for k, idx in enumerate(free)}
    nfree = len(free)

    fixed_phi = [nd.head for nd in nodes if nd.fixed]
    phi0 = sum(fixed_phi) / len(fixed_phi)
    phi = [nd.head if nd.fixed else phi0 for nd in nodes]
    phi_floor = max(_PHI_JAC_FLOOR_FRAC * phi0, 1.0)

    coef = {lk.id: lk.area ** 2 * M / (R_UNIVERSAL * T) for lk in links}

    import numpy as np

    def _residual(phiv) -> np.ndarray:
        e = np.zeros(nfree)
        for lk in links:
            m, _ = _gas_link_flow(lk, phiv[lk.a] - phiv[lk.b], coef[lk.id], phi_floor)
            lk.flow = m
            if lk.a in pos:
                e[pos[lk.a]] -= m
            if lk.b in pos:
                e[pos[lk.b]] += m
        for nd in nodes:
            if nd.idx in pos:
                e[pos[nd.idx]] -= nd.demand
        return e

    converged = nfree == 0
    it = 0
    for it in range(1, max_iter + 1) if nfree else []:
        for lk in links:
            lk.fd = _gas_friction_factor(lk, max(abs(lk.flow), 1e-9), mu)

        J = np.zeros((nfree, nfree))
        e = np.zeros(nfree)
        for lk in links:
            m, p = _gas_link_flow(lk, phi[lk.a] - phi[lk.b], coef[lk.id], phi_floor)
            lk.flow = m
            a, b = lk.a, lk.b
            if a in pos:
                e[pos[a]] -= m
                J[pos[a], pos[a]] -= p
                if b in pos:
                    J[pos[a], pos[b]] += p
            if b in pos:
                e[pos[b]] += m
                J[pos[b], pos[b]] -= p
                if a in pos:
                    J[pos[b], pos[a]] += p
        for nd in nodes:
            if nd.idx in pos:
                e[pos[nd.idx]] -= nd.demand

        enorm = float(np.max(np.abs(e)))
        if enorm < tol_mass:
            converged = True
            break
        try:
            dphi = np.linalg.solve(J, -e)
        except np.linalg.LinAlgError:
            return _err("singular gas system (disconnected network?)",
                        hint="check that every node connects to a fixed-pressure node",
                        iterations=it)

        base = list(phi)
        factor = 1.0
        for _ls in range(40):
            for idx in free:
                cand = base[idx] + factor * float(dphi[pos[idx]])
                phi[idx] = max(cand, 1.0)   # keep P² > 0 (physical)
            if float(np.max(np.abs(_residual(phi)))) < enorm or factor < 1e-6:
                break
            factor *= 0.5

    # settle mass flows against Reynolds-dependent friction at the converged potentials
    for _ in range(60):
        dmax = 0.0
        for lk in links:
            lk.fd = _gas_friction_factor(lk, max(abs(lk.flow), 1e-9), mu)
            m, _ = _gas_link_flow(lk, phi[lk.a] - phi[lk.b], coef[lk.id], phi_floor)
            dmax = max(dmax, abs(m - lk.flow))
            lk.flow = m
        if dmax < 1e-12:
            break

    imbalance = {nd.idx: (-nd.demand if not nd.fixed else 0.0) for nd in nodes}
    for lk in links:
        imbalance[lk.b] = imbalance.get(lk.b, 0.0) + lk.flow
        imbalance[lk.a] = imbalance.get(lk.a, 0.0) - lk.flow
    free_imb = [abs(v) for nd, v in imbalance.items() if not nodes[nd].fixed]
    max_imbalance = max(free_imb) if free_imb else 0.0

    return _assemble_gas_result(ctx, nodes, links, phi, fl, M, T, mu, rho_std,
                                converged, it, max_imbalance)


def _assemble_gas_result(ctx, nodes, links, phi, fl, M, T, mu, rho_std,
                         converged, it, max_imbalance) -> dict:
    node_out = []
    for nd in nodes:
        p_abs = math.sqrt(max(phi[nd.idx], 0.0))
        node_out.append({
            "id": nd.id, "xyz": nd.xyz, "fixed": nd.fixed,
            "pressure_abs_kpa": round(p_abs / 1000.0, 3),
            "pressure_kpa": round((p_abs - P_ATM) / 1000.0, 3),
            "mass_demand_kgh": round(nd.demand * 3600.0, 5),
        })
    link_out = []
    for lk in links:
        m = lk.flow
        p_a = math.sqrt(max(phi[lk.a], 0.0))
        p_b = math.sqrt(max(phi[lk.b], 0.0))
        p_mean = 0.5 * (p_a + p_b)
        rho_mean = p_mean * M / (R_UNIVERSAL * T)
        v = m / (lk.area * rho_mean) if lk.area and rho_mean else 0.0
        Re = abs(m) * lk.d_m / (lk.area * mu) if lk.area else 0.0
        link_out.append({
            "id": lk.id, "from": nodes[lk.a].id, "to": nodes[lk.b].id,
            "length_mm": round(lk.length_m * 1000.0, 2),
            "id_mm": round(lk.d_m * 1000.0, 3),
            "mass_flow_kgh": round(m * 3600.0, 5),
            "std_flow_nm3h": round(m / rho_std * 3600.0, 4) if rho_std else 0.0,
            "velocity_ms": round(v, 4),
            "reynolds": round(float(Re), 1),
            "friction_factor": round(lk.fd, 5),
            "dp_kpa": round((p_a - p_b) / 1000.0, 4),
            "k_total": round(lk.kfit, 4),
        })
    next_actions = ["deliver node pressures + mass/standard flows"] if converged else \
        ["verify every run endpoint meets another run or a fixed-pressure boundary node"]
    return {
        "ok": True,
        "network_type": "gas",
        "converged": converged,
        "iterations": it,
        "max_imbalance_kgh": round(max_imbalance * 3600.0, 8),
        "fluid": {"name": fl.get("name", "custom"), "phase": "gas",
                  "molar_mass": M, "T_K": round(T, 3), "mu": mu,
                  "rho_std_kgm3": round(rho_std, 5), "source": fl.get("source")},
        "assumptions": ["isothermal", "ideal gas (Z=1)",
                        "Weymouth form (2·ln(P1/P2) momentum term neglected)"],
        "nodes": node_out,
        "links": link_out,
        "warnings": ctx["warnings"],
        "hint": None if converged else
        f"gas solve did not converge in {it} iterations "
        f"(max node imbalance {max_imbalance * 3600.0:.3g} kg/h)",
        "next_actions": next_actions,
    }


# ── pump/system operating point + NPSH margin ─────────────────────────────────

def _system_head(system: dict, q_m3h: float) -> float:
    """System-curve head (m) at flow ``q_m3h``: ``static_head_m + k · Q²`` (Q in m³/h)."""
    return float(system.get("static_head_m", 0.0)) + float(system.get("k", 0.0)) * q_m3h * q_m3h


def _interp_curve(pts: list, x: float) -> Optional[float]:
    """Piecewise-linear interpolation of ``[[x,y],...]`` (sorted by x); ``None`` outside range."""
    if not pts:
        return None
    if x < pts[0][0] or x > pts[-1][0]:
        return None
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1 and x1 > x0:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return pts[-1][1]


def operating_point(pump_curve, *, system: Optional[dict] = None,
                    doc: Optional[dict] = None, boundary=None, fluid: Optional[dict] = None,
                    q_ref_m3h: Optional[float] = None,
                    npshr_curve=None, suction: Optional[dict] = None,
                    speed_ratio: float = 1.0) -> dict:
    """Pump operating (duty) point + NPSH margin.

    Intersects a **pump curve** H_pump(Q) with a **system curve** H_sys(Q) = static +
    k·Q² to find the duty point (Q*, H*), then evaluates the NPSH margin there.

    ``pump_curve`` — CSV text/path or a :func:`load_pump_curve` dict (flow m³/h, head m).
    ``system`` — ``{"static_head_m": h0, "k": k}`` (Q in m³/h). If omitted, it is **derived**
    from ``doc`` + ``boundary`` (+ ``fluid``): a solve at ``q_ref_m3h`` gives the network head
    loss, so ``k = Δp_head / Q_ref²`` and the static lift from the fixed-node elevations.
    ``suction`` — for NPSHa: ``{"pressure_kpa": gauge, "elevation_m": z (+ above pump),
    "friction_m": suction-line loss, "atm_kpa": 101.325, "pvap_kpa": <override>}``. Vapour
    pressure is otherwise looked up from ``fluid`` via :mod:`engine.piping.properties` (sourced).
    ``npshr_curve`` — CSV text/path with ``flow,npshr`` (m) columns; NPSHr is interpolated at Q*.

    Returns ``{"ok": True, "duty_point": {...}, "npsh": {...}, ...}`` or contract-shaped failure.
    """
    pump = pump_curve if isinstance(pump_curve, dict) and pump_curve.get("ok") else \
        load_pump_curve(pump_curve, speed_ratio=speed_ratio)
    if not pump.get("ok"):
        return pump

    derived = None
    if system is None:
        if doc is None or boundary is None:
            return _err("operating_point needs a 'system' curve or a doc+boundary to derive one",
                        hint="pass system={'static_head_m':h0,'k':k} or doc=<piping doc>, boundary=[...]")
        system, derived = _derive_system_curve(doc, boundary, fluid, q_ref_m3h)
        if isinstance(derived, dict) and derived.get("ok") is False:
            return derived

    # Intersect: g(Q) = H_pump(Q) − H_sys(Q). Bisect over the pump's flow range for the root.
    pts = pump["points"]
    q_lo, q_hi = pts[0][0], pts[-1][0]

    def _g(q):
        return _pump_head(pump, q / 3600.0) - _system_head(system, q)

    g_lo, g_hi = _g(q_lo), _g(q_hi)
    if g_lo < 0:
        return _err("no operating point: system head exceeds pump shutoff head at zero flow",
                    hint="the pump cannot meet the static lift — raise pump speed or lower static head",
                    shutoff_head_m=pump["shutoff_head"], static_head_m=system.get("static_head_m"))
    if g_hi > 0:
        return _err("no operating point within the pump curve: pump head still exceeds system "
                    "head at max curve flow",
                    hint="extend the pump curve to higher flow, or the system is over-pumped",
                    max_curve_flow_m3h=q_hi)
    for _ in range(200):
        q_mid = 0.5 * (q_lo + q_hi)
        if _g(q_mid) > 0:
            q_lo = q_mid
        else:
            q_hi = q_mid
        if q_hi - q_lo < 1e-9:
            break
    q_star = 0.5 * (q_lo + q_hi)
    h_star = _system_head(system, q_star)

    result: dict[str, Any] = {
        "ok": True,
        "duty_point": {"flow_m3h": round(q_star, 4), "head_m": round(h_star, 4),
                       "flow_m3s": round(q_star / 3600.0, 8)},
        "pump": {"shutoff_head_m": pump["shutoff_head"], "max_flow_m3h": pump["max_flow"],
                 "speed_ratio": pump.get("speed_ratio", speed_ratio)},
        "system_curve": {"static_head_m": system.get("static_head_m", 0.0), "k": system.get("k", 0.0)},
        "warnings": [], "next_actions": ["confirm the duty point sits near the pump BEP"],
    }
    if derived:
        result["system_curve"]["derived_from"] = derived

    npsh = _npsh_margin(q_star, suction, fluid, npshr_curve)
    result["npsh"] = npsh
    if npsh.get("margin_m") is not None and npsh["margin_m"] < 0:
        result["warnings"].append("NPSH margin negative — cavitation risk at the duty point")
        result["next_actions"].insert(0, "raise suction pressure / lower NPSHr — cavitation risk")
    return result


def _derive_system_curve(doc, boundary, fluid, q_ref_m3h):
    """Fit ``static + k·Q²`` from a network solve at a reference demand.

    Static lift = fixed-node head difference (elevation + fixed pressure); ``k`` from the total
    dynamic loss at ``q_ref`` divided by Q_ref². Returns ``(system_dict, derived_meta)``.
    """
    q_ref = float(q_ref_m3h) if q_ref_m3h else 50.0
    demand_bc, fixed_bc = None, None
    bcs = boundary if isinstance(boundary, list) else boundary.get("nodes", [])
    probe = []
    for bc in bcs:
        b = dict(bc)
        if any(k in bc for k in ("demand", "demand_lps")):
            b = {k: bc[k] for k in bc if k not in ("demand", "demand_lps")}
            b["demand"] = q_ref
            demand_bc = b
        elif any(k in bc for k in ("pressure", "head")):
            fixed_bc = bc
        probe.append(b)
    if demand_bc is None or fixed_bc is None:
        return None, _err("cannot derive a system curve",
                          hint="boundary needs one fixed pressure/head node and one demand node "
                               "(the operating flow), or pass system={'static_head_m','k'}")
    res = solve(doc, boundary=probe, fluid=fluid)
    if not res.get("ok"):
        return None, res
    # total dynamic loss (m) = sum of link headloss at q_ref; static = fixed head above the
    # demand node's elevation datum (z of the demand node).
    total_loss_m = sum(l["headloss_m"] for l in res["links"])
    k = total_loss_m / (q_ref * q_ref) if q_ref else 0.0
    fixed_head = next((n["head_m"] for n in res["nodes"] if n["fixed"]), 0.0)
    demand_node = next((n for n in res["nodes"]
                        if abs(n["demand_m3h"] - q_ref) < 1e-3), None)
    z_demand = (demand_node["xyz"][2] / 1000.0) if demand_node else 0.0
    static = fixed_head - z_demand
    meta = {"q_ref_m3h": q_ref, "total_loss_m": round(total_loss_m, 4),
            "static_head_m": round(static, 4), "k": k}
    return {"static_head_m": static, "k": k}, meta


def _npsh_margin(q_m3h: float, suction: Optional[dict], fluid: Optional[dict],
                 npshr_curve) -> dict:
    """NPSH available / required / margin (m) at flow ``q_m3h``.

    NPSHa = (P_atm + P_suction_gauge − P_vapour)/(ρ·g) + z_suction − h_friction.
    """
    if suction is None:
        return {"npsha_m": None, "npshr_m": None, "margin_m": None,
                "hint": "pass suction={'pressure_kpa','elevation_m','friction_m'} for NPSHa"}
    fl = properties.resolve_solver_fluid(fluid)
    if not fl.get("ok", True) and "reason" in fl:
        return {"npsha_m": None, "npshr_m": None, "margin_m": None, "reason": fl["reason"]}
    rho = fl["rho"]
    p_atm = float(suction.get("atm_kpa", P_ATM / 1000.0)) * 1000.0
    p_suct_g = float(suction.get("pressure_kpa", 0.0)) * 1000.0
    if "pvap_kpa" in suction:
        p_vap = float(suction["pvap_kpa"]) * 1000.0
        pvap_src = "caller-supplied"
    else:
        p_vap = fl.get("pvap")
        pvap_src = fl.get("source")
        if p_vap is None:
            return {"npsha_m": None, "npshr_m": None, "margin_m": None,
                    "hint": "no vapour pressure for this fluid — pass suction={'pvap_kpa':...}"}
    z = float(suction.get("elevation_m", 0.0))
    h_fric = float(suction.get("friction_m", 0.0))
    npsha = (p_atm + p_suct_g - p_vap) / (rho * G) + z - h_fric

    npshr = None
    if npshr_curve is not None:
        curve = load_pump_curve(npshr_curve, flow_col="flow", head_col="npshr") \
            if not isinstance(npshr_curve, dict) else npshr_curve
        if curve.get("ok"):
            npshr = _interp_curve(curve["points"], q_m3h)
    margin = (npsha - npshr) if (npshr is not None) else None
    out = {"npsha_m": round(npsha, 4),
           "npshr_m": round(npshr, 4) if npshr is not None else None,
           "margin_m": round(margin, 4) if margin is not None else None,
           "rho_kgm3": rho, "pvap_pa": round(p_vap, 3), "pvap_source": pvap_src}
    if npshr is None:
        out["hint"] = "pass npshr_curve (flow,npshr CSV) to get the NPSH margin"
    return out
