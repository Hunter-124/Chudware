---
id: T-BL09
title: Wire the requirements layer to the verify battery (req evidence auto-population)
workstream: backlog
owner: sonnet-vetch
status: review
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
- [x] DAG verify nodes / `design_review` battery can take a requirement ID as input,
      run the named `verify_by` verifier, and auto-write the result back into the
      requirements doc (status flip + evidence ref) instead of requiring a manual
      `req_update` call
- [x] `req_report` output shows battery-populated evidence, not just hand-entered
- [x] End-to-end demo: a requirement with `verify_by: run_fea_static` gets verified
      automatically when the DAG orchestrator runs that verify node for the doc

## AI hints

- `contracts/requirements.md` — the frozen `aicad.req/1` schema and CRUD tools
- `services/verify/battery.py` — T-H4's verifier battery, the thing that needs to
  learn about requirement IDs
- `services/dag/` — T-A4's verify nodes / T-F12's `design_review` battery
  (`DISPATCH_TABLE`) — the other side of the wiring
- `services/mcp/req_tools.py` — existing CRUD tools to extend, not replace

## Implementation notes (sonnet-vetch, 2026-07-04)

**Design: the DAG verify node is the entry point, not battery.py.** Zero edits to
`services/verify/battery.py` — deliberate, because sonnet-rush (T-BL50) is touching
that file concurrently. The requirement path *reads* `battery.CHECKS` +
`battery._extract_score` (import-only) and reuses `verify._get_fn` for the shared
tool registry, so there is still exactly ONE live registry.

What landed:

1. `services/dag/verify.py` — new `check_type: "requirement"` branch in `run()`
   (dispatches to `_run_requirement_verify`, deliberately NOT in DISPATCH_TABLE —
   it has different inputs/writeback semantics from the simple tool checks).
   Node inputs: `req_id` (required), `req_path` | `req_doc_id` (req-doc locator),
   `doc_id` (geometry doc the verifiers run against). Runs each battery-supported
   `verify_by` verifier, scores via `battery._extract_score` (pass = score >= 0.5,
   any one verifier passing suffices), then `_record_req_result` writes the status
   flip + `source: "battery"` evidence refs back via req_tools' `_load_doc` /
   `RequirementsDoc.set_status(who="agent")` / `_save_doc` — the aicad.req/1 ladder
   and audit rules are enforced by the schema layer, not re-implemented.
   FAIL-CLOSED: missing req_id / doc / requirement, zero battery-supported
   verifiers, all-verifiers-errored, and req-doc **writeback failure** all
   ESCALATE (a pass verdict whose status flip didn't land would fabricate
   "verified"). Verifier fail flips status to `failed` (evidence still written)
   and routes RETRY/ESCALATE by budget, same as other verify nodes.

2. `services/mcp/req_tools.py` — new `req_verify_by_id` MCP tool (extends the
   T-H3 CRUD set, nothing replaced): the agent-facing trigger for the same path.
   `req_report` markdown now appends an `## Evidence` section separating
   **Battery-populated evidence** (`source == "battery"`) from **Hand-entered
   evidence** — base `to_markdown()` output unchanged (contract §5 untouched).

3. `services/mcp/cad_tools.py` — cad_guide('requirements') + cad_guide('verify')
   updated with req_verify_by_id + the requirement verify-node recipe
   (MCP freshness rule, WORKFLOW.md §6, same branch).

4. `services/dag/test_req_verify_wiring.py` — 6 e2e tests incl. the DoD demo:
   requirement with `verify_by: run_fea_static` → DAG verify node run →
   status `verified` + battery evidence asserted in the persisted .req.json;
   plus fail→`failed`, report distinction, and 3 fail-closed escalation cases.

v1 limitation (documented in tool docstring): `confirm_dimensions`, `dfm_check`,
`fit_report` are VALID_VERIFIERS without a battery.CHECKS entry — they are
SKIPPED with an explicit `unsupported` list in the response (never silently
passed). Extending battery.CHECKS auto-enables them here. No contract changes;
`aicad.req/1` untouched (evidence `source` key rides inside the free-form
evidence dicts already allowed by §2.4/Trace).

Verification: 116 passed / 1 pre-existing skip across test_req_verify_wiring,
test_verify, test_req_tools, test_verify_battery, test_design_review;
test_mcp_freshness 5/5 (new tool discoverable via cad_guide). `pytest engine
services` collection errors (trimesh/langgraph imports, 8) reproduced IDENTICAL
on clean master — pre-existing, unrelated.

T-BL50 seam note: no battery.py hunks in this branch → no merge conflict with
sonnet-rush expected; if T-BL50 adds req-aware checks to battery.CHECKS they
become auto-supported by this path.
