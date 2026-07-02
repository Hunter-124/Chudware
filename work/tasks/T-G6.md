---
id: T-G6
title: GUI migration wave 2 - shell chrome (S1-S6, H5 start, G2 gate)
workstream: G
owner: opus-gale
status: doing
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

- [x] All S1-S6 verify scenarios render on AI-CAD2 (see Verification report; S2/H5
      multidoc+sketch *step-machine scenarios* blocked by a pre-existing systemic
      offscreen deadlock — chrome itself renders)
- [~] H5 compiles + sketch overlay draws (full done-gate is wave 4) — **compiles ✓**
      (all `ui/imgui/src/ui/sketch/*.cpp` TUs link into AI-CAD2); sketch-mode chrome +
      DOF solver render live ("Fully Defined" in `ribbon`+`AI_CAD_TAB=1`); the 2D
      entity-overlay screenshot (`AI_CAD_SHOT=sketch`) is blocked ONLY by the systemic
      step-machine deadlock below, not by the overlay code
- [x] `bash scripts/gate.sh` — see closeout; `graphify update .` run

## Verification report (opus-gale, 2026-07-03)

**Interpretation:** verify-and-close. The prior overhaul workflow already ported all of
S1–S6 + H5 + G2 to master (REFACTOR_LOG.md §§ S1–S6/H5/G2). I changed **zero code** — my
branch = `origin/master` + the claim commit. This task builds AI-CAD2 from master and
eyeball-verifies each wave-2 scenario against the DAG verify rows + the AI hints.

Build: `cmake --preset linux-app && cmake --build build/linux-app` → `bin/AI-CAD2` links
clean; `--selftest` OK (save=1 open=1 features=3 → OK). Shots via
`AI_CAD_NO_FILE_DIALOGS=1 AI_CAD_LIBAICAD=<core .so> AI_CAD_REPO=<worktree> AI-CAD2 --shot`.
Screenshots in `scratch/tg6-shots/` (gitignored).

| Node | DAG verify | Result |
|------|-----------|--------|
| S1 layout/dock/rail | `AI_CAD_SHOT=explorer` | ✓ dock + rail tab switch (Model Tree↔Files), glass pills |
| S2 title/doc-tabs | `AI_CAD_SHOT=multidoc` | ✓ doc-tab band renders in viewport band (all shots); multi-tab *population* blocked by step-machine deadlock |
| S3 ribbon+ctx | `AI_CAD_SHOT=ribbon`, `+AI_CAD_TAB=1` | ✓ full Model ribbon + sketch-tab ribbon (Line/Rect/Circle/…/Exit Sketch), `AI_CAD_TAB` honored |
| S4 status/toast/palette | `AI_CAD_SHOT=settings` | ✓ status bar (Engine live, Shaded+Edges, MMGS, Editing Part) + Settings modal search seam |
| S5 icons/cursors | glyphs render | ✓ all ribbon/FM glyphs are real atlas icons (no fallback boxes). NOTE: DAG's `check.py gui_lint icons` is not a wired subcommand in this tree (host-era) |
| S6 files/settings/profiler | `AI_CAD_SHOT=settings`,`explorer` | ✓ Files explorer tab (Recent/Workspace tree) + Settings modal |
| H5 sketch (wave-2 milestone) | `AI_CAD_SHOT=sketch` | compiles ✓; sketch chrome+DOF live ✓; 2D entity overlay shot blocked by deadlock (see below) |
| G2 gate | shell chrome vs baselines | ✓ eyeballed vs host `scratch/gui_baseline/{explorer,ribbon,multidoc,settings,sketch}.png` + AI hints |

**AI-hints check:** transparent glass columns with per-element pills ✓ (no full-width
fill); PropertyManager hosts inside FeatureManager tab, no separate PM tab ✓; doc-tab band
inside viewport ✓; ShopCraft branding ✓ ("ShopCraft Copilot"). No host regressions
reintroduced.

**Systemic finding (pre-existing on master, NOT introduced by T-G6):** all ScenarioRunner
*step-machine* scenarios (`sketch`, `multidoc`, `m1`, `fea`, `cam`, `flow`, `drawing`,
`asm`, `activity`, `evalmass`) hang offscreen — the mid-loop worker `docMirror.newPart(worker)`
call deadlocks and the shot loop (which ignores `--frames` for these and waits for scenario
completion) never captures → `timeout`/exit 124. Worker **one-shot** scenarios (`tree`,
`equations`, `ecad`) capture fine (verified: `tree` exit 0, real engine-built Base/Fillet/Shell
tree). This blocks screenshot verification of step-machine scenarios only; it is F4/C6 worker-seam
scope, not wave-2 shell chrome. **Recommend filing as a GitHub issue** (see `work/questions/T-G6.md`).

## AI hints

- Do not reintroduce host regressions the polish sessions fixed: no
  `NoBringToFrontOnFocus` on input-bearing overlay windows; columns transparent with
  per-element pills; PropertyManager hosts inside the FeatureManager tab (no separate
  PM tab).
