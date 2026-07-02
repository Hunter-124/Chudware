---
id: T-D2
title: PyNite integration - member forces and deflections
workstream: D
owner: opus-dawn
status: review
branch: task/T-D2-pynite-solve
day: 2
depends_on: [T-D1]
---

## Context

PyNite (MIT) is the open-source FEM library for structural analysis. Day 2 converts a `StructureDoc` into a PyNite model, runs member-force and deflection analysis under dead/live/wind load cases, and returns the results in the schema defined in T-D1. This is the computational core that T-D3 (D/C ratio) and T-D4 (code compliance) depend on.

## Files / areas touched

- `engine_oracle/structural/analysis.py` — new: StructureDoc → PyNite model + analysis runner
- `engine_oracle/structural/load_cases.py` — new: dead/live/wind load case presets (ASCE 7 combos)
- `engine_oracle/structural/test_analysis.py` — new: DoD axial post + hand-calc validations
- `services/mcp/structural_tools.py` — new: `struct_analyze` (+ `struct_check` day-3 stub)
- `services/mcp/server.py` — register structural tools (append-only, 3 lines)
- `contracts/licenses.md` — PyNiteFEA MIT row (append-only)

## Definition of done

- [x] `build_pynite_model()` converts StructureDoc elements to a valid PyNite FEModel3D
- [x] `run_analysis()` returns AnalysisResult with member forces and deflections for each load case
- [x] `struct_analyze` MCP tool registered and returns JSON-serializable result
- [x] `python -m pytest engine_oracle/structural/test_analysis.py -q` passes (axial post test)

## DoD evidence (opus-dawn)

Run: `cd <worktree> && /home/hunter/Repositories/CAD/.venv-mcp/bin/python -m pytest \
     engine_oracle/structural/test_analysis.py -q` → **7 passed**. D1's `test_elements.py` still
**10 passed** (no regression).

- **build_pynite_model** — nodes at `transform·(0,0,0)`/`transform·(0,0,length)`, coincident
  endpoints merged (micron tol) into shared nodes; materials/sections from `section_properties`;
  footing nodes fixed (else lowest node = ground). `test_two_element_shared_node_connectivity`
  proves the merge (4 endpoints → 3 nodes).
- **run_analysis** — registers ASCE combos, one first-order `analyze()`, extracts axial
  (**negated: contract +tension, PyNite +compression**), shear, moment, deflection per member ×
  combo as `AnalysisResult` (§3 shape; `dc_ratio`/`pass` = null for T-D3).
- **struct_analyze** — `(doc_path, load_combo)`; lazy PyNite import (server hot-spot: won't crash
  module load — `{ok:False, code:"solver_unavailable"}` envelope if scipy absent); registers on a
  real FastMCP (`struct_analyze`, `struct_check`); `json.dumps` of the result verified.

**Hand-calc validation** (workstream-E precedent): axial post 1 kN → `axial_n ≈ -1000`, δ≈0;
cantilever tip load → δ = PL³/3EI and M = PL within 2%; simply-supported UDL → M = wL²/8 and
δ = 5wL⁴/384EI within 2%.

Units: consistent **N·mm·MPa** (E in MPa, I in mm⁴ → deflection mm, moment N·mm = §3 units).

## Open questions (non-blocking, in `work/questions/T-D2.md`)

- Q1 `struct_analyze` signature `(doc_path, load_combo)` (task) vs §5 `{combo?}` (session doc) —
  followed the task; §5 result shape honored; day-3 wrapper is additive.
- Q2 load-case → member mapping (contract `LoadCase` has no element target) — v1 applies each case
  to all non-footing members via `gravity`; per-element targeting would be an additive `element_ids`
  field (not made unilaterally).
- Env: PyNiteFEA installed to `.venv-mcp` (has scipy); `mcp-venv` (3.14) has no scipy.
