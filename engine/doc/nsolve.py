"""Shared numeric constraint solver core (docs/PLAN.md §3.1).

ONE residual-based solver that the 2D sketch solver (C10) and the 3D mate solver
(C20) parameterize. The solver knows nothing about sketches or mates: callers
register *variables* and *residual blocks*. A residual block is a small vector
function r(x_sub) -> R^m plus its analytic Jacobian J(x_sub) -> R^(m,k), tagged
with an opaque constraint id used in diagnostics. Geometry parameterization lives
entirely in the callers.

Method: damped Newton / Levenberg-Marquardt over a sparse analytic Jacobian
(scipy.sparse, assembled per iteration from dense per-block Jacobians). Adaptive
scale-invariant damping, warm starts, a wall-clock budget for the drag path, and
QR-rank diagnostics (DOF, redundant vs conflicting constraints).

Design notes for the callers (C10 sketch solver, C20 mate solver):
  - Coordinates are plain float64 in one flat vector `x`; you own the mapping from
    your entities to variable indices (use the slice returned by `add_vars`).
  - Register ONE residual block per constraint, tagged with the constraint id, so
    diagnostics can name exactly which constraints are redundant/conflicting.
  - Equality constraints become residuals that are zero when satisfied. Dimension
    constraints (distance/angle/radius) subtract the target. A `fix` is just a
    residual `x_i - target` (or simply omit the var — but a residual keeps the tag
    in diagnostics, which the over-defined UI wants).
  - For drag: pin the dragged point with `soft_target` (low weight) so the real
    constraints win, warm-start `solve` with the previous solution, and pass
    `time_budget_ms` so a hard solve degrades to best-so-far instead of stalling.

NO finite differences anywhere — every Jacobian is supplied analytically by the
caller. All arithmetic is float64.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np
import scipy.linalg as sla
import scipy.sparse as sp

# A residual function maps the sub-vector of its variables to an m-vector.
ResidualFunc = Callable[[np.ndarray], np.ndarray]
# Its Jacobian maps the same sub-vector to a dense (m, k) block, k = len(var_idx).
JacobianFunc = Callable[[np.ndarray], np.ndarray]


@dataclass
class _Block:
    """One registered residual block (typically one constraint)."""

    rfunc: ResidualFunc
    jfunc: JacobianFunc
    var_idx: np.ndarray          # int64 indices into the global x, length k
    tag: object                  # opaque constraint id for diagnostics
    m: int                       # residual length
    weight: float = 1.0          # soft-target blocks downweight their rows
    soft: bool = False           # drag aid: excluded from convergence + diagnostics
    row0: int = 0                # filled in at assembly time (global row offset)


@dataclass
class _Assembler:
    """Precomputed sparse-assembly plan for a fixed block set (perf hot path).

    The Jacobian sparsity pattern (ridx/cidx) is constant over iterations; only the
    `data` values are refilled by calling the analytic block Jacobians each step.
    """

    n_rows: int
    n_vars: int
    ridx: np.ndarray             # COO row indices (constant)
    cidx: np.ndarray             # COO col indices (constant)
    data: np.ndarray             # COO values buffer (refilled each call)
    jac_slices: list             # [(block, data_start, data_len), …]
    res_slices: list             # [(block, row_start), …]

    def residual(self, x: np.ndarray) -> np.ndarray:
        r = np.empty(self.n_rows, dtype=np.float64)
        for b, row in self.res_slices:
            rb = np.atleast_1d(np.asarray(b.rfunc(x[b.var_idx]), dtype=np.float64))
            r[row : row + b.m] = rb
        return r

    def jacobian(self, x: np.ndarray) -> sp.csr_matrix:
        data = self.data
        for b, start, nnz in self.jac_slices:
            jb = np.asarray(b.jfunc(x[b.var_idx]), dtype=np.float64)
            data[start : start + nnz] = jb.ravel()
        if data.size == 0:
            return sp.csr_matrix((self.n_rows, self.n_vars), dtype=np.float64)
        return sp.coo_matrix(
            (data, (self.ridx, self.cidx)),
            shape=(self.n_rows, self.n_vars),
            dtype=np.float64,
        ).tocsr()


@dataclass
class Result:
    """Outcome of `solve`."""

    x: np.ndarray
    converged: bool
    iterations: int
    max_residual: float


@dataclass
class Analysis:
    """Outcome of `analyze` — drives the under/fully/over-defined UI states."""

    dof: int
    redundant_tags: List[object] = field(default_factory=list)
    conflicting_tags: List[object] = field(default_factory=list)
    well_posed: bool = True
    rank: int = 0
    n_vars: int = 0
    n_residuals: int = 0


class System:
    """A registry of variables and tagged residual blocks, plus solve/analyze.

    Build a system by calling `add_vars` and `add_residual` (or `soft_target`),
    then `solve(...)` for positions and `analyze(x)` for diagnostics. Instances
    are cheap to rebuild; callers typically rebuild on topology change and reuse
    `x` as a warm start across drags.
    """

    def __init__(self) -> None:
        self._init: List[float] = []           # initial guess accumulator
        self._blocks: List[_Block] = []
        self._n_rows: int = 0                   # total residual rows
        self._has_soft: bool = False            # any soft (drag) block present
        self._dirty: bool = True                # block row offsets need recompute

    # -- construction ----------------------------------------------------------

    @property
    def n_vars(self) -> int:
        return len(self._init)

    @property
    def n_residuals(self) -> int:
        return self._n_rows

    def add_vars(self, n: int, init: Optional[Sequence[float]] = None) -> np.ndarray:
        """Allocate `n` variables; return their int index array (contiguous).

        `init` (length n) seeds the initial guess; defaults to zeros. The returned
        array is what callers hand to `add_residual` as `var_idx` (slice it for
        sub-blocks). Indices are stable for the lifetime of the system.
        """
        if n < 0:
            raise ValueError("add_vars: n must be >= 0")
        start = len(self._init)
        if init is None:
            self._init.extend([0.0] * n)
        else:
            init = list(init)
            if len(init) != n:
                raise ValueError(f"add_vars: init has {len(init)} values, expected {n}")
            self._init.extend(float(v) for v in init)
        return np.arange(start, start + n, dtype=np.int64)

    def add_residual(
        self,
        rfunc: ResidualFunc,
        var_idx: Sequence[int],
        jfunc: JacobianFunc,
        tag: object,
        *,
        weight: float = 1.0,
        soft: bool = False,
    ) -> None:
        """Register a residual block.

        `rfunc(x_sub) -> ndarray(m)` and `jfunc(x_sub) -> ndarray(m, k)` where
        `x_sub = x[var_idx]` and `k = len(var_idx)`. `tag` is an opaque constraint
        id surfaced by `analyze`. The block is satisfied when `rfunc` returns ~0.

        `soft=True` marks a drag-aid block (see `soft_target`): it participates in
        the least-squares step but is excluded from the convergence test (which is
        on the *hard* residual only, so hard constraints are driven to `tol` and the
        soft term merely steers the leftover DOF) and from `analyze` diagnostics.

        The Jacobian must be analytic (no finite differences). Its shape is probed
        once at registration against the current initial guess to catch wiring
        mistakes early.
        """
        idx = np.ascontiguousarray(var_idx, dtype=np.int64).ravel()
        if idx.size and (idx.min() < 0 or idx.max() >= self.n_vars):
            raise ValueError("add_residual: var_idx out of range (add_vars first)")
        x_sub = np.asarray([self._init[i] for i in idx], dtype=np.float64)
        r0 = np.atleast_1d(np.asarray(rfunc(x_sub), dtype=np.float64))
        if r0.ndim != 1:
            raise ValueError("add_residual: rfunc must return a 1-D array")
        m = r0.shape[0]
        j0 = np.atleast_2d(np.asarray(jfunc(x_sub), dtype=np.float64))
        if j0.shape != (m, idx.size):
            raise ValueError(
                f"add_residual: jfunc returned {j0.shape}, expected {(m, idx.size)}"
            )
        if weight <= 0.0:
            raise ValueError("add_residual: weight must be > 0")
        self._blocks.append(_Block(rfunc, jfunc, idx, tag, m, float(weight), bool(soft)))
        self._dirty = True

    def soft_target(
        self,
        idx: int,
        target: float,
        weight: float = 0.01,
    ) -> None:
        """Low-weight residual pulling variable `idx` toward `target` (drag helper).

        Used to follow the mouse during a drag without fighting the hard
        constraints: `weight` is small so any real constraint dominates the pull.
        Register one per dragged coordinate (e.g. x and y of the dragged point).
        The residual is `sqrt(weight) * (x_idx - target)` so the weight scales the
        squared term in the LM normal equations as intended.
        """
        i = int(idx)
        if i < 0 or i >= self.n_vars:
            raise ValueError("soft_target: idx out of range")
        w = float(weight)
        if w <= 0.0:
            raise ValueError("soft_target: weight must be > 0")
        s = np.sqrt(w)
        t = float(target)
        self.add_residual(
            lambda xs, _s=s, _t=t: np.array([_s * (xs[0] - _t)]),
            [i],
            lambda xs, _s=s: np.array([[_s]]),
            ("soft", i),
            weight=w,
            soft=True,
        )

    # -- assembly --------------------------------------------------------------

    def _finalize_rows(self) -> None:
        row = 0
        for b in self._blocks:
            b.row0 = row
            row += b.m
        self._n_rows = row
        self._has_soft = any(b.soft for b in self._blocks)
        self._dirty = False

    def _x0(self, x0: Optional[Sequence[float]]) -> np.ndarray:
        if x0 is None:
            return np.asarray(self._init, dtype=np.float64).copy()
        x = np.ascontiguousarray(x0, dtype=np.float64).ravel()
        if x.shape[0] != self.n_vars:
            raise ValueError(
                f"x0 has {x.shape[0]} entries, system has {self.n_vars} variables"
            )
        return x.copy()

    def _assembler(self, blocks: List[_Block], n_rows: int) -> "_Assembler":
        """Build a reusable assembler for a fixed block set.

        The Jacobian *sparsity pattern* is constant across solver iterations — only
        the nonzero values change — so the (row, col) index arrays and per-block
        data slices are computed once here. Per-iteration assembly then just calls
        the analytic block funcs and fills a preallocated value buffer (the hot
        path; this is what keeps warm drag inside the time budget).
        """
        rows: List[np.ndarray] = []
        cols: List[np.ndarray] = []
        slices: List[tuple] = []     # (block, data_start, data_len)
        rrow_slices: List[tuple] = []  # (block, row_start) for residual fill
        row = 0
        dpos = 0
        for b in blocks:
            nnz = b.m * b.var_idx.size
            rr = np.repeat(np.arange(b.m, dtype=np.int64) + row, b.var_idx.size)
            cc = np.tile(b.var_idx, b.m)
            rows.append(rr)
            cols.append(cc)
            slices.append((b, dpos, nnz))
            rrow_slices.append((b, row))
            row += b.m
            dpos += nnz
        ridx = np.concatenate(rows) if rows else np.zeros(0, dtype=np.int64)
        cidx = np.concatenate(cols) if cols else np.zeros(0, dtype=np.int64)
        return _Assembler(
            n_rows=n_rows,
            n_vars=self.n_vars,
            ridx=ridx,
            cidx=cidx,
            data=np.zeros(dpos, dtype=np.float64),
            jac_slices=slices,
            res_slices=rrow_slices,
        )

    # -- solve -----------------------------------------------------------------

    def solve(
        self,
        x0: Optional[Sequence[float]] = None,
        tol: float = 1e-10,
        max_iter: int = 50,
        time_budget_ms: Optional[float] = None,
    ) -> Result:
        """Levenberg-Marquardt solve with adaptive damping.

        Minimizes ||r(x)||^2. `x0=None` uses the registered initial guess; pass the
        previous solution to warm-start (the drag path). `tol` is the convergence
        threshold on the max absolute residual. `time_budget_ms`, if set, returns
        the best-so-far iterate once the wall clock is exceeded (`converged=False`).

        Damping is scale-invariant: the LM term uses lambda * diag(J^T J) rather
        than lambda * I, so units across heterogeneous variables don't bias the
        step. A singular/ill-conditioned normal matrix is handled by raising
        damping and, as a last resort, a least-squares (pseudo-inverse) step.
        With a soft target present the drag is solved as a *constrained* problem —
        minimize the soft objective subject to the hard constraints — via null-space
        projected descent (`_drag_solve`): the dragged point follows the cursor along
        the constraint manifold, and the hard constraints are satisfied exactly. This
        is the SolidWorks drag-to-flex behavior; the soft term never wins over a real
        constraint. Without soft targets it is a single LM solve.
        """
        if self._dirty:
            self._finalize_rows()
        t_start = time.perf_counter()
        deadline = None if time_budget_ms is None else t_start + time_budget_ms / 1e3

        x = self._x0(x0)
        if self._n_rows == 0:
            return Result(x=x, converged=True, iterations=0, max_residual=0.0)

        if not self._has_soft:
            asm = self._assembler(self._blocks, self._n_rows)
            x, it1, maxr = self._lm(x, asm, tol, max_iter, deadline)
            return Result(x=x, converged=maxr <= tol, iterations=it1, max_residual=maxr)

        return self._drag_solve(x, tol, max_iter, deadline)

    def _drag_solve(
        self,
        x: np.ndarray,
        tol: float,
        max_iter: int,
        deadline: Optional[float],
    ) -> Result:
        """Constrained drag: minimize the soft objective s.t. the hard constraints.

        Null-space projected descent. Each outer step (a) projects x onto the hard
        manifold with a few Newton steps, then (b) takes a damped step along the
        soft objective's gradient *projected into the tangent space* of the hard
        constraints (P = I - J^+ J), so it slides along the manifold toward the
        cursor without leaving it. A final hard projection guarantees exact
        constraint satisfaction. Soft weights set the relative pull of competing
        drag targets; they do not trade off against hard constraints.
        """
        hard_blocks = [b for b in self._blocks if not b.soft]
        soft_blocks = [b for b in self._blocks if b.soft]
        hard_rows = sum(b.m for b in hard_blocks)
        hard_asm = self._assembler(hard_blocks, hard_rows)

        # Pre-pack soft pull as a simple quadratic 0.5*sum w*(x_i - t_i)^2.
        # soft_target registers single-var blocks r = sqrt(w)*(x_i - t_i); recover
        # (idx, w, t) so the gradient is w*(x_i - t_i).
        soft_idx = np.array([int(b.var_idx[0]) for b in soft_blocks], dtype=np.int64)
        soft_w = np.array([b.weight for b in soft_blocks], dtype=np.float64)
        soft_t = np.empty(len(soft_blocks), dtype=np.float64)
        for i, b in enumerate(soft_blocks):
            # r(x_i) = sqrt(w)*(x_i - t)  ->  recover t from r at x_i = 0.
            s = np.sqrt(b.weight)
            soft_t[i] = -float(b.rfunc(np.array([0.0]))[0]) / s

        total_it = 0
        for _ in range(max_iter):
            # (a) project onto the hard manifold.
            x, it_h, hard_maxr = self._lm(x, hard_asm, tol, 20, deadline)
            total_it += it_h

            if deadline is not None and time.perf_counter() >= deadline:
                break

            # (b) tangential soft step. The Newton step toward the soft minimum of
            # 0.5*sum w*(x_i - t_i)^2 is (t_i - x_i) on each soft var (the weight
            # cancels in the per-target Newton scaling); relative weights still bias
            # which target wins when two pull the same DOF. Project that desired
            # displacement into the tangent space of the hard constraints (P d) so
            # the move stays on the manifold.
            d = np.zeros(self.n_vars, dtype=np.float64)
            # weighted average pull per variable (handles >1 target on one var)
            wsum = np.zeros(self.n_vars, dtype=np.float64)
            np.add.at(wsum, soft_idx, soft_w)
            np.add.at(d, soft_idx, soft_w * (soft_t - x[soft_idx]))
            nz = wsum > 0
            d[nz] /= wsum[nz]
            if np.max(np.abs(d)) < 1e-12:
                break
            J = hard_asm.jacobian(x).toarray()
            if J.size:
                Jp = np.linalg.pinv(J)
                d = d - Jp @ (J @ d)        # tangent-space projection (P d)
            if np.max(np.abs(d)) < 1e-12:
                break

            # Damped tangential move: a full Newton step overshoots a curved
            # manifold, so backtrack until the soft objective actually decreases
            # (after re-projecting to the manifold). This is a manifold line search.
            f0 = float(np.sum(soft_w * (x[soft_idx] - soft_t) ** 2))
            alpha = 1.0
            advanced = False
            for _ in range(20):
                x_try = x + alpha * d
                if not np.all(np.isfinite(x_try)):
                    alpha *= 0.5
                    continue
                x_try, _, _ = self._lm(x_try, hard_asm, tol, 20, deadline)
                f1 = float(np.sum(soft_w * (x_try[soft_idx] - soft_t) ** 2))
                if f1 < f0 - 1e-15:
                    x = x_try
                    advanced = True
                    break
                alpha *= 0.5
            if not advanced:
                break

        # Final exact projection onto the hard constraints.
        x, it_f, hard_maxr = self._lm(x, hard_asm, tol, max_iter, deadline)
        total_it += it_f
        return Result(
            x=x, converged=hard_maxr <= tol, iterations=total_it, max_residual=hard_maxr
        )

    def _lm(
        self,
        x: np.ndarray,
        asm: "_Assembler",
        tol: float,
        max_iter: int,
        deadline: Optional[float],
    ):
        """Levenberg-Marquardt minimization of ||r||^2 for the block set in `asm`.

        Returns (x, iterations, max_residual). Adaptive scale-invariant damping
        (lambda * diag(J^T J)); singular normal matrix falls back to a least-squares
        (pseudo-inverse) step. Honors `deadline` by returning the best iterate so far.
        """
        x = x.copy()
        if asm.n_rows == 0:
            return x, 0, 0.0
        r = asm.residual(x)
        cost = float(r @ r)
        best_x = x.copy()
        best_maxr = float(np.max(np.abs(r))) if r.size else 0.0
        if best_maxr <= tol:
            return best_x, 0, best_maxr

        lam = 1e-4            # LM damping; adapted multiplicatively
        nu = 2.0             # damping growth factor on rejected steps
        it = 0
        for it in range(1, max_iter + 1):
            J = asm.jacobian(x)
            JT = J.T.tocsr()
            JtJ = (JT @ J).toarray()           # dense normal matrix (n <= ~600)
            g = JT @ r                          # gradient = J^T r
            diag = np.maximum(np.diag(JtJ), 1e-12)   # scale-invariant damping basis

            accepted = False
            for _ in range(12):
                A = JtJ + lam * np.diag(diag)
                try:
                    with warnings.catch_warnings():
                        # Near-singular A is expected (rank-deficient / low damping);
                        # we fall back to lstsq, so silence the conditioning warning.
                        warnings.simplefilter("error", sla.LinAlgWarning)
                        step = sla.solve(A, -g, assume_a="pos")
                except (np.linalg.LinAlgError, sla.LinAlgError, sla.LinAlgWarning, ValueError):
                    step, *_ = np.linalg.lstsq(A, -g, rcond=None)

                x_new = x + step
                r_new = asm.residual(x_new)
                cost_new = float(r_new @ r_new)

                if cost_new < cost:
                    # Accept; relax damping toward Newton (fast on warm starts).
                    x = x_new
                    r = r_new
                    cost = cost_new
                    lam = max(lam / 10.0, 1e-14)
                    nu = 2.0
                    accepted = True
                    maxr = float(np.max(np.abs(r))) if r.size else 0.0
                    if maxr < best_maxr:
                        best_maxr = maxr
                        best_x = x.copy()
                    break
                # Reject; tighten damping (smaller, more gradient-like step).
                lam *= nu
                nu *= 2.0
                if lam > 1e12:
                    break

            maxr = float(np.max(np.abs(r))) if r.size else 0.0
            if maxr <= tol:
                return x.copy(), it, maxr

            if deadline is not None and time.perf_counter() >= deadline:
                return best_x.copy(), it, best_maxr

            if not accepted:
                # Damping saturated without progress — stuck (singular/conflicting).
                break

            step_norm = float(np.max(np.abs(step))) if step.size else 0.0
            if step_norm <= 1e-12 * (1.0 + float(np.max(np.abs(x)))):
                break

        return best_x.copy(), it, best_maxr

    # -- diagnostics -----------------------------------------------------------

    def analyze(self, x: Sequence[float], tol: float = 1e-7) -> Analysis:
        """DOF / redundancy / conflict analysis at configuration `x`.

        Rank is computed by a column-pivoted QR of the *dense* Jacobian
        (`scipy.linalg.qr(..., pivoting=True)`), which is fine for the v1 sketch/
        mate sizes (<= ~600 vars). Definitions:

          - dof = n_vars - rank  (0 = fully defined, >0 = under, <0 impossible here
            since rank <= n_vars; over-defined shows up as redundant rows instead).
          - redundant = constraint rows that are linearly dependent on the rest of
            the system (they remove no DOF). Identified by a row-pivoted QR of J^T
            (columns = constraint rows): rows beyond the numeric rank are dependent.
            Mapped back to the owning block's tag.
          - conflicting = redundant AND not currently satisfiable: the dependent row
            carries residual that can't be driven to zero (residual norm stays high
            at the solution). These drive "over-defined (red)"; pure-redundant (the
            dependency is consistent) drives "redundant (warn)".
          - well_posed = no conflicts (redundant-but-consistent is still well posed).

        Soft-target blocks are excluded from diagnostics: they are drag aids, not
        real constraints, and must not register as redundant/conflicting.
        """
        if self._dirty:
            self._finalize_rows()
        xv = np.ascontiguousarray(x, dtype=np.float64).ravel()
        if xv.shape[0] != self.n_vars:
            raise ValueError(
                f"analyze: x has {xv.shape[0]} entries, system has {self.n_vars} variables"
            )

        # Real (non-soft) blocks only.
        real = [b for b in self._blocks if not b.soft]
        n = self.n_vars
        if not real or n == 0:
            return Analysis(dof=n, well_posed=True, rank=0, n_vars=n, n_residuals=0)

        # Assemble the dense Jacobian and residual restricted to real blocks,
        # tracking which tag owns each row.
        m_total = sum(b.m for b in real)
        J = np.zeros((m_total, n), dtype=np.float64)
        r = np.zeros(m_total, dtype=np.float64)
        row_tag: List[object] = []
        row = 0
        for b in real:
            jb = np.atleast_2d(np.asarray(b.jfunc(xv[b.var_idx]), dtype=np.float64))
            rb = np.atleast_1d(np.asarray(b.rfunc(xv[b.var_idx]), dtype=np.float64))
            J[row : row + b.m, b.var_idx] = jb
            r[row : row + b.m] = rb
            row_tag.extend([b.tag] * b.m)
            row += b.m

        # Rank + DOF via column-pivoted QR of J (rows = constraints, cols = vars).
        rank = _numeric_rank(J, tol)
        dof = n - rank

        # Redundancy: find rows of J that are linearly dependent on earlier rows.
        # QR with pivoting on J^T orders columns (= constraint rows) by independence;
        # pivot columns beyond `rank` correspond to redundant rows.
        redundant_rows = _dependent_rows(J, rank, tol)

        redundant_tags: List[object] = []
        conflicting_tags: List[object] = []
        seen_r: set = set()
        seen_c: set = set()
        for ri in redundant_rows:
            tag = row_tag[ri]
            # Conflict test: this dependent row still carries residual that the rest
            # of the system can't absorb -> contradictory (e.g. 10 vs 12 distance).
            is_conflict = abs(r[ri]) > max(1e-6, 1e3 * tol)
            tid = _tag_key(tag)
            if is_conflict:
                if tid not in seen_c:
                    conflicting_tags.append(tag)
                    seen_c.add(tid)
            else:
                if tid not in seen_r:
                    redundant_tags.append(tag)
                    seen_r.add(tid)

        # A tag flagged as both is a conflict (stronger signal); drop it from warn.
        if conflicting_tags:
            ckeys = {_tag_key(t) for t in conflicting_tags}
            redundant_tags = [t for t in redundant_tags if _tag_key(t) not in ckeys]

        well_posed = len(conflicting_tags) == 0
        return Analysis(
            dof=dof,
            redundant_tags=redundant_tags,
            conflicting_tags=conflicting_tags,
            well_posed=well_posed,
            rank=rank,
            n_vars=n,
            n_residuals=m_total,
        )


# -- module-level numeric helpers ---------------------------------------------


def _tag_key(tag: object) -> object:
    """Hashable key for a tag (tags are usually strings/ints but allow tuples)."""
    try:
        hash(tag)
        return tag
    except TypeError:
        return repr(tag)


def _numeric_rank(J: np.ndarray, tol: float) -> int:
    """Rank of J via column-pivoted QR; threshold relative to the largest pivot."""
    if J.size == 0:
        return 0
    # R's diagonal magnitudes (with pivoting) are a stable rank proxy.
    _, R, _ = sla.qr(J, mode="economic", pivoting=True)
    diag = np.abs(np.diag(R))
    if diag.size == 0:
        return 0
    thresh = max(J.shape) * np.finfo(np.float64).eps * diag[0]
    thresh = max(thresh, tol * max(diag[0], 1.0))
    return int(np.sum(diag > thresh))


def _dependent_rows(J: np.ndarray, rank: int, tol: float) -> List[int]:
    """Row indices of J that are linearly dependent on the independent set.

    Column-pivoted QR of J^T puts the most-independent rows (now columns) first;
    pivot positions from `rank` onward are the dependent rows. Returns them in
    original row order.
    """
    m = J.shape[0]
    if rank >= m or m == 0:
        return []
    _, _, piv = sla.qr(J.T, mode="economic", pivoting=True)
    dep = sorted(int(p) for p in piv[rank:])
    return dep
