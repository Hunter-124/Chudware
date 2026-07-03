---
id: T-G9
title: GUI migration wave 5 - polish, parity battery, cutover (P1, P2, G5)
workstream: G
owner: sonnet-marsh
status: doing
branch: task/T-G9-migration-cutover
day: 10
priority: 4
depends_on: [T-G8, T-G4, T-G12]
---

## Context

Final Linux wave of `docs/GUI_MIGRATION_DAG.md`: P1 adaptive-glass sweep (every
over-viewport surface on `glassPanel()`/`adaptiveSurface()`, shortcomings list in
`docs/GLASS_DESIGN_SPEC.md` cleared), P2 full 21-scenario screenshot battery on both
binaries with sign-off per pair, then the G5 cutover: retarget `scripts/check.py`,
`scripts/feature_surface_audit.py`, `scripts/parity_audit.py` to `ui/imgui` +
`ui/abstract`, and mark host EOL in `AGENTS.md`. After this task, AI-CAD2 (ShopCraft)
is the production GUI on Linux; `host/` survives only until T-G11's Windows check.

## Files / areas touched

- `ui/imgui/src/theme/**` + flagged panel files (P1)
- `scripts/check.py`, `scripts/feature_surface_audit.py`, `scripts/parity_audit.py`,
  `AGENTS.md` (G5)
- `scratch/gui_baseline2/` (output only, gitignored)

## Steps

1. Read `docs/GUI_MIGRATION_DAG.md` P1/P2/G5 and `docs/GLASS_DESIGN_SPEC.md` shortcomings.
2. P1 sweep; verify with `python3 scripts/check.py gui_lint theme`.
3. P2 battery: all 21 scenarios on both binaries, human-eye sign-off per pair (this is
   the "final human-eye screenshot pass" debt from `HANDOFF_GUI_OVERHAUL.md` §5.3 — plus
   the ~10-minute manual interactive pass: resize hit-zones, PM slider ctrl-click, chip
   firing).
3b. ELEMENT-BY-ELEMENT HUMAN WALKTHROUGH (Erik, mandatory before cutover): re-run the
   T-G12 inventory checklist against the NEW binary — every ribbon button, PM form,
   settings option, panel, and dialog gets a human keep/fix verdict. Unresolved
   must-fix rows block G5.
4. G5: retarget the audits, flip gate.sh to build+smoke AI-CAD2 as primary, note host
   EOL in AGENTS.md and host/DEPRECATED.md (criteria 1-4 there must all be green).

## Definition of done

- [ ] gui_lint theme clean; no over-viewport surface off the adaptive-glass API
- [ ] 21/21 scenario pairs signed off; interactive manual pass done
- [ ] T-G12 loop CONVERGED on the new binary: a full human walkthrough round with zero new must-fix findings (recursive protocol in T-G12 — rounds repeat until this holds)
- [ ] host/DEPRECATED.md deletion criteria 1-4 all green
- [ ] `bash scripts/gate.sh` green scanning the new tree; `graphify update .` run

## AI hints

- The `feature_surface_audit`/`parity_audit` failures were PRE-EXISTING against host —
  after retargeting they must actually pass, that's the point of the gate.

## Scope additions (foreman, 2026-07-04, at T-G8 merge)

- The 5 parity diff harnesses deferred from T-G8's DoD (G-code ×8 post dialects,
  netlist+ERC, DXF/PDF, study JSON, sketch-solver op-batch) — engine RPCs are
  byte-identical to host per the finisher, so these should diff clean; build the
  harness infra in tools/ and prove it.
- De-literal the G8-ported panels: 66 IM_COL32 color literals absorbed into
  scripts/ui_style_check_baseline.json as EXPLICIT DEBT at merge — replace with
  theme:: tokens and shrink the baseline back toward the original 18.

## Progress log
- 2026-07-03 06:56 UTC [sonnet-marsh] CHECKPOINT a001ecb pushed: scope-addition 2 DONE — all 83 wave-4 color literals de-literaled into theme::draw2d/camview/featcat/etc domain tokens (byte-identical values), preview_pane ifdef moved to platform layer, ui_style_check baseline 63 keys -> 0 (beats the 'toward 18' target). Build green, drawing/cam/tree shots eyeballed unchanged. Next: the 5 parity diff harnesses in tools/.
- 2026-07-03 07:43 UTC [sonnet-marsh] MILESTONE: both foreman scope additions COMPLETE and pushed. (1) All 5 parity harnesses built (tools/gui_parity/) and clean-room PASS 22/22: gcode 8 dialects byte-identical, netlist+ERC JSON identical, DXF 45598B + PDF 12898B byte-identical, study 15+14 identical, sketch op-batch 16+10 identical. Five REAL AI-CAD2-side divergences found+fixed en route (cam/sketch/drawing/ecad scenario gaps incl. a dead ecad_orchestrate call, plus an H4 SimManager regression: dropped captureSelectionInto in sim_fixture/load commands). (2) Color debt de-literaled: 83 literals -> theme domain tokens, baseline 63 keys -> 0 (beats 'toward 18'). P1 gui_lint icons+theme clean. gate.sh running; then status review. Remaining for humans/next session: P2 21-pair sign-off sheet + interactive pass, T-G12 convergence walkthrough (Erik), G5 audit retarget + cutover (G12-gated).
- 2026-07-03 07:54 UTC [sonnet-marsh] REVIEW: branch task/T-G9-migration-cutover pushed @2d014b4, status review. gate.sh RESULT PASS (pre-existing FAIL-RECORDED profile unchanged; ui_style_check advisory now clean PASS). Both foreman scope additions DONE: (1) 5 parity harnesses in tools/gui_parity/ — clean-room 5/5 PASS, 22/22 checks, all artifacts byte-identical (gcode x8 dialects, netlist+ERC, DXF+PDF, study JSON, sketch op-batch); found+fixed 5 real AI-CAD2-side divergences incl. an H4 SimManager selection-capture regression and a dead ecad_orchestrate call. (2) 83 color literals -> theme domain tokens, baseline 63->0 keys. Write-set extensions (worker_client trace hook + sim_manager fix) documented in work/questions/T-G9.md with rationale. NOT closed (human-gated): P2 formal 21-pair sign-off + interactive pass, T-G12 convergence walkthrough (Erik), G5 audit-retarget/cutover. Task file has full HANDOFF section. Worktree ../CAD-T-G9 left in place (task not done — G12-gated remainder).
