---
id: T-B2
title: BOM rollup with bom_rollup and bom_price MCP tools
workstream: B
owner: opus-vale
status: doing
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

- [ ] `bom_rollup` MCP tool returns a unified BOM with made/bought classification for the demo assembly
- [ ] `bom_price` MCP tool resolves bought-line prices via ProviderChain
- [ ] ECAD BOM CSV merged when a linked board doc is present
- [ ] `python -m pytest engine_oracle/test_bom.py -q` passes

## AI hints

- `graphify query "assembly tree traversal document"` — find how to walk the assembly hierarchy
- `graphify query "ECAD BOM export CSV"` — find the ECAD BOM CSV export path
- Relevant files: `engine_oracle/ecad/`, `engine_oracle/parts_providers.py`, `engine_oracle/vendor_parts.py`, `services/mcp/server.py`
- BomLine is internal; MCP tools return JSON-serializable dicts (call `dataclasses.asdict`)
