---
id: T-X4
title: Datasheet and document acquisition - authenticated-browser fallback
workstream: X
owner: sonnet-drift
status: done
branch: task/T-X4-datasheet-browser-fallback
depends_on: [T-X2]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

`engine_oracle/research/datasheet_fetch.py` (Pillar 8, contract
`contracts/research_tools.md`) already fetches datasheets via plain HTTP. Many
manufacturer/distributor sites (SnapEDA, some manufacturer portals) 403 or
paywall a plain fetch but serve the same document fine to a logged-in browser
session. This task adds a fallback: when `research_datasheet_fetch` hits a
403/paywall signal, retry via T-X1's `browser_fetch` under a matching site
profile (if one exists) before giving up — never automatically log in on the
user's behalf; if no profile exists, degrade exactly as today (surface the
403 in the existing `ok:false` shape).

The result must still conform to the frozen §1 answer envelope in
`contracts/research_tools.md` (append-only — do not change existing envelope
field meanings). Add the browser-specific provenance (`session_profile_used`)
as an additive field alongside the existing envelope, following the same
"helpers add helper-specific fields alongside, never renaming" rule the
contract already states for `web_search`/`fetch_page`.

## Files / areas touched

- `engine_oracle/research/datasheet_fetch.py` — add the authenticated-browser
  fallback path, gated behind: (a) a plain fetch returning 403/401/paywall
  signal, AND (b) a matching site profile existing (via T-X1's `profiles.py`
  lookup — no profile → skip straight to existing degradation, no behavior
  change for the common case).
- `contracts/research_tools.md` — append a short subsection (new sub-heading
  under the existing datasheet section, do not renumber or rewrite existing
  sections) documenting the additive `session_profile_used` field and the
  fallback trigger condition.
- PDF landing: once fetched via the browser, route through whatever the
  existing PDF cache path is (`engine_oracle/research/cache.py` — confirm via
  graphify) rather than adding a second cache.
- `engine_oracle/research/test_research.py` (or a new
  `test_datasheet_browser_fallback.py` alongside it) — fake T-X1 primitives;
  cases: plain-fetch-succeeds (browser path never invoked, zero behavior
  change), plain-fetch-403-with-profile (browser fallback succeeds), plain-
  fetch-403-no-profile (existing degradation, unchanged).

## Steps

1. `graphify query "datasheet_fetch cache fetch_page envelope confidence"` to
   read the exact current 403/degradation handling before touching it.
2. Add the fallback branch; keep it a small, clearly-isolated addition to the
   existing function rather than a rewrite.
3. Extend the envelope construction with `session_profile_used` (string site
   key, or `null` when the browser path wasn't used) — additive field, existing
   consumers unaffected.
4. Update the contract doc.
5. Tests for all three cases above; run the existing `test_research.py` suite
   to confirm zero regressions on the non-fallback path.

## Definition of done

- [x] `research_datasheet_fetch` falls back to an authenticated browser fetch
      only on 403/paywall AND an existing matching profile; otherwise behavior
      is byte-identical to before this task
- [x] Envelope stays contract-compliant; `session_profile_used` is additive,
      documented in `contracts/research_tools.md`
- [x] No login is ever triggered automatically from this path — missing
      profile just degrades as today
- [x] Tests cover all three cases; existing `test_research.py` suite still
      passes unchanged

## AI hints

- This is the smallest, most surgical of the T-X tasks — the fallback should
  read as "one more `if`", not a restructure of `datasheet_fetch.py`.
- Double check `contracts/research_tools.md`'s frozen-seam policy before
  editing it: append-only, no renumbering, no changing existing field meanings.

## Progress log
- 2026-07-05 19:15 UTC [sonnet-drift] Implemented: on a plain-fetch 401/403, datasheet_fetch() retries once via T-X1/T-X2's browser_download under a saved profile keyed by the URL's host (services/browser/profiles.has_profile) -- no profile means unchanged degradation, no login ever auto-triggered. Envelope gains additive session_profile_used (null unless the fallback served the bytes). contracts/research_tools.md gets a new append-only §9. 3 new tests (plain-success/never-invokes-fallback, 403-with-profile/succeeds, 403-no-profile/unchanged + asserts browser_download never called). Full engine_oracle+services regression sweep: 14 failed/44 errors, identical to the confirmed pre-existing baseline (ECAD/CFD/agent-provider, unrelated) -- zero new regressions, 2173 passed. Pushed task/T-X4-datasheet-browser-fallback, ready to merge alongside T-X1/T-X2/T-X3 (already merged+done). Earlier hit a transient local-checkout hazard (unpushed T-W17 merge commits on master) that blocked this status update -- resolved on its own, no action needed now.
- 2026-07-05 [opus-sweep] Merge-integrator: clean merge (no conflicts) into master. Verified on
  Windows: `pytest engine_oracle/research/test_source_helpers.py` 33/33 pass; full
  `engine_oracle/research/` suite 171/172 (1 pre-existing unrelated failure,
  test_knowledge_root_env_override — hardcoded `/tmp/...` literal vs WindowsPath backslash
  normalization, nothing to do with this task's diff). Merged --no-ff, status -> done.
