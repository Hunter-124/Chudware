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

- `services/parts/vendor_parts.py` — VendorPart schema, will gain price fields once keys arrive
- `contracts/sourcing_schema.md` — will be frozen on T-B1a
- Environment / secrets store (`.env` / OS keychain) — store keys outside repo

## Steps

1. Sign up / log in at Nexar developer portal (nexar.com/api); request OAuth2 client credentials.
2. Register at developer.digikey.com; request sandbox then production keys.
3. Register at developer.mouser.com; request API key.
4. Create Xometry business account; apply for Instant Quoting API access.
5. Create JLCPCB account; apply for developer API (jlcpcb.com/api).
6. Contact McMaster-Carr eProcurement team for API / EDI approval (haedmonds69@gmail.com as contact).
7. Store approved keys in `.env` (gitignored) under names `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET`, `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `MOUSER_API_KEY`, `XOMETRY_API_KEY`, `JLCPCB_API_KEY`.
8. Verify each key works with a minimal test call; record status in a comment in `services/parts/vendor_parts.py`.

## Definition of done

- [ ] Applications submitted for all six providers (Nexar, Digi-Key, Mouser, Xometry, JLCPCB, McMaster)
- [x] At least Nexar key is active and a test GraphQL query returns part data
- [x] Keys stored in `.env`, `.env` is in `.gitignore`
- [ ] Status of each application noted (pending / active) in a comment block at top of `services/parts/vendor_parts.py`

## Status (2026-07-03)

Only two of six keys obtained so far — Digi-Key, Mouser, Xometry, McMaster still pending
(Erik hasn't gotten to those applications yet).

- **Nexar: ACTIVE, verified live.** `client_id`/`client_secret` in
  `~/.local/share/Chudware/config.json` under `parts_providers.nexar` (the app's real
  credential source per `_resolve_creds` — `.env` is env-var fallback only and nothing
  in this repo auto-loads it). Ran `NexarProvider().fetch("NE555P")` against the real
  API and got live pricing/stock back.
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
