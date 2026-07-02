---
id: T-B5
title: Spec-sheet matching v2 - AI path from datasheet and integration demo
workstream: B
owner: opus-vane
status: review
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

- [x] `ai_extract_dims` extracts dimensions from a pasted capacitor datasheet (test fixture)
      — `spec_sheet_matcher.ai_extract_dims`; LLM via T-F9 provider wrapped in T-H7 LoggingProvider
      (IP-16, flywheel-logged). `test_ai_extract_dims_from_capacitor_datasheet` + unit-coercion +
      missing-required + only-template-keys tests.
- [x] `register_from_spec_sheet` creates a VendorPart with `source="spec_sheet"` and a linked model doc
      — parametric template registry (capacitor_radial/axial/box/cylinder) → `doc_apply_ops`
      geometry → `register_local_cad(source="spec_sheet")`. `test_register_from_spec_sheet_creates_vendor_part`
      asserts source + linked cad_path + specs. Offline (no core) degrades to a "no model" result
      (no hollow record) — `test_register_offline_no_model_degrades`.
- [x] `spec_sheet_match` MCP tool registered and reachable — `services/mcp/spec_tools.py`
      (`spec_sheet_match` + `list_spec_templates`), rides the existing `register_spec_tools(mcp)`
      wiring from T-B4 (server.py untouched, append-only trivially satisfied).
      `test_spec_sheet_match_tool_registered`.
- [x] Integration demo: priced BOM displayed for the sensor demo with at least one spec-sheet-matched part
      — `engine_oracle/spec_sheet_demo.py` (`run_demo` + `python -m engine.spec_sheet_demo`):
      soil-moisture sensor BOM (made enclosure + spec-sheet-matched cap + linked PCB), priced via
      injectable chain w/ supplier links, + JLCPCB fab quote (degrades to §0 offline). Hermetic
      (isolated temp registry). `test_spec_sheet_demo.py` (4 tests).
- [x] `python -m pytest engine_oracle/test_spec_sheet_matcher.py -q` passes (including AI path with mocked LLM)
      — 57 passed (matcher+demo); B-chain regression sweep (bom/cost_model/vendor_parts/parts_providers)
      99 passed. Ran under mcp-venv with `AI_CAD_LIBAICAD` set.

## Closeout (opus-vane, 2026-07-02)

- **Gate** (`bash scripts/gate.sh`): core build PASS, ui/abstract PASS, ctest 100% PASS,
  **ui/imgui build+shot PASS** (the real regression check — my changes are Python-only). Overall
  RESULT=FAIL is entirely the documented pre-existing reds: check.py feature_surface_audit +
  parity_audit (FOREMAN "do not chase"), pytest 107-deselected gate-venv gap (issue #6). Real
  mcp-venv pytest of the touched suites = green (see DoD line 5).
- **graphify update .** run on master checkout (graphify-out is gitignored; no git mutation).
- **Notes / non-blocking** in `work/questions/T-B5.md`: (N1) `search_reference_data`/`script`/GSL
  are plan-doc terms not real schemas — modeled on real primitives; (N2) **LATENT T-H7 bug** —
  `LoggingProvider._default_sidecar_dir` references `engine.paths.DATA` which is absent on Linux;
  crashes any `wrap()` without an explicit `sidecar_dir`. Worked around (I pass `sidecar_dir`
  explicitly); recommend T-H7 owner point it at `engine.paths.CACHE`. (N3) IP-16 + suppress_emit
  wiring; (N4) demo pricing preserves spec_sheet provenance; (N5) server.py needs no edit.

## AI hints

- `graphify query "script feature parametric template GSL"` — understand the `script` feature and GSL render path
- `graphify query "INTAKE search_reference_data"` — find the intake schema to reuse
- Relevant files: `engine_oracle/intake/`, `engine_oracle/spec_sheet_matcher.py`, `engine_oracle/vendor_parts.py`, `engine_oracle/kernel.py`
- For the AI extraction prompt, use a structured output schema (JSON mode) with keys from the parametric template; validate against template's required dims before calling generate
