---
id: T-B1b
title: Nexar, Digi-Key, and Mouser provider implementations
workstream: B
owner: unassigned
status: todo
branch: task/T-B1b-part-providers
day: 1
depends_on: [T-B1a, T-S1]
---

## Context

The afternoon deliverable is three `PartProvider` implementations behind the interface frozen in T-B1a. Nexar (Octopart GraphQL, free 1K calls/mo) is the primary provider and the only one likely to have an active key on day 1. Digi-Key and Mouser providers are scaffolded and wired but may run in stub mode until keys arrive. All three fill `unit_price`, `currency`, `price_breaks`, `stock`, `lead_time_days`, `moq`, `fetched_ts` on a VendorPart.

## Files / areas touched

- `engine_oracle/parts_providers.py` — add NexarProvider, DigiKeyProvider, MouserProvider classes
- `engine_oracle/vendor_parts.py` — VendorPart (already extended by T-B1a)
- `services/mcp/server.py` — no new tools today; providers are internal; tools come in T-B2

## Steps

1. Implement `NexarProvider.fetch(mpn: str) -> VendorPart | None`:
   - POST to `https://api.nexar.com/graphql` with OAuth2 bearer token from `NEXAR_CLIENT_ID` / `NEXAR_CLIENT_SECRET`.
   - Query: `supply { results { part { mpn, manufacturer { name }, sellers { company { name }, offers { inventoryLevel, moq, prices { price, quantity }, factoryLeadDays } }, datasheetUrl } } }`.
   - Map first seller's cheapest offer to VendorPart price fields.
2. Implement `DigiKeyProvider.fetch(mpn: str) -> VendorPart | None`:
   - Use Digi-Key Product Information API v4; `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` OAuth2.
   - If key not yet active, return `None` with a logged warning (graceful stub).
3. Implement `MouserProvider.fetch(mpn: str) -> VendorPart | None`:
   - Use Mouser SearchByPartNumber API; `MOUSER_API_KEY`.
   - Graceful stub if key not yet active.
4. Add a `ProviderChain` that tries providers in priority order (Nexar → DigiKey → Mouser) and returns the first non-None result.
5. Wire `ProviderChain` as the default resolver in `parts_library.py` (or wherever catalog lookups are dispatched).
6. Test: mock HTTP layer; assert NexarProvider maps GraphQL response to correct VendorPart fields.

## Definition of done

- [ ] `NexarProvider` fetches real data when `NEXAR_CLIENT_ID` is set; returns None gracefully when not set
- [ ] `DigiKeyProvider` and `MouserProvider` scaffold exists; stub mode when keys absent
- [ ] `ProviderChain` falls through providers in order
- [ ] `python -m pytest engine_oracle/test_parts_providers.py -q` passes (mock HTTP)

## AI hints

- `graphify explain "parts_providers.py"` — understand existing provider pattern / base class
- `graphify query "PartProvider fetch interface"` — check if base class already defined
- Relevant files: `engine_oracle/parts_providers.py`, `engine_oracle/vendor_parts.py`
- Nexar OAuth token endpoint: `https://identity.nexar.com/connect/token` (client_credentials grant)
- All HTTP calls must time out (5s default); never block the main thread — providers are called from the agent loop
