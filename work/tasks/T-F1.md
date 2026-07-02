---
id: T-F1
title: Freeze research_tools contract, scaffold engine/research with web_search + fetch_page
workstream: F
owner: opus-ivy
status: done
branch: task/T-F1-research-scaffold
day: 6
depends_on: []
---

## Context

Pillar 8 root: the model must look things up instead of recalling. Freeze
`contracts/research_tools.md` (the answer contract: every helper returns
`{answer, units, source_url, quote, fetched_ts, confidence, cached}`), scaffold
`engine/research/`, and ship the first two helpers with the provider interface and the
local cache. READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 8 in full — it is the spec,
including the ToS/legal hard rules (no MatWeb/McMaster, DuckDuckGo as zero-key fallback,
per-user local caches only).

## Files / areas touched

- `contracts/research_tools.md` — new frozen contract
- `engine/research/` — new module (`__init__.py`, `providers.py`, `cache.py`,
  `web_search.py`, `fetch_page.py`)
- `services/mcp/server.py` — register `research_*` tools

## Steps

1. Draft the contract: answer schema, provider interface (Tavily default BYO-key,
   Exa/Serper alternates, DDG HTML zero-key fallback labeled "limited"), cache tiers
   (pricing=hours, datasheets=days, standards≈immutable), offline/ITAR degradation.
2. Implement `web_search` + `fetch_page` (readability→markdown; Firecrawl `/scrape`
   optional when `FIRECRAWL_API_KEY` present).
3. Cache under `%APPDATA%/AI-CAD/knowledge/` keyed by URL+hash; `fetched_ts` surfaced.
4. Register MCP tools; add a `cad_guide('research')` topic.
5. Tests: provider-mocked search/fetch, cache hit/expiry, offline mode serves cache only.

## Definition of done

- [x] Contract frozen; answer schema documented with examples
- [x] `web_search`/`fetch_page` work with a mock provider + DDG fallback
- [x] Cache + offline mode tested; `python -m pytest engine/research/ -q` green (14/14)
- [x] MCP registration + cad_guide topic; `graphify update .` run; `bash scripts/gate.sh`
  green except the two documented PRE-EXISTING failures (unrelated to this diff):
  (a) `ui/imgui` `PanelField::selMulti` build break, and (b) `pytest engine/doc` collection
  error — `engine/doc/test_aicm.py` imports `OCP` (OpenCASCADE bindings not provisioned on
  this box); reproduces identically on a clean tree. The gate's pytest stage runs only
  `engine/doc` + `agent`, not `engine/research`; this diff touches neither.

## Review notes (opus-ivy, 2026-07-02)

- **Design decisions:** (1) shared helpers in `engine/research/common.py` (envelope +
  offline gate + ts) to avoid an engine↔agent import cycle — offline gate lazily imports
  `agent.config.offline_enabled` (mirrors `ecad_tools`), with a `config.json` fallback.
  (2) Cache root = `AI_CAD_KNOWLEDGE_DIR` env override → else `engine.paths.appdata_root()
  /knowledge` (reuses the named reference; env override makes tests hermetic). Note:
  `engine/paths.py` is Windows-flavored (no Linux XDG branch); when a cross-platform
  paths.py lands, `knowledge/` moves with it — contract §4 documents this.
  (3) `cad_guide('research')` shipped as `shared/cheatsheets/research.md` — the existing
  fallback auto-serves + auto-lists it, so **no edit to the frozen `cad_tools.py`**.
  (4) MCP registration is a new `services/mcp/research_tools.py` (mirrors `intake_tools.py`)
  + a 2-line append to `server.py` (hot-spot kept minimal).
- **Scope:** exactly web_search + fetch_page + cache + contract; no material/datasheet/
  standards helpers (those are T-F2). Contract §8 forward-declares them on the same envelope.
- **No new deps:** stdlib readability→markdown; `httpx` (already a dep) for network.

## Driver notes (Fable, 2026-07-02)

- **Offline gate to reuse, not reinvent:** `services/agent/config.py:40-109` has
  `assert_offline_safe`/`provider_offline_safe` (loopback-only enforcement). Your
  helpers must consult the same config flag; in offline mode serve cache + bundled
  data only, with `cached: true` and a clear "offline" confidence note.
- **Cache root:** resolve per-user appdata via the same convention as
  `core/agent/paths.cpp:31-54` / `engine/paths.py` (Windows `%APPDATA%/AI-CAD`, Linux
  `~/.local/share/AI-CAD`) → `knowledge/` subdir. Key = URL + content hash; store
  `fetched_ts`; tiered TTLs as specced. Never sync this cache anywhere (legal rule).
- **Provider pattern to copy:** `services/agent/providers/` — small classes behind one
  interface, mock provider for tests (`mock_provider.py`). Mirror that shape for
  search providers; tests must not hit the network (gate runs offline).
- **MCP registration precedent:** any `register_*_tools()` module in `services/mcp/`
  (e.g. `intake_tools.py`) — one function, tools closed over a context object.
- **Scope fence:** do NOT build material_lookup/datasheet/standards helpers here —
  that's T-F2. Ship exactly web_search + fetch_page + cache + contract.
- **DDG fallback:** HTML endpoint, low volume, exponential backoff, `confidence:
  "limited"` label per the master plan. No API key in code; Tavily key via config/env.
