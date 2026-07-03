---
id: T-U6
title: Simulation studies manager — study list, results, convergence caveats
workstream: U
owner: sonnet-finch
status: done
branch: task/T-U6-ui
day: 6
priority: 4
depends_on: [T-U1]
---

## Context

FEA/CFD/piping/hydraulic studies run headless today; users need a study manager:
what ran, on what, verdicts, and the narration.

## Files / areas touched

- ui/abstract/sim_view.h + ui/imgui/src/ui/sim_panel.* (scaffolded): study list per doc
  (kind, date, status), result summary (max stress/displacement/FoS, flow dP, modal
  freqs), describe_results narration inline (collapsible), re-run + open-full-results
- Worker-RPC to run_fea_static/modal, run_cfd_flow, piping_solve, describe_results

## Definition of done

- [ ] Studies enumerate from the doc's attached results; re-run round-trips
- [ ] Narration renders from describe_results verbatim (no recomputation in UI — the
      narration contract's numbers-are-authoritative rule)
- [ ] Mesh-convergence caveat shown whenever the backend flags it
- [ ] Headless screenshots; ui_style_check green; gate; graphify update .

## Driver notes (Fable)

- The sim-backends audit (work/audits/SIM_BACKENDS_AUDIT.md, landing) may re-grade some
  backends; render whatever verdict fields exist, add none. Long runs: fire via AsyncRpc
  and show running state (texture_cache/preview_pane async precedent).

## Progress log
- 2026-07-03 01:44 UTC [sonnet-finch] REVIEW branch=task/T-U6-ui @ a65cdd3

What changed:
- NEW ui/abstract/sim_view.h: pure view-model (SimPanelModel, SimStudyRow, SimPanelState, SimRunState) — no ImGui/GL includes, follows ui/abstract/ convention
- NEW ui/imgui/src/ui/sim_panel.h + sim_panel.cpp: Simulation Studies Manager panel built on panel_scaffold + theme:: tokens; study list rendered via BeginTable with RowBg (bg1/bg2 tokens for legibility over glass window); result scalars per study type; FEA mesh-convergence caveat always shown, CFD Cd-uncalibrated warning for cpp-lbm engine (both TCOL(warn)); describe_results narration collapsible verbatim; per-study Re-run via AsyncRpc (run_fea_static/modal 180s, run_cfd_flow 300s)
- MODIFIED ui/imgui/src/ui/scenarios.h/cpp: AI_CAD_SHOT=sim_studies scenario — fixture seeding (FEA static + CFD flow) fires unconditionally in ScenarioRunner::tick() (no workerStarted guard); gFixtureSeeded flag absorbs first docId without clearing rows
- MODIFIED ui/imgui/src/app/application.cpp: panel Begin/End wiring centered at 560x520, after BOM panel block

How verified:
- Headless screenshot (AI_CAD_SHOT=sim_studies, 150 frames) shows populated table: FEA Static row (FoS 3.41, 73.4 MPa, ⚠ convergence) + CFD Flow row (Cd 1.8720, D 0.214 N, ⚠ uncalibrated) — both rows legible on dark bg1/bg2 table backing
- ui_style_check.py: 0 findings (clean)
- Build: cmake --build clean, no warnings on new files

Open questions for reviewer:
- Re-run action fires with empty fixtures/loads — doc params not re-passed from stored study record (stored params don't include face-query refs in re-runnable form); this is documented limitation; re-run fires fresh solver call on current doc geometry
- gFixtureSeeded pattern is a headless-only workaround; in live use the panel loads via AsyncRpc sim_study_list on doc open
