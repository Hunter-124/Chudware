---
id: T-D5
title: Cost takeoff and struct_estimate MCP tool with integration demo
workstream: D
owner: unassigned
status: todo
branch: task/T-D5-cost-takeoff-demo
day: 5
priority: 3
depends_on: [T-D4, T-B2]
---

## Context

Day 5 delivers cost takeoff from the structure doc: board-feet for lumber, steel weight, and fastener count feed into Workstream B's BOM rollup (`bom_rollup` / `bom_price`). The user-editable rate table is seeded with ENR national averages. The `struct_estimate` MCP tool returns a line-item cost estimate. The demo: cedar post under the sensor-enclosure load, checked (D/C < 1.0, code compliance passed), and priced. Cross-workstream dependency: `bom_rollup` must be available from T-B2.

## Files / areas touched

- `engine_oracle/structural/estimate.py` — new: takeoff calculator + rate table
- `engine_oracle/data/enr_rates.json` — ENR national average rate table (user-overridable)
- `engine_oracle/structural/bom_bridge.py` — new: calls engine_oracle/bom.py rollup with structural elements
- `services/mcp/server.py` — register struct_estimate tool

## Steps

1. Create `engine_oracle/data/enr_rates.json`: `{"cedar_lumber_per_bf": 4.50, "steel_per_lb": 1.20, "concrete_per_cy": 145.0, "labor_per_hr": 85.0, "fastener_per_ea": 0.35}` — seeded with ENR Q4-2025 national averages; user can override via config.
2. Implement `estimate.py`:
   - `board_feet(element: Post | Stud | Joist) -> float`: nominal size × length in linear feet ÷ 12 (standard BF formula).
   - `steel_weight_lbs(element: Beam | Column, section: dict) -> float`: A (in²) × length (ft) × (weight/ft from AISC DB).
   - `fastener_count(structure_doc) -> int`: count connections (each post base = 4 fasteners, each joist hanger = 10 fasteners, etc.).
   - `takeoff(structure_doc) -> list[TakeoffLine]`: run all quantities; multiply by rates from `enr_rates.json`.
3. Implement `bom_bridge.py`: `structural_bom_lines(structure_doc) -> list[BomLine]`: convert takeoff lines to BomLine format for `bom_rollup`.
4. Register `struct_estimate(doc_path: str) -> dict` MCP tool: runs takeoff; returns `{line_items, total_material_cost, total_labor_cost, currency, rate_table_date}`.
5. Integration demo: run `struct_estimate` on the demo cedar post; verify total cost is plausible (~$50–200 for a single post assembly); compare to `bom_price` output.

## Definition of done

- [ ] `board_feet()` returns correct BF for 4x4 post × 8ft = 10.67 BF
- [ ] `struct_estimate` MCP tool returns a line-item estimate with totals
- [ ] `struct_estimate` for the demo cedar post returns a total cost in the $50–200 range
- [ ] `bom_bridge.py` produces BomLine-compatible output consumable by `bom_rollup`
- [ ] `python -m pytest engine_oracle/structural/test_estimate.py -q` passes

## AI hints

- `graphify query "BOM rollup BomLine interface"` — verify BomLine schema from T-B2 before implementing bom_bridge
- `graphify query "ENR construction cost rate"` — check if any rate data already exists in repo
- Relevant files: `engine_oracle/structural/elements.py`, `engine_oracle/structural/sections.py`, `engine_oracle/bom.py`, `services/mcp/server.py`
- Board feet formula: (thickness_in × width_in × length_ft) / 12 — using nominal dimensions, not actual
- ENR Building Cost Index and Materials Cost Index are published monthly; seed from the most recent public issue available at ENR.com/economics
