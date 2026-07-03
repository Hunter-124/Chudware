---
id: T-BL25
title: Doc-scoped IntakeLedger - persist scope=doc answers per interaction.md section 5
workstream: backlog
owner: fable-foreman
status: doing
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

- [ ] Doc-scoped ledger adapter stores/retrieves answers keyed on document identity,
      surviving process restart (serialize with the doc or alongside it — follow the
      canonical serialization policy from T-H2)
- [ ] The no-re-ask test from T-F4 passes across two separate simulated sessions on
      the same doc (not just two mechanisms in one run)
- [ ] ask_user/intake call sites route scope=doc through it per contract §5

## AI hints

- `work/questions/T-F4.md` Q1 — what was deferred and the run-scope workaround
- `graphify query "intake ledger ask_user scope"` — FileLedger shape to mirror
- `contracts/interaction.md` §5 is the binding contract text
