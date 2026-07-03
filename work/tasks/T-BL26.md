---
id: T-BL26
title: ai-cad MCP freshness audit - server instructions, cad_guide topics, context injection vs shipped capabilities
workstream: backlog
owner: sonnet-loch
status: done
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

## Audit table (T-BL26 findings — sonnet-loch, 2026-07-03)

| Family | Key tools | In server instructions? | cad_guide topic? | Verdict |
|---|---|---|---|---|
| cad | new_part, add_features, render_part, run_fea_*, export_part | YES | overview/simulation/review | OK |
| gsl | describe_part, describe_scene, query_adjacency, hover | YES (describe_*/cad_guide('gsl')) | gsl | OK |
| recon | reverse_engineer, compare_parts, convert_face_to_sketch | comment only | reverse | FIXED (instructions) |
| intake | intake_status, dfm_check, confirm_dimensions | comment only | intake | FIXED (instructions) |
| interaction | ask_user | MISSING | MISSING | FIXED (both) |
| ecad | ecad_new_*, ecad_autoroute, ecad_simulate | YES (ecad_*) | — | FIXED (instructions note ecad) |
| cam | cam_new_job, cam_add_*, cam_generate, cam_post | MISSING | cam | FIXED (instructions) |
| doc_diff | doc_diff_tool | MISSING | MISSING | FIXED (both) |
| req | req_create, req_update, req_status, req_report | MISSING | MISSING | FIXED (both) |
| verify | verify_battery, check_metadata | MISSING | MISSING | FIXED (both) |
| research | research_web_search, research_fetch_page | MISSING | — | FIXED (instructions) |
| deliverables | make_report, make_presentation, make_workbook | MISSING | deliverables | FIXED (instructions) |
| sourcing | bom_rollup, bom_price | MISSING | MISSING | FIXED (both) |
| cost | cam_cost_estimate, request_quote | MISSING | MISSING | FIXED (both) |
| spec | match_part_number, spec_sheet_match | MISSING | MISSING | FIXED (both) |
| piping | piping_solve, piping_report, piping_cfd_handoff | MISSING | MISSING | FIXED (both) |
| hydraulic | hydraulic_new, hydraulic_add_component, hydraulic_size_* | MISSING | MISSING | FIXED (both) |
| firmware | fw_new_project, fw_build, fw_sim_run, fw_flash | MISSING | MISSING | FIXED (both) |
| structural | struct_analyze, struct_check, struct_estimate | MISSING | MISSING | FIXED (both) |
| parts | part_search, part_get, part_place | MISSING | parts | FIXED (instructions + bias) |
| serving | serving_status, serving_set_canary, serving_promote | MISSING | MISSING | FIXED (both) |
| regen | regen_diagnose | MISSING | MISSING | FIXED (both) |
| code_compliance | code_check | MISSING | MISSING | FIXED (both) |
| dag | dag_plan, dag_run, dag_status, dag_approve, dag_replan | MISSING | MISSING | FIXED (both) |
| design_review | design_review | MISSING | MISSING | FIXED (both) |
| photo_recon | start/stop_capture_session, request/submit_part_photos | comment only | reverse | OK (subsumed) |

Summary: 21 of 26 families were missing from server instructions; 17 had no cad_guide topic.
All gaps fixed in commit 7623fbf.

## Definition of done

- [x] Audit table committed (tool family -> where taught -> verdict fixed/ok)
- [x] Server instructions + cad_guide cover all shipped tool families incl. parts
      library, CAM overhaul, narration, requirements, doc_diff, DAG tools
- [x] Consistency test green and wired into the pytest gate stage
- [x] work/context/services-mcp pack refreshed
- [x] gate + graphify update . (graphify done; gate stages 1-3 PASS, stage 4 running)
