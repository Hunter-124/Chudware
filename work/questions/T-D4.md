# T-D4 questions

## Q1: agentic_rag — copyright fence conflict with live-scraping public code viewers

**Driver:** sonnet-wren2  
**Date:** 2026-07-02  
**Status:** OPEN (driver proceeding with fence-safe default)

### The conflict

Step 5 and the AI hints tell the driver to implement `agentic_rag()` by calling the LLM
with a RAG prompt that points to upcodes.com / ICC public viewer pages at runtime, and to
"retrieve at runtime, never cache text."

The setup block for this task contains a **hard rule / copyright fence**:

> "building codes and design standards (IBC, ASCE 7, AISC 360, NDS full text) are
> COPYRIGHTED — never vendor, scrape, or embed their text… If the work order implies
> shipping code text, write work/questions/T-D4.md and build the user-provided-corpus
> path meanwhile."

Even runtime-fetched text from a public viewer is scraped copyrighted content and does not
clear the fence. Upcodes.com and codes.iccsafe.org serve IBC/IRC text under license; our
tool would be a scraper proxy.

### Recommended answer (implemented as the default)

Implement `agentic_rag()` as a **pluggable interface** with two backends:

1. **`UserCorpusProvider` (default, fence-safe):** indexes user-supplied PDF files from a
   local folder (`AICAD_CODE_CORPUS_PATH` env var or `~/.local/share/AI-CAD/code_corpus/`).
   Searches the local index via keyword/BM25 (stdlib only — no new deps); assembles a
   prompt; calls an LLM via `select_provider()` (T-F9 pattern); returns citations whose
   `source_url` is the file path. No copyrighted text is bundled or fetched — the user
   drops their own licensed PDFs.

2. **`LiveViewerProvider` (deferred, never default):** the runtime-fetch approach from step 5.
   Not implemented until Fable rules it clears the fence. Would need legal review: does
   "retrieve at runtime, attribute URL, never cache" clear the CC-BY license restriction on
   ICC codes? Our reading is: it's still scraping. Gate behind `AICAD_CODE_LIVE_VIEWER=1`.

### What this task ships

- `agentic_rag(query, jurisdiction, provider=None)` — interface + `UserCorpusProvider`
  (default: graceful no-op when corpus dir is empty or absent) + `MockRAGProvider` for tests.
- Tests mock the provider; all DoD boxes green without any network or PDF dep.
- `disclaimer` in `code_check` tool output calls out that citations are from
  user-supplied docs, not guaranteed current, and require a licensed engineer.

### Alternatives

A. Implement live fetch but disable by default (needs Fable ruling before enabling).  
B. Omit RAG entirely (breaks DoD box 3).  
C. Current recommendation: pluggable, user-corpus default, deferred live fetch.

**Recommended:** C (implemented). Fable: please confirm or override; live-viewer backend
implementation blocked pending your ruling.
