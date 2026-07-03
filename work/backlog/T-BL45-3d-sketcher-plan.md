---
id: T-BL45
title: 3D sketcher — OSS survey + design/plan freeze (data model, 3D constraint solver, integration with 2D sketch + features)
workstream: backlog
owner: unassigned
status: todo
priority: 3
lane: modeling-core
kind: new
depends_on: []
branch: task/T-BL45-3d-sketcher-plan
---

## Context

Direction from Hunter (2026-07-03): add a **3D sketcher** to enable more advanced features.
Hunter asked for "the plans to add" it — so THIS task is the DESIGN/PLAN freeze, not the
build (precedent: T-R1 PLM contract freeze). The 3D sketcher is the foundation for advanced
features (3D paths for sweeps, space frames, wire routing, complex lofts); scope it properly
before any build.

## Definition of done (design task — deliverable is a frozen plan doc, no engine build)

- [ ] **OSS-first survey** (per OSS-first directive): evaluate existing 3D geometric
      constraint solvers / 3D sketchers — SolveSpace (note: **GPLv3** — subprocess-only or
      clean-room the approach, never link in-process; license guardrail), FreeCAD's sketcher,
      planegcs, and any others. Summarize approach, license, and fit.
- [ ] **Data model:** define 3D sketch entities (points/lines/arcs/splines free in space, not
      plane-locked), and how they persist in the doc model alongside 2D sketches.
- [ ] **Constraint solver approach:** 3D constraint types + solver choice (build vs
      wrap-OSS-subprocess vs clean-room), with the license path spelled out.
- [ ] **Integration plan:** how 3D sketches feed features (sweep paths, lofts, space frames),
      how they coexist with the 2D sketch improvements (T-BL41/42/43), and the viewport/UX
      implications (3D dragging, plane-free constraints).
- [ ] **Phased build plan:** freeze a plan/contract doc (e.g. `contracts/sketch3d.md` or
      `docs/SKETCH3D_PLAN.md`) with phases + acceptance per phase. Cut the follow-on build
      tasks (deferred until Hunter greenlights) referencing it.
- [ ] No engine build in this task. `graphify update .` after the doc lands.

## Driver notes

- Tier: **OPUS** — design + OSS survey + contract freeze (heavy-review, T-R1 precedent).
- Coordinate with Hunter before freezing — this is foundational and he wants to steer the
  ambitious direction. Treat it like a design proposal for his sign-off, not a fait accompli.
- License guardrails are load-bearing here (SolveSpace/FreeCAD are GPL) — CONTRIBUTING.md:
  GPL/MPL subprocess-only, AGPL never, MIT/BSD/Apache in-process.
- Related to the whole sketch cluster: T-BL41, T-BL42, T-BL43, T-BL44.

## Progress log
