---
id: T-SIM1
title: Thermal FEA solver - steady-state heat conduction kernel + sim_new_thermal end-to-end
workstream: SIM
owner: opus-terra
status: review
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

## Progress log
- 2026-07-05 05:06 UTC [opus-terra] Steady-state thermal (heat conduction) FEA landed end-to-end on branch task/T-SIM1-thermal-fea (commit 33bbb4ac, pushed). Kernel core/sim/fea_thermal.{h,cpp}: Fourier conduction K=int k gradN.gradN dV on tet4/tet10 (1 DOF/node, re-derived tet10 gradients since fea_static keeps them private+parity-critical), Dirichlet fixed-T (RHS elimination), heat-flux Neumann, convection Robin (tri3/tri6 surface consistent-mass for h*int N N dS + h*Tamb load). SPD Cholesky + SparseLU fallback, singular guard, transient explicitly out of scope. Glue fea_run_thermal in fea_run.cpp reuses mesh/faceset plumbing + does the mm unit conversions; sim_run_thermal RPC registered next to static/modal in libaicad.cpp. Test core/tests/test_thermal.cpp (ctest 'thermal', 7 cases/19 asserts): 1D plane-wall Dirichlet linear to ~1e-13 (tet4+tet10), hot+convection cold face vs series resistance 0.5% (tet4+tet10), flux+cold face exact, singular+validation guards. Advisor thermal_anchor()+precheck lint added (test_sim_advisor 53 passed). Cited k table engine_oracle/sim/thermal_conductivity.json (Incropera/ASM/MatWeb), kept separate from the generated materials.json mirror (which is guarded by test_export_sim_json - still green). GUI additive-only, isFlowFamily untouched: runThermal dispatch, drawThermalForm inputs, max/min temp row, persisted thermal inputs, studyReady+tree branches. VERIFICATION: core ctest 45/45 PASS incl thermal; ui/imgui build+shot+selftest PASS; gate.sh RESULT PASS (pytest FAIL-RECORDED are pre-existing T-V2/#16, none in my files). DoD DEVIATIONS: (1) singular-lint condition is 'no fixed-T AND no convection' (physically correct) not the DoD's 'no fixed-T and no flux' - a flux does NOT anchor the datum, convection does. (2) 'graphify update .' not run: graphify-out is gitignored (a main-checkout artifact absent in the worktree); a from-scratch build timed out. Graph should be refreshed post-merge in the main checkout. (3) Contour/temperature-field RENDERING left to T-SIM2 per sibling scoping; thermal GUI shows scalar max/min temp only. All GUI edits are additive branches; the drawStudyRow tree got a self-contained Thermal block (not a merged conditional) to stay additive.
