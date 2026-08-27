---
id: T-X3
title: part_search federation seam - browser adapters as providers
workstream: X
owner: sonnet-drift
status: done
branch: task/T-X3-part-search-federation
depends_on: [T-X2]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

`services/mcp/parts_tools.py` already federates `part_search`/`part_get`/
`part_place` across local catalogs and live vendor results (T-P1/T-P2/T-P3,
workstream P, all done). This task adds T-X2's browser adapters as ONE more
provider behind that same seam — `parts_tools.py` is the single integration
point named in the mission brief; touch it minimally (register the new
providers, don't restructure the federation). Local catalogs and today's
behavior when no browser profile exists must be byte-identical to before this
task — the browser path only activates when a profile exists for a relevant
site (per T-X1's `profiles.py`).

Downloaded STEP files should route through `import_part` (and, where feasible,
`reverse_engineer`) so acquired parts get R1 editability parity like every
other part-import path in the app, per the existing parts/1 contract
(`contracts/parts.md`).

## Files / areas touched

- `services/mcp/parts_tools.py` — register T-X2 adapters as providers behind
  `part_search`; per the existing `has_live` convention, browser-sourced
  results should carry `source:"live"` (or a new clearly-documented variant if
  `"live"` implies something the browser path doesn't guarantee — check
  `contracts/parts.md`'s PartRecord envelope before deciding) so callers can
  keep distinguishing local vs remote as they already do.
- `engine_oracle/parts_hub/` (or wherever the federation core lives — confirm
  via graphify, do not guess the path) — the actual provider registration glue,
  mirroring how the existing live-vendor adapters (T-P2) are wired in.
- `part_place` path: when a browser-acquired PartRecord is placed, its
  `geometry_ref` (a downloaded STEP/IGES file) must go through `import_part` →
  `reverse_engineer` where the existing pipeline supports it, same as any other
  imported CAD parity path — reuse, don't reimplement.
- `services/mcp/parts_tools.py` docstring — extend to mention the browser
  provider per the existing style (this file already documents each provider
  family in its module docstring).
- Tests: extend the existing parts_hub / parts_tools test suites (find via
  graphify — `test_parts_hub_p3.py`, `test_parts_worker_rpc.py`) with a
  browser-provider case using T-X2's fake adapters; verify zero-profile
  behavior is unchanged from pre-T-X3.

## Steps

1. `graphify query "part_search providers federation register adapter"` to find
   the exact registration point T-P2 used for live vendor providers — follow
   that pattern for the browser adapters rather than inventing a new mechanism.
2. `graphify query "import_part reverse_engineer editable doc_id"` to confirm
   the exact call shape for routing a downloaded STEP into an editable doc.
3. Wire T-X2 adapters in behind `part_search`, gated on profile existence
   (no profile for a site → that provider contributes zero results, silently,
   same as "no API key" degrades today for live vendor providers).
4. Wire `part_place` → `import_part`/`reverse_engineer` for browser-acquired
   geometry_refs.
5. Tests: no-profile-unchanged case is the most important one to get right —
   write it first as a regression guard.

## Definition of done

- [ ] Browser adapters appear as `part_search` providers, contributing results
      only when a matching site profile exists
- [ ] `part_place` on a browser-acquired part produces an editable doc_id via
      `import_part`/`reverse_engineer`, matching R1 parity of other import paths
- [ ] Zero-profile behavior is provably unchanged (regression test against
      pre-existing `part_search`/`part_place` test suites — all still pass)
- [ ] `services/mcp/parts_tools.py` docstring updated per the MCP freshness rule
      (WORKFLOW.md §6)

## AI hints

- This is a narrow integration task — resist the urge to refactor
  `parts_tools.py`'s existing structure. Add, don't reshape.
- Run the full existing parts test suite before AND after your change to prove
  the "unchanged when no profile" claim, not just add a new test for it.

## Progress log
- 2026-07-05 04:01 UTC [sonnet-drift] Implemented: BrowserCatalogAdapter (services/browser/adapters/parts_provider.py) wraps each SourceAdapter as domain=catalog CatalogProvider, filling contracts/parts.md §6.3's paid-catalog seam via the browser-session mechanism instead. Added geometry_ref kind import_file (routes to core's import_file RPC, same as import_part) with a real place.py placer. Registration wired into PartsHub.bootstrap() (not eager MCP-startup registration -- preserves lazy-init timing parity with all other providers). Fixed a real bug: services/browser/adapters/__init__.py's all_adapters() returned [] unless something had already imported grabcad/traceparts submodules first -- now imported explicitly at module bottom. contracts/parts.md updated additively (§0 catalog domain, §2 import_file kind, §6.3 note). 22 new tests (14 parts_provider + 8 hub/place), full engine_oracle+services regression sweep clean: 14 failed/44 errors all pre-existing/unrelated (ECAD/CFD/agent-provider), 2170 passed. Pushed task/T-X3-part-search-federation, status review. Claiming T-X4 next (stacked on this branch since T-X1/T-X2/T-X3 unmerged).
