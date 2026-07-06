---
id: T-BL24
title: JLCPCB signed-request auth - rework JLCPCBProvider off the static-bearer assumption
workstream: backlog
owner: opus-sweep
status: review
branch: task/T-BL24-jlcpcb-auth
depends_on: []
lane: parts-sourcing
kind: fix
priority: 4
---

## Context

**Filed 2026-07-03 (Fable advisor, from T-S1 status).** Erik obtained real JLCPCB Open
API credentials: an `accesskey`/`secretkey` pair for SIGNED requests. The scaffold-era
`engine_oracle/quote_providers.py::JLCPCBProvider` assumes a single static bearer token
(`JLCPCB_KEY` / `quote_providers.jlcpcb.api_key`) — it cannot use the real keys. Raw
keys are recorded in `.env` (gitignored). The T-S1 driver correctly declined to
guess-implement the signing scheme without the real API docs.

## Definition of done

- [x] Scheme recorded in the provider docstring (`_jlcpcb_sign` section): JLCPCB Open API
      HMAC-SHA256, header `JOP appid/accesskey/nonce/timestamp/signature`, string-to-sign =
      METHOD\npath\ntimestamp\nnonce\nbody, base64. Sources: https://api.jlcpcb.com/ +
      https://jlcpcb.com/help/catalog/213-JLCPCB-API. CAVEAT: the exact byte ordering/separators
      are from public reverse-eng sources; the isolated `_jlcpcb_sign` is the ONE place to adjust
      after confirming against the authenticated official docs.
- [x] JLCPCBProvider builds signed requests from `JLCPCB_ACCESS_KEY`/`JLCPCB_SECRET_KEY`/
      `JLCPCB_APP_ID` (env) or `quote_providers.jlcpcb.{appid,accesskey,secretkey}` (config),
      via `_resolve_jlcpcb_creds` (mirrors parts_providers `_resolve_creds`). appId added — the
      real Open API needs the triple, not just accesskey/secretkey.
- [~] Mocked signing test DONE (engine_oracle/test_quote_providers.py — byte-exact signature
      fixture + degrade-closed + signed-header re-verify; 8 passed). LIVE smoke with the real
      keys NOT done autonomously — sending real credentials to JLCPCB is an outbound action I
      won't take without confirmation; it's the one env/human step left (see note below).
- [x] Old static-bearer path removed: JLCPCBProvider.quote() no longer reads JLCPCB_KEY /
      quote_providers.jlcpcb.api_key; the `env_var` attr is kept only as a deprecation marker
      and quote() surfaces a clear message pointing at the new appid/accesskey/secretkey.

## Progress log
- 2026-07-06 [opus-sweep] DONE bar the live smoke. Reworked JLCPCBProvider to HMAC-SHA256 signed
  requests (commits 8358652d impl + f31460d0 test). 8 tests pass on repo .venv. LIVE SMOKE
  REMAINING (env/human): run one real quote with the .env accesskey/secretkey (+ appId) and
  record the result here — I did NOT do this autonomously (won't transmit real credentials). If
  the live call 401s, adjust the string-to-sign in the isolated `_jlcpcb_sign` per the
  authenticated JLCPCB docs. Env caveat: full gate.sh + graphify are Linux/CI-only on this box.
  NOTE: this session collided with a concurrent agent on the shared checkout mid-task (see memory
  multi-session-checkout-collision) — recovered; work was preserved in the wip commit.

## AI hints

- `graphify query "quote providers JLCPCB"` — current provider shape
- `engine_oracle/parts_providers.py` `_resolve_creds` — the credential-resolution
  pattern to mirror; T-S1 status section documents where the keys live
