---
id: T-P2
title: Unified parts library v2 - all-domain adapters + live vendor federation
workstream: P
owner: sonnet-reef
status: done
branch: task/T-P2-parts-library
day: 2
depends_on: [T-P1]
---

## Context

Wave 2 of the Erik-directed parts unification: every remaining catalog becomes a
CatalogProvider, and vendor APIs join the federation as live (non-indexed) providers.

## Files / areas touched

- `engine_oracle/parts_hub/adapters/structural_sections_adapter.py` (NEW)
- `engine_oracle/parts_hub/adapters/piping_fittings_adapter.py` (NEW)
- `engine_oracle/parts_hub/adapters/hydraulic_components_adapter.py` (NEW)
- `engine_oracle/parts_hub/adapters/ecad_kicad_adapter.py` (NEW)
- `engine_oracle/parts_hub/adapters/ecad_vendor_adapter.py` (NEW)
- `engine_oracle/parts_hub/hub.py` — bootstrap all 7 providers + live-vendor fallback search
- `engine_oracle/parts_hub/test_parts_hub_p2.py` (NEW — 51 tests)
- `services/mcp/parts_tools.py` — updated docstring + has_live flag in response envelope
- `engine_oracle/ecad/library/starter.py` — added MountingHole_3.2mm_M3 footprint (M3 cross-domain DoD)

## Definition of done

- [x] All six domains searchable through the ONE part_search surface
- [x] Live vendor results marked source:"live", degrade gracefully key-less/offline
      (CI-safe — B1b precedent; all vendor tests mocked + keyless-graceful)
- [x] Cross-domain query test: "M3" surfaces fasteners AND ecad footprints, ranked sanely
- [x] Per-adapter tests + federation merge tests; mcp-venv real counts: **51/51 new + 44/44 T-P1 = 95/95**
- [x] gate + graphify update .
      Gate: core PASS, ui/abstract PASS, ctest 100%, ui/imgui PASS.
      FAIL-RECORDED (pre-existing, non-fatal): check.py audits (feature_surface_audit) +
      pytest stage 4 107-deselected gate-venv gap (issue #6). No new regressions.

## Cross-domain "M3" sample output

```
part_search("M3") — 7 hits across 2 domains:
  fastener M3  — "Flat Washer M3 flat washer washer"           (exact-designation rank=+1000, first)
  fastener M3  — "Hex Nut M3 hex nut nut"
  fastener M3  — "Hex Head Bolt M3 hex head bolt hhcs"
  fastener M3  — "Button Head Cap Screw M3 button head bhcs"
  fastener M3  — "Flat Head Cap Screw M3 flat head fhcs"
  fastener M3  — "Socket Head Cap Screw M3 shcs"
  ecad   MountingHole:MountingHole_3.2mm_M3  — "MountingHole:MountingHole_3.2mm_M3 KiCad footprint — Mounting hole 3.2 mm for M3 screw"
```

Fasteners rank first (exact designation +1000), ecad mounting hole surfaces via BM25 on title.
Both domains present in top-20 unfiltered results.

## Architecture decisions made

1. **ecad_kicad is INDEXABLE** (iter_records enumerates starter symbols/footprints + user libs).
   ecad_vendor is LIVE-ONLY (iter_records raises NotImplementedError).
   This is the correct split per contracts/parts.md §6.1 — ecad is one of the four domain
   adapters, not a live-only provider.

2. **MountingHole_3.2mm_M3 added to starter** — clean-room IPC-7251 geometry (3.2 mm drill,
   NPTH, courtyard 4 mm, silkscreen 3.4 mm circle). Required so "M3" surfaces an ecad hit
   even without AICAD_KICAD_SYM_PATH configured.

3. **Live-vendor fallback in hub.search()** — when FTS5 index has no hits AND domain matches
   a live provider, live providers are queried directly (not blended with index results).
   Per contracts/parts.md §6.1 "falls back to live search when the local index has no hits."

4. **Params verbatim**: native column names (A, Ix, Sx, nps, center_to_end, component_type)
   carried as-is; only small common core (nominal_size, standard) added.

5. **source:"live" is an additive optional field** on live PartRecords — contract permits
   additive fields without a bump; has_live flag added to part_search response envelope.

## Driver notes (Fable)

- Read contracts/parts.md (frozen by T-P1) FIRST; adapters conform, never extend the
  envelope silently (questions file for gaps).
- Vendor chain: reuse engine_oracle/sourcing ProviderChain exactly (raises→chain-None
  contract per work/questions/T-B1b.md ruling). No scraping, no McMaster (licensing).
- Structural/piping/hydraulic adapters are thin JSON-table wraps — resist the urge to
  normalize their native param names; PartRecord.params carries them verbatim +
  a small common core (nominal size, standard).
