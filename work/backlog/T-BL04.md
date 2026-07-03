---
id: T-BL04
title: Fatigue + thermal + buckling + contact FEA (CalculiX)
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL04-calculix-fea
depends_on: []
---

## Context

S-N curve fatigue post-processing on existing static FEA results; CalculiX (GPL,
subprocess-only per CONTRIBUTING.md license rules) for everything PyNite (T-D2)
doesn't cover.

**Expanded 2026-07-03 (Erik-directed):** since CalculiX is being integrated anyway
for thermal, bundle in the other analysis types it natively supports rather than
three separate solver-integration tasks:

1. Fatigue life estimate (S-N curve post-processing on existing `run_fea_static`
   results — no new solver needed, pure post-processing)
2. Thermal steady-state solve via CalculiX subprocess
3. Linear buckling (eigenvalue) via CalculiX — critical for slender members/thin
   panels where static stress alone doesn't catch the failure mode
4. Nonlinear/contact FEA (large deflection, contact between bodies) via CalculiX —
   needed for snap-fits, press-fits, anything where parts touch under load

Expose all four as MCP tools and DAG verify-node end conditions, same pattern as
`run_fea_static`/`run_fea_modal`.

## Definition of done

- [ ] Fatigue life estimate tool on static results
- [ ] Thermal steady-state solve via CalculiX subprocess
- [ ] Linear buckling (eigenvalue) solve via CalculiX
- [ ] Nonlinear/contact FEA (large deflection + contact) via CalculiX
- [ ] All four callable as MCP tools and DAG end conditions
- [ ] Hand-calc-validated per solver (same discipline as T-D2/T-BL01) — no fabricated
      results; degrade honestly (§0 shape) when CalculiX is absent
