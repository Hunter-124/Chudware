# Wire requests — cross-agent shared-file edits

Append-only. If you need a change in a file you do NOT own, add a bullet here
(under `flock /tmp/aicad-git.lock`) with: requesting agent, target file:line,
exact change wanted, and why. The integrator applies these at the end.

Format:

- [ ] (agent) `path/to/file.cpp:123` — change X to Y because Z

## Polish agent requests (Goal 4 — axis triad dynamic placement)
- [ ] (polish) `host/src/ui/viewport_panel.cpp:3285-3290` — The orientation triad anchor
  currently uses the FM column's RIGHT edge X (hudLeft) regardless of column height. The goal is:
  when the FM column does NOT reach the triad row (i.e. the column height in screen pixels is less
  than `cr1.y - 52.0f * ts - 60.0f * ts`), anchor the triad at `cr0.x + 52.0f * ts` (far left of
  viewport, same as before FM existed); when the column IS tall enough to reach it, use the
  existing `hudLeft + 52.0f * ts`. Publish FM column height via a new
  `ui::publishOverlayColumnHeight(int slot, float heightPx)` in layout.h/layout.cpp (mirrors
  publishOverlayColumnEdge), called from drawLeftPane with the actual rendered column height each
  frame. In viewport_panel.cpp read `ui::overlayColumnHeight(0)` and compare to
  `(cr1.y - cr0.y) - 52.0f * ts` to decide the anchor. No visual change on tall columns; triad
  moves to the far left when FM is short or collapsed.

## Hunter mid-run requirements (2026-07-06 ~22:45) — integrator MUST apply
- [ ] (hunter/orchestrator) Simulate workspace left pane: HIDE the feature/model tree entirely; show ONLY the simulation setup tree (SimManager::drawTreeBody) styled like the feature tree (same row layout/padding rules). No FeatureManager/Explorer/PM tabs visible in Simulate. Owner: workspace-shell (layout.cpp/app.cpp) + sim-kinematics (sim_manager).
- [ ] (hunter/orchestrator) Sim study-type pickers must be scoped by open doc types (progressive disclosure): mechanical FEA/CFD kinds for Part/Assembly docs; motion only when an assembly with mates is open; electronics-sim/emulation entries only when Schematic/PCB docs are open (full electronics wiring is follow-up task T-SIM10 — tonight: the scoping of what is OFFERED must already be in place, even if electronics kinds show a "coming online" state).

## unify-build agent — build blocker (2026-07-06)
- [ ] (unify-build) `host/src/ui/scenarios.cpp` — `ScenarioRunner::runWorkspaces(AppState&)` is
  called at line ~911 but has NO definition in scenarios.cpp (only added as a declaration in
  scenarios.h by a prior agent run). Link fails with "undefined reference". Either provide a stub
  body or remove the call — the host ninja build is RED until this is fixed.
  Filed by: unify-build finisher. Owner: workspace-shell/sim-kinematics agent.

## chrome-tabs agent — ribbon.cpp duplicate static variable (build blocker)
- [ ] (chrome-tabs) `host/src/ui/widgets/ribbon.cpp` — File has `static int s_workspace = 0;`
  declared TWICE: once at line ~71 and again at line ~333. The second declaration causes a
  "was not declared in this scope" compile error on ribbon_tabVisible() (lines ~90–104) because
  the function sees only one of them depending on TU linkage. Fix: remove the DUPLICATE declaration
  at line ~333, keeping only the one at line ~71. The variable `s_workspace` is a module-level
  int that tracks the active workspace index for tab-visibility filtering. This is NOT my file
  (chrome-tabs owns title_bar.cpp/.h + win_chrome.cpp/.h only); the ribbon agent must apply this.
  Build is RED until resolved.

## dialogs agent (fix(dialogs)) — Command Search screenshot scenario
- [ ] (dialogs) `host/src/ui/scenarios.cpp` — Add a `dialogs` scenario (AI_CAD_SHOT=dialogs) that:
  1. On step 0: sets `st.showCommandSearch = true` and settles 8 frames.
  2. On step 1: captures to `scratch/dialog_shots/search_dark.png` (use AI_CAD_REPO env like appearance scenario).
  3. On step 2: sets `st.openSettings = true`, `st.openSettingsTab = 0`, settles 8 frames.
  4. On step 3: captures to `scratch/dialog_shots/settings_verify.png`.
  Also add `runDialogs(AppState&)` declaration to ScenarioRunner in scenarios.h and a dispatch call
  in the per-frame tick alongside the other scenarios. Needed for verify: both dialogs are visually
  inspected in both themes by the dialogs finisher agent.
