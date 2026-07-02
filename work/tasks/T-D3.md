---
id: T-D3
title: Section libraries, D/C ratio checks, and struct_check MCP tool
workstream: D
owner: sonnet-mave
status: review
branch: task/T-D3-section-libraries-checks
day: 3
depends_on: [T-D2]
finisher: sonnet-lume
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

- [x] AISC shapes DB bundled under `engine_oracle/data/aisc_shapes/` — 18-shape curated W/S/C table (`aisc_shapes_curated.json`); dimensional facts from public-domain refs; clean-room posture per Q1 in `work/questions/T-D3.md`; licenses.md row added. Note: full 3000-row AISC XLSX not bundled (license unclear); T-D4/T-D5 can accept a user-supplied XLSX at runtime.
- [x] `get_aisc_section("W8X31")` returns correct props — A=5884 mm², Sx=4.506e5 mm³, Ix=4.574e7 mm⁴ (all within ±2% of published, verified by test); `get_lumber_section("2x4")` returns b=38 mm, d=89 mm (PS 20 actual dressed dims, verified by test). Lumber table covers 2x4 through 6x12.
- [x] `check_member()` returns D/C ratio and passes the disclaimer string — LRFD steel (φ=0.90, Fy from A36/A992 lookup) and NDS ASD wood (Fb/Fc for DF-No.2/SPF/Cedar); `DISCLAIMER` field byte-exact on every `CheckResult`.
- [x] `struct_check` MCP tool registered in `mcp_server/structural_tools.py`; `register_structural_tools` called from `services/mcp/server.py` (append-only line 149–150); always includes `disclaimer` at top level and per-row. Registration verified by `TestStructuralToolsRegistered` (no PyNite required).
- [x] `pytest engine_oracle/structural/test_checks.py -q` passes — **37 passed, 6 skipped** (PyNite PyPI absent in mcp-venv; skips are D2 PyNite integration tests, not T-D3 logic). T-D3 contributes 32 tests including: hand-calc validation (W8x31/A992 literal-demand pin — see below), absolute capacity pin (φMy ≈ 139.8 kN·m), wood ASD, axial governs, disclaimer always-present, MCP registration.

## Hand-calc validation case (workstream review bar)

W8x31 A992 steel beam, pure bending, AISC LRFD elastic:

- Published: Sx = 27.5 in³ = 4.506×10⁵ mm³, Fy = 344.7 MPa (50 ksi), φ = 0.90
- φMy = 0.90 × 4.506×10⁵ × 344.7 = **139.8 kN·m** (139 801 620 N·mm)
- Demand at 80%: M = 0.80 × 139.8 kN·m = **111.8 kN·m** (literal: 111 900 000 N·mm in test)
- Expected D/C = 111.9 / 139.8 = **0.800**, passed=True, governing_limit="bending_yield_LRFD"
- Test `test_capacity_absolute_value` feeds the **literal** 111 900 000 N·mm demand and asserts D/C ≈ 0.80 to catch any Sx units bug; also asserts φMy ≈ 139.8 kN·m independently.

## Known v1 limitations (non-blocking, noted for T-D4/D5)

- `_section_for` fallback Sx proxy (`2·Iz/√A`) is approximate for non-square sections; only used when section_name is in neither AISC nor lumber table.
- `struct_check` error path (solver unavailable) omits disclaimer — not engineering output, by design.
- No slenderness (Euler buckling) or column stability (Cp) — conservative for short compact members; label says "axial_yield_LRFD" / "axial_compression_ASD_NDS" to distinguish from full buckling check.

## AI hints

- `graphify query "material structural section properties"` — check if any section library already exists
- AISC shapes DB download URL: https://www.aisc.org/globalassets/aisc/manual/v16.0-shapes-database/aisc-shapes-database-v16.0.xlsx — convert to JSON; or use the CSV at steel-shapes.org (public domain)
- Relevant files: `engine_oracle/structural/analysis.py`, `engine_oracle/structural/elements.py`, `services/mcp/server.py`
- For NDS lumber checks: Fb (allowable bending stress) for Douglas Fir No.2: 900 psi; Fc (compression parallel): 1350 psi — use public-domain values from NDS Supplement tables as found in open textbooks, not from the NDS document itself
