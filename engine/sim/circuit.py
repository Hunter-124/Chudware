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
        idx = 0
        for nm in raw_nodes:
            if str(nm) in ("0", "gnd", "GND"):
                self.node_index[str(nm)] = -1
            else:
                self.node_index[str(nm)] = idx
                idx += 1
        # Map common ground aliases to ground.
        for alias in ("0", "gnd", "GND"):
            self.node_index.setdefault(alias, -1)

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

        # A template MnaSystem to pin branch-row allocation (so indices are stable).
        self.node_count = idx

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
    if kind == "V":
        if len(args) == 1:
            i = sim.node_index.get(args[0], None)
            if i is None:
                return None
            return _val(x, i)
        elif len(args) == 2:
            i = sim.node_index.get(args[0], -1)
            j = sim.node_index.get(args[1], -1)
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
                a = sim.node_index.get(d.node_names[0], -1)
                b = sim.node_index.get(d.node_names[1], -1)
                r = float(d.value) if d.value else 1e-9
                return (_val(x, a) - _val(x, b)) / r
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

def run_ac(sim: _Sim, an: dict, probes: list[str], log: list) -> tuple[dict, bool]:
    sweep = an.get("sweep", "dec")
    points = int(an.get("points", 10))
    fstart = float(an.get("fstart", 1.0))
    fstop = float(an.get("fstop", 1e6))

    if sweep == "dec":
        ndec = math.log10(fstop / fstart)
        nfreq = max(2, int(round(points * ndec)) + 1)
        freqs = np.logspace(math.log10(fstart), math.log10(fstop), nfreq)
    elif sweep == "oct":
        noct = math.log2(fstop / fstart)
        nfreq = max(2, int(round(points * noct)) + 1)
        freqs = np.logspace(math.log10(fstart), math.log10(fstop), nfreq)
    else:  # lin
        freqs = np.linspace(fstart, fstop, max(2, points))

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

def simulate(circuit: dict, study: dict) -> dict:
    """Run the requested analyses on a Circuit dict; return sim.md §4 results."""
    t0 = time.perf_counter()
    options = dict(study.get("options", {}))
    probes = list(study.get("probes", []))
    log: list[str] = []

    result: dict = {"log": log, "convergence": {}}

    try:
        sim = _Sim(circuit, options)
    except DeviceError as e:
        log.append(f"build failed: {e}")
        result["convergence"]["build"] = False
        result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        return result

    # Reset the probe-mna cache for this run.
    _MNA_CACHE.pop(id(sim), None)

    for an in study.get("analyses", []):
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

            else:
                log.append(f"unknown analysis type '{atype}' — skipped")
        except DeviceError as e:
            log.append(f"{atype}: device error — {e}")
            result["convergence"][atype] = False
        except Exception as e:  # noqa: BLE001  — never crash the worker; report instead
            log.append(f"{atype}: solver exception — {type(e).__name__}: {e}")
            result["convergence"][atype] = False

    result["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return result
