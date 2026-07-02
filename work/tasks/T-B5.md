---
id: T-B5
title: Spec-sheet matching v2 - AI path from datasheet and integration demo
workstream: B
owner: opus-vane
status: doing
branch: task/T-B5-spec-sheet-ai-integration
day: 5
depends_on: [T-B4]
---

## Context

The AI path: the LLM extracts a dimension dict from a pasted datasheet or listing URL, fills a parametric template (`script` feature), triggers a GSL render for user confirmation, then registers the result as a VendorPart with `source: "spec_sheet"`. This reuses the INTAKE plan's `search_reference_data` schema. Day 5 also connects workstream B to the integration demo: the soil-moisture sensor demo needs a priced BOM with supplier links and a PCB fab quote from JLCPCB.

## Files / areas touched

- `engine_oracle/spec_sheet_matcher.py` — add `ai_extract_dims()` and `register_from_spec_sheet()` to existing module
- `engine_oracle/intake/` — `search_reference_data` schema (existing INTAKE plan artifacts)
- `services/mcp/server.py` — register `spec_sheet_match` tool (AI path)
- `engine_oracle/bom.py` — wire spec-sheet matched parts into BOM resolution

## Steps

1. Run `graphify query "intake search_reference_data spec sheet"` to find the INTAKE plan schema.
2. Implement `ai_extract_dims(text_or_url: str) -> dict`: prompt the LLM with the datasheet text; extract keys matching the parametric template (dimensions, tolerances, ratings); return a validated dict.
3. Implement `register_from_spec_sheet(dims: dict, template_name: str, mpn: str) -> VendorPart`: call the `script` feature with `dims`; invoke GSL render; write a VendorPart with `source="spec_sheet"`, link to the generated doc.
4. Register `spec_sheet_match(text_or_url: str, mpn: str, template: str) -> dict` MCP tool: runs AI extract → parametric generate → GSL render → return VendorPart dict + preview URL.
5. Integration demo prep: run `bom_price` on the demo assembly; call `request_quote` for JLCPCB PCB fab; ensure the sourcing panel shows supplier links and the priced BOM.
6. Fix any wiring gaps found during integration.

## Definition of done

- [ ] `ai_extract_dims` extracts dimensions from a pasted capacitor datasheet (test fixture)
- [ ] `register_from_spec_sheet` creates a VendorPart with `source="spec_sheet"` and a linked model doc
- [ ] `spec_sheet_match` MCP tool registered and reachable
- [ ] Integration demo: priced BOM displayed for the sensor demo with at least one spec-sheet-matched part
- [ ] `python -m pytest engine_oracle/test_spec_sheet_matcher.py -q` passes (including AI path with mocked LLM)

## AI hints

- `graphify query "script feature parametric template GSL"` — understand the `script` feature and GSL render path
- `graphify query "INTAKE search_reference_data"` — find the intake schema to reuse
- Relevant files: `engine_oracle/intake/`, `engine_oracle/spec_sheet_matcher.py`, `engine_oracle/vendor_parts.py`, `engine_oracle/kernel.py`
- For the AI extraction prompt, use a structured output schema (JSON mode) with keys from the parametric template; validate against template's required dims before calling generate
