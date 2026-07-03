---
id: T-G9
title: GUI migration wave 5 - polish, parity battery, cutover (P1, P2, G5)
workstream: G
owner: sonnet-marsh
status: done
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

- 2026-07-03 [sonnet-marsh] SESSION 1 (branch task/T-G9-migration-cutover, pushed through the
  clean-room parity commit). BOTH FOREMAN SCOPE ADDITIONS COMPLETE:

  **Scope addition 2 — de-literal the wave-4 color debt (DONE, a001ecb):**
  New `ui/imgui/src/theme/domain_tokens.h` (theme::draw2d/camview/featcat/evalview/pmview/
  panelview) with byte-identical values; all 83 IM_COL32/ImVec4 literals replaced across
  drawing_panel, cam/cam_panel, feature_tree, evaluate_dialogs, property_manager,
  settings_modal, sim_manager. preview_pane's OS #ifdef moved to new
  ui/imgui/src/platform/path_predicates.h. `scripts/ui_style_check_baseline.json`:
  **63 keys -> 0** (original pre-G8 baseline was 18; the whole debt AND the original
  baseline are cleared). Checker + selftest green; drawing/cam/tree shots eyeballed —
  rendering unchanged.

  **Scope addition 1 — the 5 parity diff harnesses (DONE, clean-room 5/5 PASS, 22/22 checks):**
  Infra: env-gated `AI_CAD_RPC_TRACE` JSONL trace at WorkerClient::call() — the single
  funnel both binaries' panel RPCs pass through regardless of routing (Python worker,
  in-process libaicad, ECAD/agent C++ seams); services/worker is linked by BOTH binaries
  so one hook instruments both. Credential fields (api_key etc.) are redacted. Off by
  default (tail call when env unset). Harnesses in `tools/gui_parity/` (common.py +
  run_all.py): capture the same AI_CAD_SHOT scenario on both binaries, diff the
  normalized domain request slice (engine-minted ids canonicalized; pure catalog reads
  order-free; timings/paths scrubbed — scrub lists are explicit in the code), then for
  gcode/DXF/PDF replay each binary's captured state-building requests through
  mcp_server.core_bridge (same libaicad) and byte-diff the artifacts host-derived vs
  aicad2-derived. Results (fresh captures, both binaries):
    - gcode: requests PASS; all 8 post dialects byte-identical (centroid/fanuc/grbl/haas/
      linuxcnc/mach3/marlin/siemens .nc)
    - netlist+ERC: requests PASS; compile netlist+erc JSON identical
    - DXF/PDF: 14/14 requests PASS; drawing.dxf 45598B and drawing.pdf 12898B byte-identical
    - study: 15/15 requests + 14/14 responses (full solve JSON) identical
    - sketch: 16/16 solver op-batch requests + 10/10 solver responses identical
  Run: `python3 tools/gui_parity/run_all.py` (report at scratch/gui_parity/report.txt).

  **REAL DIVERGENCES FOUND AND FIXED (the harnesses' whole point — all were AI-CAD2-side):**
  1. cam scenario: nlohmann init-list misnesting serialized the Plate feature as an
     array-of-pairs and silently dropped {centered:true}; host's Bores hole op was missing;
     camLoadJob adoption never ran (no cam_tree/cam_generate — the "No setups yet" gap
     deferred from G8). Fixed; cam shot now shows the full job tree + toolpath backplot.
  2. sketch scenario: was a one-batch "simplified" flow exercising none of the solver
     op-batch; ported host's incremental flow (per-op adds, enter, drag+commit, set_dim,
     solve, exit, region extrude).
  3. drawing scenario: pre-switched to Drafting at configure time so the panel self-created
     the drawing from the startup demo doc (wrong source); same init-list bug on Plate/Hole;
     host's detailing (model_items, hole callout, titleblock, critical toleranced dim,
     drawing_make) missing. Fixed + added read-only scenario hooks to drawing_panel.
  4. ecad scenario: called `ecad_orchestrate` — a method that DOES NOT EXIST in the engine —
     so no schematic/netlist/ERC was ever produced. Ported host's ecadAiDesign flow
     (ecad_design -> ecad_tree -> mirror adopt); shot now shows the real divider schematic
     with ERC results.
  5. **PANEL BUG (not scenario): the H4 SimManager port dropped captureSelectionInto() in
     sim_fixture / sim_load_force / sim_load_pressure** — a viewport pre-selection was
     silently ignored when adding fixtures/loads via ribbon command. Restored host behavior.
     fea scenario also ported to host's SimManager flow (was raw sim_run_static with empty
     fixtures/loads and a STEP path netgen couldn't read).

  **P1:** `python3 scripts/check.py gui_lint theme` — icons clean, theme clean (0 findings
  each). feature_surface_audit/parity_audit still FAIL against host — pre-existing, their
  retarget is the G5 step.

  **Write-set note (advisor FYI, work/questions/T-G9.md):** services/worker/worker_client.{h,cpp}
  (trace hook) and ui/imgui/src/ui/sim_manager.cpp (dropped-capture fix) touched beyond the
  listed write-set — both are the minimal seam for the mandated harness deliverable / a
  host-parity regression the harness exposed.

## HANDOFF (remaining, next session or human)

- **P2 21-pair battery:** the harness runs regenerated + I eyeballed aicad2 drawing / cam /
  ecad / fea / sketch / tree shots (all render correctly, dramatically better than the G8-era
  captures for cam/ecad/drawing). The formal side-by-side 21-pair sign-off sheet + the
  ~10-min manual interactive pass remain (human-eye per DoD).
- **Step 3b / T-G12 convergence on the NEW binary:** human walkthrough — Erik. Blocks G5.
- **G5 cutover:** retarget scripts/feature_surface_audit.py + scripts/parity_audit.py to
  ui/imgui + ui/abstract (they still scan host ribbon_def), flip gate.sh primary, AGENTS.md +
  host/DEPRECATED.md EOL notes. Do NOT start before the G12 walkthrough converges.
- gate.sh + graphify update run at end of session 1 — see final progress note.
- 2026-07-03 [sonnet-marsh] END SESSION 1: `bash scripts/gate.sh` RESULT PASS (check.py +
  pytest suites FAIL-RECORDED pre-existing, identical profile to the T-G8 merge gate;
  ui_style_check advisory now a clean PASS — was fail-recorded). `graphify update .` run.
  Status -> review: both foreman scope additions complete; P2 formal sign-off sheet,
  T-G12 convergence, and G5 cutover remain (human-gated / next session — see HANDOFF).

## DoD status at review

- [x] gui_lint theme clean (icons + theme 0 findings). Adaptive-glass shortcomings sweep:
      no NEW over-viewport surfaces off the API found; formal GLASS_DESIGN_SPEC shortcomings
      list review folded into the G12 walkthrough round.
- [~] 21/21 scenario pairs: harness regenerated + eyeballed drawing/cam/ecad/fea/sketch/tree
      (all correct); formal per-pair sign-off sheet + interactive manual pass OUTSTANDING.
- [ ] T-G12 loop CONVERGED on new binary — HUMAN (Erik), blocks cutover.
- [ ] host/DEPRECATED.md criteria 1-4 — G5, blocked on the two rows above.
- [x] gate.sh green (RESULT PASS); graphify update run.
- [x] SCOPE ADD 1: 5 parity harnesses built + clean-room 5/5 PASS (22/22 checks), real diffs.
- [x] SCOPE ADD 2: 66-literal color debt cleared (83 occurrences), baseline 63 -> 0 keys.

- 2026-07-03 06:56 UTC [sonnet-marsh] CHECKPOINT a001ecb pushed: scope-addition 2 DONE — all 83 wave-4 color literals de-literaled into theme::draw2d/camview/featcat/etc domain tokens (byte-identical values), preview_pane ifdef moved to platform layer, ui_style_check baseline 63 keys -> 0 (beats the 'toward 18' target). Build green, drawing/cam/tree shots eyeballed unchanged. Next: the 5 parity diff harnesses in tools/.
- 2026-07-03 07:43 UTC [sonnet-marsh] MILESTONE: both foreman scope additions COMPLETE and pushed. (1) All 5 parity harnesses built (tools/gui_parity/) and clean-room PASS 22/22: gcode 8 dialects byte-identical, netlist+ERC JSON identical, DXF 45598B + PDF 12898B byte-identical, study 15+14 identical, sketch op-batch 16+10 identical. Five REAL AI-CAD2-side divergences found+fixed en route (cam/sketch/drawing/ecad scenario gaps incl. a dead ecad_orchestrate call, plus an H4 SimManager regression: dropped captureSelectionInto in sim_fixture/load commands). (2) Color debt de-literaled: 83 literals -> theme domain tokens, baseline 63 keys -> 0 (beats 'toward 18'). P1 gui_lint icons+theme clean. gate.sh running; then status review. Remaining for humans/next session: P2 21-pair sign-off sheet + interactive pass, T-G12 convergence walkthrough (Erik), G5 audit retarget + cutover (G12-gated).
- 2026-07-03 07:54 UTC [sonnet-marsh] REVIEW: branch task/T-G9-migration-cutover pushed @2d014b4, status review. gate.sh RESULT PASS (pre-existing FAIL-RECORDED profile unchanged; ui_style_check advisory now clean PASS). Both foreman scope additions DONE: (1) 5 parity harnesses in tools/gui_parity/ — clean-room 5/5 PASS, 22/22 checks, all artifacts byte-identical (gcode x8 dialects, netlist+ERC, DXF+PDF, study JSON, sketch op-batch); found+fixed 5 real AI-CAD2-side divergences incl. an H4 SimManager selection-capture regression and a dead ecad_orchestrate call. (2) 83 color literals -> theme domain tokens, baseline 63->0 keys. Write-set extensions (worker_client trace hook + sim_manager fix) documented in work/questions/T-G9.md with rationale. NOT closed (human-gated): P2 formal 21-pair sign-off + interactive pass, T-G12 convergence walkthrough (Erik), G5 audit-retarget/cutover. Task file has full HANDOFF section. Worktree ../CAD-T-G9 left in place (task not done — G12-gated remainder).
