---
id: T-B4
title: Spec-sheet matching v1 - deterministic BOLTS/KiCad lookup
workstream: B
owner: opus-birch
status: done
branch: task/T-B4-spec-sheet-deterministic
day: 4
depends_on: [T-B3]
lane: parts-sourcing
kind: new
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

- [x] BOLTS blt tables bundled under `engine_oracle/data/bolts/` (metric fasteners + bearings minimum) — clean-room ISO/DIN `.blt.yaml` tables (BOLTS is GPL-3.0, NOT vendored; see Q1)
- [x] `normalize_mpn` correctly strips at least 10 common vendor suffixes — 14 patterns; test asserts ≥10, legit numeric PNs preserved
- [x] `match_bolts` returns params for standard ISO metric fasteners and bearings — fasteners (family hints), bearings (seal codes), explicit standards
- [x] `match_part_number` MCP tool registered and returns model_path for a matched M3 screw — registered via `spec_tools.py`; returns `model_path` (real path with native core, `null` offline; see Q3)
- [x] `python -m pytest engine_oracle/test_spec_sheet_matcher.py -q` passes — 37 passed, offline/key-free

## Implementation notes (opus-birch)

- **License (Q1):** BOLTS is GPL-3.0 → NOT vendored. Bundled clean-room ISO/DIN tables under
  `engine_oracle/data/bolts/*.blt.yaml` transcribed from `hardware.md` / `core/parts/parts_data.h`
  (non-copyrightable dimensional standards; IPC-7351 precedent). Loader parses the documented
  BOLTS `.blt` format so a user's own BOLTS checkout also works. `contracts/licenses.md` rows added.
- **KiCad (Q2):** `match_kicad` reads only the user's own libraries via `AICAD_KICAD_SYM_PATH`,
  using the existing clean-room `.kicad_sym` parser; nothing KiCad vendored; `None` offline.
- **Wiring (Q3):** new `services/mcp/spec_tools.py` + append-only `server.py` hunk (T-B3 idiom).
- **T-B3 Q2 seam:** `match_part_number` is the real per-component spec→geometry resolver the
  cost-model seam anticipated; `generate_model` builds via the `parts_make` core RPC.
- **Verification:** `pytest engine_oracle/test_spec_sheet_matcher.py` = 37 passed; sourcing/cost/
  bom/vendor suites = 29 passed (no regressions); `bash scripts/gate.sh` core build + ctest PASS
  (pre-existing reds only: check.py parity/feature-surface audit, ui/imgui selMulti, pytest-stage
  marker-filter exit 5 — all documented, none introduced here).

## AI hints

- `graphify query "parametric part generator fastener screw"` — find existing `core/parts` generators
- BOLTS data repo: https://github.com/boltsparts/BOLTS (MIT license for code, LGPL for data — use data only)
- Relevant files: `engine_oracle/parts_library.py`, `core/parts/`, `engine_oracle/vendor_parts.py`
- KiCad symbol libraries (github.com/KiCad/kicad-symbols) are GPL — use as spec reference only; parse the `.kicad_sym` XML format to extract footprint metadata, never import KiCad Python code
