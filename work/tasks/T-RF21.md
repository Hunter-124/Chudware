---
id: T-RF21
title: RF closeout — remove AI_CAD_CPP_* fallback flags, delete engine_oracle (node R5a)
workstream: RF
owner: integrator
status: todo
branch: task/T-RF21-oracle-delete
depends_on: [T-RF20]
lane: platform-infra
kind: overhaul
priority: 4
---

## Context

Once every subsystem routes C++-only, the escape hatches become dead weight:
AI_CAD_CPP_DOCS / AI_CAD_CPP_AGENT / AI_CAD_CPP_SIM / AI_CAD_CPP_ECAD flags and the
Python routing branches in services/worker/worker_client.cpp, plus the Python worker
spawn path (python -m engine.worker) and the root `engine` symlink.

## Scope

Remove the flags + Python fallback branches from worker_client.cpp (integrator-owned),
delete engine_oracle/ entirely, delete the root engine + services/engine symlinks, and
remove the host's Python-worker spawn/provision path (bootstrap runtime stays only if
something non-engine needs it — prove first). Web parity inventory is empty by
construction; keep the probe as a regression gate.

## Definition of done

- [x] engine_oracle/ gone from the tree; zero Python engine processes spawn anywhere.
- [x] Routing table is single-path C++; flags removed from code + docs.
- [ ] Full gate green incl. WASM smoke + parity probe; REFACTOR_LOG handoff.
