---
id: T-BL45
title: 3D sketcher — OSS survey + design/plan freeze (data model, 3D constraint solver, integration with 2D sketch + features)
workstream: backlog
owner: opus-corm
status: review
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

- [x] **OSS-first survey** (per OSS-first directive): evaluate existing 3D geometric
      constraint solvers / 3D sketchers — SolveSpace (note: **GPLv3** — subprocess-only or
      clean-room the approach, never link in-process; license guardrail), FreeCAD's sketcher,
      planegcs, and any others. Summarize approach, license, and fit.
- [x] **Data model:** define 3D sketch entities (points/lines/arcs/splines free in space, not
      plane-locked), and how they persist in the doc model alongside 2D sketches.
- [x] **Constraint solver approach:** 3D constraint types + solver choice (build vs
      wrap-OSS-subprocess vs clean-room), with the license path spelled out.
- [x] **Integration plan:** how 3D sketches feed features (sweep paths, lofts, space frames),
      how they coexist with the 2D sketch improvements (T-BL41/42/43), and the viewport/UX
      implications (3D dragging, plane-free constraints).
- [x] **Phased build plan:** freeze a plan/contract doc (e.g. `contracts/sketch3d.md` or
      `docs/SKETCH3D_PLAN.md`) with phases + acceptance per phase. Cut the follow-on build
      tasks (deferred until Hunter greenlights) referencing it.
- [x] No engine build in this task. `graphify update .` after the doc lands.

## Driver notes

- Tier: **OPUS** — design + OSS survey + contract freeze (heavy-review, T-R1 precedent).
- Coordinate with Hunter before freezing — this is foundational and he wants to steer the
  ambitious direction. Treat it like a design proposal for his sign-off, not a fait accompli.
- License guardrails are load-bearing here (SolveSpace/FreeCAD are GPL) — CONTRIBUTING.md:
  GPL/MPL subprocess-only, AGPL never, MIT/BSD/Apache in-process.
- Related to the whole sketch cluster: T-BL41, T-BL42, T-BL43, T-BL44.

## Progress log

- 2026-07-04 opus-corm: OSS survey done (SolveSpace GPLv3/native-3D, planegcs LGPL/2D-only,
  ezpz MIT/2D-only, Frontier GPL/broken-3D, Ceres BSD-3/numeric-backbone). Conclusion: no
  permissive in-process 3D solver exists -> BUILD-OWN clean-room, extend our existing 2D Newton
  solver (core/sketch, itself a clean-room port) to 3D. Plan frozen at docs/SKETCH3D_PLAN.md as a
  PROPOSAL for Hunter sign-off. Data model = new `sketch3d` feature type (plane-free [x,y,z]).
  Integration is largely already wired: sweep `refs.path: sel(edge, region-chain)` already accepts
  a 3D edge body (helix precedent) -> feature integration is mostly emit-an-edge-body. Cut 5
  deferred build tasks T-BL51..T-BL55 (P1-P5). DECISION-NEEDED for Hunter: (A) parity oracle
  Python-first vs C++-first, (B) build-own vs GPL-subprocess, (C) near-term scope/greenlight order.
  Status -> review; doc-only, no engine build.
- 2026-07-04 opus-corm: RENUMBER — follow-on tasks originally cut as T-BL49..T-BL53 collided
  with T-BL49-interface-fitment-checker / T-BL50-layout-first-critical-dims cut earlier today
  on master (different filenames, same ids — silent duplicate-id risk). Renumbered to
  T-BL51..T-BL55 (P1→T-BL51 core-solver, P2→T-BL52 curves, P3→T-BL53 features, P4→T-BL54 gui,
  P5→T-BL55 advanced); depends_on chain, branch names, and all refs in docs/SKETCH3D_PLAN.md
  updated. RULE (future task-cutting): check BOTH work/backlog/ and work/tasks/ on up-to-date
  origin/master for the next free id — ids are global across the board.
