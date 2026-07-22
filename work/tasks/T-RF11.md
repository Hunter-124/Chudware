---
id: T-RF11
title: RF wave-2 app.cpp dispatch decomposition — per-workspace handler tables (node R2b)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF11-dispatch-decomp
depends_on: [T-RF6]
lane: app-gui
kind: overhaul
priority: 3
---

## Context

host/src/app.cpp is a 5,200-line monolith; ALL ribbon commands route through one
hardcoded if-chain (app.cpp:1534) and AppState (host/src/core/app_state.h, 1,803 lines)
is a god-aggregate every panel pokes directly. This is the main "hard to modify"
surface in the GUI. YAGNI constraint: NO framework, NO view-model layer (the ui/abstract
effort was built once and deleted) — data-driven within C++ only.

## Scope

Replace the if-chain with per-workspace command-handler registration: a plain table of
cmd_id → handler living in each workspace module, registered at init, matching existing
code patterns. Slim AppState only where a moved handler forces it. Conflict magnet:
this node owns app.cpp exclusively within wave 2 — sequence it to land FIRST.

## Definition of done

- [x] Ribbon dispatch is table-driven; app.cpp shrinks materially.
- [ ] All ~150 ribbon commands still route (feature_surface_audit green).
- [ ] Behavior unchanged (AI_CAD_SHOT scenarios); gate.sh green; REFACTOR_LOG handoff.
