# Research tools — look it up, don't recall

Pillar 8: when a fact could be wrong from memory (a material property, a price, a spec, a
standard, current data), **look it up and cite it** instead of recalling. The `research_*`
tools return a provenance-stamped answer envelope you quote verbatim.

## Tools

- `research_web_search(query, provider?, max_results?)` — FIND sources. Returns ranked
  `results[]` (`title`, `url`, `snippet`) and, when the provider offers one, an LLM-ready
  `answer`. Uses Tavily when `TAVILY_API_KEY` is set, else a keyless DuckDuckGo fallback
  (marked `confidence:"limited"` — hedge accordingly).
- `research_fetch_page(url, tier?)` — READ one source. Returns clean
  readability→markdown `content` plus provenance. `tier` sets cache freshness:
  `pricing` (hours) · `datasheet` (days) · `standard` (~immutable). Pick by how fast the
  fact changes.

## Answer envelope (every field)

`{ ok, answer, units, source_url, quote, fetched_ts, confidence, cached }`

- `answer` — the digested, human-readable answer. Quote it; never invent around it.
- `units` — SI unit of any number in `answer` (code converts units, never you — ksi vs
  MPa is a silent 6.9× trap).
- `source_url` / `quote` — provenance. A `quote` must be a verbatim substring of the
  source; if a claim has no supporting quote, treat it as unverified.
- `fetched_ts` — when the content was fetched (UTC). `cached:true` means it came from the
  local cache; check `fetched_ts` for staleness on fast-moving facts.
- `confidence` — `high` · `medium` · `limited` (DDG fallback) · `bundled` · `offline`.

## Workflow

1. `research_web_search("6061-T6 aluminum yield strength MPa")` → scan `results[]`.
2. `research_fetch_page(<best result url>, tier="standard")` → read `content`.
3. Quote the exact number + `source_url` in your reasoning; convert units in code.

## Failure & offline

Failures return `{ ok:false, reason, hint }` — never a crash. `reason` ∈
`no_provider · offline · network · rate_limited · bad_url · bad_query · not_found`;
follow the `hint`. In **offline / ITAR mode** the tools serve only the local cache +
bundled data (`confidence:"offline"`), and a cache miss returns `reason:"offline"` — do
not treat that as the fact being unavailable in general, only offline right now.

## Don'ts

- Don't recall a fit-critical number from memory when a tool can fetch it.
- Don't convert units yourself — report `answer` + `units` and let code normalize.
- Don't cite an `answer` that has no `quote` / `source_url` backing it.
