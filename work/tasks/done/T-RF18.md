---
id: T-RF18
title: RF stream-Z sourcing/BOM/cost + parts providers cutover to core/ (node R4d)
workstream: RF
owner: unassigned
status: done
branch: task/T-RF18-sourcing-cpp
depends_on: [T-RF3]
lane: parts-sourcing
kind: overhaul
priority: 3
---

## Context

BOM rollup, cost model, spec-sheet matching, vendor providers (Nexar/Digi-Key/Mouser),
parts_hub live in engine_oracle/ (parts_providers.py 353 lines, spec_sheet_matcher.py
664, parts_hub/, bom/cost modules) — Python-only, so sourcing is unavailable on web.

## Scope

Port the sourcing/BOM/cost/parts-provider surface to core/ (HTTP provider calls follow
core/agent/providers.cpp patterns; secrets stay server-side per the gateway rule),
reach corpus parity (engine_oracle/parts_hub tests + BOM fixtures), flip routing,
delete the Python modules in the same commit. MCP sourcing tools surface unchanged.

## Definition of done

- [x] bom_*/quote/spec-match/parts-hub methods served by libchudware; parity green.
- [x] Python modules deleted same commit; routing flipped (integrator merge).
- [x] gate.sh green; REFACTOR_LOG handoff; web_parity inventory shrinks.
