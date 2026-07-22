---
id: T-RF7
title: RF wave-1 single event-schema + prompt-catalog source (node R1d)
workstream: RF
owner: unassigned
status: todo
branch: task/T-RF7-event-schema
depends_on: [T-RF3]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

The aicad.agent_events vocabulary is written twice: core/agent/events.cpp (696) and
services/agent/agent_events.py (443) — any drift breaks the GUI for one runtime only.
Prompt catalog is manually synced (core/agent/roles.cpp:24-28 "Keep in sync with
services/agent/prompts/*.md" — a hardcoded list of 15 roles).

## Scope

Make contracts/agent_events.md machine-checkable: a JSON event-kind list under
contracts/ consumed by both emitters, plus a check.py lint that fails on drift.
Replace the hardcoded role list in roles.cpp with directory enumeration of
services/agent/prompts/*.md (both runtimes already read that dir).

## Definition of done

- [ ] One canonical event-kind list; C++ and Python emitters both validated against it.
- [ ] roles.cpp enumerates the prompts dir (no hardcoded names).
- [ ] New lint in check.py green; gate.sh green; REFACTOR_LOG handoff.
