# Wire requests — cross-agent shared-file edits

Append-only. If you need a change in a file you do NOT own, add a bullet here
(under `flock /tmp/aicad-git.lock`) with: requesting agent, target file:line,
exact change wanted, and why. The integrator applies these at the end.

Format:

- [ ] (agent) `path/to/file.cpp:123` — change X to Y because Z

## Hunter mid-run requirements (2026-07-06 ~22:45) — integrator MUST apply
- [ ] (hunter/orchestrator) Simulate workspace left pane: HIDE the feature/model tree entirely; show ONLY the simulation setup tree (SimManager::drawTreeBody) styled like the feature tree (same row layout/padding rules). No FeatureManager/Explorer/PM tabs visible in Simulate. Owner: workspace-shell (layout.cpp/app.cpp) + sim-kinematics (sim_manager).
- [ ] (hunter/orchestrator) Sim study-type pickers must be scoped by open doc types (progressive disclosure): mechanical FEA/CFD kinds for Part/Assembly docs; motion only when an assembly with mates is open; electronics-sim/emulation entries only when Schematic/PCB docs are open (full electronics wiring is follow-up task T-SIM10 — tonight: the scoping of what is OFFERED must already be in place, even if electronics kinds show a "coming online" state).
