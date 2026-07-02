---
id: T-G7
title: GUI migration wave 3 - core panels (C1-C6, G3 gate)
workstream: G
owner: unassigned
status: todo
branch: task/T-G7-migration-wave3
day: 8
depends_on: [T-G6]
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
5. G3 gate: all 21 `AI_CAD_SHOT` scenarios captured on AI-CAD2, diffed vs host baselines.

## Definition of done

- [ ] All 44 panel_spec forms render data-driven (`AI_CAD_SHOT=pmform AI_CAD_PMCMD=extrude`)
- [ ] All 21 shot scenarios work on AI-CAD2
- [ ] `bash scripts/gate.sh` green; `graphify update .` run

## AI hints

- graphify DOES index ui/abstract — `graphify query "panel spec forms"` before reading.
- The copilot port (C3) should carry the ShopCraft branding and shrunken header from
  T-G2/T-G3, not the old h2-font "AI-CAD Copilot".
