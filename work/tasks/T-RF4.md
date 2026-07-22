---
id: T-RF4
title: RF wave-1 subscription auth/token-refresh ported to core/agent C++ (node R1a)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF4-agent-auth-cpp
depends_on: [T-RF3]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

Every C++ agent run currently calls BACK into Python to refresh subscription tokens
(host/src/core/jobs.cpp:45-62), and provider logic is duplicated: core/agent/providers.cpp
(1,573 lines) vs services/agent/providers/ (~10k: cursor_sub_provider, xai_sub_provider,
kimi_sub_provider, codex_sub_provider, claude_cli_provider, sub_auth.py, oauth_pkce.py,
subscription_login.py, usage.py). User decision (2026-07-22): KEEP the CLI-subscription
providers — port auth/token-refresh into C++ so the Python sidecar dependency dies.

## Scope

Port token refresh/PKCE/session store into core/agent/ (new auth.* + CLI provider
adapters that spawn the CLI binaries — they are subprocess transports already). Delete
the per-run Python callback in jobs.cpp. Python providers keep working read-only against
the same token store until T-RF20. Test against a scratch config dir, never live creds.

## Definition of done

- [x] C++ agent runs end-to-end with a subscription provider, no Python in the loop.
- [x] jobs.cpp Python token callback removed.
- [ ] Token-store format unchanged (Python side reads it until T-RF20).
- [ ] Tests for refresh/expiry paths; gate.sh green; REFACTOR_LOG handoff.
