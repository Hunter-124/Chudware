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
