---
id: T-S1
title: Apply for vendor API keys
workstream: setup
owner: Bruh
status: todo
branch: task/T-S1-api-keys
day: 1
depends_on: []
lane: platform-infra
kind: new
priority: 4
---

## Context

Lead-time risk: API approvals for Nexar, Digi-Key, Mouser, Xometry, JLCPCB, and McMaster eProcurement can take days to weeks. Apply on day 1 so keys arrive before they are needed. Nexar (Octopart) is the primary free-tier electronic-parts provider (1K calls/mo). Digi-Key and Mouser require developer-portal sign-up. Xometry and JLCPCB require business accounts for programmatic quoting. McMaster eProcurement is gated behind a separate approval process.

## Files / areas touched

- `core/sourcing/providers.cpp` (Nexar/Digi-Key/Mouser pricing) and `core/sourcing/quote.cpp`
  (Xometry/JLCPCB quote-out) — the live providers and their credential resolution (the
  Python provider chain was deleted in T-RF18)
- `contracts/sourcing_schema.md` — will be frozen on T-B1a
- Credentials stay outside the repo: the app config (`config.json` in the Chudware app-data
  dir, sections `parts_providers.<provider>` / `quote_providers.<provider>`, also writable
  from the in-app vendor pane), with environment variables as the fallback

## Steps

1. Sign up / log in at Nexar developer portal (nexar.com/api); request OAuth2 client credentials.
2. Register at developer.digikey.com; request sandbox then production keys.
3. Register at developer.mouser.com; request API key.
4. Create Xometry business account; apply for Instant Quoting API access.
5. Create JLCPCB account; apply for developer API (jlcpcb.com/api).
6. Contact the McMaster-Carr eProcurement team for API / EDI approval.
7. Store approved keys in the app config: `parts_providers.{nexar,digikey,mouser}` and
   `quote_providers.{xometry,jlcpcb}`. The env fallbacks are `NEXAR_CLIENT_ID`,
   `NEXAR_CLIENT_SECRET`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `MOUSER_API_KEY`,
   `XOMETRY_API_KEY`, `JLCPCB_APP_ID`, `JLCPCB_ACCESS_KEY` and `JLCPCB_SECRET_KEY`. Nothing in
   the repo auto-loads a `.env` file.
8. Verify each key works with a minimal test call; record status in this card's Status section.

## Definition of done

- [ ] Applications submitted for all six providers (Nexar, Digi-Key, Mouser, Xometry, JLCPCB, McMaster)
- [x] At least Nexar key is active and a test GraphQL query returns part data
- [x] Nexar credentials stored outside the repo in the app config `parts_providers.nexar`
      (recorded 2026-07-03, Status below)
- [ ] Every other obtained key stored the same way (step 7), not in a repo `.env` that
      nothing reads
- [ ] Status of each application noted (pending / active) in this card's Status section

## Status (2026-07-03)

Only two of six keys obtained so far — Digi-Key, Mouser, Xometry, McMaster still pending
(applications not yet submitted).

- **Nexar: ACTIVE, verified live.** `client_id`/`client_secret` live in the app config under
  `parts_providers.nexar`, which is the credential source `core/sourcing/providers.cpp` reads
  first (env vars are the fallback; nothing auto-loads a `.env`). A live `NE555P` fetch
  through the then-current provider returned real pricing/stock.
  - **Real bug found+fixed while verifying:** the `supSearchMpn` GraphQL query asked
    for a `datasheetUrl` field that no longer exists on Nexar's `SupPart` type. The
    live query and mapper in `core/sourcing/providers.cpp` use
    `bestDatasheet { url }`; `core/tests/test_sourcing.cpp` carries the matching fixture.
- **JLCPCB: keys held; signing wired, live smoke pending.** The live
  `core/sourcing/quote.cpp` path uses JLCPCB's signed-request credential triple
  (`appid`/`accesskey`/`secretkey`) and rejects the retired static bearer form. The
  remaining credentialed end-to-end smoke is tracked by T-BL24.

## AI hints

- `graphify query "vendor parts provider interface"` to see current VendorPart schema
- Relevant file: `services/parts/vendor_parts.py` — check the existing VendorPart pattern before adding new fields
- Nexar GraphQL playground: https://nexar.com/api (free tier, no credit card)
