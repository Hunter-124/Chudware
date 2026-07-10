---
id: T-PERF9
title: Sketch connected-component decomposition
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF9-sketch-decomposition
depends_on: [T-PERF8]
lane: modeling-core
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §5 [L]. One dense global System for the whole sketch = O(n³) growth; 40 independent profiles solve together. Union-find over (entity, constraint refs) → solve components independently; drag solves only the dragged component; merge DOF/statuses across components. Biggest asymptotic win for large sketches; natural parallelization seam. Warm-state layout changes — parity-gate like T-PERF8.

## Definition of done

- [ ] drag in a 40-profile sketch solves only its component
- [ ] statuses/DOF merged correctly (parity suite green in full-rigor mode)
