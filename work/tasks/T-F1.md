---
id: T-F1
title: Freeze research_tools contract, scaffold engine/research with web_search + fetch_page
workstream: F
owner: unassigned
status: todo
branch: task/T-F1-research-scaffold
day: 2
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

- [ ] Contract frozen; answer schema documented with examples
- [ ] `web_search`/`fetch_page` work with a mock provider + DDG fallback
- [ ] Cache + offline mode tested; `python -m pytest engine/research/ -q` green
- [ ] MCP registration + cad_guide topic; `bash scripts/gate.sh` passes; `graphify update .`
