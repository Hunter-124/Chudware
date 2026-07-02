---
id: T-F3
title: Freeze interaction contract - ask_user core, in-app dialog, MCP elicitation
workstream: F
owner: unassigned
status: todo
branch: task/T-F3-interaction-contract
day: 2
depends_on: []
---

## Context

Pillar 9: ONE frozen contract for "agent asks the human", replacing three ad-hoc
mechanisms (MCP intake ledger, C++ clarify/ask gates, DAG gate nodes). READ
`docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 9. This is also the autonomy-balance keystone:
typed questions with suggested defaults let the agent propose decisions instead of
either stalling or guessing.

## Files / areas touched

- `contracts/interaction.md` — new frozen contract
- `services/agent/interaction.py` — new: `ask_user(questions[], kind, blocking?)` core
- `host/src/ui/` — in-app dialog transport (PropertyManager-style form)
- `services/mcp/server.py` — MCP elicitation transport

## Steps

1. Freeze the contract: question kinds (choice, dimension+unit, free text, file/image
   request, approval), ≤4 per batch, suggested defaults, machine-readable answer schema,
   persistence of every answer to the run/doc record (a resumed agent never re-asks).
2. Implement the core + the answer ledger.
3. Transports: in-app ImGui dialog; MCP elicitation (spec-stable, shipped in Claude
   Code); CLI prompt for headless.
4. Proactive path: a failing verify step can raise a question with evidence attached
   (render + numbers) instead of silently retrying.

## Definition of done

- [ ] `contracts/interaction.md` frozen with schemas + examples
- [ ] ask_user round-trips through in-app dialog, MCP elicitation, and CLI
- [ ] Answers persist and replay on resume; tests green; `bash scripts/gate.sh`; `graphify update .`

## AI hints

- `graphify query "clarify ask gate intake ledger"` — find all three existing mechanisms
  before writing the contract; they become callers later (T-F4), don't break them now.
