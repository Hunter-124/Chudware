---
id: T-P2
title: Unified parts library v2 - all-domain adapters + live vendor federation
workstream: P
owner: unassigned
status: todo
branch: task/T-P2-parts-library
day: 2
depends_on: [T-P1]
---

## Context

Wave 2 of the Erik-directed parts unification: every remaining catalog becomes a
CatalogProvider, and vendor APIs join the federation as live (non-indexed) providers.

## Files / areas touched

- engine_oracle/parts_hub/adapters/: ecad_kicad (kicad_sym user libs + ecad_find_part),
  ecad_vendor (B1b Nexar/DK/Mouser ProviderChain as LIVE provider — rate-limited,
  never indexed), structural_sections (T-D3 aisc_shapes + lumber JSON), piping_fittings
  (T-E1 catalog), hydraulic_components (T-E5 ISO 1219 set)
- services/mcp/parts_tools.py — part_search grows domain filters + source attribution
  (local vs live); results merge deterministically

## Definition of done

- [ ] All six domains searchable through the ONE part_search surface
- [ ] Live vendor results marked source:"live", degrade gracefully key-less/offline
      (CI-safe — B1b precedent)
- [ ] Cross-domain query test: "M3" surfaces fasteners AND ecad footprints, ranked sanely
- [ ] Per-adapter tests + federation merge tests; mcp-venv real counts
- [ ] gate + graphify update .

## Driver notes (Fable)

- Read contracts/parts.md (frozen by T-P1) FIRST; adapters conform, never extend the
  envelope silently (questions file for gaps).
- Vendor chain: reuse engine_oracle/sourcing ProviderChain exactly (raises→chain-None
  contract per work/questions/T-B1b.md ruling). No scraping, no McMaster (licensing).
- Structural/piping/hydraulic adapters are thin JSON-table wraps — resist the urge to
  normalize their native param names; PartRecord.params carries them verbatim +
  a small common core (nominal size, standard).
