---
id: T-U6
title: Simulation studies manager — study list, results, convergence caveats
workstream: U
owner: unassigned
status: todo
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
