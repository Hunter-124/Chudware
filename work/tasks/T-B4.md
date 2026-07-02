---
id: T-B4
title: Spec-sheet matching v1 - deterministic BOLTS/KiCad lookup
workstream: B
owner: unassigned
status: todo
branch: task/T-B4-spec-sheet-deterministic
day: 4
depends_on: [T-B3]
---

## Context

The deterministic path for spec-sheet matching uses BOLTS (LGPL data, redistributable) blt tables to generate standard fastener/bearing geometry via existing `core/parts` generators. Part-number normalization (strip vendor suffixes like `-ND`, `-TR`, `-CT-ND`) maps purchase PNs to MPN → BOLTS/KiCad lookup. The output is a parametric model linked to the BomLine as `vendor_part.source = "bolts"` or `"kicad"`.

## Files / areas touched

- `engine_oracle/spec_sheet_matcher.py` — new: deterministic matcher
- `engine_oracle/bolts_loader.py` — new: parse BOLTS blt/YAML tables
- `core/parts/` — existing parametric part generators (fasteners, bearings)
- `engine_oracle/vendor_parts.py` — VendorPart gains `source` field if not already present
- `services/mcp/server.py` — register `match_part_number` tool

## Steps

1. Run `graphify query "BOLTS parts generators core/parts"` to understand existing parametric generators.
2. Download/bundle BOLTS blt tables (LGPL data) under `engine_oracle/data/bolts/` — ISO metric fasteners, bearings, structural sections.
3. Implement `bolts_loader.py`: parse blt YAML → dict of `{standard: {size: {params}}}`.
4. Implement `spec_sheet_matcher.py`:
   - `normalize_mpn(pn: str) -> str`: strip common vendor suffixes (regex list).
   - `match_bolts(mpn: str) -> dict | None`: look up normalized MPN in BOLTS tables; return param dict if found.
   - `match_kicad(mpn: str) -> dict | None`: look up in KiCad symbol library metadata (use KiCad data as spec reference — clean-room; never import KiCad GPL code).
   - `generate_model(match: dict) -> str`: call existing `core/parts` generator with params; return doc path.
5. Register `match_part_number(mpn: str) -> dict` MCP tool: runs normalize → BOLTS → KiCad → returns `{matched: bool, source, params, model_path}`.
6. Test: normalize `M3x8-ND` → `M3x8`; match against BOLTS ISO metric table; assert params returned.

## Definition of done

- [ ] BOLTS blt tables bundled under `engine_oracle/data/bolts/` (metric fasteners + bearings minimum)
- [ ] `normalize_mpn` correctly strips at least 10 common vendor suffixes
- [ ] `match_bolts` returns params for standard ISO metric fasteners and bearings
- [ ] `match_part_number` MCP tool registered and returns model_path for a matched M3 screw
- [ ] `python -m pytest engine_oracle/test_spec_sheet_matcher.py -q` passes

## AI hints

- `graphify query "parametric part generator fastener screw"` — find existing `core/parts` generators
- BOLTS data repo: https://github.com/boltsparts/BOLTS (MIT license for code, LGPL for data — use data only)
- Relevant files: `engine_oracle/parts_library.py`, `core/parts/`, `engine_oracle/vendor_parts.py`
- KiCad symbol libraries (github.com/KiCad/kicad-symbols) are GPL — use as spec reference only; parse the `.kicad_sym` XML format to extract footprint metadata, never import KiCad Python code
