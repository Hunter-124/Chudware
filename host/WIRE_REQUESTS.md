# Wire requests — cross-agent shared-file edits

Append-only. If you need a change in a file you do NOT own, add a bullet here
(under `flock /tmp/aicad-git.lock`) with: requesting agent, target file:line,
exact change wanted, and why. The integrator applies these at the end.

Format:

- [ ] (agent) `path/to/file.cpp:123` — change X to Y because Z

## Polish agent requests (Goal 4 — axis triad dynamic placement)
- [x] (polish) `host/src/ui/viewport_panel.cpp:3285-3290` — DONE e5e90805. The orientation triad anchor
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
- [x] (unify-build) `host/src/ui/scenarios.cpp` — DONE e5e90805. `ScenarioRunner::runWorkspaces(AppState&)` is
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
- [x] (ribbon) `host/src/app.cpp` — DONE e5e90805. before each `drawRibbon(...)` call, add:
  `ui::setRibbonWorkspace(static_cast<int>(st.activeWorkspace));`
  (include `host/src/ui/widgets/ribbon.h` if not already included at that call-site).
  This enables per-workspace tab filtering: Simulate shows only Simulation+Reference,
  Electronics shows PCB only, Manufacture shows Manufacture+Reference only,
  PLM/PartsLibrary/AI collapse the ribbon to 0 height. Default is Model (all 3D tabs).
  Owner: workspace-shell.

## Polish agent — parts_workspace.cpp build fix (correct fonts access)
- [ ] (polish) `host/src/ui/parts/parts_workspace.cpp:93,111` — `ui::fonts` is not a member of namespace `ui`; fonts live on AppState. Fix: replace `ui::fonts.h2` with `st.fonts.h2` at L93 (in sectionHeader) and `ui::fonts.h1` with `st.fonts.h1` at L111 (in emptyState). The `st` AppState& param is in scope in both functions. This blocks the ninja build — not in polish file ownership so filing as wire request.

## sim-kinematics agent — motion scenario (2026-07-06)
- [x] (sim-kinematics) `host/src/ui/scenarios.cpp` — DONE e5e90805. Add a `motion` scenario
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

## parts-plm agent requests
- [ ] (parts-plm) `host/src/app.cpp` — After the PLM/PartsLibrary workspace blocks, wire a
  `parts_insert_into_doc` RPC result handler: when the worker returns successfully from
  `parts_insert_into_doc`, call `st.addToast("Part inserted into document", ToastLevel::Ok)`.
  The RPC is already fired from parts_workspace.cpp; only the success toast is missing.
  This is a low-priority polish item — the Insert button works silently without it.

## ribbon agent — exact insertion point for setRibbonWorkspace (2026-07-06)
- [x] (ribbon) `host/src/app.cpp:2719` — DONE e5e90805. insert before the `drawRibbon` call (currently line ~2720):
  `ui::setRibbonWorkspace(static_cast<int>(st.activeWorkspace));`
  ribbon.h is already included at that translation unit. This one-liner wires the per-workspace
  ribbon filter. Confirmed the call site: lines 2717-2722 of app.cpp read:
    ui::setRibbonActiveCmd(activeCmd);
    ui::setRibbonSketchActive(st.sketchMode->active());
    float ribbonH = ui::drawRibbon(...)
  Insert between setRibbonSketchActive and drawRibbon. Owner: workspace-shell.

## furniture agent — workspace hotkeys + window title (2026-07-06)
- [x] (furniture) `host/src/app.cpp` — DONE e5e90805. Ctrl+1..Ctrl+8 workspace switching. Insert inside the
  existing global-keys block (the `{ const bool canDoc = ... }` scope, after the Ctrl+P handler
  at ~L2531-2534 — same guards style as Ctrl+N/Ctrl+P). The shortcut bar + docs now advertise
  these, so this block is the only missing piece:

  ```cpp
  // [workspace-tabs seam] Ctrl+1..Ctrl+8 switch the title-bar workspace (matches tab order).
  // Guards: text input owns digits while typing; ECAD keeps its own accelerator space.
  if (!textFocus && !ecadFocused && (io.KeyCtrl || io.KeySuper) && !io.KeyShift && !io.KeyAlt) {
      using WS = AppState::Workspace;
      static constexpr struct { ImGuiKey key; WS ws; } kWsKeys[] = {
          {ImGuiKey_1, WS::Model},       {ImGuiKey_2, WS::Drawing},
          {ImGuiKey_3, WS::Simulate},    {ImGuiKey_4, WS::Electronics},
          {ImGuiKey_5, WS::Manufacture}, {ImGuiKey_6, WS::PLM},
          {ImGuiKey_7, WS::PartsLibrary},{ImGuiKey_8, WS::AI},
      };
      for (const auto& k : kWsKeys)
          if (ImGui::IsKeyPressed(k.key, false)) { st.activeWorkspace = k.ws; break; }
  }
  ```

- [x] (furniture) `host/src/app.cpp` — DONE e5e90805. live window title "<doc name>[*] — AI-CAD" via
  glfwSetWindowTitle on doc/tab/dirty change. Once per frame (anywhere after the doc-tab state is
  settled, e.g. near the per-frame display-flag derivation at ~L2804), cheap change-detection:

  ```cpp
  // Window title mirrors the active document + dirty marker (SolidWorks-style).
  {
      std::string title;
      if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
          const auto& tab = st.docTabs[st.activeTab];
          title = tab.title.empty() ? "Untitled" : tab.title;   // use the tab-strip display name field
          if (tab.dirty) title += "*";
          title += " \xe2\x80\x94 AI-CAD";                      // " — AI-CAD"
      } else {
          title = "AI-CAD";
      }
      static std::string lastTitle;
      if (title != lastTitle) { lastTitle = title; glfwSetWindowTitle(window, title.c_str()); }
  }
  ```
  Note: substitute the actual DocTab display-name member (`title`/`name` — whichever the tab strip
  renders) and the GLFWwindow* variable in scope (`window` in run_gui). Owner: workspace-shell.

## ai-workspace agent — route WS::AI to the full AI hub (2026-07-06)
- [ ] (ai-workspace) `host/src/app.cpp` — 3 edits wire the new full-page AI hub
  (`host/src/ui/ai/ai_workspace.cpp`, already built + linked into AI-CAD):

  1. Add the include next to the other ui includes (~L50, beside `ui/legacy_chat.h`):
  ```cpp
  #include "ui/ai/ai_workspace.h"
  ```

  2. At ~L2892, gate the docked Copilot column out of the AI workspace (the hub renders the
  SAME conversation state full-width via the legacy_chat reuse hooks — two composers over one
  st.prompt in the same frame would double-render):
  ```cpp
  // The AI workspace hosts the expanded chat itself (same st.chat/st.conversations state);
  // keep the docked Copilot column in every OTHER workspace exactly as today.
  if (ws != WS::AI) ui::drawAIPanel(st, busy, canRun);
  ```
  (replaces the bare `ui::drawAIPanel(st, busy, canRun);` line)

  3. Replace the WS::AI stub block (`} else if (ws == WS::AI) { ... }` at ~L2971-2995 — the
  "##AISessionList" placeholder window) verbatim with:
  ```cpp
        } else if (ws == WS::AI) {
            // AI workspace: full-page AI hub — sessions/sub-agents rail, expanded Copilot
            // chat or project build-plan DAG, and AI workflow/model settings
            // (host/src/ui/ai/ai_workspace.cpp). The docked Copilot is suppressed in this
            // workspace (gate above), so the hub spans the whole central band.
            const ui::OverlayBand& band = ui::overlayBand();
            if (band.valid) {
                const float m = 12.0f;
                ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m),
                                        ImGuiCond_Always);
                ImGui::SetNextWindowSize(ImVec2((band.maxX - band.minX) - 2.0f * m,
                                                (band.maxY - band.minY) - 2.0f * m),
                                         ImGuiCond_Always);
                ImGui::Begin("##AIWorkspace", nullptr,
                             ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                             ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                             ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                             ImGuiWindowFlags_NoSavedSettings);
                ui::drawAiWorkspace(st);
                ImGui::End();
            }
        }
  ```
  Note `band.maxX` (not `ui::overlayColumnEdge(1)`) — with the docked Copilot suppressed the
  published column edge is stale, and the hub owns the full width. Owner: ai-workspace.

## Explorer restyle agent (2026-07-06)

- **layout.cpp — swap the left-pane header chip to `ui::fmPaneHeaderChip` (Hunter red-pen item).**
  The current header block in `drawLeftPane` (the `glassRowAtCursor(4.0f,...)` +
  `Dummy(iconSz)` + `drawIconGlass(titleIcon,...)` + `PushFont(medium)` + `TextUnformatted(title)`
  run, right after the `IconUV headIcon = getIcon(titleIcon, 20);` line, ~layout.cpp:565-588) has
  three flagged defects: text not centered in its pill, the 20px icon overflows the text-height
  pill, and the FM tab shows the meaningless "history" three-dot glyph. Replace that whole run
  (KEEP the trailing `ImGui::Separator();` and the `titleX`/`groupW`/`headIcon` computations can
  be deleted too) with ONE call:
  ```cpp
  ui::fmPaneHeaderChip(st, title, titleIcon, paneCenterX);
  ```
  (declared in feature_tree.h — already implemented/compiling). It draws a content-fitted glass
  pill, icon fully inside with even padding, icon+text group centered on `paneCenterX`, and for
  `titleIcon == "history"` renders a purpose-built line-art feature-tree glyph (root + two branch
  children, row-icon stroke weight). Owner: explorer-restyle (feature_tree.cpp side already done).

- **scenarios.cpp — add an `explorer` AI_CAD_SHOT scenario** that sets
  `st.leftPaneTab = AppState::LP_Explorer;` (one line, first frame) so the Explorer tab is
  headless-screenshot coverable like the other 21 scenarios. Verified manually this session via a
  temporary hook (since removed); proof at /tmp/hunter-verify/explorer_restyle.png.
