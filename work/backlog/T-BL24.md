---
id: T-BL24
title: JLCPCB signed-request auth - rework JLCPCBProvider off the static-bearer assumption
workstream: backlog
owner: opus-sweep
status: blocked
branch: task/T-BL24-jlcpcb-auth
depends_on: []
lane: parts-sourcing
kind: fix
priority: 4
blocker: needs live JLCPCB API credentials for an end-to-end signed-request smoke test; signing logic and mocked tests are verified complete.
---

## Context

**Filed 2026-07-03 (Fable advisor, from T-S1 status).** Erik obtained real JLCPCB Open
API credentials: an `accesskey`/`secretkey` pair for signed requests. The live provider
in `core/sourcing/quote.cpp` uses the required `appid`/`accesskey`/`secretkey` triple and
per-request HMAC-SHA256 signing. Raw keys are recorded in `.env` (gitignored); the
remaining work is the credentialed end-to-end smoke test against JLCPCB.

## Definition of done

- [x] Scheme recorded in `core/sourcing/quote.cpp::jlcpcb_sign`: JLCPCB Open API
      HMAC-SHA256, header `JOP appid/accesskey/nonce/timestamp/signature`, string-to-sign =
      METHOD\npath\ntimestamp\nnonce\nbody, base64. Sources: https://api.jlcpcb.com/ +
      https://jlcpcb.com/help/catalog/213-JLCPCB-API. CAVEAT: the exact byte
      ordering/separators are from public reverse-engineering sources;
      `jlcpcb_sign` is the one place to adjust after checking authenticated docs.
- [x] `core/sourcing/quote.cpp::request_quote` builds signed requests from
      `JLCPCB_ACCESS_KEY`/`JLCPCB_SECRET_KEY`/`JLCPCB_APP_ID` (environment) or
      `quote_providers.jlcpcb.{appid,accesskey,secretkey}` (config).
- [~] Mocked signing coverage DONE (`core/tests/test_sourcing.cpp` — byte-exact
      signature fixture, field-sensitivity checks, and keyless degrade-closed behavior).
      LIVE smoke with the real keys is the one env/human step left (see note below).
- [x] Old static-bearer path removed: `core/sourcing/quote.cpp::request_quote()` reads
      only the appid/accesskey/secretkey triple and its keyless error explicitly rejects
      the old single-key bearer configuration.

## Progress log
- 2026-07-06 [opus-sweep] DONE bar the live smoke. Implemented HMAC-SHA256 signed
  requests and pinned the byte-exact signature fixture. LIVE SMOKE REMAINING
  (env/human): run one real quote with the appid/accesskey/secretkey triple and record
  the result here. If the live call 401s, adjust `jlcpcb_sign` per the authenticated
  JLCPCB docs.
- 2026-08-27 [cleanup audit] Remaining DoD item: run one credentialed JLCPCB quote
  end-to-end with the real appid/accesskey/secretkey triple and record the response;
  source inspection confirms the signing, credential resolution, mocked fixture, and
  removal of the static-bearer path are present in the C++ implementation.

## AI hints

- `graphify query "quote providers JLCPCB"` — current provider shape
- `core/sourcing/providers.cpp` and `core/sourcing/quote.cpp` contain the live
  credential-resolution patterns; T-S1 documents where the keys live
