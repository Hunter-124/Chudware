---
id: T-BL25
title: Doc-scoped IntakeLedger - persist scope=doc answers per interaction.md section 5
workstream: backlog
owner: sonnet-heath
status: review
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

## Implementation notes (sonnet-heath, 2026-07-03)

### Persistence choice: doc-scoped sidecar (DocLedger)

Chose **sidecar** over IntakeState-backed approach:
- `intake_tools.py` saves IntakeState via `doc_set_meta` (C++ bridge) — round-tripping
  that in tests requires libaicad.so; tests must be stdlib-only.
- Contract §5 explicitly allows "with the doc **or alongside it**".
- FileLedger pattern mirrors cleanly; zero new deps.

Sidecar lives at `<AICAD_CACHE_ROOT>/docs/<doc_id>/interaction.jsonl`.
`AICAD_CACHE_ROOT` env var overrides for tests (same pattern as FileLedger).

### Changes

- `services/agent/interaction.py`: `doc_dir()` + `DocLedger` class (mirrors FileLedger,
  keyed on doc_id). `ask_user()` scope=doc branch now uses `DocLedger(batch.doc_id)`
  instead of falling back to `MemoryLedger`.
- `services/mcp/intake_tools.py`: `_ask_via_contract()` gains `doc_id` kwarg; picks
  DocLedger when doc_id supplied. `confirm_dimensions()` routes scope=doc when run_id
  absent but doc_id available.
- `services/dag/test_intake_gate_ledger.py`: `TestDocLedgerCrossSession` — 3 tests
  covering cross-session no-re-ask (BoomTransport), doc isolation, and intake routing.

### Test results

- `services/dag/test_intake_gate_ledger.py`: 17/17 pass + 2 pre-existing skips
- `services/agent/test_interaction.py`: 18/18 pass
- Total: 35 pass, 2 skip

## AI hints

- `work/questions/T-F4.md` Q1 — what was deferred and the run-scope workaround
- `graphify query "intake ledger ask_user scope"` — FileLedger shape to mirror
- `contracts/interaction.md` §5 is the binding contract text
