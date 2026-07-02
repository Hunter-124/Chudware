---
id: T-F9
title: Providers contract, tool registry/allow-listing, session-auth spike
workstream: F
owner: unassigned
status: todo
branch: task/T-F9-providers-contract
day: 7
depends_on: []
---

## Context

Pillar 12: converge the three provider layers (Python providers, C++ port, external MCP
clients) onto one frozen `contracts/providers.md`; add per-context tool allow-listing
(the DAG node contract requires `tools:` lists); spike the BLESSED session-auth routes
only. READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 12 — the prohibited list is binding
(NEVER consumer OAuth token reuse for Claude/Gemini; OpenRouter PKCE is the blessed
default; AI-CAD Cloud relay is a config seam only, human-gated).

## Files / areas touched

- `contracts/providers.md` — new frozen contract (message/tool-call schema, image
  slots, streaming, auth kinds)
- `services/agent/providers/` — conform existing adapters; OpenRouter PKCE adapter
- `services/mcp/server.py` + a new registry module — tool tags (family + risk class:
  read/write/spend/external) enforced at dispatch
- `installer/setup_mcp_clients.py` — first-run "connect your client" wizard groundwork

## Definition of done

- [ ] Contract frozen; Python adapters conformant (C++ mirrors later, seam documented)
- [ ] Every MCP tool tagged; allow-list enforced at dispatch with tests
- [ ] OpenRouter PKCE login works end-to-end behind a feature flag
- [ ] No prohibited auth path in code; `bash scripts/gate.sh`; `graphify update .`
