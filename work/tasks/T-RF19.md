---
id: T-RF19
title: RF stream-Z long-tail oracle modules cutover (node R4e)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF19-longtail-cpp
depends_on: [T-RF15, T-RF16, T-RF17, T-RF18]
lane: modeling-core
kind: overhaul
priority: 3
---

## Context

After R4a–d, the remaining engine_oracle/ modules are the long tail: hydraulic, piping,
intake, vision, research, render (headless PyVista screenshots), structural, tolerance,
vendor_parts leftovers, and engine_oracle/doc format/export remnants (formats,
tessellate, op_registry). ~25 modules total before this stream started.

## Scope

Sub-split at dispatch into per-module leaves (each small enough for one subagent):
port to the matching core/ subsystem, corpus parity, flip routing, delete the Python
module in the same commit. Modules with no live consumer get deleted, not ported
(YAGNI — prove the consumer first via the MCP tool surface and GUI call sites).

## Definition of done

- [ ] Every remaining oracle module either ported+deleted or proven dead+deleted.
- [ ] engine_oracle/ reduced to worker.py shell + __init__ shims for T-RF20.
- [ ] gate.sh green per leaf; REFACTOR_LOG handoffs; web_parity inventory ~empty.
