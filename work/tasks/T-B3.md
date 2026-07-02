---
id: T-B3
title: Cost model (material + machining) and quote-out tools
workstream: B
owner: opus-hale
status: review
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

- [x] `material_cost()` and `machining_cost()` return plausible values for steel/aluminum/cedar
- [x] `cam_cost_estimate` MCP tool returns a cost dict for a doc with CAM operations
- [x] `request_quote` MCP tool stubs gracefully when API keys absent; returns a real quote dict when keys present
- [x] `python -m pytest engine_oracle/test_cost_model.py -q` passes

## Implementation notes (opus-hale)

Branched FROM `task/T-B2-bom-rollup` (merge order **B1a → B1b → B2 → B3**).

- **`engine_oracle/cost_model.py`** — provenance-carrying seed table `_MATERIAL_SEED` (density_g_cm3
  mirrors the FEA `_MATERIALS` in cad_tools.py; rate_usd_per_kg carries `citation` + `asof`), from
  which the task-contract `MATERIAL_RATES: dict[str,float]` + `MATERIAL_DENSITY` are derived. Fully
  **config-overridable** (`paths.CONFIG` → `cost_model.{material_rates,densities,machine_rate_per_hr,
  setup_time_s}`). `material_cost`/`machining_cost`/`estimate_made_cost` (§3 cost_model object:
  material + machine + amortized setup → unit_cost + provenance). **Never fabricates**: unknown
  material / missing input → §0 contract-shaped unknown (`ok:false, reason, hint, unit_cost:null`),
  never a guessed numeral (standing supervisor ruling). `# SEAM(T-F2 merge)` marks the future join
  to the unified material registry (unmerged `task/T-F2-source-helpers`) — NOT depended on here.
  Optional `live_metal_price` (Commodities-API) is a graceful no-op with no key; never runs in CI.
- **`engine_oracle/quote_providers.py`** — `XometryProvider`/`JLCPCBProvider` producing
  `aicad.sourcing/1` §4 `QuoteResult`. Config-only keys (`quote_providers.<key>.api_key` + env
  fallback `XOMETRY_API_KEY`/`JLCPCB_KEY`), stdlib `urllib` only (**no new dep, no licenses.md
  row**), no scraping. Missing key → §0 degraded shape with no network call. Single `_http_post_json`
  seam is what tests monkeypatch for the key-present path (fully offline).
- **`engine_oracle/bom.py`** — new `resolve_costs()` fills made lines' `cost_model` + unit/ext cost
  via an injectable estimator seam (default core-backed, SEAM-noted for future per-line source
  plumbing — see Q2). Contract §0 honored: no numeric unit_cost ⇒ line stays unpriced.
- **`services/mcp/cost_tools.py`** (new, mirrors `sourcing_tools.py`) + **append-only** hunk in
  `server.py`: `cam_cost_estimate(doc_path)` (doc volume + CAM setup material + simulated
  `cycle_time_s` → §3 cost object) and `request_quote(doc_path, vendor, quantity)` (STEP export for
  mechanical / Gerber for PCB → approved API; missing key → graceful stub). See Q1.
- **cycle_time_s seam**: `core_bridge.call("cam_simulate", {"doc_id"})` → `results[].cycle_time_s`
  (C++ core `core/cam/…`, exposed via the CAM MCP tools). Volume: `doc_open` result `volume`.

### Verification
- `python -m pytest engine_oracle/test_cost_model.py -q` → **14 passed** (offline, key-free; asserts
  the HTTP seam is never called without a key). `engine_oracle/test_bom.py` → **10 passed**
  (no T-B2 regression). Full `engine_oracle/` → 238 passed; the only failures/errors are the known
  pre-existing quirks (`doc/test_aicm.py` golden FileNotFoundError; ecad/sim collection errors) —
  none touch cost/quote/bom.
- `bash scripts/gate.sh`: my stages green (core build, ui/abstract, ctest). FAILs are all
  pre-existing known blockers: `check.py` FAIL-RECORDED; pytest stage `25 deselected` (marker-filter
  exit-5 quirk, zero cost_model failures); ui/imgui `PanelField::selMulti`.
- `graphify update .` run (new modules indexed).

### Open questions (non-blocking; recommended answers implemented) — `work/questions/T-B3.md`
- **Q1** `cost_tools.py` as a new MCP module (mirrors `sourcing_tools`; `server.py` append-only) vs.
  inline in `server.py`. Proceeded with the module (established one-family-per-module convention).
- **Q2** Made-line auto-costing needs a per-component source path `rollup()` doesn't yet thread;
  shipped the injectable `resolve_costs` mechanism + a SEAM; `cam_cost_estimate` does per-doc costing
  today. No fabricated cost in the interim.

## AI hints

- `graphify query "CAM cycle time operation"` — find cycle_time_s in the CAM module
- `graphify query "STEP export doc path"` — find how to get the STEP file path for a doc
- Relevant files: `engine_oracle/`, CAM module path (verify with graphify), `services/mcp/server.py`
- Commodities-API free tier: `https://commodities-api.com/api/latest?access_key=KEY&base=USD&symbols=ALUM,COPPER,TIMBER` — graceful no-op if key absent
