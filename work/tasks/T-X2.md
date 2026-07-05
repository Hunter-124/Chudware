---
id: T-X2
title: Source adapter framework + GrabCAD/TraceParts adapters
workstream: X
owner: unassigned
status: todo
branch: task/T-X2-source-adapters
depends_on: [T-X1]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Builds on T-X1's `browser_login`/`browser_fetch` primitives. This task adds the
adapter contract (a small per-site module: `search(query)` + `acquire(result)`)
and the first two real adapters: GrabCAD and TraceParts. GrabCAD requires a free
account login; TraceParts requires account + per-catalog terms acceptance for
some suppliers — both are exactly the "sign into the source site in your own
browser" case the mission targets, and both currently only exist in this repo as
the *parked, paid-API* T-BL12 (`work/backlog/T-BL12.md`, priority 5, "revisit
after v1 objectives close") — that plan is untouched by this task; this is a
different, session-based mechanism, not the paid API. Note the relationship in
`contracts/license_debt.md` when the TraceParts row is added (per-supplier terms
vary; some catalogs are open, others gate CAD downloads behind reseller
agreements — record what you actually observe, don't assume).

Downloaded artifacts must carry the provenance envelope described in the mission
brief: `{source_url, fetched_ts, session_profile_used, license_note}`, styled on
the `aicad.calc/1` envelope in `contracts/advisor.md` §7 and the `research_*`
answer envelope in `contracts/research_tools.md` §1 — reuse those conventions
(ISO-8601 UTC timestamps, explicit `null` over absence) rather than inventing a
third shape.

## Files / areas touched

- `services/browser/adapters/__init__.py` — adapter registry + the shared
  contract: `class SourceAdapter: key: str; def search(self, query: str) ->
  list[dict]; def acquire(self, result: dict) -> dict`. `search` returns
  lightweight result stubs (title, url, thumbnail?, format hint); `acquire`
  does the actual `browser_fetch`-backed download and returns the provenance
  envelope + a local artifact path.
- `services/browser/adapters/grabcad.py`, `services/browser/adapters/traceparts.py`
  — data (site config: base URL, search endpoint/selector, login-required
  detection heuristic) + a thin per-site parser over `browser_fetch`'s returned
  content. No adapter talks to Playwright directly — only through T-X1's two
  entry points.
- Login-required detection: when `browser_fetch` (or the parsed page) indicates
  a login wall, `acquire`/`search` return the §6 `ask_user` `approval`-shaped
  "sign in needed" response per `contracts/interaction.md` §1/§2 (never attempt
  to bypass or automate the login) — surfaced cleanly, not as an exception.
- Artifact cache: route downloaded STEP/IGES/PDF files through whatever the
  existing local artifact cache convention is (check `engine_oracle/research/cache.py`
  from T-X1's orientation pass, and `vendor_parts.py`'s CAD storage layout) rather
  than inventing a new cache directory.
- `services/browser/adapters/test_adapters.py` — tests against a fake
  `browser_login`/`browser_fetch` (no real Playwright, no real network): search
  returns stub results, acquire returns a provenance envelope, login-wall path
  returns the ask_user shape.

## Steps

1. `graphify query "research answer envelope confidence provenance"` and
   `graphify query "ask_user interaction file_request approval"` before writing
   the envelope/ask_user code — reuse existing helper functions if
   `services/agent/interaction.py` already exposes a constructor for the
   question object.
2. Define the `SourceAdapter` protocol + registry in `adapters/__init__.py`.
3. Implement GrabCAD adapter: `search` scrapes the (post-login) search results
   page reachable via the user's session; `acquire` downloads the model file.
4. Implement TraceParts adapter: same shape; note in a code comment + the
   license_debt row anything catalog-specific (some TraceParts suppliers require
   accepting per-download terms in-browser — if a click-through is required and
   can't be scripted safely, surface it as an `approval` ask_user, don't
   auto-click legal agreements on the user's behalf).
5. Provenance envelope helper shared by both adapters (small function, not
   copy-pasted per adapter).
6. Tests with fake session primitives.

## Definition of done

- [ ] `SourceAdapter` contract defined; GrabCAD + TraceParts adapters implement it
- [ ] `acquire()` downloads land in the existing artifact cache with the full
      provenance envelope (`source_url, fetched_ts, session_profile_used,
      license_note`)
- [ ] Login-required / terms-click-through cases surface as clean `ask_user`
      questions (contracts/interaction.md shapes), never raise
- [ ] `contracts/license_debt.md` has a row for each adapter's actual observed
      TOS posture
- [ ] Tests pass with faked T-X1 primitives, no real network/browser in CI

## AI hints

- `graphify query "artifact cache download step iges pdf"` before picking a
  storage location for downloaded files.
- Keep adapters DATA-heavy, CODE-light — most of GrabCAD vs TraceParts should be
  config (selectors/URLs), not divergent logic, so T-X3+ adapters stay cheap to add.

## Progress log
