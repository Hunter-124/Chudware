---
id: T-RF2
title: RF wave-0 dead-code purge — services/worker dupes + orphaned ui/viewport (node R0b)
workstream: RF
owner: integrator
status: todo
branch: task/T-RF2-dead-code-purge
depends_on: []
lane: platform-infra
kind: overhaul
priority: 2
---

## Context

`services/worker/` carries duplicate modules in NO build: async_rpc.*, doc_mirror.*,
jobs.*, sim_client.*, selection.*, doc_tabs.*, bootstrap.*, worker_command_dispatcher.*
(the last is broken — includes headers from the deleted ui/abstract). The host compiles
only worker_client/engine_backend/cpp_core/worker_router from services/worker/; live
copies of the rest come from host/src/core/. `ui/viewport/` is an orphaned "shared"
renderer — host builds its own (host/src/renderer.cpp + host/src/render/*); core builds
only ui/viewport/render/aicm_loader.cpp. Two AICM loaders exist.

## Scope

Prove each candidate unreferenced (all CMakeLists + includes, incl. WASM branch), delete
it; keep the four compiled services/worker modules and the one core-built AICM loader.
Update services/worker/AGENTS.md + ui/viewport/AGENTS.md to post-purge reality. Do NOT
merge the two AICM loaders (report only). CMake edits, if any, go through the integrator.

## Definition of done

- [x] Dead files deleted; grep proof no references remain.
- [ ] Host + core rebuild green after deletion.
- [ ] Sub-AGENTS.md files match on-disk reality.
- [ ] REFACTOR_LOG.md handoff appended; gate.sh green (integrator run).
