---
id: T-B2
title: BOM rollup with bom_rollup and bom_price MCP tools
workstream: B
owner: opus-vale
status: done
branch: task/T-B2-bom-rollup
day: 2
depends_on: [T-B1b]
---

## Context

Walk the assembly tree and ECAD BOM CSV to produce a unified BOM that classifies each line as "made" (our geometry + CAM) or "bought" (resolved via PartProvider). The `bom_rollup` MCP tool returns the unified BOM dict; `bom_price` adds resolved prices. This BOM is the D5 cost-takeoff input (T-D5 depends on this) and the integration demo's order summary.

## Files / areas touched

- `engine_oracle/bom.py` — new module: assembly-tree walker + ECAD CSV merge
- `engine_oracle/ecad/` — ECAD BOM CSV export (existing); find the export function
- `services/mcp/server.py` — register bom_rollup, bom_price tools
- `engine_oracle/parts_providers.py` — ProviderChain called for bought-part resolution

## Steps

1. Run `graphify query "assembly tree BOM ECAD CSV"` to find existing BOM/assembly traversal code.
2. Implement `engine_oracle/bom.py`:
   - `rollup(doc_path) -> list[BomLine]`: walk the assembly tree (call existing assembly API); merge with ECAD BOM CSV if a linked board doc exists; classify each line.
   - `BomLine`: `{id, description, quantity, unit, kind: "made"|"bought", mpn: str|None, vendor_part: VendorPart|None, cost_model_result: dict|None}`.
3. Implement `resolve_prices(bom: list[BomLine]) -> list[BomLine]`: for bought lines, call `ProviderChain.fetch(mpn)`; for made lines, leave `cost_model_result=None` (filled by T-B3).
4. Register MCP tools in `server.py`:
   - `bom_rollup(doc_path: str) -> dict` — returns serialized BOM
   - `bom_price(doc_path: str) -> dict` — returns BOM with prices resolved
5. Test: fixture assembly + mock providers; assert bought lines have `vendor_part` populated, made lines have `vendor_part=None`.

## Definition of done

- [x] `bom_rollup` MCP tool returns a unified BOM with made/bought classification for the demo assembly
- [x] `bom_price` MCP tool resolves bought-line prices via ProviderChain
- [x] ECAD BOM CSV merged when a linked board doc is present
- [x] `python -m pytest engine_oracle/test_bom.py -q` passes

## Implementation notes (T-B2, opus-vale)

- `BomLine` follows the FROZEN `aicad.sourcing/1` §3 shape field-for-field (superseding this
  task file's older sketch). Two additive-OPTIONAL fields (`mpn`, `description`) added per
  contract §0 (no version bump) — see `work/questions/T-B2.md` Q1.
- `engine_oracle/bom.py`: `rollup(doc_path)` walks the `aicad.asm/1` component tree (injectable
  loaders → C++ core `doc_tree` by default), classifies made/bought, aggregates repeated
  instances, and expands linked ECAD board components through the real `write_bom` CSV.
  `resolve_prices()` folds `ProviderChain` pricing into bought lines (declined fetch → price
  stays `None`, never fabricated — §0). Provider seam raises `ProviderError`; chain/resolver
  returns `None` (per `work/questions/T-B1b.md` ruling).
- MCP tools `bom_rollup` / `bom_price` in `services/mcp/sourcing_tools.py`; append-only
  registration in `services/mcp/server.py`.
- No new pip deps (stdlib + engine modules only) → no `contracts/licenses.md` row.
- Upstream `write_bom` drops the MPN column; worked around by recovering MPN from board
  footprints (Q2). Out of scope to fix that shared exporter here.
- Merge order (binding): B1a → B1b → B2. Branch based on `task/T-B1b-part-providers`.

## AI hints

- `graphify query "assembly tree traversal document"` — find how to walk the assembly hierarchy
- `graphify query "ECAD BOM export CSV"` — find the ECAD BOM CSV export path
- Relevant files: `engine_oracle/ecad/`, `engine_oracle/parts_providers.py`, `engine_oracle/vendor_parts.py`, `services/mcp/server.py`
- BomLine is internal; MCP tools return JSON-serializable dicts (call `dataclasses.asdict`)
