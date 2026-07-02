---
id: T-F9
title: Providers contract, tool registry/allow-listing, session-auth spike
workstream: F
owner: opus-pike
status: done
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

- [x] Contract frozen; Python adapters conformant (C++ mirrors later, seam documented)
- [x] Every MCP tool tagged; allow-list enforced at dispatch with tests
- [x] OpenRouter PKCE login works end-to-end behind a feature flag
- [x] No prohibited auth path in code; `bash scripts/gate.sh`; `graphify update .`

## Completion notes (opus-pike, 2026-07-02)

- `contracts/providers.md` v1 frozen: neutral message/adapter/thinking/streaming schema,
  token budgets, auth kinds (api_key | oauth_pkce | none/local), OpenRouter PKCE §4.1,
  BINDING prohibited-auth §4.2, AI-CAD Cloud relay config seam §4.3, offline invariant §5,
  tool registry + allow-listing §6, RESERVED `routing` §7 (T-H9). Corrected cite:
  `call_budget_for` is in `core/agent/run.cpp:48-63` (not config.cpp).
- Registry: `services/mcp/tool_registry.py` tags {family,risk} via name-pattern classifier
  + one shared dispatch guard (contract-shaped `{ok:false,reason,hint}` on deny); `server.py`
  wraps all 6 `register_*_tools()` call sites with `view(mcp, family)`. Default = allow all.
- OpenRouter PKCE: `providers/oauth_pkce.py` (S256, feature-flagged, transport-injectable) +
  `providers/openrouter_provider.py`; wired into `config.py` (resolve + offline refusal +
  never-offline-safe). Prohibited consumer-OAuth grep gate in `test_prohibited_auth.py`.
- Installer path is `services/installer/setup_mcp_clients.py` (task listed `installer/…`);
  added `--detect` / `--wizard` groundwork.
- New tests live under `agent/` (the gate runs `pytest engine/doc agent`; `services/mcp`
  tests are NOT in the gate path — a pre-existing gap): test_tool_registry,
  test_openrouter_pkce, test_providers_conformance, test_prohibited_auth — all green
  (openai/anthropic-SDK cases importorskip; SDKs absent on this box).
- Gate caveat: `pytest engine/doc agent` has PRE-EXISTING failures in `test_loop_mock` /
  `test_compaction` (`ModuleNotFoundError: build123d`) reproduced on clean master by stash —
  NOT a T-F9 regression; environmental (no runtime venv, build123d unprovisioned). The
  ui/imgui selMulti break is the separately-known pre-existing red.

## Driver notes (Fable, 2026-07-02)

- **What you're converging:** Python adapters in `services/agent/providers/`
  (anthropic `:39-59` complete_ex returns `(text, thinking)`; openai `:31-70`;
  gemini; llamacpp inherits openai; mock for tests) and the C++ port
  `core/agent/providers.cpp` (BuiltRequest/ParsedReply). The contract freezes the
  NEUTRAL layer both already implicitly share: message list `[{role, content:
  [{type: text|image, ...}]}]`, tool-call schema, thinking channel, streaming,
  image slots, auth kinds (api_key | oauth_pkce | none/local), token budgets
  (`call_budget_for` in C++ `:48-63` — 4096 local / 8192 cloud defaults).
- **Registry + allow-listing:** ~150 tools register unconditionally today. Add a
  registry module where every `register_*_tools()` call site declares
  `{family, risk: read|write|spend|external}`; enforcement is ONE check in the
  dispatch path, not per-tool edits. Default allow-list = everything (no behavior
  change); the DAG node contract (T-A1's dag_schema) will pass explicit lists later.
- **Prohibited list is binding and testable:** grep-level CI assertion that no code
  references Claude/Gemini consumer OAuth endpoints. OpenRouter PKCE is the one new
  auth path; feature-flagged, key stored via the existing config.json mechanism
  (`core/agent/config.cpp` resolution, `%APPDATA%/AI-CAD/config.json`).
- **AI-CAD Cloud relay = one config entry** ("openai-compatible base_url + account
  auth kind"), no code path. Write the seam into the contract; implementation is
  human-gated (master plan Pillar 12).
- **New consumer to plan for (don't build):** T-H9 adds an `adapter` field for
  per-task LoRA routing and a per-insertion-point routing table — leave a
  `routing` section stub in the contract marked reserved-for-T-H9.
- **Offline mode invariants:** `provider_offline_safe` (`services/agent/config.py:
  40-109`) must keep working unchanged; conformance tests should include it.
