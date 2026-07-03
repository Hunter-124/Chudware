---
id: T-BL26
title: ai-cad MCP freshness audit - server instructions, cad_guide topics, context injection vs shipped capabilities
workstream: backlog
owner: sonnet-loch
status: review
branch: task/T-BL26-mcp-freshness-audit
depends_on: []
lane: ai-copilot
kind: fix
priority: 3
---

## Context

HUNTER (2026-07-03): "make sure the MCP is getting updated with all of the advanced
workflows and smarter design context injection and model suggestions/prompting that
we are adding." The ai-cad MCP server's instructions + `cad_guide` topics are the
agent's map of the product; every capability wave since they were written (unified
parts library part_search/part_place, CAM overhaul M1-M6, narration describe_*,
requirements req_*, interaction/elicitation, doc_diff, DAG orchestrator dag_*,
autosave/recovery) risks being invisible or under-prompted. The standing rule that
prevents future drift is work/WORKFLOW.md §6; this task pays down the existing debt
and adds the mechanical check.

## Files / areas touched

- services/mcp/server.py + instructions string(s): audit every registered tool
  family vs what the instructions/cad_guide actually teach; fix drift, add missing
  topic entries (model-suggestion/prompting guidance: when to prefer part_search
  before custom geometry, describe_* for no-vision clients, verify battery before
  "done", elicitation via ask_user)
- cad_guide topic table (wherever topics live — locate via graphify): one topic per
  shipped family; PLM plm_* topic stub reserved (T-R2 fills it)
- NEW consistency test (services/mcp/ tests): every registered tool name appears in
  either the server instructions or a cad_guide topic; fails on future drift
- work/context/ section packs: refresh services-mcp pack if stale

## Definition of done

- [ ] Audit table committed (tool family -> where taught -> verdict fixed/ok)
- [ ] Server instructions + cad_guide cover all shipped tool families incl. parts
      library, CAM overhaul, narration, requirements, doc_diff, DAG tools
- [ ] Consistency test green and wired into the pytest gate stage
- [ ] work/context/services-mcp pack refreshed
- [ ] gate + graphify update .
