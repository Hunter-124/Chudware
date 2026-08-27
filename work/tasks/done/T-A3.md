---
id: T-A3
title: Human-in-the-loop gate nodes and autonomy dial
workstream: A
owner: sonnet-hale2
status: done
branch: task/T-A3-hitl-gates
day: 3
depends_on: [T-A2]
lane: ai-copilot
kind: new
---

## Context

Gate nodes implement human approval checkpoints via LangGraph `interrupt()`. When a gate node fires, execution suspends and persists; the user resumes after app restart. The `pause` verdict must be added to `core/agent/machine.h` (per the INTAKE plan Gate 0 spec). The autonomy dial is per-run (default) with per-node override: `auto` = no gate, `approve` = gate node inserted before the node, `manual` = gate node + pre-filled draft shown to user.

## Files / areas touched

- `services/dag/gate.py` — new: gate node implementation using LangGraph interrupt()
- `core/agent/machine.h` — add `pause` verdict to the stage machine enum
- `services/dag/executor.py` — wire resume-from-checkpoint after interrupt
- `services/dag/autonomy.py` — new: per-run default + per-node override logic
- `contracts/dag_schema.md` — verify autonomy field is correctly specified (read-only today)

## Steps

1. Run `graphify explain "machine.h"` to locate the verdict enum and understand the stage machine before editing.
2. Add `VERDICT_PAUSE` to the verdict enum in `core/agent/machine.h`; add a comment referencing the DAG gate contract.
3. Implement `gate.py`: a LangGraph node that calls `interrupt({"gate_id": ..., "context": ...})`; on resume, reads the human decision from the interrupt result (`approve` / `reject` / `modify`); propagates accordingly.
4. Implement `autonomy.py`: given a node's `autonomy` field and the run-level default, decide whether to insert a gate node before execution.
5. Update `executor.py` to support `resume(run_id, gate_decision)`: re-open the checkpoint SQLite and feed the interrupt result.
6. Test: run a graph with a `gate` node, assert it suspends (raises `LangGraph.Interrupt`), resume with `approve`, assert execution continues to completion.

## Definition of done

- [ ] `VERDICT_PAUSE` added to `core/agent/machine.h` stage machine enum
- [ ] `gate.py` implements interrupt/resume round-trip via LangGraph interrupt()
- [ ] `executor.resume(run_id, decision)` reloads checkpoint and continues
- [ ] Autonomy dial applied: `auto` nodes skip gates, `approve` nodes insert gate, `manual` nodes insert gate with pre-filled draft
- [ ] `python -m pytest services/dag/test_gate.py -q` passes including app-restart simulation (close + reopen checkpoint DB)
- DEFERRED (foreman review, round 1): C++ wiring of the `pause` verdict — `machine.cpp` `run_stage()` treats only pass/fail/skip/cancelled as terminal, so `pause` is documented as RESERVED in `machine.h` (Python-side LangGraph interrupt only). Wire the C++ side in a follow-up task when a C++ stage machine needs to yield to human input.

## AI hints

- `graphify explain "machine.h verdict"` — find existing verdict values before adding VERDICT_PAUSE
- `graphify query "interrupt resume LangGraph checkpoint"` — check if any prior usage in repo
- Relevant files: `core/agent/machine.h`, `services/dag/executor.py`
- LangGraph interrupt pattern: `from langgraph.types import interrupt` — call inside the node function; the graph raises `GraphInterrupt`; resume via `graph.invoke(None, config={"configurable": {"thread_id": run_id}})`

## Driver notes (Fable, 2026-07-02 — WS-A opened to agents; chain off A2)

- Branch FROM T-A2's branch when it exists (merge order A1 → A2 → A3).
- machine.h VERDICT_PAUSE is a C++ change: rebuild core AND RESTAGE libchudware.so — the
  staged .so is stale (predates sim_run_static; open board note says "next C++ task
  refreshes it" — that is YOU; record the restage in your check-in so Python suites
  stop skipping).
- Gate question payloads MUST be shaped as T-F3 interaction-contract ask_user calls
  (kind=approval, ledger-backed) so T-F4's migration is a thin adapter, not a rewrite —
  contracts/interaction.md is on master by then (or origin/task/T-F3-interaction-contract).
- autonomy levels are FROZEN in aicad.dag/1 (auto/approve/manual, per-run default +
  per-node override) — implement exactly; resume(run_id, decision) re-opens A1's
  SqliteSaver checkpoint.
- interrupt() persistence must survive app restart (the DoD's suspend-across-restart is
  the hard bit — test it by re-instantiating the executor against the same checkpoint
  DB, not by mocking).
