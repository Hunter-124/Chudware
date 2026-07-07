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

## Polish agent requests (build fix — parts_workspace.cpp fonts member)
- [ ] (polish) `host/src/ui/parts/parts_workspace.cpp:93,111` — `ui::fonts` should be `ui::Fonts` (capital F). The struct is declared as `ui::Fonts` in ui_theme.h (L13) but parts_workspace.cpp references it as lowercase `ui::fonts`. Fix: replace `ui::fonts.h2` with `ui::Fonts.h2` at L93 and `ui::fonts.h1` with `ui::Fonts.h1` at L111. This is blocking the build (not in polish file ownership).

## ribbon agent — per-workspace ribbon filter (2026-07-06)
- [ ] (ribbon) `host/src/app.cpp` — before each `drawRibbon(...)` call, add:
  `ui::setRibbonWorkspace(static_cast<int>(st.activeWorkspace));`
  (include `host/src/ui/widgets/ribbon.h` if not already included at that call-site).
  This enables per-workspace tab filtering: Simulate shows only Simulation+Reference,
  Electronics shows PCB only, Manufacture shows Manufacture+Reference only,
  PLM/PartsLibrary/AI collapse the ribbon to 0 height. Default is Model (all 3D tabs).
  Owner: workspace-shell.

## Polish agent — parts_workspace.cpp build fix (correct fonts access)
- [ ] (polish) `host/src/ui/parts/parts_workspace.cpp:93,111` — `ui::fonts` is not a member of namespace `ui`; fonts live on AppState. Fix: replace `ui::fonts.h2` with `st.fonts.h2` at L93 (in sectionHeader) and `ui::fonts.h1` with `st.fonts.h1` at L111 (in emptyState). The `st` AppState& param is in scope in both functions. This blocks the ninja build — not in polish file ownership so filing as wire request.

## sim-kinematics agent — motion scenario (2026-07-06)
- [ ] (sim-kinematics) `host/src/ui/scenarios.cpp` — Add a `motion` scenario
  (AI_CAD_SHOT=motion) that validates the Motion study viewport playback end-to-end.
  The scenario should:
  1. On step 0: build a minimal 2-body assembly (box + arm) with one angle mate driven
     0→120 deg, then open the Simulate workspace and create a Motion study via
     `st.simManager->handleCommand(st, "sim_new_motion")`. Set
     `st.leftPaneTab = AppState::LP_Simulation`. Settle 4 frames. Capture
     `scratch/motion_shots/motion_t0.png` — this is the setup form.
  2. On step 1: call `st.simManager->handleCommand(st, "sim_run")` to run the motion
     solve. Poll `st.simClient` until !busy or use the synchronous path (runMotion uses
     st.worker.call, so it returns on the same thread before the next frame). Settle 4
     frames. Capture `scratch/motion_shots/motion_t1.png` — frame 0 with bodies at
     start pose.
  3. On step 2: advance the active motion study to its midpoint frame
     (motionFrame = nFrames/2, call SimManager::applyMotionFrame via the tick path by
     setting motionFrame directly and calling tick), settle 4 frames. Capture
     `scratch/motion_shots/motion_t2.png` — midpoint: bodies must be at a visibly
     different position from step 1.
  Owner: workspace-shell (scenarios.cpp ownership). Body positions MUST differ between
  step 1 and step 2 screenshots to prove motion playback is live.
