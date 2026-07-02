---
id: T-BL01
title: V&V benchmark harness
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL01-vv-harness
depends_on: []
---

## Context

Run MacNeal-Harder (public domain), CoFEA, and NASA TMR cases in CI against our FEA/CFD solvers; auto-generate a V&V report citing ASME V&V 10/20 methodology. CalculiX (subprocess) as a second structural opinion.

## Definition of done

- [ ] CI job runs the benchmark suite and fails on tolerance regression
- [ ] Generated V&V report (md/pdf) with case-by-case error tables
- [ ] Never bundles NAFEMS/ASME documents themselves
