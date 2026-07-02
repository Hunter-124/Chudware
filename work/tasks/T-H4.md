---
id: T-H4
title: Verifier battery API + gap verifiers (groundedness, manifold, unit sanity, dim coverage)
workstream: H
owner: unassigned
status: todo
branch: task/T-H4-verifier-battery
day: 12
depends_on: []
---

## Context

Pillar 16 / verifier inventory (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §4). 30
deterministic checks exist; 13 are subsecond. This task (a) wraps them behind one
`verify(doc, checks[]) → {scores{}, findings[]}` battery API usable as an RL reward
vector, a CI gate, and T-F12's design_review body (coordinate — T-F12 owns the
narrated verdict, this task owns the scoring substrate); (b) builds the gap verifiers:
standalone GSL groundedness checker (model text → slot-match numerals vs state; also
the IP-7 inference output gate), `is_manifold()` + STEP export/re-import round-trip
check, unit-sanity range validator (the ksi/MPa 6.9× guard), drawing
dimension-coverage check, requirements join checker (req statuses vs verifier
evidence). Tolerance stack stays T-BL08. Note: run_cfd_accurate is Linux/Mac only —
the battery must tag per-check platform support, never assume it in rewards.

## Files / areas touched

- `services/` verify battery module + MCP tool `verify_battery`
- groundedness checker under `tools/translator/` (shared with T-F10 harness — one
  implementation, two callers)
- manifold/round-trip + unit-sanity + dim-coverage + req-join checkers near their
  domains (core/doc, drawing, requirements)
- per-check metadata: cost class (subsecond/seconds/minutes), platform, reward shape

## Definition of done

- [ ] verify_battery runs any subset, returns a stable scores vector + findings list;
      deterministic across repeated runs
- [ ] All five gap verifiers implemented with tests (groundedness validated against
      T-F10's harness fixtures)
- [ ] Battery callable from Python in <50 ms overhead over the underlying checks
      (reward-loop viable)
- [ ] `graphify update .`; gate green
