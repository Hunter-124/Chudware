---
id: T-BL21
title: Hollow structural sections - model true wall voids (RHS/HSS) for mass/interference correctness
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL21-hollow-sections
depends_on: []
lane: modeling-core
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-D1 Q3).** `section_profile` extrudes
hollow structural sections (RHS/HSS/pipe) as their solid outer envelope; wall
thickness lives only in `section_name` for takeoff. Accepted for v1, but every hollow
member currently has wrong volume, mass, and interference geometry — this bites any
future mass-properties readout, clash check, or rendering-fidelity work on structural
docs.

## Definition of done

- [ ] Hollow section profiles (RHS, HSS, CHS/pipe at minimum) generate true
      outer-minus-inner prisms (sketch with inner void or equivalent boolean)
- [ ] Mass/volume of a reference member matches handbook value within tolerance
      (cited reference, byte-exact expected literal — T-D3 discipline)
- [ ] check_interference on two touching hollow members reflects real wall geometry
- [ ] Takeoff/cost path (T-D5) still produces identical weights (it already uses
      per-length handbook mass, not solid volume — verify, don't assume)

## AI hints

- `graphify query "section_profile structural member extrusion"` — where the solid
  envelope is built today
- `work/questions/T-D1.md` Q3 — the original deferral and its rationale
