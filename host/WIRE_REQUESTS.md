# Wire requests — cross-agent shared-file edits

Append-only. If you need a change in a file you do NOT own, add a bullet here
(under `flock /tmp/chudware-git.lock`) with: requesting agent, target file:line,
exact change wanted, and why. The integrator applies these at the end.

Format:

- [ ] (agent) `path/to/file.cpp:123` — change X to Y because Z

## Archive
Landed requests through 2026-08 are in [`archive/wire-requests-2026-08.md`](archive/wire-requests-2026-08.md).

## Hunter mid-run requirements (2026-07-06 ~22:45) — integrator MUST apply
- [ ] (hunter/orchestrator) Simulate workspace left pane: HIDE the feature/model tree entirely; show ONLY the simulation setup tree (SimManager::drawTreeBody) styled like the feature tree (same row layout/padding rules). No FeatureManager/Explorer/PM tabs visible in Simulate. Owner: workspace-shell (layout.cpp/app.cpp) + sim-kinematics (sim_manager).
- [ ] (hunter/orchestrator) Sim study-type pickers must be scoped by open doc types (progressive disclosure): mechanical FEA/CFD kinds for Part/Assembly docs; motion only when an assembly with mates is open; electronics-sim/emulation entries only when Schematic/PCB docs are open (full electronics wiring is follow-up task T-SIM10 — tonight: the scoping of what is OFFERED must already be in place, even if electronics kinds show a "coming online" state).

## dialogs agent (fix(dialogs)) — Command Search screenshot scenario
- [ ] (dialogs) `host/src/ui/scenarios.cpp` — Add a `dialogs` scenario (CHUDWARE_SHOT=dialogs) that:
  1. On step 0: sets `st.showCommandSearch = true` and settles 8 frames.
  2. On step 1: captures to `scratch/dialog_shots/search_dark.png` (use CHUDWARE_REPO env like appearance scenario).
  3. On step 2: sets `st.openSettings = true`, `st.openSettingsTab = 0`, settles 8 frames.
  4. On step 3: captures to `scratch/dialog_shots/settings_verify.png`.
  Also add `runDialogs(AppState&)` declaration to ScenarioRunner in scenarios.h and a dispatch call
  in the per-frame tick alongside the other scenarios. Needed for verify: both dialogs are visually
  inspected in both themes by the dialogs finisher agent.

