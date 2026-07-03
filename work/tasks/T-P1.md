---
id: T-P1
title: Unified parts library v1 - parts/1 contract, federated search core, first two adapters
workstream: P
owner: sonnet-gale
status: done
branch: task/T-P1-parts-library
day: 1
depends_on: []
lane: parts-sourcing
kind: new
---

## Context

ERIK DIRECTIVE (2026-07-03): the standard parts library is one of our most valuable
features — the AI must be able to design ANYTHING (electronics, assemblies, piping,
structures) from standard parts, through ONE search surface. Today six catalogs exist
with no unifying layer: C++ core/parts families (make_standard_part), fastener/bearing
tables (engine_oracle/bolts_loader.py + spec_sheet_matcher.py), ECAD
(engine/ecad/library/kicad_sym.py + ecad_find_part + B1b vendor providers), structural
sections (T-D3 AISC/lumber JSON), piping fittings (T-E1), hydraulic components (T-E5).

## Files / areas touched

- contracts/parts.md — NEW, freeze `parts/1`: PartRecord envelope {part_id, domain,
  title, standard, designation, params{}, geometry_ref (instantiation recipe: core
  family+params | generate_model | symbol/footprint | section profile | fitting id),
  sourcing (aicad.sourcing/1 VendorPart refs), provenance, license} + CatalogProvider
  protocol {search(query, domain?, filters?) -> [PartRecord], get(part_id), count()}
- engine_oracle/parts_hub/ — NEW: provider registry, SQLite FTS5 search index (stdlib
  only — no new deps) built/refreshed from provider enumerations, ranking (exact
  designation > standard prefix > FTS score)
- Adapters #1 and #2: core/parts families (via the existing list_standard_parts JSON
  surface) and fasteners/bearings (bolts_loader designation_index)
- services/mcp/parts_tools.py — NEW: part_search, part_get (register append-only)

## Definition of done

- [x] parts/1 frozen; changes require version bump
      → contracts/parts.md written; PartRecord + CatalogProvider protocol frozen.
      → 7-domain vocab reserved; T-P2/T-P3/T-BL12 seams documented.
- [x] part_search("M8 socket head") and part_search("bearing 6205") return ranked
      PartRecords from the fastener adapter; part_search(domain="mech_family") lists
      core families
      → Verified: "M8 socket head" → fastener:ISO 4762:M8 (rank 1)
      → Verified: "bearing 6205" → bearing:ISO 15:6205 bore=25mm OD=52mm W=15mm (rank 1)
      → domain="mech_family" → degrades gracefully (empty) when core absent, real list with core
      → Extended bearings.blt.yaml with 6200/6300 series (clean-room ISO 15) to enable 6205
- [x] FTS index rebuilds idempotently; search p95 < 50ms on the full corpus
      → test_search_p95_latency passes; test_rebuild_idempotent passes
- [x] 20+ tests incl. envelope validation + both adapters; mcp-venv real counts
      → 44 tests pass in mcp-venv (44 passed, 0 failed, 0 skipped for parts suite)
      → Covers: envelope validation, BoltsAdapter (fastener+bearing), CoreFamiliesAdapter
        (graceful degradation), PartsIndex (FTS + ranking + p95), PartsHub (DoD examples),
        MCP tools (part_search/part_get)
- [x] gate + graphify update .
      → gate.sh: core PASS, ui/abstract PASS, ctest 100%, ui/imgui PASS
      → check.py FAIL-RECORDED (pre-existing); pytest FAIL = known 0-collected gate-venv gap
      → graphify update . run on branch

## Deliverables

- contracts/parts.md — aicad.parts/1 frozen contract
- engine_oracle/data/bolts/bearings.blt.yaml — extended with 6200/6300 series
- engine_oracle/parts_hub/ — ProviderRegistry + PartsIndex (FTS5) + PartsHub + adapters
- services/mcp/parts_tools.py (= mcp_server/parts_tools.py via symlink) — part_search, part_get
- services/mcp/server.py — append-only registration
- 44 tests in engine_oracle/parts_hub/test_parts_hub.py

## Sample outputs (DoD verification)

```
part_search("M8 socket head"):
  'fastener:ISO 4762:M8'  | Socket Head Cap Screw M8 socket head cap screw ...
  'fastener:ISO 4017:M8'  | Hex Head Bolt M8 ...
  'fastener:ISO 7380:M8'  | Button Head Cap Screw M8 ...

part_search("bearing 6205"):
  'bearing:ISO 15:6205'   | Deep-Groove Ball Bearing 6205 ...  bore=25.0mm OD=52.0mm W=15.0mm
  'bearing:ISO 15:623'    | Deep-Groove Ball Bearing 623 ...
```

## Driver notes (Fable)

- ADAPTER pattern is the ruling — do NOT rewrite or move any existing catalog; wrap them.
  Domain vocab: fastener|bearing|section|fitting|hydraulic|ecad|mech_family (extensible).
- geometry_ref is a RECIPE not geometry: it names the instantiation path T-P3 will route.
- Sourcing links reuse aicad.sourcing/1 VendorPart AS-IS (T-B1a, merged) — no parallel
  vendor schema. Vendor LIVE search is T-P2's job, not yours.
- stdlib sqlite3 FTS5 only. License/provenance fields REQUIRED on every record
  (clean-room lineage per T-B4/T-D3 precedents).
