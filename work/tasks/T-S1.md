---
id: T-S1
title: Apply for vendor API keys
workstream: setup
owner: Bruh
status: todo
branch: task/T-S1-api-keys
day: 1
depends_on: []
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
- [ ] At least Nexar key is active and a test GraphQL query returns part data
- [ ] Keys stored in `.env`, `.env` is in `.gitignore`
- [ ] Status of each application noted (pending / active) in a comment block at top of `engine_oracle/vendor_parts.py`

## AI hints

- `graphify query "vendor parts provider interface"` to see current VendorPart schema
- Relevant file: `engine_oracle/vendor_parts.py` — check existing provider pattern before adding new ones
- Nexar GraphQL playground: https://nexar.com/api (free tier, no credit card)
