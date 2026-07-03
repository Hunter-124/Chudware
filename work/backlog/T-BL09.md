---
id: T-BL09
title: Wire the requirements layer to the verify battery (req evidence auto-population)
workstream: backlog
owner: unassigned
status: todo
branch: task/T-BL09-req-verify-wiring
depends_on: [T-H3, T-H4, T-A4]
lane: ai-copilot
kind: new
priority: 3
---

## Context

**Re-scoped 2026-07-03 — NOT fully superseded by T-H3.** T-H3 froze `aicad.req/1`,
shipped CRUD tools (`req_create/req_update/req_status/req_report`), and proved ONE
manual example (a human calls `measure_between`, then hand-flips status via
`req_update`). That's only half of this task's original point. Verified 2026-07-03:
`services/verify/battery.py` (T-H4's verifier battery) and the DAG verify nodes /
`design_review` battery (T-A4/T-F12) have **zero** reference to `aicad.req` anywhere
in the codebase — there is no automatic path from "a verify battery ran against this
doc" to "requirement N is marked verified with an evidence ref." Closing that loop
(idea → requirement → automatic proof) was the actual ask; it's still open.

## Definition of done

- [x] Requirements schema in contracts/ — done via T-H3 (`contracts/requirements.md`)
- [ ] DAG verify nodes / `design_review` battery can take a requirement ID as input,
      run the named `verify_by` verifier, and auto-write the result back into the
      requirements doc (status flip + evidence ref) instead of requiring a manual
      `req_update` call
- [ ] `req_report` output shows battery-populated evidence, not just hand-entered
- [ ] End-to-end demo: a requirement with `verify_by: run_fea_static` gets verified
      automatically when the DAG orchestrator runs that verify node for the doc

## AI hints

- `contracts/requirements.md` — the frozen `aicad.req/1` schema and CRUD tools
- `services/verify/battery.py` — T-H4's verifier battery, the thing that needs to
  learn about requirement IDs
- `services/dag/` — T-A4's verify nodes / T-F12's `design_review` battery
  (`DISPATCH_TABLE`) — the other side of the wiring
- `services/mcp/req_tools.py` — existing CRUD tools to extend, not replace
