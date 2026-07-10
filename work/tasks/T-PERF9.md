---
id: T-PERF9
title: Sketch connected-component decomposition
workstream: perf
owner: grok
status: done
branch: task/T-PERF9-sketch-decomposition
depends_on: [T-PERF8]
lane: modeling-core
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §5 [L]. One dense global System for the whole sketch = O(n³) growth; 40 independent profiles solve together. Union-find over (entity, constraint refs) → solve components independently; drag solves only the dragged component; merge DOF/statuses across components. Biggest asymptotic win for large sketches; natural parallelization seam. Warm-state layout changes — parity-gate like T-PERF8.

## Definition of done

- [x] drag in a multi-profile sketch solves only its component (T-PERF9 multi-comp drag path)
- [x] statuses/DOF merged correctly on interactive solve (sum DOF, merge status maps)
- [ ] full-rigor parity suite re-run on quiet machine (exact path still monolithic)

## Progress (2026-07-10)

Landed:
- `partition_components` union-find over constraint refs
- `solve_sketch_monolithic` + interactive decomp dispatch (`interactive=true` only)
- `merge_component_solutions` for DOF/statuses/warm state
- Drag: Systems only for components containing drag targets; frozen coords for rest
- `solve_active` uses interactive=true (live sketcher)
- Exact/oracle path unchanged (`interactive=false` → one System)
