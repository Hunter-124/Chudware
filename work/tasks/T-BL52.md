---
id: T-BL52
title: Reconcile HSS imperial takeoff designations with the mm section-geometry grammar
workstream: BL
owner: unassigned
status: todo
branch: task/T-BL52-hss-imperial-mm-reconcile
depends_on: [T-BL21]
lane: modeling-core
kind: fix
priority: 4
deferred: yes
---

## Context

T-BL21 (merged) made `section_profile`/`element_to_brep` mm-only for hollow
sections, and closed the dangerous hole where an imperial fractional string
(`HSS4X4X1/4`) could silently tokenize into a plausible-but-wrong mm solid (now a
safe per-type default). But `estimate._AISC_LBS_PER_FT` still keys mass off the
imperial AISC catalog format, so an element named `HSS4X4X1/4` gets **correct
takeoff mass** (exact string match) but a **generic default-rectangle solid** —
geometry and cost disagree about the same member. See work/questions/archive/T-BL21.md A1
(foreman ruling: accept mm-only, defer this reconciliation here).

**Current code (2026-10-04):** the Python modules named on this card were ported to C++.
The mass table is now `kAiscLbsPerFt` in `core/structural/estimate.cpp`, which still keys
`HSS4X4X1/4`-style imperial strings. The hollow-section grammar is in
`core/structural/model.cpp::section_properties`, where `HSS`/`RHS` require numeric `WxHxT`.
Before starting, re-check what the C++ geometry side does with an imperial fractional name;
the "default rectangle" behavior above was observed in the pre-port Python code.

## Scope

- Convert `kAiscLbsPerFt`'s imperial designations to their mm outer×wall
  equivalents (e.g. `HSS4X4X1/4` -> `HSS101.6x101.6x6.35`) so a single
  `section_name` drives BOTH the geometry engine and the mass table.
- Either add the mm designation strings to the lbs/ft table, or route mass lookup
  through `research_standard_section` for the canonical section props.
- Keep a compatibility alias so existing docs/tests naming `HSS4X4X1/4` still
  resolve (map old imperial key -> new mm key at lookup).

## Definition of done

- [ ] One `section_name` yields consistent geometry AND mass for an HSS member;
      regression test asserts a named imperial HSS gets its TRUE hollow solid
      (not the default rectangle) and matching catalog mass.
- [ ] Existing `HSS4X4X1/4`-style names still resolve (alias test).
- [ ] scripts/gate.sh green; graphify update.

## Notes

Deferred/priority-4: correctness-improving but not blocking; BL21 ships without it.
