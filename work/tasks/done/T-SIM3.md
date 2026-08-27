---
id: T-SIM3
title: Simulation UI overhaul - material assignment dialog, T-W9 advisor wiring, studies panel polish
workstream: SIM
owner: fable-cove
status: done
branch: task/T-SIM3-sim-ui-overhaul
depends_on: []
lane: simulation
kind: overhaul
priority: 2
---

## Context

Hunter directive (overnight 2026-07-05): simulation UI overhaul. Remaining UX gaps after
T-U6 (studies manager) per the T-V1 audit + scope pass:

1. `sim_material` command only navigates to the Material panel node — there is NO
   material assignment dialog. Studies run with default/placeholder material unless the
   user hand-edits JSON (ui/imgui/src/ui/sim_manager.cpp, PARTIAL wiring).
2. The T-W9 sim advisor (services/agent/advisor/sim_advisor.py — precheck BC lint +
   mesh suggestion, postcheck margin verdict + narration) is implemented but NOT wired
   into the GUI dispatch path: precheck should run before Run dispatch (advise, never
   veto — freedom rule; failing precheck shows an advisory card, does not block unless
   CHUDWARE_SIM_PRECHECK_BLOCK is set), postcheck verdict/margin narration should render
   with the results.
3. Studies panel readability: inconsistent styling vs the glass-column design language;
   fixture/load lists lack edit/delete affordances once captured.

## Definition of done

- [ ] Material assignment dialog: pick from doc materials + global material library,
      apply to study (whole-body first; per-face only if cheap), persisted on the study
      record, shown in the study table row. Material properties come from the data
      tables (provenance rule — no invented numbers).
- [ ] Precheck wiring: on Run, call sim advisor precheck; render advisory block (issues +
      computed mesh suggestion) in the panel BEFORE dispatch; advise-never-veto semantics
      (block only when CHUDWARE_SIM_PRECHECK_BLOCK=1). Ledger event emitted when a mission
      context exists (inspectability rule).
- [ ] Postcheck wiring: margin verdict (PASS/WARN/FAIL vs margin_policy.json) + narration
      rendered with results; FAIL styling obvious but not modal.
- [ ] Fixtures/loads list: per-entry delete + magnitude edit after capture.
- [ ] Panel style pass: section headers, spacing, theme tokens instead of hard-coded
      colors — consistent with the glass-column design language, dynamic color ON.
- [ ] Headless CHUDWARE_SHOT scenarios for: material dialog, precheck advisory shown,
      postcheck verdict row. Light + dark, eyeballed, described in the work note.
- [ ] scripts/gate.sh green (issue #16 known failures excepted); `graphify update .` run.

## Driver notes

- Python test venv: `~/.local/share/Chudware/mcp-venv/bin/python`.
- graphify rule: `graphify query "<question>"` before raw-file reading.
- Branch task/T-SIM3-sim-ui-overhaul, worktree ../CAD-T-SIM3. Stop at status: review.
- ADDITIVE SEAMS ONLY vs T-SIM1/T-SIM2 (parallel): sim_manager.cpp is shared ground —
  keep your edits scoped to material dialog + precheck/postcheck hooks + panel styling;
  do not touch thermal dispatch (T-SIM1) or contour rendering (T-SIM2).

## Progress log
- 2026-07-05 19:29 UTC [fable-cove] Claimed T-SIM3, worktree ../CAD-T-SIM3 on task/T-SIM3-sim-ui-overhaul. Oriented via graphify: sim_manager.cpp/.h read, sim_advisor.py precheck/postcheck API read, margin_policy.json read. Plan: (1) material assignment dialog (doc material + engine/sim/materials.json library + custom), (2) precheck via python CLI shim on Run with CHUDWARE_SIM_PRECHECK_BLOCK gating (GUI default = never veto), (3) postcheck verdict row in results, (4) fixtures/loads delete+edit affordances, (5) glass-column style pass, (6) shot scenarios light+dark. Baseline build running.
- 2026-07-05 20:22 UTC [fable-cove] DRIVER DIED (session limit, resets Jul 10). Integrator pushed the branch origin/task/T-SIM3-sim-ui-overhaul (4 clean commits: sim_advisor CLI shim, material dialog + T-W9 advisor wiring + panel style, shot scenarios sim_material/sim_precheck/sim_postcheck, subprocess tests). No uncommitted work lost. Needs verification + gate + finisher review before merge.
