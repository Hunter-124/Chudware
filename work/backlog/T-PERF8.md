---
id: T-PERF8
title: Sketcher solver interactive mode — LDLT-first, in-trial deadline, single-QR redundancy, tangency warm-start
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF8-solver-interactive-mode
depends_on: []
lane: modeling-core
kind: fix
---

## Context

docs/PERF_AUDIT_2026-07-10.md §5 P2. DECISION: all four relaxations must be gated behind an interactive-mode flag to keep Python-oracle parity on the exact path. (a) JacobiSVD per damping trial (nsolve.cpp:335, up to 12x/iter) → LDLT-first + rcond, SVD fallback (or BDCSVD minimum). (b) deadline checked only between outer iterations (365) → check in trial loop. (c) dependent_rows = repeated full QRs (96-124), guaranteed triggered by kept redundant coincidents (session.cpp:9629) → single ColPivQR attribution or on-demand only. (d) spline tangency 625-pt mutual_closest scan per evaluation (sketch.cpp:571-620) → warm-start (p,q) within a solve.

## Definition of done

- [ ] interactive flag plumbed; oracle/parity path byte-identical
- [ ] 200-entity sketch drag solve <10ms budget actually held

## Progress (2026-07-10)

Landed (all gated on interactive flag; default/exact path unchanged):
- `System::set_interactive` / `solve_sketch(..., interactive=false)`
- drag always interactive; apply_ops auto-coincident (budgeted) interactive=true
- (a) LDLT-first + rcond, SVD fallback in nsolve
- (b) deadline check inside LM damping trial loop when interactive
- (c) analyze uses single ColPivQR dependent-row attribution when interactive
- (d) curve-curve tangency warm-starts mutual_closest (p,q) within a solve
