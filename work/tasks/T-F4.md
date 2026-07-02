---
id: T-F4
title: Migrate intake, clarify, and DAG gates onto the interaction contract
workstream: F
owner: unassigned
status: todo
branch: task/T-F4-interaction-migration
day: 9
depends_on: [T-F3, T-A3]
---

## Context

Pillar 9 completion: the MCP intake ledger (`confirm_dimensions`, `resolve_process`),
the C++ `clarify`/`ask` gates, and the DAG `gate` nodes (T-A3) become CALLERS of the
T-F3 contract instead of three parallel implementations. READ
`docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 9.

## Files / areas touched

- `services/mcp/` intake tool implementations
- `core/agent/` clarify/ask paths (C++ — mirror the contract, keep the seam thin)
- `services/dag/gate.py` (T-A3's implementation — adapt, don't rewrite)
- `contracts/interaction.md` — append-only clarifications if gaps surface

## Steps

1. Route each mechanism through `ask_user`; delete the bespoke question/answer plumbing
   each had.
2. Keep external behavior identical (existing tests must pass unmodified — don't weaken
   assertions).
3. Verify the shared ledger: a dimension confirmed via intake is visible to a DAG gate
   and never re-asked.

## Definition of done

- [ ] All three mechanisms call the contract; bespoke duplicates deleted
- [ ] Existing intake/clarify/gate tests pass unmodified
- [ ] Cross-mechanism no-re-ask test added; `bash scripts/gate.sh`; `graphify update .`

## Driver notes (Fable, 2026-07-02 — next-wave prep)

- The three callers to migrate (verified locations): C++ clarify() core/agent/
  clarify.cpp:112 (≤4-question batch cap — preserve), ask() core/agent/ask.cpp:180,
  MCP intake gates services/mcp/intake_tools.py:68-82 (resolve_process clarification)
  + :228-248 (confirm_dimensions). DAG gate nodes per T-A3's landed interrupt().
- Behavior-identical migration: same questions, same answer routing — the win is ONE
  contract + persistent answer ledger (T-F3's), not new UX.
- Answers persisting via T-F3's ledger should ALSO emit ai_artifact_action events
  (T-H1 landed the helpers: emit_ai_artifact_action in core/agent/events, consent-
  stamped) — a human answer to an agent question is intake gold per
  contracts/telemetry.md derivation rules.
