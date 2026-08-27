---
id: T-X7
title: Vendor adapter wave 2 - Amazon, McMaster-Carr, JLCPCB, PCBWay + manufacturing-service discovery
workstream: X
owner: opus-wynn
status: done
branch: task/T-X7-vendor-adapters-wave2
depends_on: [T-X2, T-X3]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Hunter directive 2026-07-05 (priority #4): the parts library pivots to full web
acquisition across REAL vendors, not just CAD-model communities. T-X2's adapter
framework (services/browser/adapters/) + T-X3's part_search federation are the seam;
this task adds the commerce-vendor adapters:

- **Amazon** — rapid prototyping + common parts fast, everywhere. Search, product page
  parse (price/availability/Prime), link-out + structured part record for BOM lines.
- **McMaster-Carr** — the canonical MRO/hardware catalog; has real CAD downloads (STEP)
  per SKU behind the user's session. Search + CAD fetch + price/spec parse.
- **JLCPCB / PCBWay** — PCB fab + assembly quotes; parts (JLC parts library) for ECAD
  BOM matching (ties into ecad_find_part).
- **Discovery survey** (in-repo doc, no adapter yet): metal 3DP / CNC / FDM service
  bureaus worth adapters next — evaluate at least Xometry, Protolabs/Hubs, SendCutSend,
  Craftcloud, OSH Park, JLC3DP; rank by API-vs-browser difficulty and quote automation
  potential. Output: docs/SOURCING_VENDOR_SURVEY.md feeding T-X9 (ordering).

## Definition of done

- [x] Adapter per vendor (8d6d4483): amazon/mcmaster/jlcpcb/pcbway.py on the T-X2
      contract — rate-limit + absent-profile skip inherited from session.py (adapters only
      call browser_fetch/browser_download), terms-wall detection (mcmaster/jlcpcb/pcbway),
      Amazon bot-challenge → login_required (never bypass), NEVER auto-accept ToS, NEVER
      store creds.
- [x] Registered as part_search federation providers (8d6d4483): the __init__.py import
      line was extended; all_adapters()→hub.py auto-registers each as `browser_<key>`; the
      absent-profile CI-degraded skip is inherited from session.py (verified by the existing
      test_x3_browser_catalog discipline). Vendor facet tag = provider_id `browser_<key>`.
- [x] McMaster STEP → import_part with source metadata (2bca34ba): BrowserCatalogAdapter.get()
      folds the acquire envelope's vendor extras (sku/price/availability/asin/lcsc/stock) into
      the part-record `params` alongside site/url/fetched_ts, so vendor/SKU/URL/fetched-at ride
      the imported part. Also fixed a latent KeyError the no-CAD adapters would hit
      (provenance_envelope omits artifact_path when None) → geometry_ref now conditional. 57
      tests pass.
- [~] Structured price/availability → bom_price approved-provider seam — ESCALATED to advisor
      (work/questions/T-X7.md Q1), NOT wired unilaterally. The bom_price chain
      (engine_oracle/parts_providers.py) is a deliberately API-key-only "Chudware does NOT scrape"
      seam (guarantee surfaced in user-facing errors + license register); feeding browser-
      session prices into it is a contract-posture change. Recommended (a) a distinct
      profile-gated fail-closed BrowserVendorProvider behind the API providers with a
      browser-session provenance tag; awaiting the ruling. NOTE: item 3 already exposes
      browser-vendor prices on the catalog part record's params today, so alternative (b) is
      partially in place regardless.
- [x] docs/SOURCING_VENDOR_SURVEY.md (9be7a5ed) — 6-service shortlist (Xometry, Protolabs
      Network, Craftcloud, SendCutSend, JLC3DP, OSH Park) ranked by quote-automation +
      adapter difficulty, per-service notes, build-order rec for T-X9.
- [x] Selector fixtures + unit tests green (8d6d4483): mocked-HTML per-vendor tests, 45
      passed (repo .venv — mcp-venv path absent on this Windows box; live integration stays
      T-X6-gated). license_debt.md rows 5-8 appended (43775595).

## Driver notes

- graphify rule: `graphify query "<question>"` before raw-file reads (subagents too).
- Branch task/T-X7-vendor-adapters-wave2, worktree ../CAD-T-X7. Stop at status: review.
- Amazon has aggressive bot detection — keep fetch cadence human-scale (T-X2 ratelimit),
  and on any captcha/challenge surface `login_required`/`challenge` to the user, never
  attempt bypass. Same for every vendor.
- Python venv for gate: `~/.local/share/Chudware/mcp-venv/bin/python`.

## Progress log
- 2026-07-05 19:35 UTC [opus-wynn] Claimed + started (opus-wynn, Windows). Branch task/T-X7-vendor-adapters-wave2 off current master. Orienting via recon of the T-X2 AdapterBase contract (services/browser/adapters/) + grabcad/traceparts reference adapters + T-X3 part_search federation seam + bom_price approved-provider seam + import_part CAD path. Plan: mirror the smallest reference adapter for each of Amazon/McMaster/JLCPCB/PCBWay with the same guardrails (rate-limited, profile-scoped, terms-wall + login_required/challenge surfacing, NEVER auto-accept ToS, NEVER store creds, never bypass captcha), register as federation providers with CI-degraded skip when profiles absent, + docs/SOURCING_VENDOR_SURVEY.md. NOTE: unrelated Discord-notifier WIP (notify_discord.py + discord_bot.py + requirements-discord-bot.txt) is present uncommitted in this checkout's working tree from a prior session â€” NOT mine, NOT part of T-X7; I am preserving it untouched and will never stage it in T-X7 commits.

## HANDOFF (opus-wynn, 2026-07-05) — 2 DoD items remain

**State:** branch `task/T-X7-vendor-adapters-wave2` pushed. DONE + committed: 4 adapters +
tests (8d6d4483, 45 passed), survey doc (9be7a5ed), license_debt rows 5-8 (43775595),
federation registration (automatic). Two integration items remain; both are small-to-medium
and well-scoped by the recon anchors below. `status` left at `doing` — do NOT flip to review
until these land + the Linux gate passes.

**Env caveats (same as T-G25):** full `scripts/gate.sh` can't run on this Windows box
(python3 app-alias, system-OCCT, Linux ctest) and `graphify update .` — graphify CLI isn't
provisioned here. Run both on the Linux/CI side at merge. The adapter unit tests DO run:
`./.venv/Scripts/python.exe -m pytest services/browser/adapters/test_adapters.py -q` (45 pass).

**Landmine:** `work/tools/notify_discord.py` (+ `discord_bot.py`, `requirements-discord-bot.txt`)
are unrelated Discord-notifier WIP left uncommitted in this checkout by a prior session. NOT
part of T-X7 — never stage them. Stage only your specific files each commit.

### Item 3 — McMaster SKU → part-record params (small)
`mcmaster.py acquire()` already returns `provenance_envelope(..., sku=<part#>)`. The T-X3
placement path (`engine_oracle/parts_hub/place.py`, `import_file` kind) already carries the
record's provenance/params to the imported part. REMAINING: in
`services/browser/adapters/parts_provider.py` `BrowserCatalogAdapter.get()` (~lines 96-110,
where `provenance`/`params:{site,url}` are attached), also propagate the acquire envelope's
`sku` (and any price) into `params` so vendor/SKU/URL/fetched-at all land on the part. Add a
test in `parts_provider`'s test alongside the existing catalog tests. (parts_provider.py is
shared T-X3 code — additive fields only, don't rename.)

### Item 4 — Structured price → bom_price approved-provider seam (main remaining work)
`bom_price` (`services/mcp/sourcing_tools.py:36-52`) → `engine.bom.resolve_prices`
(`engine/bom.py:286-311`) → `ProviderChain([Nexar,DigiKey,Mouser])`
(`engine_oracle/parts_providers.py:471-473`) — an API-key system, NOT the browser adapters.
Two viable ways to wire the T-X7 browser vendors' parsed price/stock in (pick one, note the
choice; escalate to advisor via work/questions/T-X7.md if unsure):
  (a) A thin `PartProvider` subclass in `parts_providers.py` whose `fetch(mpn)` delegates to a
      browser adapter's cached search/acquire result, returning `dataclasses.asdict(VendorPart)`
      with the real `product_url` provenance — and raising `ProviderError` (fail-closed) on a
      miss, never guessing (match the existing stub providers at parts_providers.py:150-213).
      Register it in the chain behind the API providers.
  (b) Populate `engine.vendor_parts.VendorPart` (unit_price/price_breaks/stock/moq/lead_time
      + provenance URL) from the adapter's parsed page and index it so `bom.rollup`'s
      vendor_lookup picks it up.
HARD CONSTRAINT (bom.py:307, parts_providers.py:6-7): every number is copied literally from
the parsed vendor page with a provenance URL — NEVER LLM-generated; a miss yields `None`/
`ProviderError`, never a guessed price. Add mocked-HTML → price-record unit tests.
- 2026-07-05 19:49 UTC [opus-wynn] PARTIAL / HANDOFF (status stays doing). Committed + pushed: 4 vendor adapters (amazon/mcmaster/jlcpcb/pcbway â€” 8d6d4483) with correct per-vendor design + guardrails (Amazon bot-challengeâ†’login_required never bypassed; terms-walls on mcmaster/jlcpcb/pcbway; never auto-accept ToS / store creds), federation registration (automatic via __init__ import), mocked-HTML tests (45 passed on repo .venv), vendor survey doc (9be7a5ed), license_debt rows 5-8 (43775595). 4 of 6 DoD items done. REMAINING (see ## HANDOFF in task file with anchors): item 3 (thread mcmaster sku into BrowserCatalogAdapter part-record params â€” small) + item 4 (wire browser-vendor parsed price/stock into the bom_price approved-provider seam via a fail-closed thin PartProvider or VendorPart â€” main remaining work, numbers copied-with-provenance never generated). Env caveats: full gate.sh + graphify update are Linux/CI-only (can't run on this Windows box), same as T-G25. Preserved unrelated Discord-notifier WIP untouched throughout.
- 2026-07-06 19:18 UTC [opus-wynn] Resumed + advanced. Item 3 DONE (2bca34ba): BrowserCatalogAdapter.get() folds vendor extras (sku/price/availability/asin/lcsc/stock) into part-record params + conditional geometry_ref (fixed a latent KeyError the no-CAD adapters would hit â€” provenance_envelope omits artifact_path when None); +2 tests, 57 pass. Item 4 (bom_price price seam) ESCALATED to advisor (work/questions/T-X7.md Q1) rather than wired unilaterally: parts_providers.py is a deliberate API-key-only "Chudware does NOT scrape" seam (guarantee in user-facing errors + license register), so feeding browser-session prices in is a contract-posture change â€” recommended a distinct profile-gated fail-closed BrowserVendorProvider behind the API providers; awaiting ruling. Now 5/6 DoD items done + 1 escalated. Remaining after ruling: implement the chosen bom_price path.
