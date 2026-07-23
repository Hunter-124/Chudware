---
id: T-BL25
title: Doc-scoped IntakeLedger - persist scope=doc answers per interaction.md section 5
workstream: backlog
owner: sonnet-heath
status: done
branch: task/T-BL25-doc-intake-ledger
depends_on: []
lane: ai-copilot
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-F4 Q1).** The frozen interaction
contract (`contracts/interaction.md` §5) specifies that `scope=doc` answers persist
to the document's intake ledger so a dimension confirmed in one session is never
re-asked in the next. T-F4 shipped only the run-scoped `FileLedger` (keyed on
`run_id`) and deferred the doc-scoped adapter. Verified on master 2026-07-03:
`IntakeLedger` has zero references in services/ or engine code — the doc-scope path
is entirely unwired. This is foundational for intake/interaction UX (no re-asking
the user across sessions).

## Definition of done

- [x] Doc-scoped ledger adapter stores/retrieves answers keyed on document identity,
      surviving process restart (serialize with the doc or alongside it — follow the
      canonical serialization policy from T-H2)
- [x] The no-re-ask test from T-F4 passes across two separate simulated sessions on
      the same doc (not just two mechanisms in one run)
- [x] ask_user/intake call sites route scope=doc through it per contract §5

## Implementation notes (sonnet-heath, 2026-07-03 — REWORKED per foreman review)

### Rework history

**v1 (REJECTED):** cache sidecar keyed on doc_id at `<cache>/docs/<doc_id>/interaction.jsonl`.
Foreman review found doc_id is a PER-PROCESS counter (`core/doc/session.cpp new_doc_id()`
L223 mints `doc-cpp%08llx`; `doc_open` stamps a fresh id every open) — the sidecar was
unfindable after restart, and the v1 tests missed it because they injected `path=` and
never exercised the production lookup. Also deviated from contract §5's specified store.

**v2 (this branch):** contract §5 as written — answers persist in the document's
`IntakeState` record, round-tripped as JSON into the `.step+` via `doc_set_meta`/
`doc_get_meta` ("Same records; different store"). The store travels WITH the document,
so the per-process doc_id churn is irrelevant by construction.

### Changes

- `engine_oracle/intake/intake.py`: additive `IntakeState.interaction` field
  (`{"questions": [...], "answers": {qid: answer}}`); to_dict/from_dict round-trip it
  so dataclass load/save paths never wipe recorded answers.
- `services/mcp/intake_ledger.py` (NEW): `IntakeLedger` — Ledger-protocol adapter over
  the doc's intake meta record. No FastMCP import (safe for interaction.py's lazy
  default path); engine bridge imported lazily; `get_meta`/`set_meta` injectable for
  engine-less tests; every mutation is load-modify-save so concurrent IntakeState
  field writes (status/needs) are preserved.
- `services/agent/interaction.py`: `_default_doc_ledger()` — ask_user's scope=doc
  default ledger is now IntakeLedger(doc_id) when the engine bridge is available,
  MemoryLedger otherwise (module stays stdlib-only at load).
- `services/mcp/intake_tools.py`: `_ask_via_contract(doc_id=...)` routes to
  IntakeLedger; `confirm_dimensions` persists scope=doc when run_id absent (guard
  fixed — `d` verified to be the RESOLVED doc-id string from `cad_tools._doc`, the
  same identity the crit_dims write uses, so `if confirmations:` suffices);
  `request_part_photos` carries forward prior interaction records when it rebuilds
  the IntakeState (wipe hazard closed).

### Tests

- `services/dag/test_intake_gate_ledger.py::TestIntakeLedgerCrossSession` (5 tests,
  pure Python): cross-session replay over a JSON-round-tripping meta store
  (BoomTransport asserts no re-ask), doc isolation, IntakeState round-trip
  preservation, load-modify-save field preservation, _ask_via_contract routing.
- `services/mcp/test_intake_ledger.py` (NEW, 3 tests vs REAL engine): doc_new →
  record → doc_save → doc_open (asserts the reopened doc_id DIFFERS) → fresh
  IntakeLedger from the doc alone replays with transport never invoked; same via
  ask_user's DEFAULT ledger path (nothing injected); confirm_dimensions call-site
  answer survives save+reopen. All 3 PASS against libchudware.so.

### Test results (v2)

- dag + agent + intake suites: 336 passed / 2 skipped; only pre-existing failures
  (test_h7_shadow offline-mode + test_providers_conformance, both red on master)
- services/mcp/test_intake_ledger.py: 3/3 vs real engine

## AI hints

- `work/questions/T-F4.md` Q1 — what was deferred and the run-scope workaround
- `graphify query "intake ledger ask_user scope"` — FileLedger shape to mirror
- `contracts/interaction.md` §5 is the binding contract text
