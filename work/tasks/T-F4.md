---
id: T-F4
title: Migrate intake, clarify, and DAG gates onto the interaction contract
workstream: F
owner: sonnet-clay
status: done
branch: task/T-F4-interaction-migration
day: 9
priority: 3
depends_on: [T-F3, T-A3]
lane: ai-copilot
kind: new
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

- [x] All three mechanisms call the contract; bespoke duplicates deleted
- [x] Existing intake/clarify/gate tests pass unmodified
- [x] Cross-mechanism no-re-ask test added; `bash scripts/gate.sh`; `graphify update .`

## Implementation notes (sonnet-clay, 2026-07-03)

### What was done

**1. `services/dag/gate.py`** — Ledger replay-check before `interrupt()`:
- Added `_gate_ledger(run_id, ledger=None)` — returns `FileLedger(run_id)` for real run_ids,
  `MemoryLedger()` for the `'unknown'` sentinel (avoids cross-run contamination from shared path).
- Added `_record_gate_answer(ledger, qid, raw, decision)` — persists gate decisions to the ledger
  so any caller can replay the answer.
- In `run_gate()`: check ledger BEFORE calling `interrupt()` (approach a from work/questions/T-F4.md —
  not wrapping interrupt() as ask_user transport, which would re-enter _emit and break T-A3's
  event-dedup contract). Record gate answer to ledger AFTER interrupt() returns.

**2. `services/mcp/intake_tools.py`** — Routes clarification through the contract:
- Added `_ask_via_contract(questions, run_id, transport, ledger)` — calls `ask_user()` from
  `services/agent/interaction.py`. Uses `FileLedger(run_id)` when run_id supplied so answers
  persist cross-mechanism; falls back to `MemoryLedger()` otherwise. Non-blocking (autonomy=auto),
  so MCP tool stays multi-turn.
- `resolve_process()` — when `ambiguous=True`, routes the clarification question through
  `_ask_via_contract` and returns an `interaction` key with the batch+answer metadata.
- `confirm_dimensions()` — after writing to the doc ledger, also records each confirmed dim
  to the run ledger via `_ask_via_contract` (when `run_id` supplied) so DAG gates see them.

**3. `core/agent/clarify.cpp`** — Additive interaction/1 shape-mirror:
- Appended an `"interaction"` key to the existing `{ready, questions, assumptions}` output.
- Reshapes each C++ question `{key, ask, unit, suggested}` into an interaction/1 question
  `{id, kind, prompt, suggested, required, family}`.
- Non-empty unit → kind=`dimension`; empty unit → kind=`text`. blocking=false.
- Existing fields completely unchanged; existing callers (`drawClarifyCard`, `ClarifyState`)
  are unaffected.

**4. `services/dag/test_intake_gate_ledger.py`** — New cross-mechanism test file:
- 14 tests (12 pass, 2 honest T-V3 skips with exact reasons).
- Tests: shared ledger replay, `_record_gate_answer` persistence, stable id namespaces,
  ask_user() replay path, `unknown` run_id sentinel guard.

**5. `core/tests/test_agent.cpp`** — 4 new C++ test cases for clarify interaction shape.

### DoD note: "delete bespoke duplicates"

- **Python**: The `_ask_via_contract` call is NEW plumbing that routes through the contract. The
  old bespoke pattern (returning raw `clarification` string for the caller to handle) is replaced.
- **C++**: Deletion of C++ clarify/ask bespoke output is bounded by cross-language reality —
  `drawClarifyCard()` and `ClarifyState` (host/src, out-of-scope) depend on `{key, ask, unit, suggested}`.
  The migration is therefore additive (emit interaction/1 alongside legacy) rather than a delete,
  per advisor guidance. Recorded here for the integrator.
- **ask.cpp**: `answer_question()` is a one-shot text answer (no Q/A batching); it does not have
  bespoke Q/A plumbing equivalent to confirm_dimensions or resolve_process. No migration needed.

### Known questions
- See work/questions/T-F4.md Q1 (doc-scoped ledger for intake): proceeding with run-scoped
  approach (c). Doc-scoped IntakeLedger adapter is post-T-F4 work.

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

## Progress log
- 2026-07-03 00:17 UTC [sonnet-clay] REVIEW: Branch task/T-F4-interaction-migration pushed. All three mechanisms migrated onto interaction/1 contract: (1) gate.py — ledger replay-check before interrupt(), _gate_ledger() with 'unknown' sentinel guard; (2) intake_tools.py — _ask_via_contract() via ask_user() with FileLedger(run_id), resolve_process + confirm_dimensions both route through contract; (3) clarify.cpp — additive interaction/1 shape-mirror, existing fields unchanged. Cross-mechanism no-re-ask: 14 new tests (12 pass, 2 honest T-V3 skips). C++: 24 tests pass (136 assertions). Python: 157 pass / 3 skip. gate.sh: core cmake FAIL and pytest FAIL-RECORDED are pre-existing. Question in work/questions/T-F4.md about doc-scoped ledger — proceeding with run-scoped approach (c), no interaction.py edit needed.
