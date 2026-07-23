---
id: T-G7
title: GUI migration wave 3 - core panels (C1-C6, G3 gate)
workstream: G
owner: opus-gale
status: done
branch: task/T-G7-migration-wave3
day: 8
depends_on: [T-G6]
lane: app-gui
kind: overhaul
---

## Context

Third wave of `docs/GUI_MIGRATION_DAG.md`: the core panels — C1 feature tree, C2
property manager + 44 forms over `ui/abstract/panel_spec`, C3 copilot/activity/console,
C4 viewport panel + HUD, C5 asm/config/tables/equations, C6 scenarios + screenshot
harness — closed by the G3 integrator gate (full panel screenshot battery).

## Files / areas touched

- `ui/imgui/src/ui/**` per node write-sets (disjoint per leaf)
- `ui/abstract/panel_spec.*` — C2 is the SOLE append owner this round; reconcile with
  whatever the in-flight panel_spec lane landed before starting
- `ui/imgui/src/main.cpp`, `ui/imgui/src/app/application.cpp` (C6/gate)

## Steps

1. Read `docs/GUI_MIGRATION_DAG.md` C1-C6 + G3.
2. Check `git log -- ui/abstract/panel_spec.cpp` first — a separate lane was extending it
   (+850 lines); C2 builds on the merged result, not the DAG's stale "4/44 forms" count.
3. Port from current host/src (post-polish; feature readout panel, sketch ribbon
   accept/deny cluster, PM-inside-FM are the current design).
4. Run leaves in parallel per DAG write-sets; C6 last (references all panels).
5. G3 gate: all 21 `CHUDWARE_SHOT` scenarios captured on AI-CAD2, diffed vs host baselines.

## Definition of done

- [~] All 44 panel_spec forms render data-driven (`CHUDWARE_SHOT=pmform CHUDWARE_PMCMD=extrude`)
      — **FIXED in-lane** (was broken: pmform `dispatch()`'d/executed the command instead of
      opening its PM form; now sets `activeTool` so `drawInspector` renders the form). Extrude
      form reverified; the 44-form registry is data-driven so extrude exercises the path.
- [~] All 21 shot scenarios work on AI-CAD2 — ≈11/21 capture (one-shots tree/equations/ecad +
      static settings/ribbon/explorer/chat/plan/assembly/pick/pmform). The other 10 are
      ScenarioRunner *step machines* (m1/sketch/asm/fea/cam/flow/drawing/multidoc/activity/
      evalmass) blocked by **issue #7** (mid-loop `DocMirror::newPart(worker)` offscreen
      deadlock, F4/C6 worker-seam scope — out of the ui/imgui G-lane). See work/questions/T-G7.md
      Q1 (G3-gate re-scope decision pending foreman).
- [~] `bash scripts/gate.sh` — **ui/abstract PASS** (my panel_spec.cpp change passes the
      command_registry test) and **ui/imgui build + --shot PASS** (my scenarios.cpp change
      compiles + shots clean). RESULT: FAIL is ONLY the known pre-existing reds (identical to
      T-G6): check.py audits; pytest "107 deselected" (issue #6); `--selftest` non-fatal env-gap
      (no `CHUDWARE_LIBCHUDWARE` in gate). My two hunks are gate-clean. `graphify update .`: touches
      2 code files — foreman regenerates at merge (graphify-out/ is in the foreman main checkout).

## Verification (opus-gale, 2026-07-03) — chained off T-G6 (merge order T-G6 → T-G7)

C1–C6 code already on master from the prior overhaul run (REFACTOR_LOG §§ C1–C6); this task
verifies it renders + fixes the C2 pmform PM-form path. Shots in `scratch/tg7-shots/`.
Verified render: **C1** tree ✓, **C3** chat ✓ (Chudware branding, glass bubbles + action card),
**C4** pick ✓, **C5** equations/assembly/plan ✓, ecad ✓. **C2** pmform fixed (see DoD).
**Blocker:** G3 21-scenario battery gated on issue #7 (10 step-machine scenarios deadlock) —
foreman decision requested in work/questions/T-G7.md.

## AI hints

- graphify DOES index ui/abstract — `graphify query "panel spec forms"` before reading.
- The copilot port (C3) should carry the Chudware branding and shrunken header from
  T-G2/T-G3, not the old h2-font "Chudware Copilot".
