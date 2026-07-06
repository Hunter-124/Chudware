---
id: T-BL24
title: JLCPCB signed-request auth - rework JLCPCBProvider off the static-bearer assumption
workstream: backlog
owner: opus-sweep
status: doing
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

- [ ] Fetch and follow JLCPCB's official Open API signing documentation (record the
      URL + scheme summary in the provider docstring)
- [ ] JLCPCBProvider builds signed requests from `JLCPCB_ACCESS_KEY`/`JLCPCB_SECRET_KEY`
      (env) or `quote_providers.jlcpcb.{accesskey,secretkey}` (config.json), mirroring
      the `_resolve_creds` pattern Nexar uses
- [ ] Mocked signing test (deterministic signature fixture) + one live smoke call
      verified with the real keys, result noted in the task file
- [ ] Old static-bearer path removed or clearly deprecated

## AI hints

- `graphify query "quote providers JLCPCB"` — current provider shape
- `engine_oracle/parts_providers.py` `_resolve_creds` — the credential-resolution
  pattern to mirror; T-S1 status section documents where the keys live
