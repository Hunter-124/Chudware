# Contract: Research tools — look-it-up instead of recall (`research_*`)

> Pillar 8 root: a model must **look things up** with provenance, never recall from
> weights. One tool family (`engine/research/`, ECAD-precedent module layout) returns
> **pre-digested, unit-normalized, provenance-stamped answers**, never raw HTML.
> This contract is the **answer envelope** every helper shares plus the provider,
> cache, and offline rules. **v1 ships `web_search` + `fetch_page`**; later helpers
> (`extract_structured`, `material_lookup`, `thread_spec`, …) are appended as new
> sections and MUST return this same envelope.

**Frozen-seam policy:** append-only. New helpers append new sections; the §1 answer
envelope fields and their meanings do not change without maintainer sign-off
and a version bump (`v1` below).

## 1. Answer envelope (`v1`) — every helper returns this shape

Success:

```json
{
  "ok": true,
  "answer": "6061-T6 aluminum yield strength is 276 MPa",
  "units": "MPa",
  "source_url": "https://example.org/6061-t6",
  "quote": "Yield strength ... 276 MPa (40 ksi)",
  "fetched_ts": "2026-07-02T12:00:00Z",
  "confidence": "high",
  "cached": false
}
```

| field | type | meaning |
|---|---|---|
| `ok` | bool | `true` on success. Failures are the §6 degradation shape (`ok:false`). |
| `answer` | string | The digested, human-readable answer. A 4B model quotes it verbatim; a frontier model chains it. Never raw HTML. |
| `units` | string \| null | SI (or SI-derived) unit label of any numeric quantity in `answer` (`"MPa"`, `"mm"`, `"USD"`); `null` when non-numeric. **Code converts to SI, never the model** (ksi/MPa is the classic silent 6.9× error). |
| `source_url` | string \| null | Canonical URL the answer was drawn from. `null` only for bundled first-party data (then `confidence:"bundled"`). |
| `quote` | string \| null | **Verbatim** substring of the source that supports `answer` — must substring-match fetched content or the claim is untrusted. Cheapest anti-hallucination gate. `null` for aggregate results (e.g. a search hit-list). |
| `fetched_ts` | string (ISO-8601 UTC, `Z`) | When the underlying content was fetched from the network (or, for a cache hit, the original fetch time — **not** the read time). Always surfaced. |
| `confidence` | enum | see §1.1 |
| `cached` | bool | `true` when served from the local cache (§4) rather than a live fetch. |

Helpers add helper-specific fields **alongside** these (never renaming envelope fields):
- `web_search` adds `query`, `provider`, `results[]` (§2).
- `fetch_page` adds `url`, `title`, `content` (markdown) (§3).

### 1.1 `confidence` enum

| value | when |
|---|---|
| `high` | Answer from a recommended provider (Tavily/Exa/Serper) or a clean readability extraction with a matching `quote`. |
| `medium` | Live content fetched but extraction was partial / `quote` is approximate. |
| `limited` | **DDG HTML zero-key fallback**, or any low-volume/best-effort source. Always labeled so the model can hedge. |
| `bundled` | First-party bundled data, no network (`source_url` may be a citation string). |
| `offline` | Served from cache **because** offline/ITAR mode is on (§5); pair with `cached:true`. |

## 2. `web_search(query, provider?, max_results?)`

Returns the §1 envelope plus:

```json
{
  "ok": true,
  "query": "6061-T6 yield strength",
  "provider": "tavily",
  "results": [
    {"title": "...", "url": "https://...", "snippet": "...", "score": 0.91}
  ],
  "answer": "...", "units": null, "source_url": "https://...(top hit)",
  "quote": null, "fetched_ts": "...", "confidence": "high", "cached": false
}
```

- `results[]`: ranked `{title, url, snippet, score?}`. `answer` is the provider's LLM-ready
  summary when it offers one (Tavily), else `null` with `results[]` still populated.
- `provider` echoes the provider actually used (may differ from the request when a
  provider has no key and we fall through to DDG).

### 2.1 Provider interface (§ mirrors `services/agent/providers/`)

Small classes behind one interface; selection is config/env driven; a **mock provider**
backs tests (tests never hit the network — the gate runs offline).

| provider | key env | posture |
|---|---|---|
| **Tavily** (default) | `TAVILY_API_KEY` | Recommended BYO-key default: recurring free tier, LLM-ready answers + markdown. |
| Exa | `EXA_API_KEY` | Alternate. |
| Serper | `SERPER_API_KEY` | Alternate. |
| **DuckDuckGo HTML** | *(none)* | **Zero-key fallback.** Low volume, exponential backoff, `confidence:"limited"`. No key in code. |
| custom SearXNG | `SEARX_URL` | User-supplied URL only (shared instances are a single blockable IP — never bundled). |

Selection order: explicit `provider=` arg → first provider whose key is present → DDG
fallback. **Brave is intentionally excluded** (free tier eliminated 2026-02).

**v1 wires** Tavily + DuckDuckGo + a mock (tests). Exa/Serper/SearXNG conform to this
same `SearchProvider` interface (`search(query, max_results) → {results, answer,
confidence}`) and are added as later tasks need them — the interface, not each back-end,
is what this contract freezes.

## 3. `fetch_page(url)`

Returns the §1 envelope plus:

```json
{
  "ok": true,
  "url": "https://example.org/6061-t6",
  "title": "6061-T6 Aluminum",
  "content": "# 6061-T6 Aluminum\n\nYield strength ... 276 MPa (40 ksi)\n...",
  "answer": "6061-T6 Aluminum — page fetched (readability→markdown, 1.2 kB)",
  "units": null, "source_url": "https://example.org/6061-t6",
  "quote": null, "fetched_ts": "...", "confidence": "high", "cached": false
}
```

- Built-in **readability→markdown** (stdlib-only extractor: strips script/style/nav,
  collapses to text/markdown — **zero new hard deps**, CI-safe).
- **Firecrawl `/scrape`** is used for hard/JS pages **only when `FIRECRAWL_API_KEY` is
  present**; absence degrades to the built-in extractor, never an error.
- `content` is capped (default ~200 kB of markdown) to protect the model window.

## 4. Cache — per-user local, `knowledge/` under appdata

- **Root:** `<appdata>/Chudware/knowledge/`, where `<appdata>` = `engine.paths.appdata_root()`
  (the single app-wide root — `%APPDATA%/Chudware` on Windows; when a cross-platform
  `paths.py` lands, `knowledge/` moves with it). `CHUDWARE_KNOWLEDGE_DIR` overrides the root
  (used by tests for a hermetic cache). Master-plan target for the eventual cross-platform
  root: Windows `%APPDATA%/Chudware`, Linux `~/.local/share/Chudware`, macOS
  `~/Library/Application Support/Chudware`.
- **Key:** `sha256(url)` (+ a short hash of any provider/params that change the content);
  raw content + metadata (`fetched_ts`, `source_url`, tier) stored side by side.
- **Tiered TTLs** (staleness → refetch when online; serve stale when offline):

  | tier | TTL | examples |
  |---|---|---|
  | `pricing` | hours | live prices, stock, lead time |
  | `datasheet` | days | datasheets, component specs |
  | `standard` | ~immutable (very long) | ISO/ASME/AISC tables, alloy properties |

- **`fetched_ts` is always surfaced** on the envelope, cache hit or miss.
- **LEGAL — never sync this cache anywhere.** Syncing caches through our servers, or
  shipping them, converts caching into redistribution. Per-user local only.

## 5. Offline / ITAR degradation

Helpers consult the **same** offline flag as the agent (`services/agent/config.py`
`assert_offline_safe` / `provider_offline_safe` — loopback-only enforcement).

- Offline mode: **serve cache + bundled data only.** Cache hit → `cached:true`,
  `confidence:"offline"`, `fetched_ts` = original fetch time. Cache miss → §6 degradation
  with `reason:"offline"` and a hint to disable offline mode / provide bundled data.
- Never attempt an outbound request in offline mode (no leak past loopback).

## 6. Degradation shape — contract-shaped, never a traceback

Any failure (no provider key **and** DDG unreachable, network error, offline cache miss,
malformed URL, rate-limit exhaustion) returns:

```json
{"ok": false, "reason": "no_provider", "hint": "Set TAVILY_API_KEY or allow the DuckDuckGo fallback (network required)."}
```

`reason` is a stable machine token (`no_provider`, `offline`, `network`, `rate_limited`,
`bad_url`, `bad_query`, `not_found`). `hint` is one actionable human sentence. **CI passes with no
optional external binaries/keys** — the DDG-unreachable / no-key path returns this shape,
never raises.

## 7. License posture (see `CONTRIBUTING.md` + `contracts/licenses.md`)

| dependency | license | linkage | used for |
|---|---|---|---|
| `httpx` | BSD | in-process (already a dep) | HTTP for providers / fetch |
| stdlib `urllib`, `html.parser`, `re` | PSF | in-process | zero-dep readability→markdown fallback |
| Tavily / Exa / Serper / Firecrawl | hosted API (BYO-key) | network, opt-in | provider back-ends |
| DuckDuckGo HTML | public endpoint | network | zero-key fallback |

**Hard rules (legal pass):** never wire an agent to MatWeb / MakeItFrom / Matmatch /
McMaster-Carr (all prohibit programmatic access). _(Exception, owner-directed 2026-07-08:
an OPT-IN, off-by-default, rate-limited point-lookup path exists at
`services/research/material_web_lookup.py` — tracked as an accepted-risk override in
`contracts/license_debt.md` row 17. MatWeb stays effectively unreachable there: it serves a
CAPTCHA to automated sessions, so that path returns no data. The default automated path
remains the first-party `material_lookup`.)_ **AGPL never in-process** (e.g.
PyMuPDF / pymupdf4llm — a later PDF helper must isolate or avoid). GPL/MPL subprocess
only. No API key committed to code — keys come from config/env.

## 8. Version

`v1` — 2026-07-02. Ships `web_search`, `fetch_page`, cache, offline degradation.
Future helpers (`extract_structured`, `material_lookup`, `thread_spec`,
`standard_section`, `datasheet_fetch`, …) append here and reuse the §1 envelope.

## 9. `datasheet_fetch(source, tier?)` — authenticated-browser fallback (additive)

`services/research/datasheet_fetch.py` extracts a datasheet PDF (local path or
`http(s)` URL) to markdown, returning the §1 envelope plus `url`, `title`, `content`,
`extractor`, `tier`.

**Addition (additive field, existing consumers unaffected):** when a plain fetch
returns HTTP 401 or 403 (a paywall/login-wall signal) **and** a matching browser session
profile already exists for that URL's host (`services/browser/profiles.py`, saved via
the user's own `browser_login`), the helper retries once via
`services/browser/session.py`'s `browser_download` under that saved session before
falling back to the existing degradation. **No login is ever triggered from this path** —
a missing profile degrades exactly as before this addition (`reason:"network"`).

The envelope gains one additive field:

```json
{
  "ok": true,
  "url": "https://example-vendor.com/parts/widget.pdf",
  "title": "widget.pdf",
  "content": "# widget\n...",
  "extractor": "pdfminer.six",
  "tier": "datasheet",
  "session_profile_used": "example-vendor.com",
  "answer": "widget.pdf — datasheet extracted to markdown (…)",
  "units": null, "source_url": "https://example-vendor.com/parts/widget.pdf",
  "quote": null, "fetched_ts": "...", "confidence": "high", "cached": false
}
```

- `session_profile_used`: `str | null` — the site key (URL host) of the browser profile
  that served the bytes, or `null` when the plain fetch succeeded / no fallback applied
  (the common case; behavior byte-identical to before this addition).
- Uses `browser_download` (not `browser_fetch`) — a PDF is binary and must not go through
  a rendered-page-content path.

---

## Addendum (2026-07-18): Firecrawl key pool

`services/research/firecrawl_pool.py` supersedes the single-`FIRECRAWL_API_KEY` behavior
described above for Firecrawl-backed fetching. Key sources (merged, deduped, env wins):
`FIRECRAWL_API_KEYS` (separated list), `FIRECRAWL_API_KEY`, config.json
`keys.firecrawl` (list or separated string). Rotation semantics: 429 → cool the key
(`Retry-After` honored, else 60 s) and retry the next ready key; 401/402/403 → park
24 h; 5xx/transport → cool 30 s; all keys cooling → sleep until the soonest recovers,
capped by `FIRECRAWL_POOL_MAX_WAIT_S` (default 180 s), then degrade to the built-in
extractor exactly as the no-key case. Cooldown state is shared cross-process via a
flocked JSON state file beside config.json (keys stored as hashes). Absence of keys
still degrades to the built-in extractor, never an error.
