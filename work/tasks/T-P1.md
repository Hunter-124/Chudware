---
id: T-P1
title: Unified parts library v1 - parts/1 contract, federated search core, first two adapters
workstream: P
owner: sonnet-gale
status: doing
branch: task/T-P1-parts-library
day: 1
depends_on: []
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

- [ ] parts/1 frozen; changes require version bump
- [ ] part_search("M8 socket head") and part_search("bearing 6205") return ranked
      PartRecords from the fastener adapter; part_search(domain="mech_family") lists
      core families
- [ ] FTS index rebuilds idempotently; search p95 < 50ms on the full corpus
- [ ] 20+ tests incl. envelope validation + both adapters; mcp-venv real counts
- [ ] gate + graphify update .

## Driver notes (Fable)

- ADAPTER pattern is the ruling — do NOT rewrite or move any existing catalog; wrap them.
  Domain vocab: fastener|bearing|section|fitting|hydraulic|ecad|mech_family (extensible).
- geometry_ref is a RECIPE not geometry: it names the instantiation path T-P3 will route.
- Sourcing links reuse aicad.sourcing/1 VendorPart AS-IS (T-B1a, merged) — no parallel
  vendor schema. Vendor LIVE search is T-P2's job, not yours.
- stdlib sqlite3 FTS5 only. License/provenance fields REQUIRED on every record
  (clean-room lineage per T-B4/T-D3 precedents).
