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

- `engine_oracle/vendor_parts.py` — VendorPart schema, will gain price fields once keys arrive
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
8. Verify each key works with a minimal test call; record status in a comment in `engine_oracle/vendor_parts.py`.

## Definition of done

- [ ] Applications submitted for all six providers (Nexar, Digi-Key, Mouser, Xometry, JLCPCB, McMaster)
- [x] At least Nexar key is active and a test GraphQL query returns part data
- [x] Keys stored in `.env`, `.env` is in `.gitignore`
- [ ] Status of each application noted (pending / active) in a comment block at top of `engine_oracle/vendor_parts.py`

## Status (2026-07-03)

Only two of six keys obtained so far — Digi-Key, Mouser, Xometry, McMaster still pending
(Erik hasn't gotten to those applications yet).

- **Nexar: ACTIVE, verified live.** `client_id`/`client_secret` in
  `~/.local/share/Chudware/config.json` under `parts_providers.nexar` (the app's real
  credential source per `_resolve_creds` — `.env` is env-var fallback only and nothing
  in this repo auto-loads it). Ran `NexarProvider().fetch("NE555P")` against the real
  API and got live pricing/stock back.
  - **Real bug found+fixed while verifying:** the `supSearchMpn` GraphQL query asked
    for a `datasheetUrl` field that no longer exists on Nexar's `SupPart` type (schema
    drift since T-B1b) — every `fetch()` call was silently swallowing the GraphQL error
    and returning "no supply results" instead of real data. Introspected the live schema,
    found the field moved to `bestDatasheet { url }`, fixed the query + mapper in
    `engine_oracle/parts_providers.py`, updated the mocked test fixture in
    `test_parts_providers.py` (`datasheetUrl` → `bestDatasheet.url`). 17/17 tests pass.
- **JLCPCB: keys held, NOT wired.** Got an `accesskey`/`secretkey` pair (JLCPCB's real
  Open API auth model — signed requests, not a static bearer token). But
  `engine_oracle/quote_providers.py::JLCPCBProvider` only supports a single static
  bearer token (`JLCPCB_KEY` / `quote_providers.jlcpcb.api_key`) — a scaffold-era
  assumption that doesn't match the real API. Raw keys recorded in `.env` (see comment
  there); needs a small follow-up task to rework `JLCPCBProvider`'s auth flow before
  these can be used. Not attempted blind — didn't want to guess-implement a signing
  scheme without the real API docs in hand.

## AI hints

- `graphify query "vendor parts provider interface"` to see current VendorPart schema
- Relevant file: `engine_oracle/vendor_parts.py` — check existing provider pattern before adding new ones
- Nexar GraphQL playground: https://nexar.com/api (free tier, no credit card)
