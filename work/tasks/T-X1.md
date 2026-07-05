---
id: T-X1
title: Browser session service - persistent per-site login profiles + headless fetch
workstream: X
owner: sonnet-drift
status: done
branch: task/T-X1-browser-sessions
depends_on: []
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Hunter directive 2026-07-04: pivot parts/document acquisition from vendor APIs to
user-authenticated headless-browser acquisition. Users sign into source sites
(GrabCAD, TraceParts, 3DContentCentral, McMaster, SnapEDA, manufacturer portals) in
a real, HEADED browser window; we persist their session profile LOCALLY so they
never have to re-login; the copilot then fetches models/datasheets/specs under the
user's own session. This supersedes the old blanket "no scraping" posture in
`contracts/sourcing_schema.md` — the browser acts as the *user's* browser, under
the user's own credentials/consent, not an anonymous scraper. Record each source's
TOS/licensing stance in `contracts/license_debt.md` as adapters are added (T-X6
owns the register sweep; note anything you learn here too).

This task is the foundation seam: no adapters yet, just the session/profile
lifecycle and a rate-limited fetch primitive. `services/agent/`, `work/tasks/T-W*.md`
and workstream W's branches are out of scope for this workstream — do not touch them.

## Files / areas touched

- `services/browser/` — NEW package:
  - `session.py` — `browser_login(site: str) -> dict`: launches a HEADED Playwright
    browser context with a persistent profile dir for `site`, waits for the user to
    complete login (poll for a site-specific "logged in" signal or a manual
    "done" confirmation), then closes and persists the profile. `browser_fetch(url,
    site) -> dict`: opens the same profile HEADLESS, navigates, returns page
    content/response for an adapter to parse. Both are the only two entry points
    adapters (T-X2+) call — no direct Playwright use outside this module.
  - `profiles.py` — profile directory layout under the app data dir (mirror the
    existing `%APPDATA%/AI-CAD/` / `~/.local/share/AI-CAD/` convention used by
    `engine_oracle/vendor_parts.py` config), one persistent-context dir per site,
    metadata sidecar (`{site, created_ts, last_used_ts, last_login_ts}` — never
    credentials/cookie values in metadata, those live only inside Playwright's own
    profile dir). LOCAL ONLY: no sync, no upload, document this explicitly in a
    module docstring and in `contracts/license_debt.md` if any ambiguity arises.
  - `ratelimit.py` — per-site cooldown/rate-limit baked into `browser_fetch` itself
    (token-bucket or simple min-interval-per-site), so every adapter gets polite
    behavior for free without remembering to throttle.
  - `test_browser_session.py` — unit tests against a mocked Playwright (no real
    network, no real browser launch in CI); verify profile dir creation, metadata
    round-trip, rate-limit gating, and graceful degradation when Playwright itself
    isn't installed (`ok:false`, not a traceback or import crash at module load).
- `contracts/license_debt.md` — add a row if anything here (e.g. bundling
  Playwright's Chromium download) needs a compliance note.
- OSS-first check (record inline as a code comment or short note in the PR/branch,
  ~15 min budget): confirm Playwright (Apache-2.0) over Selenium/Puppeteer-node for
  this Python service — Playwright ships a first-party async Python API and
  built-in persistent-context storage_state support, which is the main reason to
  prefer it here; note the choice and move on, this is not a deep bake-off.

## Steps

1. `graphify query "vendor_parts config app data dir"` and `graphify query
   "engine_oracle providers ProviderError degradation"` to reuse the existing
   config-dir and degradation conventions rather than inventing new ones.
2. Add `playwright` to the MCP venv's dependency list (wherever `services/mcp`
   pins deps today); confirm `pip install playwright` + `playwright install
   chromium` works in the dev venv, document the one-time browser-binary download
   in a short README note inside `services/browser/`.
3. Implement `profiles.py`: profile dir resolution + metadata sidecar (JSON),
   pure-stdlib, no Playwright import needed just to list/inspect profiles.
4. Implement `session.py`: `browser_login`/`browser_fetch` wrapping
   `playwright.sync_api` (matches the sync style already used elsewhere in the
   codebase's Python services — check before assuming async). Both return the
   provenance-envelope-flavored shape T-X2+ will extend: at minimum
   `{ok, site, profile_dir, last_login_ts}` for login, `{ok, url, html/content,
   fetched_ts, session_profile_used}` for fetch.
5. Implement `ratelimit.py`: per-site min-interval, in-memory + persisted last-hit
   timestamp in the profile metadata sidecar so cooldowns survive process restarts.
6. Degradation: importing `services/browser` with Playwright absent must not
   crash any MCP server startup — lazy-import inside the functions, `ok:false`
   with a clear "playwright not installed" hint otherwise.
7. Tests with mocked Playwright objects (monkeypatch `sync_playwright`), run via
   `~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest services/browser/`.

## Definition of done

- [ ] `browser_login(site)` opens a HEADED window, persists a per-site profile
      under the app data dir; profile survives process restart
- [ ] `browser_fetch(url, site)` runs HEADLESS against a saved profile; returns
      `ok:false` (not a traceback) when no profile exists for that site
- [ ] Per-site rate-limiting enforced inside `browser_fetch`, not left to callers
- [ ] No credentials/cookies ever written to metadata sidecars or logs; profiles
      dir documented as local-only, never synced
- [ ] Graceful, non-crashing degradation when Playwright is not installed
- [ ] Tests pass with mocked Playwright, no real network/browser in CI

## AI hints

- `graphify query "app data dir config.json vendor_parts"` before writing
  `profiles.py` — reuse the existing per-OS path resolution if one already exists
  rather than adding a second convention.
- Keep `session.py`/`profiles.py`/`ratelimit.py` small and composable — T-X2's
  adapters should only ever need `browser_login`/`browser_fetch` and nothing else
  from this package.

## Progress log
- 2026-07-05 03:41 UTC [sonnet-drift] T-X1 DONE — branch task/T-X1-browser-sessions pushed. Built services/browser/ (session.py: browser_login/browser_fetch over playwright.sync_api, lazy-imported; profiles.py: local-only per-site profile dirs + metadata sidecar under engine.paths.BROWSER_PROFILES; ratelimit.py: per-site cooldown persisted in metadata). 12/12 new tests pass against a mocked Playwright (no real browser/network in CI). Added playwright==1.61.0 to requirements.txt + contracts/licenses.md row (Apache-2.0, OSS-first pick over Selenium/Puppeteer for persistent-context support). Verified graceful degradation with playwright import blocked (ok:false, no crash). Full engine_oracle+services regression sweep: 14 failed/44 errors, same as master baseline (ECAD/CFD/agent-provider, unrelated) — zero new regressions. Claiming T-X2 next.
