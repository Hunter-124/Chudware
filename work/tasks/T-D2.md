---
id: T-D2
title: PyNite integration - member forces and deflections
workstream: D
owner: opus-dawn
status: doing
branch: task/T-D2-pynite-integration
day: 2
depends_on: [T-D1]
---

## Context

PyNite (MIT) is the open-source FEM library for structural analysis. Day 2 converts a `StructureDoc` into a PyNite model, runs member-force and deflection analysis under dead/live/wind load cases, and returns the results in the schema defined in T-D1. This is the computational core that T-D3 (D/C ratio) and T-D4 (code compliance) depend on.

## Files / areas touched

- `engine_oracle/structural/analysis.py` — new: StructureDoc → PyNite model + analysis runner
- `engine_oracle/structural/load_cases.py` — new: dead/live/wind load case presets
- `services/mcp/server.py` — register struct_analyze tool (day 3 also adds struct_check; stub today)
- `engine_oracle/structural/elements.py` — StructureDoc and element dataclasses (from T-D1)

## Steps

1. Install `PyNite` into `.venv-mcp` (`pip install PyNite`); confirm MIT license.
2. Implement `load_cases.py`: `LoadCase` dataclass with `name`, `dead_factor`, `live_factor`, `wind_factor`; presets: `DEAD_ONLY`, `DEAD_LIVE` (1.2D+1.6L), `DEAD_LIVE_WIND` (1.2D+1.6L+0.5W).
3. Implement `analysis.py`:
   - `build_pynite_model(structure_doc: StructureDoc) -> FEModel3D`: add nodes at element endpoints; add members with E, G, Iy, Iz, J from section properties (call `section_properties(name)`); add supports at footings.
   - `run_analysis(model: FEModel3D, load_cases: list[LoadCase]) -> AnalysisResult`: `model.analyze()`; extract member forces (`Members["M1"].max_moment("Mz", combo="DEAD_LIVE")`), deflections, reactions.
   - `section_properties(section_name: str) -> dict`: return E (GPa), G, Iy, Iz, J for the section (cedar lumber: E=12, G=0.75 GPa; steel W6x25: from AISC DB).
4. Register `struct_analyze(doc_path: str, load_combo: str) -> dict` MCP tool in `server.py`.
5. Test: 2m cedar post with a 1kN axial load; assert axial force ≈ 1kN, deflection ≈ 0.

## Definition of done

- [ ] `build_pynite_model()` converts StructureDoc elements to a valid PyNite FEModel3D
- [ ] `run_analysis()` returns AnalysisResult with member forces and deflections for each load case
- [ ] `struct_analyze` MCP tool registered and returns JSON-serializable result
- [ ] `python -m pytest engine_oracle/structural/test_analysis.py -q` passes (axial post test)

## AI hints

- `graphify query "PyNite structural FEM model"` — check if PyNite is already used elsewhere
- PyNite API: `from PyNite import FEModel3D; m = FEModel3D(); m.add_node("N1", 0,0,0); m.add_member("M1","N1","N2", E,G,Iy,Iz,J,A)`
- Relevant files: `engine_oracle/structural/elements.py`, `engine_oracle/structural/load_cases.py`
- Section properties for structural lumber: NDS Table 1B dimensional lumber sizes (nominal vs. actual); E and G from NDS Supplement Table 4A for Douglas Fir-Larch (use public-domain NDS values, not the NDS text itself)
