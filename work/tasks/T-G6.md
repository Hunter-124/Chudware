---
id: T-G6
title: GUI migration wave 2 - shell chrome (S1-S6, H5 start, G2 gate)
workstream: G
owner: unassigned
status: todo
branch: task/T-G6-migration-wave2
day: 7
depends_on: [T-G5]
---

## Context

Second wave of `docs/GUI_MIGRATION_DAG.md`: shell chrome into `ui/imgui` — S1 layout/
dock/rail, S2 title bar + doc tabs, S3 ribbon + context toolbar, S4 status/toast/
shortcut/palette, S5 icons atlas + cursors, S6 files/settings/profiler — plus the start
of H5 sketch mode (the long pole, compile + overlay milestone only this wave), closed by
the G2 integrator gate.

## Files / areas touched

- `ui/imgui/src/ui/**` per each node's write-set in the DAG doc (disjoint per leaf)
- `ui/imgui/src/theme/icons.*`, `ui/imgui/assets/` (S5)
- Integrator gate only: `ui/imgui/src/app/application.cpp`, `ui/imgui/CMakeLists.txt`

## Steps

1. Read `docs/GUI_MIGRATION_DAG.md` S1-S6 + H5 + G2 and `docs/GLASS_DESIGN_SPEC.md`.
2. Port from the CURRENT host/src (post T-G1/T-G2/T-G3 — includes transparent columns,
   collapse-to-pill, doc-tab input fix, ShopCraft branding). The DAG's Source rows
   predate those fixes; the live tree wins where they differ.
3. Run leaves as parallel subagents with the DAG's write-sets and verify scenarios.
4. G2 gate: screenshot-diff shell chrome vs `scratch/gui_baseline/` set.

## Definition of done

- [ ] All S1-S6 verify scenarios render on AI-CAD2
- [ ] H5 compiles + sketch overlay draws (full done-gate is wave 4)
- [ ] `bash scripts/gate.sh` green; `graphify update .` run

## AI hints

- Do not reintroduce host regressions the polish sessions fixed: no
  `NoBringToFrontOnFocus` on input-bearing overlay windows; columns transparent with
  per-element pills; PropertyManager hosts inside the FeatureManager tab (no separate
  PM tab).
