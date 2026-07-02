---
id: T-B3
title: Cost model (material + machining) and quote-out tools
workstream: B
owner: unassigned
status: todo
branch: task/T-B3-cost-model
day: 3
depends_on: [T-B2]
---

## Context

Made-part cost = material cost (volume × density × $/kg from a seeded, user-overridable table) + machining cost (existing CAM `cycle_time_s` × machine-rate config). Quote-out sends the STEP file to Xometry (instant quote API) and the board to JLCPCB (PCB quote API) via a `request_quote` MCP tool. Material prices optionally refresh from Commodities-API for live metal/lumber rates.

## Files / areas touched

- `engine_oracle/cost_model.py` — new: material cost + machining cost calculators
- `engine_oracle/quote_providers.py` — new: XometryProvider, JLCPCBProvider
- `services/mcp/server.py` — register cam_cost_estimate, request_quote tools
- `engine_oracle/bom.py` — fill `cost_model_result` for made lines (T-B2 left this None)
- CAM module (existing) — `cycle_time_s` must be accessible; find via graphify

## Steps

1. Run `graphify query "CAM cycle time simulation"` to find how `cycle_time_s` is exposed.
2. Implement `cost_model.py`:
   - `MATERIAL_RATES: dict[str, float]` — seeded table ($/kg): steel 2.0, aluminum 3.5, titanium 35.0, PLA 25.0, cedar_lumber 1.2, etc. User-overridable via config file.
   - `material_cost(volume_mm3, material_name) -> float`: volume × density[material] × rate[material].
   - `machining_cost(cycle_time_s, machine_rate_per_hr=75.0) -> float`.
   - Optional: `live_metal_price(symbol) -> float` via Commodities-API (graceful stub if no key).
3. Implement `quote_providers.py`:
   - `XometryProvider.quote(step_path, material, quantity) -> QuoteResult`: POST STEP to Xometry API; stub if `XOMETRY_API_KEY` not set.
   - `JLCPCBProvider.quote(gerber_zip, layers, quantity) -> QuoteResult`: POST to JLCPCB quote API; stub if key not set.
4. Register MCP tools in `server.py`:
   - `cam_cost_estimate(doc_path: str) -> dict` — runs cost model on made parts
   - `request_quote(doc_path: str, vendor: str, quantity: int) -> dict` — dispatches to Xometry or JLCPCB
5. Wire `cost_model` into `bom.py`: fill `cost_model_result` for made BomLines.

## Definition of done

- [ ] `material_cost()` and `machining_cost()` return plausible values for steel/aluminum/cedar
- [ ] `cam_cost_estimate` MCP tool returns a cost dict for a doc with CAM operations
- [ ] `request_quote` MCP tool stubs gracefully when API keys absent; returns a real quote dict when keys present
- [ ] `python -m pytest engine_oracle/test_cost_model.py -q` passes

## AI hints

- `graphify query "CAM cycle time operation"` — find cycle_time_s in the CAM module
- `graphify query "STEP export doc path"` — find how to get the STEP file path for a doc
- Relevant files: `engine_oracle/`, CAM module path (verify with graphify), `services/mcp/server.py`
- Commodities-API free tier: `https://commodities-api.com/api/latest?access_key=KEY&base=USD&symbols=ALUM,COPPER,TIMBER` — graceful no-op if key absent
