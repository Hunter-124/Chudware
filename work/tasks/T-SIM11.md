---
id: T-SIM11
title: EM simulation family — deferred solvers (GetDP/Meep/Elmer/MoM), doc-geometry intake, field viz
workstream: SIM
owner: unassigned
status: todo
deferred: yes
branch: task/T-SIM11-em-solver-family
depends_on: []
lane: simulation
kind: new
priority: 2
---

## Context

The EM sim family landed 2026-07-07 (docs/EM_SIMULATION.md): pluggable adapter layer in
`engine_oracle/sim/em/` behind `em_status` / `em_eigenmode` / `em_driven` / `em_timedomain`
worker RPCs. Shipped REAL: builtin-fdfd 2-D eigensolver (WR-90 TE10 validated to 0.006 %),
analytic-uniform driven/time-domain, and a complete mock-proven openEMS FDTD adapter
(`openems_adapter.py` — only the `_execute` solver seam needs a live binary). This task
captures everything intentionally deferred so nothing is lost.

## Definition of done (pick up incrementally; each box is independently shippable)

- [ ] **openEMS live validation**: install openEMS on a dev machine (no PyPI wheel — recipe
      in docs/EM_SIMULATION.md), run `test_live_openems_if_installed`, record the real WR-90
      S21 curve vs analytic in the docs validation table. Consider flipping `_execute` to
      external-process CLI invocation per the license review in contracts/license_debt.md row 9.
- [ ] **Gmsh+GetDP FEM adapter** (`GetdpAdapter`, currently detection-only scaffold):
      eigenmode + driven on curved/loaded geometry. Big synergy: the engine already uses gmsh
      for FEA meshing (engine.sim pack). GetDP `.pro` templates for the 2-D cutoff eigenproblem
      and 3-D driven VectorHelmholtz; parse `.pos`/table output.
- [ ] **Meep adapter** (`MeepAdapter` scaffold): FDTD alternative, conda-only pymeep;
      driven + timedomain, mirroring the openEMS plan/execute/shape seams.
- [ ] **Elmer adapter** (`ElmerAdapter` scaffold): VectorHelmholtz SIF generation, external
      ElmerSolver process.
- [ ] **MoM / open radiation (antennas)**: survey OSS first (e.g. necpp/PyNEC for wire
      antennas; openEMS NF2FF for radiation patterns from FDTD). Low priority for closed
      waveguides (Hunter spec).
- [ ] **Doc geometry intake**: `em_driven`/`em_timedomain` accept `geometry: {stl_path}`
      today (PEC polyhedron via CSXCAD AddPolyhedronReader). Wire the doc's solids/tess +
      material/boundary assignments directly (doc_id → tess export → dielectric/metal per
      body material), matching how FEA studies consume the doc.
- [ ] **Circular-guide ports in openEMS** (`build_plan` currently rejects circular; the
      builtin FDFD already handles circular eigenmodes).
- [ ] **Field visualization**: mode field previews exist as JSON grids (`fields: true` on
      em_eigenmode); render them in the GUI (heatmap in the results panel) and add openEMS
      E/H dump boxes → AICM overlay chunks like the FEA/CFD results (contracts/mesh_format.md).
- [ ] **GUI study kinds**: apply the host/WIRE_REQUESTS.md entry (EM Eigenmode / EM Frequency
      Sweep / EM Time Domain in SimManager, gated by em_status) once the sim-GUI rework merges.
- [ ] **MCP tools**: expose em_* as MCP tools (services/mcp) so agents can run EM studies —
      mirror run_fea_static's shape (`em_status` capability text in the tool docstring).

## Verify

- `python3 -m pytest engine_oracle/sim/em/test_em.py` stays green (21 real tests; the live
  test un-skips wherever a solver is installed).
- Differential rule: numerical adapters validate against `engine_oracle/sim/em/analytic.py`
  (WR-90 TE10 6.5571 GHz; uniform-section S21=exp(-γL)) — never weaken tolerances.

## Linkage (FIN finalize, 2026-07-10)

T-FIN24 wires the EM GUI forms against the **live** EM backend (toast-only gap closed)
regardless of this card's deeper solver family work. This task remains the home for
GetDP/Meep/Elmer/MoM adapters, doc-geometry intake, and field viz — still deferred /
optional parallel work. GUI study kinds that depend on em_status now land via the
T-FIN24 Simulate tab; extend that tree rather than resurrecting host-path stubs.
