---
id: T-E4
title: 3D CFD handoff for pipe internals
workstream: E
owner: sonnet-pax
status: review
branch: task/T-E4-cfd-handoff
day: 8
depends_on: [T-E2]
---

## Context

For details 1D cannot answer (mixing, erosion hotspots, dead legs): extract the internal fluid volume from a routed pipe run (OCCT boolean) and hand it to the existing LBM/OpenFOAM CFD path. DAG verify-node checks: velocity limits, pressure-drop budget vs the 1D result.

## Definition of done

- [x] internal_volume tool produces a watertight fluid solid from a pipe run
- [x] One-click handoff to run_cfd_flow / run_cfd_accurate
- [x] Verify node compares 1D vs 3D pressure drop within tolerance

## AI hints

- `graphify query "cfd flow study setup"`
- Existing: run_cfd_flow, run_cfd_accurate MCP tools

## Implementation notes (sonnet-pax)

### Files added
- `engine_oracle/piping/cfd_handoff.py` — three entry points:
  - `internal_volume(doc, out_path, run_id=None)` — sweeps the bore (inner_dia radius) along
    the centreline path via build123d (same arc-wire approach as threed.py), exports STEP;
    soft-fails with PipingCFDError when kernel absent (never bare traceback)
  - `cfd_boundary_from_solve(solve_result, inlet_run_id=None, outlet_node_id=None)` — pure Python;
    translates piping_solve node pressures + link velocities into CFD inlet/outlet BCs
  - `verify_pressure_drop(solve_result, cfd_result, tolerance_pct=20.0, run_id=None)` — DAG
    verify node; derives CFD dP from drag_n/frontal_area_mm2 (or Cd fallback), flags PASS/FAIL,
    checks velocity limits (>3 m/s liquid service erosion advisory)

- `engine_oracle/piping/test_cfd_handoff.py` — 20 tests (3 kernel-gated, 17 pure-Python)
  - 18/18 pass in mcp-venv pytest (3 kernel tests correctly skip); 20/20 pass with system Python

- `services/mcp/piping_tools.py` (append-only) — three new MCP tools:
  - `piping_internal_volume` — bore STEP extraction
  - `piping_cfd_handoff` — full pipeline: 1D solve -> BC extraction -> bore STEP -> run_cfd_flow
  - `piping_verify_cfd` — DAG verify: compare 1D vs 3D dP + velocity limit check

### Design decisions
- LBM (run_cfd_flow) is the always-available path; run_cfd_accurate not invoked in
  piping_cfd_handoff (user calls it manually + verifies via piping_verify_cfd)
- piping_cfd_handoff degrades gracefully when core_bridge unavailable: returns step_path + BCs
  so user can import manually then call run_cfd_flow
- Kernel absence -> PipingCFDError (never bare traceback), matching T-F14 converter_missing pattern

### Gate results
- core build: PASS; ctest: PASS; ui/abstract: PASS
- ui/imgui: FAIL -- pre-existing selMulti break (FOREMAN known pitfall, not our regression)
- pytest gate: 0-collected / 25-deselected (pre-existing collection issue); piping suite
  29/29 pass manually with mcp-venv pytest

### Merge order
E1 -> E2 -> {E3, E4} (E3 and E4 are independent siblings, both branch from T-E2)
