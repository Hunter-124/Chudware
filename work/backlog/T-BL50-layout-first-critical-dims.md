---
id: T-BL50
title: Layout-first planning helper — critical-dimension & interface capture up front, with a coverage gate so the AI can't miss critical dims
workstream: backlog
owner: sonnet-rush
status: doing
priority: 2
lane: ai-copilot
kind: new
depends_on: []
branch: task/T-BL50-layout-first-critical-dims
---

## Context

Cut 2026-07-04 by fable-helm from a live Hunter directive: the AI should "ensure proper
layout plans the first time" and "not miss critical dimensions." Today the pieces exist
separately — requirements CRUD (aicad.req/1, T-H3), a dim-coverage verifier in the T-H4
battery, confirm_dimensions/IntakeLedger for user-confirmed values, dag_plan — but no
workflow makes an agent CAPTURE the layout-critical dimensions and interfaces BEFORE
modeling, and nothing GATES a design review on that capture being covered.

## Definition of done

- [ ] **layout_plan capture:** an agent-facing tool/flow (`layout_plan` MCP tool or an
      extension of existing req tooling — prefer extending `aicad.req/1` usage over a new
      parallel store) that records, per design: overall envelope, each component
      interface (what mates to what, via which features), and the CRITICAL DIMENSIONS
      (with value + tolerance + why-critical) — created at intake/planning time, before
      detailed geometry.
- [ ] **Coverage gate:** a check (new verifier in the T-H4 battery or extension of the
      existing dim-coverage verifier) that fails/warns when a captured critical dimension
      or interface has no verification evidence (measure_between/interface_check/FEA
      result etc.). Wired so `design_review` surfaces it — an agent finishing a part with
      an unverified critical dim gets a WARN/FAIL, not silence.
- [ ] **Planning-loop integration (the "first time right" part):** the modeling guidance
      agents actually read — cad_guide main loop + dag_planner prompt — instructs:
      capture layout_plan first, model to it, run the coverage gate before declaring
      done. MCP freshness (WORKFLOW.md §6) is the core of this task, not an afterthought.
- [ ] **Ties into existing stores:** user-confirmed values via confirm_dimensions/
      IntakeLedger count as evidence; requirements created here show up in req_report
      with their coverage state. Do NOT invent a second requirements schema — reuse
      aicad.req/1 (a `category: critical_dim` / `interface` tag or similar lightweight
      convention; if a schema field addition is unavoidable, question file first).
- [ ] End-to-end demo asserted in a test: plan a 2-part assembly (envelope + 1 interface
      + 2 critical dims) → model it → coverage gate reports the unverified dim → verify
      it (measure_between / interface_check) → gate goes green.
- [ ] Honest scope: what counts as "evidence" for each dim type is documented; no
      auto-passing anything the battery didn't actually check.

## Driver notes

- Tier: Sonnet (workflow/wiring over existing seams; no kernel work).
- graphify-first: `graphify explain "requirements layer"`,
  `graphify query "verify battery dim coverage"`, `graphify query "dag planner prompt"`
  before raw reads.
- Key files (verify with graphify first): contracts/requirements.md (frozen aicad.req/1 —
  do not change without a question file), services/verify/battery.py,
  services/mcp/req_tools.py, services/mcp/intake_tools.py (confirm_dimensions ledger),
  services/dag/ planner prompt.
- COORDINATION: T-BL49 (interface_check, in flight same wave) produces the interface
  evidence this consumes — consume its documented JSON output; if it isn't landed yet,
  code against the contract in its task file and note the join point. sonnet-vetch is
  wiring battery→req evidence on T-BL09 CONCURRENTLY — you two share the
  battery/req seam: whoever lands first, the other rebases; post FOREMAN.md notes and
  keep battery.py edits small and additive. If overlap gets tangled, write the question
  file rather than fighting over the seam.
- Related: T-BL25 (doc-scoped IntakeLedger, landed), T-BL09 (req↔battery evidence
  wiring, in flight).

## Progress log
