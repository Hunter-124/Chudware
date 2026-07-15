"""Native Modified-Nodal-Analysis circuit simulator (GPL-clean, numpy/scipy).

Public entry point: :func:`simulate(circuit, study) -> dict` returning the sim.md §4 results
shape. The internal ``Circuit`` dict (sim.md §1) is the integration boundary with the SPICE
parser and must be consumed byte-for-byte as specified.

Pipeline:
  1. Build a node-name -> matrix-row index map (ground "0" -> -1, removed row).
  2. Instantiate devices via the ``devices.py`` registry; pre-allocate MNA branch rows
     (V, L, E, H, OPAMP ...).
  3. For each analysis:
       op   — Newton DC solve with gmin- then source-stepping fallback.
       dc   — sweep an independent source value, re-solving (continuation from prior point).
       tran — trapezoidal integration, backward-Euler bootstrap on the first step and after
              every source breakpoint; nonlinear Newton inside each step.
       ac   — complex MNA (G + jωC) per frequency about the DC operating point.
  4. Evaluate probes (V(node), V(a,b) differential, I(Vname) branch current) per sim.md §4.

Linear algebra: scipy.sparse + splu, with a dense numpy fallback on singular/small systems.
Non-convergence / singular matrices set ``convergence.*: false`` with a ``log`` diagnostic — never
silent NaNs.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np
import scipy.sparse
import scipy.sparse.linalg

from engine.sim import analysis_ext
from engine.sim import devices as dev
from engine.sim.devices import DeviceError, make_device, source_breakpoints


# =============================================================================
# MNA accumulator
# =============================================================================

class MnaSystem:
    """Accumulates the MNA system A x = z for one (linearized) solve.

    Dimension = (#non-ground nodes) + (#branch rows). Stamps are buffered into COO triplets and
    assembled lazily. Supports a real path (DC/transient) and a complex path (AC) sharing the same
    interface; ``complex_mode`` selects which matrix the C-stamps and Gc-stamps land in.
    """

    def __init__(self, node_index: dict[str, int], complex_mode: bool = False) -> None:
        self.node_index = node_index           # node name -> row (ground -> -1)
        self.n_nodes = max(node_index.values()) + 1 if node_index else 0
        self.branch_names: list[str] = []
        self.branch_lookup: dict[str, int] = {}
        self.complex_mode = complex_mode
        self.src_scales: dict[str, float] = {}
        self.dim = self.n_nodes  # grows as branches are allocated
        self._reset_stamps()

    def _reset_stamps(self) -> None:
        self._g_rows: list[int] = []
        self._g_cols: list[int] = []
        self._g_vals: list[complex] = []
        self._c_rows: list[int] = []
        self._c_cols: list[int] = []
        self._c_vals: list[float] = []
        dtype = complex if self.complex_mode else float
        self._z = np.zeros(self.dim if self.dim else self.n_nodes, dtype=dtype)

    # -- index management -------------------------------------------------------

    def n(self, name: str) -> int:
        return self.node_index.get(str(name), -1)

    def add_branch(self, name: str) -> int:
        idx = self.dim
        self.branch_names.append(name)
        self.branch_lookup[name] = idx
        self.dim += 1
        return idx

    def branch_index(self, name: str) -> Optional[int]:
        return self.branch_lookup.get(name)

    def src_scale(self, name: str) -> float:
        return self.src_scales.get(name, 1.0)

    def begin_solve(self) -> None:
        """Reset the accumulator for a fresh linearized solve (dimensions fixed)."""
        self._g_rows.clear(); self._g_cols.clear(); self._g_vals.clear()
        self._c_rows.clear(); self._c_cols.clear(); self._c_vals.clear()
        dtype = complex if self.complex_mode else float
        self._z = np.zeros(self.dim, dtype=dtype)

    # -- stamping primitives ----------------------------------------------------

    def stamp_G(self, i: int, j: int, val: float) -> None:
        if i < 0 or j < 0 or val == 0.0:
            return
        self._g_rows.append(i); self._g_cols.append(j); self._g_vals.append(val)

    def stamp_Gc(self, i: int, j: int, val: complex) -> None:
        """Stamp a complex conductance directly (AC only)."""
        if i < 0 or j < 0:
            return
        self._g_rows.append(i); self._g_cols.append(j); self._g_vals.append(val)

    def stamp_C(self, i: int, j: int, val: float) -> None:
        if i < 0 or j < 0 or val == 0.0:
            return
        self._c_rows.append(i); self._c_cols.append(j); self._c_vals.append(val)

    def stamp_I(self, i: int, val) -> None:
        if i < 0:
            return
        self._z[i] += val

    # -- assembly + solve -------------------------------------------------------

    def _matrix(self, omega: float = 0.0):
        dtype = complex if self.complex_mode else float
        n = self.dim
        if not self._g_rows and not self._c_rows:
            return scipy.sparse.csc_matrix((n, n), dtype=dtype)
        rows = list(self._g_rows); cols = list(self._g_cols); vals = list(self._g_vals)
        if self.complex_mode and self._c_rows:
            for r, c, v in zip(self._c_rows, self._c_cols, self._c_vals):
                rows.append(r); cols.append(c); vals.append(1j * omega * v)
        A = scipy.sparse.coo_matrix(
            (np.asarray(vals, dtype=dtype), (rows, cols)), shape=(n, n)
        ).tocsc()
        return A

    def solve(self, omega: float = 0.0) -> tuple[Optional[np.ndarray], str]:
        """Solve A x = z. Returns (x, status) where status is '' on success or a diagnostic."""
        n = self.dim
        if n == 0:
            return np.zeros(0), ""
        A = self._matrix(omega)
        z = self._z
        # Tiny systems / very dense: go dense directly (robust + fast).
        if n <= 8:
            try:
                x = np.linalg.solve(A.toarray(), z)
                if not np.all(np.isfinite(x)):
                    return None, "dense solve produced non-finite values (singular)"
                return x, ""
            except np.linalg.LinAlgError as e:
                return None, f"dense solve singular: {e}"
        # Sparse LU.
        try:
            lu = scipy.sparse.linalg.splu(A)
            x = lu.solve(z)
            if not np.all(np.isfinite(x)):
                raise RuntimeError("non-finite")
            return x, ""
        except (RuntimeError, ValueError, scipy.sparse.linalg.MatrixRankWarning):
            pass
        except Exception:  # noqa: BLE001  — splu raises a bare RuntimeError on singular
            pass
        # Dense fallback.
        try:
            x = np.linalg.solve(A.toarray(), z)
            if not np.all(np.isfinite(x)):
                return None, "matrix singular (dense fallback non-finite)"
            return x, ""
        except np.linalg.LinAlgError as e:
            # Least-squares last resort.
            try:
                x, *_ = np.linalg.lstsq(A.toarray(), z, rcond=None)
                if np.all(np.isfinite(x)):
                    return x, "matrix singular — used least-squares"
            except Exception:  # noqa: BLE001
                pass
            return None, f"matrix singular: {e}"


# =============================================================================
# Circuit container
# =============================================================================

class _Sim:
    """Holds the parsed device objects + node map for a Circuit dict."""

    def __init__(self, circuit: dict, options: dict) -> None:
        self.circuit = circuit
        self.options = options
        self.params = dict(circuit.get("params", {}))
        if "temp" in options:
            self.params["TEMP"] = options["temp"]
        self.models = dict(circuit.get("models", {}))

        # Build node index (ground -> -1).
        raw_nodes = list(circuit.get("nodes", []))
        # Ensure every device node appears.
        for d in circuit.get("devices", []):
            for nm in d.get("nodes", []):
                if nm not in raw_nodes:
                    raw_nodes.append(nm)
        if "0" not in raw_nodes:
            raw_nodes.insert(0, "0")
        self.node_index: dict[str, int] = {}
        # Case-insensitive aliases: SPICE / ngspice often emit lower-case
        # vectors (v(3v3)) while schematic nets keep human case (3V3).
        self._node_aliases: dict[str, str] = {}
        idx = 0
        for nm in raw_nodes:
            key = str(nm)
            if key in ("0", "gnd", "GND") or key.lower() in ("0", "gnd"):
                self.node_index[key] = -1
                self._node_aliases[key.lower()] = key
            else:
                self.node_index[key] = idx
                self._node_aliases[key.lower()] = key
                idx += 1
        # Map common ground aliases to ground.
        for alias in ("0", "gnd", "GND"):
            self.node_index.setdefault(alias, -1)
            self._node_aliases.setdefault(alias.lower(), alias)

        self.gmin = float(options.get("gmin", 1e-12))
        self.reltol = float(options.get("reltol", 1e-3))
        self.abstol = float(options.get("abstol", 1e-12))
        self.vntol = float(options.get("vntol", 1e-6))
        self.maxiter = int(options.get("maxiter", 100))
        self.method = options.get("method", "trap")

        # Instantiate devices.
        self.devices: list[dev.Device] = []
        for spec in circuit.get("devices", []):
            d = make_device(spec, self.models, self.params)
            d.gmin = self.gmin
            self.devices.append(d)

        # Resolve inter-device references (e.g. K mutual-inductance couplings bind to their two
        # inductor instances). Done once after all devices exist; raises DeviceError on a bad ref.
        by_name = {d.name: d for d in self.devices}
        for d in self.devices:
            d.link(by_name)

        # A template MnaSystem to pin branch-row allocation (so indices are stable).
        self.node_count = idx

    def resolve_node(self, name: str):
        """Case/format-tolerant node → index. None only when truly unknown.

        mega-23 AC5: accept v(3v3)/V(3V3)/+3V3 aliases against schematic nets.
        """
        if name is None:
            return None
        key = str(name).strip()
        if key in self.node_index:
            return self.node_index[key]
        low = key.lower()
        if low in ("0", "gnd", "ground", "vss", "vssa", "agnd", "dgnd"):
            return -1
        orig = getattr(self, "_node_aliases", {}).get(low)
        if orig is not None and orig in self.node_index:
            return self.node_index[orig]
        bare = low.lstrip("+").replace("net_", "").replace("n$", "")
        for cand in (bare, bare.upper()):
            if cand in self.node_index:
                return self.node_index[cand]
            o2 = getattr(self, "_node_aliases", {}).get(str(cand).lower())
            if o2 is not None and o2 in self.node_index:
                return self.node_index[o2]
        for k, v in self.node_index.items():
            kl = str(k).lower()
            if kl == low or kl.lstrip("+") == bare:
                return v
        return None

    def new_system(self, complex_mode: bool = False) -> MnaSystem:
        mna = MnaSystem(self.node_index, complex_mode=complex_mode)
        # Pre-allocate branch rows for every device that needs them, in device order, so the
        # row layout is deterministic and shared across all solves of this analysis.
        for d in self.devices:
            d.branch_rows = []
            d.alloc_branches(mna)
            if hasattr(d, "record_indices"):
                d.record_indices(mna)
        mna.begin_solve()
        return mna

    def has_nonlinear(self) -> bool:
        return any(d.is_nonlinear() for d in self.devices)


# =============================================================================
# DC / Newton solve
# =============================================================================

def _stamp_all_dc(sim: _Sim, mna: MnaSystem, x: np.ndarray, gmin: float, src_scale: dict) -> str:
    mna.begin_solve()
    mna.src_scales = src_scale
    # gmin: add a tiny conductance from every non-ground node to ground (helps convergence).
    if gmin > 0:
        for i in range(sim.node_count):
            mna.stamp_G(i, i, gmin)
    for d in sim.devices:
        try:
            d.stamp_dc(mna, x)
        except DeviceError as e:
            return str(e)
    return ""


def _newton_dc(sim: _Sim, mna: MnaSystem, x0: np.ndarray, gmin: float,
               src_scale: dict, log: list) -> tuple[Optional[np.ndarray], int, str]:
    """Damped Newton iteration for a DC solve at a given gmin / source scale."""
    x = x0.copy()
    last_err = ""
    for it in range(1, sim.maxiter + 1):
        err = _stamp_all_dc(sim, mna, x, gmin, src_scale)
        if err:
            return None, it, err
        xnew, status = mna.solve()
        if xnew is None:
            last_err = status
            return None, it, status
        if not sim.has_nonlinear():
            # Linear system: one solve is exact — no damping, no iteration.
            return xnew, it, ""
        # Damping: limit per-iteration node-voltage swing (nonlinear only).
        dx = xnew - x
        max_step = 1.0  # volts; junction-limited devices already clamp internally
        big = np.abs(dx) > max_step
        if np.any(big) and it < sim.maxiter:
            scale = max_step / np.max(np.abs(dx))
            xnew = x + dx * max(scale, 0.1)
        # Convergence check (vntol + reltol on the node voltages).
        dv = np.abs(xnew - x)
        tol = sim.vntol + sim.reltol * np.maximum(np.abs(xnew), np.abs(x))
        if np.all(dv <= tol):
            return xnew, it, ""
        x = xnew
    return x, sim.maxiter, "newton did not converge"


def solve_dc_op(sim: _Sim, log: list, x_guess: Optional[np.ndarray] = None,
                src_scale: Optional[dict] = None) -> tuple[Optional[np.ndarray], bool]:
    """DC operating point with gmin-stepping then source-stepping fallback (sim.md §5)."""
    mna = sim.new_system()
    n = mna.dim
    x0 = np.zeros(n) if x_guess is None else x_guess.copy()
    if x0.shape[0] != n:
        x0 = np.zeros(n)
    base_scale = src_scale or {}

    # 1. Direct attempt.
    x, iters, err = _newton_dc(sim, mna, x0, sim.gmin, base_scale, log)
    if x is not None and not err:
        log.append(f"op: converged in {iters} iters")
        return x, True

    if not sim.has_nonlinear():
        log.append(f"op: linear solve failed — {err}")
        return None, False

    # 2. gmin stepping.
    log.append(f"op: direct Newton failed ({err}); trying gmin stepping")
    gmin = 1e-2
    x = x0.copy()
    ok = False
    while gmin >= sim.gmin / 10:
        xs, iters, e = _newton_dc(sim, mna, x, gmin, base_scale, log)
        if xs is not None and not e:
            x = xs
            if gmin <= sim.gmin:
                ok = True
                break
            gmin /= 10.0
        else:
            gmin *= 1.5
            if gmin > 1.0:
                break
    if ok:
        # Final clean solve at target gmin.
        xs, iters, e = _newton_dc(sim, mna, x, sim.gmin, base_scale, log)
        if xs is not None and not e:
            log.append(f"op: gmin stepping converged ({iters} iters)")
            return xs, True

    # 3. Source stepping.
    log.append("op: gmin stepping failed; trying source stepping")
    x = np.zeros(n)
    src_names = [d.name for d in sim.devices if d.kind in ("V", "I")]
    steps = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    converged_all = True
    for alpha in steps:
        scale = {nm: alpha for nm in src_names}
        xs, iters, e = _newton_dc(sim, mna, x, sim.gmin, scale, log)
        if xs is None or e:
            converged_all = False
            break
        x = xs
    if converged_all:
        log.append("op: source stepping converged")
        return x, True

    log.append("op: all DC convergence strategies failed")
    return None, False


# =============================================================================
# Probes
# =============================================================================

def _parse_probe(probe: str) -> tuple[str, list[str]]:
    """Return (kind, args). kind in {'V','I'}; args are node names or a source name."""
    p = probe.strip()
    up = p.upper()
    if up.startswith("V(") and p.endswith(")"):
        inner = p[2:-1]
        args = [a.strip() for a in inner.split(",")]
        return "V", args
    if up.startswith("I(") and p.endswith(")"):
        inner = p[2:-1]
        return "I", [inner.strip()]
    # Bare node name -> node voltage.
    return "V", [p]


def _eval_probe(sim: _Sim, mna: MnaSystem, x: np.ndarray, probe: str):
    kind, args = _parse_probe(probe)
    resolve = getattr(sim, "resolve_node", None)

    def _node(name: str, default=None):
        if resolve is not None:
            v = resolve(name)
            if v is not None:
                return v
            return default
        return sim.node_index.get(name, default)

    if kind == "V":
        if len(args) == 1:
            i = _node(args[0], None)
            if i is None:
                return None
            return _val(x, i)
        elif len(args) == 2:
            i = _node(args[0], -1)
            j = _node(args[1], -1)
            return _val(x, i) - _val(x, j)
    elif kind == "I":
        # Branch current through a source (V/L/E/H/OPAMP) — its MNA branch row.
        name = args[0]
        # Find the device and its first branch row.
        for d in sim.devices:
            if d.name == name and d.branch_rows:
                return _val(x, d.branch_rows[0])
        # Resistor current: I(R1) = (V(a)-V(b))/R.
        for d in sim.devices:
            if d.name == name and d.kind == "R":
                a = _node(d.node_names[0], -1)
                b = _node(d.node_names[1], -1)
                r = float(d.value) if d.value else 1e-9
                return (_val(x, a) - _val(x, b)) / r
        # Two-terminal nonlinear device current: I(D1)/I(LED1) = the junction current at the
        # operating point (into node_names[0], matching the resistor sign convention). Without
        # this, a diode current probe fell through to None and _safe() mapped it to NaN, so an
        # op-point probe like I(LED1) read NaN even on a perfectly converged solve.
        for d in sim.devices:
            if d.name == name and hasattr(d, "_linearize") and hasattr(d, "_vd"):
                try:
                    idd, _gd = d._linearize(d._vd(mna, x))
                    return float(idd)
                except Exception:  # noqa: BLE001 — a probe must never raise
                    return None
    return None


def _val(x: np.ndarray, i: int):
    if i is None or i < 0:
        return 0.0 if not np.iscomplexobj(x) else 0.0 + 0.0j
    return x[i]


# =============================================================================
# Transient
# =============================================================================

def _stamp_all_tran(sim: _Sim, mna: MnaSystem, x: np.ndarray, dt: float,
                    hist: dict, gmin: float) -> str:
    mna.begin_solve()
    mna.src_scales = {}
    if gmin > 0:
        for i in range(sim.node_count):
            mna.stamp_G(i, i, gmin)
    for d in sim.devices:
        try:
            d.stamp_tran(mna, x, dt, hist)
        except DeviceError as e:
            return str(e)
    return ""


def _newton_tran(sim: _Sim, mna: MnaSystem, x0: np.ndarray, dt: float,
                 hist: dict) -> tuple[Optional[np.ndarray], str]:
    x = x0.copy()
    nl = sim.has_nonlinear()
    for it in range(1, sim.maxiter + 1):
        err = _stamp_all_tran(sim, mna, x, dt, hist, sim.gmin)
        if err:
            return None, err
        xnew, status = mna.solve()
        if xnew is None:
            return None, status
        if not nl:
            return xnew, ""
        dv = np.abs(xnew - x)
        tol = sim.vntol + sim.reltol * np.maximum(np.abs(xnew), np.abs(x))
        if np.all(dv <= tol):
            return xnew, ""
        x = xnew
    return x, "tran newton did not converge"


def run_tran(sim: _Sim, an: dict, probes: list[str], log: list) -> tuple[dict, bool]:
    tstep = float(an.get("tstep", 1e-6))
    tstop = float(an.get("tstop", 1e-3))
    tstart = float(an.get("tstart", 0.0))
    uic = bool(an.get("uic", False))

    mna = sim.new_system()
    hist: dict = {"_method": "trap", "_t": 0.0}

    # Collect breakpoints from all sources.
    bps = set()
    for d in sim.devices:
        if d.kind in ("V", "I"):
            for bp in source_breakpoints(d.dparams, tstop):
                bps.add(bp)
    breakpoints = sorted(b for b in bps if 0.0 < b <= tstop)

    # Initial condition: DC op (unless uic) or .ic values.
    if uic:
        x = np.zeros(mna.dim)
        # Apply device .ic to history.
        for d in sim.devices:
            ic = d.dparams.get("ic")
            if ic is not None and d.kind == "C":
                hist.setdefault(d.name, {})["v"] = float(ic)
            if ic is not None and d.kind == "L":
                hist.setdefault(d.name, {})["i"] = float(ic)
    else:
        x, ok_op = solve_dc_op(sim, log)
        if x is None:
            log.append("tran: initial DC op failed")
            return {"x_t": [], "traces": {p: [] for p in probes}}, False
        # Seed reactive history from the op point.
        for d in sim.devices:
            if hasattr(d, "update_state"):
                d.update_state(x, tstep, hist)

    times = [0.0]
    trace_vals: dict[str, list] = {p: [_safe(_eval_probe(sim, mna, x, p))] for p in probes}

    t = 0.0
    step = tstep
    converged = True
    first = True
    next_bp_idx = 0
    max_steps = int(tstop / tstep * 4) + 1000

    n_steps = 0
    while t < tstop - 1e-15 and n_steps < max_steps:
        n_steps += 1
        # Determine dt, honoring the next breakpoint.
        dt = step
        method = "trap"
        if first:
            method = "be"  # backward-Euler bootstrap
        if next_bp_idx < len(breakpoints):
            bp = breakpoints[next_bp_idx]
            if t < bp <= t + dt + 1e-18:
                dt = bp - t
                if dt < 1e-18:
                    next_bp_idx += 1
                    continue
        tnew = min(t + dt, tstop)
        dt = tnew - t
        hist["_method"] = method
        hist["_t"] = tnew

        xnew, err = _newton_tran(sim, mna, x, dt, hist)
        if xnew is None:
            log.append(f"tran: non-convergence at t={tnew:.3e} ({err})")
            converged = False
            break

        # Commit step: update reactive history.
        for d in sim.devices:
            if hasattr(d, "update_state"):
                d.update_state(xnew, dt, hist)

        x = xnew
        t = tnew
        times.append(t)
        for p in probes:
            trace_vals[p].append(_safe(_eval_probe(sim, mna, x, p)))

        first = False
        # If we just hit a breakpoint, re-bootstrap with BE on the next step.
        if next_bp_idx < len(breakpoints) and abs(t - breakpoints[next_bp_idx]) < 1e-18:
            next_bp_idx += 1
            first = True

    if t < tstop - 1e-9 and converged:
        log.append(f"tran: stopped early at t={t:.3e} (max steps)")

    out = {"x_t": times, "traces": {p: trace_vals[p] for p in probes}}
    if converged:
        log.append(f"tran: completed {len(times)} points to t={t:.3e}")
    return out, converged


# =============================================================================
# DC sweep
# =============================================================================

def run_dc_sweep(sim: _Sim, an: dict, probes: list[str], log: list) -> tuple[dict, bool]:
    src = an["source"]
    start = float(an["start"]); stop = float(an["stop"]); step = float(an.get("step", (stop - start) / 50))
    if step == 0:
        step = (stop - start) / 50 or 1.0
    n = int(round((stop - start) / step)) + 1
    xs = [start + i * step for i in range(n)]

    # Find the swept source device.
    target = None
    for d in sim.devices:
        if d.name == src:
            target = d
            break
    if target is None:
        log.append(f"dc: source '{src}' not found")
        return {"sweep": src, "x": [], "traces": {p: [] for p in probes}}, False

    orig_value = target.value
    orig_wf = target.dparams.get("waveform")
    traces: dict[str, list] = {p: [] for p in probes}
    x_guess = None
    converged = True
    for xv in xs:
        target.value = xv
        target.dparams.pop("waveform", None)  # force DC value
        xsol, ok = solve_dc_op(sim, log, x_guess=x_guess)
        if xsol is None:
            converged = False
            for p in probes:
                traces[p].append(float("nan"))
            continue
        x_guess = xsol
        for p in probes:
            traces[p].append(_safe(_eval_probe(sim, mna_for(sim), xsol, p)))
    target.value = orig_value
    if orig_wf is not None:
        target.dparams["waveform"] = orig_wf

    log.append(f"dc: swept {src} {start}..{stop} ({len(xs)} points)")
    return {"sweep": src, "x": xs, "traces": traces}, converged


_MNA_CACHE: dict = {}


def mna_for(sim: _Sim) -> MnaSystem:
    """A cached MnaSystem for probe evaluation (branch rows already allocated)."""
    key = id(sim)
    m = _MNA_CACHE.get(key)
    if m is None:
        m = sim.new_system()
        _MNA_CACHE[key] = m
    return m


# =============================================================================
# AC
# =============================================================================

def _freq_grid(an: dict) -> np.ndarray:
    """Build the frequency sweep grid from an AC/noise analysis spec (dec/oct/lin)."""
    sweep = an.get("sweep", "dec")
    points = int(an.get("points", 10))
    fstart = float(an.get("fstart", 1.0))
    fstop = float(an.get("fstop", 1e6))
    if fstart <= 0.0:
        fstart = 1.0
    if sweep == "dec":
        ndec = math.log10(fstop / fstart)
        nfreq = max(2, int(round(points * ndec)) + 1)
        return np.logspace(math.log10(fstart), math.log10(fstop), nfreq)
    if sweep == "oct":
        noct = math.log2(fstop / fstart)
        nfreq = max(2, int(round(points * noct)) + 1)
        return np.logspace(math.log10(fstart), math.log10(fstop), nfreq)
    return np.linspace(fstart, fstop, max(2, points))


def run_ac(sim: _Sim, an: dict, probes: list[str], log: list) -> tuple[dict, bool]:
    sweep = an.get("sweep", "dec")
    fstart = float(an.get("fstart", 1.0))
    fstop = float(an.get("fstop", 1e6))
    freqs = _freq_grid(an)

    # DC operating point for nonlinear device linearization.
    x_op = None
    if sim.has_nonlinear():
        x_op, ok = solve_dc_op(sim, log)
        if x_op is None:
            log.append("ac: DC op for linearization failed")
            x_op = None

    mna = sim.new_system(complex_mode=True)
    n = mna.dim
    if x_op is None:
        x_op_real = np.zeros(n)
    else:
        x_op_real = x_op

    mag: dict[str, list] = {p: [] for p in probes}
    phase: dict[str, list] = {p: [] for p in probes}
    converged = True
    for f in freqs:
        omega = 2.0 * math.pi * f
        mna.begin_solve()
        mna.src_scales = {}
        for d in sim.devices:
            try:
                d.stamp_ac(mna, x_op_real, omega)
            except (DeviceError, NotImplementedError) as e:
                log.append(f"ac: {d.name} has no AC stamp ({e})")
        xsol, status = mna.solve(omega=omega)
        if xsol is None:
            converged = False
            for p in probes:
                mag[p].append(float("nan")); phase[p].append(float("nan"))
            continue
        for p in probes:
            v = _eval_probe(sim, mna, xsol, p)
            if v is None:
                mag[p].append(float("nan")); phase[p].append(float("nan"))
            else:
                c = complex(v)
                mag[p].append(abs(c))
                phase[p].append(math.degrees(math.atan2(c.imag, c.real)))

    log.append(f"ac: {len(freqs)} points {fstart}..{fstop} Hz ({sweep})")
    return {"x_f": list(map(float, freqs)), "mag": mag, "phase_deg": phase}, converged


def run_noise(sim: _Sim, an: dict, probes_or_log, log: list = None) -> tuple[dict, bool]:
    """Resistor-thermal noise analysis — thin wrapper that hands ``analysis_ext.run_noise`` the
    circuit-internal callables it needs (new_system / solve_dc_op / probe eval / freq grid)."""
    # Tolerate either (sim, an, log) or (sim, an, probes, log) call shapes.
    if log is None:
        log = probes_or_log
    helpers = {
        "new_system": sim.new_system,
        "solve_dc_op": solve_dc_op,
        "eval_probe": _eval_probe,
        "freq_grid": _freq_grid,
    }
    return analysis_ext.run_noise(sim, an, log, helpers)


# =============================================================================
# .tf transfer function (DC gain, input resistance, output resistance)
# =============================================================================

def _tf_node_pair(sim: "_Sim", output: str) -> tuple[int, int]:
    """Resolve the output probe's node-row pair (i, j) for V(node) or V(a,b). Ground -> -1."""
    kind, args = _parse_probe(output)
    if kind != "V":
        # Only voltage outputs are meaningful for the resistance/gain definitions here.
        raise DeviceError(f"tf: output '{output}' must be a voltage probe V(node) or V(a,b)")
    if len(args) == 1:
        return sim.node_index.get(args[0], -1), -1
    return sim.node_index.get(args[0], -1), sim.node_index.get(args[1], -1)


def _tf_stamp_linear(sim: "_Sim", mna: MnaSystem) -> str:
    """Stamp every device's small-signal (AC, omega=0) contribution into ``mna`` and then ZERO the
    RHS, leaving only the linearized topology matrix with all independent sources nulled.

    The matrix is built from the G/C triplets (independent of the RHS vector), so clearing ``_z``
    after stamping removes every independent V/I source's excitation while keeping its branch
    topology (a shorted V source / opened I source) -- exactly the network SPICE uses for the .tf
    output-resistance and zeroed-source gain solves."""
    mna.begin_solve()
    mna.src_scales = {}
    err = ""
    for d in sim.devices:
        try:
            d.stamp_ac(mna, mna._tf_xop, 0.0)
        except (DeviceError, NotImplementedError) as e:
            err = str(e)
            return err
    # Null all independent-source excitation (their RHS entries) -- keep the matrix.
    mna._z[:] = 0.0
    return err


def run_tf(sim: "_Sim", an: dict, log: list) -> tuple[dict, bool]:
    """Small-signal transfer function (.tf): DC gain, input resistance, output resistance.

    Spec: ``{"type":"tf", "output":"V(OUT)"|"V(a,b)", "source":"V1"|"I1"}``.

    All three quantities are computed on the network linearized at the DC operating point and
    evaluated at DC (omega=0 through the complex AC stamping path):

      * gain : output / source. Drive ``source`` with a unit small-signal excitation (1 V for a
        V source, 1 A for an I source) with every other independent source nulled; read ``output``.
        For a V-source input this is dimensionless V/V; for an I-source input it is a
        transimpedance (V/A) -- the classic SPICE .tf "transfer_function" value.
      * rin  : resistance seen by ``source``. For a V source, Rin = V_drive / I(source-branch);
        for an I source, Rin = V(across the source nodes) / I_drive.
      * rout : resistance at ``output``. Null every independent source, inject a 1 A test current
        across the output nodes, and read the resulting output voltage (Rout = V/1A) -- the
        source-shorted output resistance.

    Returns ``({"gain","rin","rout","output","source"}, converged)``."""
    output = an.get("output") or "V(OUT)"
    source = an.get("source")

    # DC operating point for linearization (nonlinear devices) -- also the node voltages used to
    # recompute MOSFET/BJT/diode small-signal parameters.
    x_op = None
    if sim.has_nonlinear():
        x_op, ok = solve_dc_op(sim, log)
        if x_op is None:
            log.append("tf: DC op for linearization failed")

    mna = sim.new_system(complex_mode=True)
    n = mna.dim
    mna._tf_xop = x_op if x_op is not None else np.zeros(n)

    try:
        oi, oj = _tf_node_pair(sim, output)
    except DeviceError as e:
        log.append(str(e))
        return {"gain": float("nan"), "rin": float("nan"), "rout": float("nan"),
                "output": output, "source": source}, False

    # Locate the source device.
    src_dev = None
    for d in sim.devices:
        if d.name == source:
            src_dev = d
            break
    if src_dev is None or src_dev.kind not in ("V", "I"):
        log.append(f"tf: source '{source}' must name an independent V or I source")
        return {"gain": float("nan"), "rin": float("nan"), "rout": float("nan"),
                "output": output, "source": source}, False

    sa = mna.n(src_dev.node_names[0])
    sb = mna.n(src_dev.node_names[1])

    def _read(x, i, j) -> complex:
        return complex(_val(x, i)) - complex(_val(x, j))

    converged = True

    # --- gain + Rin : unit drive of the chosen source, all others nulled -------------------
    err = _tf_stamp_linear(sim, mna)
    if err:
        log.append(f"tf: linearization failed ({err})")
        return {"gain": float("nan"), "rin": float("nan"), "rout": float("nan"),
                "output": output, "source": source}, False

    if src_dev.kind == "V":
        # Unit voltage across the source branch: stamp +1 V on its branch RHS (mirrors stamp_ac).
        br = src_dev.branch_rows[0]
        mna.stamp_I(br, 1.0)
        drive = 1.0
    else:  # I source: inject 1 A INTO the from-node (n+), out of n- -> raises V(n+), so the
        # transimpedance/Rin come out positive for a passive load (textbook .tf sign).
        mna.stamp_I(sa, 1.0)
        mna.stamp_I(sb, -1.0)
        drive = 1.0
    xsol, status = mna.solve(omega=0.0)
    gain = float("nan"); rin = float("nan")
    if xsol is None:
        converged = False
        log.append(f"tf: gain solve singular ({status})")
    else:
        out_v = _read(xsol, oi, oj)
        gain = (out_v / drive).real
        if src_dev.kind == "V":
            # The V-source branch current uses the passive sign convention (negative when current
            # is delivered to the network, cf. I(V1) in the divider gate). The current the source
            # *drives into* the network is therefore -I(branch); Rin = V_drive / I_delivered.
            i_src = complex(_val(xsol, src_dev.branch_rows[0]))
            i_delivered = -i_src
            rin = (drive / i_delivered).real if abs(i_delivered) > 0 else float("inf")
        else:
            # Voltage developed across the current-source nodes per unit injected current = Rin.
            v_src = _read(xsol, sa, sb)
            rin = (v_src / drive).real

    # --- Rout : all independent sources nulled, 1 A test current across the output nodes ----
    err = _tf_stamp_linear(sim, mna)
    if err:
        converged = False
    mna.stamp_I(oi, 1.0)
    mna.stamp_I(oj, -1.0)
    xsol2, status2 = mna.solve(omega=0.0)
    rout = float("nan")
    if xsol2 is None:
        converged = False
        log.append(f"tf: rout solve singular ({status2})")
    else:
        rout = _read(xsol2, oi, oj).real  # V developed per 1 A test source

    log.append(f"tf: gain={gain:.6g} rin={rin:.6g} rout={rout:.6g} "
               f"(output={output}, source={source})")
    return {"gain": gain, "rin": rin, "rout": rout, "output": output, "source": source}, converged


# =============================================================================
# helpers
# =============================================================================

def _safe(v):
    if v is None:
        return float("nan")
    if isinstance(v, complex):
        return v.real
    return float(v)


# =============================================================================
# public entry point
# =============================================================================

def _default_probes(circuit: dict) -> list[str]:
    """Default probe set when the study specifies none: V(node) for every non-ground node, plus
    I(Vname) for every independent voltage source.

    High value for callers (and the weak AI) that do not enumerate probes — every node voltage and
    source current is emitted instead of an empty result. Ground aliases are excluded.
    """
    probes: list[str] = []
    seen: set[str] = set()
    grounds = {"0", "gnd", "GND"}
    # Collect node names in a stable order: declared nodes first, then any device-only nodes.
    nodes: list[str] = list(circuit.get("nodes", []))
    for d in circuit.get("devices", []):
        for nm in d.get("nodes", []):
            if nm not in nodes:
                nodes.append(nm)
    for nm in nodes:
        s = str(nm)
        if s in grounds or s in seen:
            continue
        seen.add(s)
        probes.append(f"V({s})")
    for d in circuit.get("devices", []):
        if d.get("kind") == "V":
            probes.append(f"I({d.get('name')})")
    return probes


def simulate(circuit: dict, study: dict, backend: str = "mna") -> dict:
    """Run the requested analyses on a Circuit dict; return sim.md §4 results.

    ``backend`` selects the solver (T-FIN14 backend seam):
      * "mna"     -- the validated internal Python MNA solver (DEFAULT, deterministic)
      * "ngspice" -- run through an installed ngspice binary (unlocks vendor
                     .SUBCKT/.MODEL libraries). If ngspice is absent, or the study
                     uses an analysis the subprocess backend does not support
                     (four/mc/temp/noise/step), or the run fails, this degrades
                     HONESTLY to the MNA solver and says so in result["backend_note"].
      * "auto"    -- mna unless the circuit carries vendor .lib/.subckt attachments
                     (circuit["includes"]/["xdevices"]) AND ngspice is installed.
    Every result carries result["backend"] = the solver that actually ran.
    """
    from engine.sim import ngspice_backend as _ng  # local import; cheap, avoids cycles

    choice, note = _ng.resolve_backend(backend, circuit, study)
    if choice == "ngspice":
        try:
            result = _ng.simulate_ngspice(circuit, study)
            result["backend"] = "ngspice"
            if note:
                result["backend_note"] = note
            return result
        except _ng.NgspiceError as e:
            note = ((note + " ") if note else "") + \
                f"ngspice run failed ({e}) — fell back to the internal MNA solver."
    result = _simulate_mna(circuit, study)
    result["backend"] = "mna"
    if note:
        result["backend_note"] = note
        result.setdefault("log", []).append(f"backend: {note}")
    return result


def _simulate_mna(circuit: dict, study: dict) -> dict:
    """The internal MNA solver path (the pre-T-FIN14 body of ``simulate``)."""
    t0 = time.perf_counter()
    options = dict(study.get("options", {}))
    probes = list(study.get("probes", []))
    # Default-all-nodes output: when no probes are requested, emit every node voltage (+ source
    # currents) so callers that omit probes still get a useful result. Callers that DO pass probes
    # are unaffected.
    if not probes:
        probes = _default_probes(circuit)
        if probes:
            log_default = True
        else:
            log_default = False
    else:
        log_default = False
    log: list[str] = []
    if log_default:
        log.append(f"probes: none specified — defaulting to all {len(probes)} node/source outputs")

    result: dict = {"log": log, "convergence": {}}

    analyses = list(study.get("analyses", []))
    # A `.step` parameter sweep may be expressed either as a top-level study["step"] spec or as an
    # analysis entry {"type":"step", ...}; it wraps the remaining (inner) analyses.
    step_spec = study.get("step")
    inner_analyses = analyses
    if step_spec is None:
        step_entries = [a for a in analyses if a.get("type") == "step"]
        if step_entries:
            step_spec = step_entries[0]
            inner_analyses = [a for a in analyses if a.get("type") != "step"]

    if step_spec is not None:
        return _run_stepped(circuit, options, step_spec, inner_analyses, probes, log, t0)

    # Monte-Carlo / temperature sweep wrap an inner analysis and re-invoke simulate per run. They
    # are additive top-level analysis entries; detect and route before the normal per-analysis path.
    mc_entries = [a for a in inner_analyses if a.get("type") == "mc"]
    temp_entries = [a for a in inner_analyses if a.get("type") == "temp"]
    if mc_entries:
        an = mc_entries[0]
        seed_default = int(study.get("seed", options.get("seed", 12345)))
        result["mc"] = analysis_ext.run_monte_carlo(
            circuit, options, an, probes, log, _simulate_mna, seed_default)
        result["convergence"]["mc"] = result["mc"].get("runs", 0) > 0
        # Run any remaining (non-mc) analyses on the nominal circuit too.
        rest = [a for a in inner_analyses if a.get("type") not in ("mc",)]
        if rest:
            try:
                sim = _Sim(circuit, options)
                _run_analyses(sim, rest, probes, result, log)
            except DeviceError as e:
                log.append(f"build failed: {e}")
        result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return result
    if temp_entries:
        an = temp_entries[0]
        result["temp"] = analysis_ext.run_temp_sweep(
            circuit, options, an, probes, log, _simulate_mna)
        result["convergence"]["temp"] = bool(result["temp"].get("points"))
        rest = [a for a in inner_analyses if a.get("type") not in ("temp",)]
        if rest:
            try:
                sim = _Sim(circuit, options)
                _run_analyses(sim, rest, probes, result, log)
            except DeviceError as e:
                log.append(f"build failed: {e}")
        result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return result

    try:
        sim = _Sim(circuit, options)
    except DeviceError as e:
        log.append(f"build failed: {e}")
        result["convergence"]["build"] = False
        result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return result

    _run_analyses(sim, inner_analyses, probes, result, log)

    result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return result


def _run_analyses(sim: "_Sim", analyses: list, probes: list[str],
                  result: dict, log: list) -> None:
    """Execute each requested analysis on ``sim``, writing into ``result`` / ``result['convergence']``."""
    # Reset the probe-mna cache for this run.
    _MNA_CACHE.pop(id(sim), None)

    for an in analyses:
        atype = an.get("type")
        try:
            if atype == "op":
                mna = mna_for(sim)
                x, ok = solve_dc_op(sim, log)
                result["convergence"]["op"] = ok
                if x is not None:
                    result["op"] = {p: _safe(_eval_probe(sim, mna, x, p)) for p in probes}
                else:
                    result["op"] = {p: float("nan") for p in probes}

            elif atype == "dc":
                out, ok = run_dc_sweep(sim, an, probes, log)
                result["dc"] = out
                result["convergence"]["dc"] = ok

            elif atype == "tran":
                out, ok = run_tran(sim, an, probes, log)
                result["tran"] = out
                result["convergence"]["tran"] = ok

            elif atype == "ac":
                out, ok = run_ac(sim, an, probes, log)
                result["ac"] = out
                result["convergence"]["ac"] = ok

            elif atype == "four":
                # Fourier/THD on a transient trace. Use a preceding tran result if present;
                # otherwise run a tran from this spec's own tstep/tstop and analyze it.
                tran_res = result.get("tran")
                if tran_res is None:
                    fprobe = an.get("probe")
                    tprobes = list(probes)
                    if fprobe and fprobe not in tprobes:
                        tprobes = [fprobe] + tprobes
                    tran_res, tok = run_tran(sim, an, tprobes, log)
                    result["tran"] = tran_res
                    result["convergence"]["tran"] = tok
                four = analysis_ext.run_fourier(tran_res, an, log)
                if four is not None:
                    result["four"] = four
                    result["convergence"]["four"] = True
                else:
                    result["convergence"]["four"] = False

            elif atype == "noise":
                out, ok = run_noise(sim, an, log)
                result["noise"] = out
                result["convergence"]["noise"] = ok

            elif atype == "tf":
                out, ok = run_tf(sim, an, log)
                result["tf"] = out
                result["convergence"]["tf"] = ok

            else:
                log.append(f"unknown analysis type '{atype}' — skipped")
        except DeviceError as e:
            log.append(f"{atype}: device error — {e}")
            result["convergence"][atype] = False
        except Exception as e:  # noqa: BLE001  — never crash the worker; report instead
            log.append(f"{atype}: solver exception — {type(e).__name__}: {e}")
            result["convergence"][atype] = False


def _step_values(spec: dict) -> list[float]:
    """Resolve the swept values from a step spec: explicit ``values`` list or start/stop/step."""
    vals = spec.get("values")
    if vals is not None:
        return [float(v) for v in vals]
    start = float(spec.get("start", 0.0))
    stop = float(spec.get("stop", 0.0))
    step = float(spec.get("step", 0.0))
    out: list[float] = []
    if step == 0.0:
        return [start]
    n = int(round((stop - start) / step)) + 1
    for i in range(max(n, 1)):
        out.append(start + i * step)
    return out


def _apply_step_param(circuit: dict, param: str, value: float) -> bool:
    """Set ``param`` to ``value`` on a *copy-safe* circuit dict in place.

    ``param`` matches (in order): a device name (sets its ``value``); a ``.param`` /
    ``params`` key (case-insensitive); otherwise no-op. Returns True if something was set.
    """
    hit = False
    for d in circuit.get("devices", []):
        if d.get("name") == param:
            d["value"] = value
            # Device value wins over any waveform DC for a stepped sweep.
            hit = True
    if hit:
        return True
    # Fall back to a .param entry (case-insensitive).
    params = circuit.setdefault("params", {})
    for key in (param, param.upper(), param.lower()):
        if key in params:
            params[key] = value
            return True
    # Unknown param: still record it under the given name so device _mp lookups can find it.
    params[param] = value
    return False


def _run_stepped(circuit: dict, options: dict, step_spec: dict, inner_analyses: list,
                 probes: list[str], log: list, t0: float) -> dict:
    """Run ``inner_analyses`` once per value of the stepped parameter (sim.md §3 ``.step``).

    Results are returned under ``result["step"]`` = {"param":..., "values":[...],
    "runs":[{"value":v, <per-run sim.md §4 results>}, ...]}. Each run is an independent sim build,
    so the swept device value (or .param) takes effect cleanly.
    """
    import copy

    param = str(step_spec.get("param", ""))
    values = _step_values(step_spec)
    runs: list[dict] = []
    all_ok = True
    log.append(f"step: sweeping '{param}' over {len(values)} values {values}")

    for v in values:
        c = copy.deepcopy(circuit)
        _apply_step_param(c, param, v)
        run_res: dict = {"value": v, "convergence": {}}
        run_log: list[str] = []
        try:
            sim = _Sim(c, options)
        except DeviceError as e:
            run_log.append(f"build failed: {e}")
            run_res["convergence"]["build"] = False
            run_res["log"] = run_log
            runs.append(run_res)
            all_ok = False
            continue
        run_res["log"] = run_log
        _run_analyses(sim, inner_analyses, probes, run_res, run_log)
        if not all(run_res["convergence"].values()):
            all_ok = False
        runs.append(run_res)

    result: dict = {
        "log": log,
        "convergence": {"step": all_ok},
        "step": {"param": param, "values": values, "runs": runs},
    }
    result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return result
