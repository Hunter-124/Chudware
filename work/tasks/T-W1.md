---
id: T-W1
title: Design ledger - persistent mission state with budgeted rendering (aicad.ledger/1)
workstream: W
owner: sonnet-loam
status: doing
branch: task/T-W1-design-ledger
depends_on: []
lane: ai-copilot
kind: new
priority: 2
---

## Context

Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.1, contracts/advisor.md §2 — READ BOTH
FIRST, the schemas are frozen). The ledger is the anti-context-loss mechanism: mission
brief, requirements, decisions, dimension table with drivers/tolerances/provenance,
open questions, stage log, artifacts. Every agent role cold-starts from
`ledger_render(budget_tokens)` instead of chat history — this is what makes 24k-context
local models viable across a whole mission.

## Files / areas touched

- `services/agent/ledger.py` — new: event log + materialized state + budgeted renderer
- `services/mcp/ledger_tools.py` — new: `ledger_get`, `ledger_note` MCP tools
- `services/mcp/server.py` — register the `ledger` family
- `services/agent/loop.py` — seam only: create/attach a ledger per Session (run_id),
  record artifacts (renders, ok builds) as events; do NOT restructure the loop
- `services/agent/test_ledger.py` — new

## Steps

1. Read docs/ADVISOR_ARCHITECTURE.md and contracts/advisor.md §2. The JSON shapes are
   frozen — implement them exactly (schema string `aicad.ledger/1`).
2. Implement `Ledger` in `services/agent/ledger.py`: `append(actor, kind, payload)`
   (assigns seq/ts, persists JSONL event + rewrites materialized `state`),
   `load(run_id)`, typed upsert helpers (`add_decision`, `upsert_dimension`,
   `add_question`, `log_stage`, `add_artifact`). Dimension events must carry
   `provenance` (tool result / calc / table / user answer) per contract rule.
3. Implement `ledger_render(budget_tokens)`: deterministic compact text, priority
   order brief → active-stage dimensions → open questions → decisions → requirements
   → rest; whole-entry truncation with a trailing `(+N more entries)` line. Use the
   existing ~4 chars/token heuristic from loop.py compaction if present.
4. Add `ledger_get` / `ledger_note` in `services/mcp/ledger_tools.py`, register in
   server.py as family `ledger` (read/write risk classes auto-detected by name —
   verify `ledger_get` classifies as read in tool_registry).
5. Wire the loop seam: `run_turn` creates/loads the run ledger, appends an `artifact`
   event per successful build (stl path, contact sheet) and a `decision` event when
   the model's json notes declare done. Keep changes minimal and behind a
   `AI_CAD_LEDGER=1` env default-on flag.
6. Tests: event append/replay round-trip; renderer determinism + budget compliance at
   200/800/4000 tokens; provenance required on dimension events; MCP tool smoke test.
7. MCP freshness rule (work/WORKFLOW.md §6): update server instructions to mention the
   ledger family; run `graphify update .`.

## Definition of done

- [ ] `aicad.ledger/1` implemented exactly as contracts/advisor.md §2
- [ ] Replay: state is reconstructible from events alone (test proves it)
- [ ] `ledger_render` deterministic, budget-respecting, whole-entry truncation
- [ ] `ledger_get`/`ledger_note` registered + risk-classed; server instructions updated
- [ ] loop.py records artifacts/decisions per run without behavior change otherwise
- [ ] `python -m pytest services/agent/test_ledger.py -q` passes

## AI hints

- `graphify query "agent loop session persistence"` before touching loop.py.
- Answer-ledger precedent: `services/agent/interaction.py` (answers.jsonl) — mirror
  its cache_root/runs/<run_id>/ pathing so both ledgers sit together.
- Do not invent extra ledger sections; additive changes need a contract minor bump.
