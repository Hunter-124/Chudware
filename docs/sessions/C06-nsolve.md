# C06 — nsolve: the shared numeric constraint core

Model: **opus** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
`engine/doc/nsolve.py`: ONE residual-based solver core that the 2D sketch solver (C10) and the
3D mate solver (C20) parameterize. Damped Newton / Levenberg-Marquardt over sparse analytic
Jacobians, warm starts, DOF analysis, redundant-constraint identification. This is the
make-or-break component for SolidWorks feel — diagnostics quality matters as much as convergence.

## Read first
1. `docs/PLAN.md` §3.1 (one numeric solver core; budgets: warm drag <10 ms @ ~200 unknowns)
2. `docs/contracts/doc_schema.md` §3.3–3.4 (what constraint diagnostics must support)

## Owned paths
`engine/doc/nsolve.py` · `engine/doc/test_nsolve.py`

## Deliverables
1. API:
   - `System()`: `add_vars(n, init) -> slice/indices`; `add_residual(rfunc, var_idx, jfunc,
     tag)` where `rfunc(x_sub) -> np.ndarray(m)` and `jfunc(x_sub) -> np.ndarray(m, k)` (dense
     per-residual block; assembled sparse via scipy.sparse); `tag` is an opaque id (constraint
     id) used in diagnostics.
   - `solve(x0=None, tol=1e-10, max_iter=50, time_budget_ms=None) -> Result {x, converged,
     iterations, max_residual}` — LM with adaptive damping; warm start = pass previous x.
     `time_budget_ms` returns best-so-far when exceeded (drag path).
   - `analyze(x) -> Analysis {dof: int, redundant_tags: list, conflicting_tags: list,
     well_posed: bool}` — rank via QR (dense fine ≤ ~600 vars; use
     `scipy.linalg.qr(J.toarray(), pivoting=True)`): DOF = n_vars − rank; redundancy = rows
     dependent on others (identify via small pivots / row-space test), map back to tags;
     conflicting = redundant AND residual not satisfiable (residual norm stays high) — the
     distinction drives "over-defined (red)" vs "redundant (warn)" UI states.
   - Soft target residual helper for dragging: `soft_target(idx, target, weight=0.01)` —
     a low-weight residual pulling a point toward the mouse without fighting constraints.
2. Numerics: scale-invariant damping; guard singular J (pseudo-inverse step or increased
   damping); all float64; NO finite differences anywhere (analytic only — callers supply J).
3. `test_nsolve.py`:
   - point-on-line + distance system converges from rough init; warm-started resolve after a
     small perturbation takes ≤3 iterations
   - DOF: free point in 2D = 2; with fix → 0; rectangle-like system (8 vars, 7 independent
     constraints) → dof 1
   - redundancy: duplicate distance constraint → its tag in `redundant_tags`, well_posed True
   - conflict: contradictory distances (10 vs 12 on same pair) → conflicting_tags non-empty
   - performance: synthetic chain of 100 points / ~200 vars with coincident+distance
     constraints: warm solve < 10 ms median over 20 runs (print the number; assert < 50 ms to
     keep CI margin, report actual)
   - soft drag: target pulls free endpoint, constraints win over the soft term

## Constraints
- numpy + scipy only. No sketch-specific or 3D-specific knowledge leaks in (entity
  parameterizations live in the callers).

## Verification
`& $py -m engine.doc.test_nsolve` green; print the perf medians in your report.
