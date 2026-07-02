---
id: T-D3
title: Section libraries, D/C ratio checks, and struct_check MCP tool
workstream: D
owner: sonnet-mave
status: doing
branch: task/T-D3-section-libraries-checks
day: 3
depends_on: [T-D2]
---

## Context

Day 3 bundles section libraries (AISC shapes DB free download + dimensional lumber table) and implements the D/C ratio post-processor using simplified AISC/NDS-style checks. All output is clearly labeled "preliminary — not sealed engineering." The `struct_check` MCP tool returns pass/fail per member with D/C ratio, governing load combo, and controlling limit state. This is the DAG verify node's input (T-A4 wires D/C ratio < 1.0 as an end condition).

## Files / areas touched

- `engine_oracle/structural/sections.py` — new: AISC shapes DB loader + dimensional lumber table
- `engine_oracle/structural/checks.py` — new: D/C ratio calculator (AISC/NDS simplified)
- `engine_oracle/data/aisc_shapes/` — bundle AISC shapes DB (free download)
- `engine_oracle/data/lumber_table.json` — dimensional lumber section properties
- `services/mcp/server.py` — register struct_check tool

## Steps

1. Download AISC Shapes Database v16.0 (free, steel-shapes.org or aisc.org/research); save as `engine_oracle/data/aisc_shapes/aisc_shapes_v16.json`.
2. Create `engine_oracle/data/lumber_table.json`: nominal-to-actual sizes, cross-section props (A, Sx, Sy, Ix, Iy) for 2x4 through 6x6 lumber.
3. Implement `sections.py`:
   - `get_aisc_section(name: str) -> dict`: load from `aisc_shapes_v16.json`; return `{A, Ix, Iy, Sx, Sy, Zx, Zy, J, Cw, tf, tw, d, bf}`.
   - `get_lumber_section(nominal: str) -> dict`: load from `lumber_table.json`; return `{A, Sx, Sy, Ix, Iy}`.
4. Implement `checks.py`:
   - `dc_ratio_axial(force_kN, area_mm2, Fy_MPa) -> float`: P/(φ × A × Fy) where φ=0.9.
   - `dc_ratio_bending(moment_kNm, Sx_mm3, Fy_MPa) -> float`: M/(φ × Sx × Fy) where φ=0.9.
   - `check_member(member_result: dict, section: dict, material: str) -> CheckResult`: run both checks; return `{dc_ratio: float, passed: bool, governing_limit: str, disclaimer: str}`.
   - `disclaimer` must always be: `"Preliminary check — not sealed engineering. Verify with a licensed structural engineer before construction."`
5. Register `struct_check(doc_path: str, load_combo: str) -> dict` MCP tool.
6. Test: W8x31 steel beam with moment at 80% capacity; assert D/C ≈ 0.8, passed=True.

## Definition of done

- [ ] AISC shapes DB bundled under `engine_oracle/data/aisc_shapes/`
- [ ] `get_aisc_section("W8X31")` returns correct props; `get_lumber_section("2x4")` returns correct actual dims
- [ ] `check_member()` returns D/C ratio and passes the disclaimer string
- [ ] `struct_check` MCP tool registered; always includes disclaimer in response
- [ ] `python -m pytest engine_oracle/structural/test_checks.py -q` passes

## AI hints

- `graphify query "material structural section properties"` — check if any section library already exists
- AISC shapes DB download URL: https://www.aisc.org/globalassets/aisc/manual/v16.0-shapes-database/aisc-shapes-database-v16.0.xlsx — convert to JSON; or use the CSV at steel-shapes.org (public domain)
- Relevant files: `engine_oracle/structural/analysis.py`, `engine_oracle/structural/elements.py`, `services/mcp/server.py`
- For NDS lumber checks: Fb (allowable bending stress) for Douglas Fir No.2: 900 psi; Fc (compression parallel): 1350 psi — use public-domain values from NDS Supplement tables as found in open textbooks, not from the NDS document itself
