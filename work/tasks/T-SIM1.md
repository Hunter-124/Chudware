---
id: T-SIM1
title: Thermal FEA solver - steady-state heat conduction kernel + sim_new_thermal end-to-end
workstream: SIM
owner: unassigned
status: todo
branch: task/T-SIM1-thermal-fea
depends_on: []
lane: simulation
kind: new
priority: 1
---

## Context

Hunter directive (overnight 2026-07-05): finish "the rest of the simulation implementation".
The T-V1 audit (work/audits/SIM_AUDIT.md) found the #1 gap: `sim_new_thermal` is wired in
the GUI (ui/imgui/src/ui/sim_manager.cpp — StudyType::Thermal exists in the newStudy()
switch) but there is NO thermal solver backend anywhere in core/sim/ or engine_oracle/sim/.
A user can create a thermal study and click Run and nothing real happens.

Existing FEA infrastructure to reuse (do NOT reinvent):
- core/sim/mesher.{h,cpp} — tet10 unstructured meshing (shared with static/modal)
- core/sim/fea_static.{h,cpp} — assembly/solve patterns, BC handling, result export shape
- services/agent/advisor/sim_advisor.py (T-W9) — precheck/postcheck seam; has
  compute_beam_anchor() as the pattern for hand-calc anchors

## Definition of done

- [ ] core/sim/fea_thermal.{h,cpp}: steady-state heat conduction on the tet10 mesh —
      Fourier conduction, fixed-temperature BCs, heat-flux loads, convection film BCs
      (h + T_ambient). Linear solve reusing the existing solver scaffolding. Transient is
      OUT of scope (note it as follow-up, do not stub it half-way).
- [ ] `sim_run_thermal` RPC exposed the same way `sim_run_static`/`sim_run_modal` are
      (mirror the existing plugin registration path). Results: nodal temperature field,
      max/min T, per-face flux summary.
- [ ] core/tests/test_thermal.cpp: 1D wall / plate conduction case validated against the
      closed-form Fourier solution (analytic anchor, tolerance stated in the test).
      ctest green.
- [ ] thermal_anchor() added to services/agent/advisor/sim_advisor.py (plate/wall Fourier
      steady-state reference, same shape as compute_beam_anchor) + pytest cases in
      test_sim_advisor.py. Precheck lints thermal BCs (no fixed-T + no flux = singular).
- [ ] ui/imgui sim_manager.cpp: thermal study Run dispatches to the new backend; loads UI
      offers temperature/flux/convection inputs (mirror how force/pressure dialogs work).
      Results row shows max_temp_c in the T-U6 study table.
- [ ] Numbers provenance rule: all default material conductivities come from a data table
      (core or engine data file with source comment), never invented inline.
- [ ] scripts/gate.sh green (known pre-existing failures per issue #16 excepted);
      `graphify update .` run before review.

## Driver notes

- Python test venv: `~/.local/share/AI-CAD/mcp-venv/bin/python` (NOT .venv-mcp).
- Known pre-existing failures (issue #16): test_h7_shadow offline-mode,
  test_providers_conformance, test_loop_mock collection — not yours to fix.
- graphify rule: run `graphify query "<question>"` to orient before reading raw source.
- Work on branch task/T-SIM1-thermal-fea in worktree ../CAD-T-SIM1. Stop at
  status: review — never merge to master, never set done.
- T-BL16 (motion/kinematics, opus-dune) is separate — do not touch its files.
