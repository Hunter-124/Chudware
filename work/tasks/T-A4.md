---
id: T-A4
title: Verify nodes and DAG MCP tools
workstream: A
owner: sonnet-hollis
status: done
branch: task/T-A4-verify-nodes-mcp
day: 4
priority: 3
depends_on: [T-A3]
lane: ai-copilot
kind: new
---

## Context

Verify nodes run deterministic checks as end conditions: `check_interference` (mechanical), `ecad_run_erc/drc` (ECAD), firmware test pass (Renode assert), D/C ratio < 1.0 (structural), budget ≤ target (cost). Each verify node calls an existing MCP tool and checks its output against the node's `end_condition` predicate. Day 4 also wires the five DAG MCP tools (`dag_plan`, `dag_run`, `dag_status`, `dag_approve`, `dag_abort`) into `services/mcp/server.py`.

## Files / areas touched

- `services/dag/verify.py` — new: verify node implementation; dispatches to MCP tools by check type
- `services/mcp/server.py` — register dag_plan, dag_run, dag_status, dag_approve, dag_abort
- `services/dag/mcp_tools.py` — new: MCP tool handlers for the five DAG tools
- `engine_oracle/ecad/` — ecad_run_erc/drc tools (existing, verify node calls these)
- `engine_oracle/sim/` — check_interference (existing)

## Steps

1. Run `graphify query "check_interference ecad_run_erc MCP tools"` to find existing tool names and signatures.
2. Implement `verify.py`: a node runner variant that, instead of calling the LLM, calls a named MCP tool and evaluates the boolean end_condition predicate against the result. Map verify types to tool names: `interference → check_interference`, `erc → ecad_run_erc`, `drc → ecad_run_drc`, `firmware → fw_sim_run`, `dc_ratio → struct_check`, `budget → bom_price`.
3. Implement `mcp_tools.py` with handlers for: `dag_plan` (run dag_planner, return graph JSON), `dag_run` (start/resume a run), `dag_status` (return run state + current node), `dag_approve` (resume a gate with approve), `dag_abort` (cancel a run).
4. Register the five tools in `services/mcp/server.py` following the existing registration pattern.
5. Test: create a graph with a verify node (interference check); mock the tool result; assert node passes when no interference, fails → retry when interference found.

## Definition of done

- [ ] `verify.py` dispatches all five check types to existing MCP tools without calling the LLM
- [ ] Five DAG MCP tools registered in `services/mcp/server.py` and reachable via the MCP server
- [ ] `dag_status` returns current node ID, run state, and gate-pending flag
- [ ] `dag_approve` resumes a paused gate from outside (e.g., from CLI or UI panel)
- [ ] `python -m pytest services/dag/test_verify.py services/dag/test_mcp_tools.py -q` passes

## AI hints

- `graphify query "MCP server tool registration pattern"` — find how tools are registered (function name, docstring, schema pattern)
- `graphify query "check_interference ecad DRC ERC"` — locate existing tool implementations
- Relevant files: `services/mcp/server.py`, `engine_oracle/ecad/`, `engine_oracle/sim/`
- End-condition predicate is stored as a string in the graph JSON; evaluate with `eval(predicate, {"result": tool_result})` inside a try/except — or use a safe predicate DSL

## Driver notes (Fable, 2026-07-02 — WS-A opened to agents; chain off A3)

- Branch FROM T-A3's branch (merge order A1 → A2 → A3 → A4).
- Keep verify.py's check-type→tool dispatch a REUSABLE function/table — T-F12's
  design_review() battery reuses it verbatim (recorded in T-F12 driver notes).
- Tool availability caveat: budget→bom_price lives on the WS-B chain, firmware→fw_sim_run
  on WS-C — by dispatch time these should be merged; if one is missing on your base,
  mock it in tests and note [~], don't block.
- The five dag_* MCP tools: services/mcp/server.py registrations APPEND-ONLY; follow
  the existing registration pattern (hydraulic_tools.py / cost_tools.py are recent
  exemplars). dag_approve resumes T-A3 gates — it is the MCP twin of resume(run_id,
  decision); keep one implementation.

## Progress log
- 2026-07-02 23:51 UTC [sonnet-hollis] REVIEW: branch task/T-A4-verify-nodes-mcp pushed.

What landed:
- services/dag/verify.py: deterministic verify node runner dispatching 6 check types (interference/erc/drc/firmware/dc_ratio/budget) to real MCP tools via battery._get_fn singleton. DISPATCH_TABLE is public API for T-F12 reuse. Fail-closed: backend_unavailable/tool_error/predicate_error → ESCALATE; check_failed → RETRY/ESCALATE by budget. Never auto-pass.
- services/dag/mcp_tools.py: dag_plan/dag_run/dag_status/dag_approve/dag_abort handlers. dag_approve = executor.resume(decision="approve") — ONE implementation. dag_status is restart-safe from .graph.json sidecar.
- services/dag/graph_loader.py: verify nodes WITH check_type routed to vr.run(); WITHOUT check_type fall through to node_runner (agent path, backward compat).
- services/mcp/server.py: append-only registration of register_dag_tools(mcp).
- Tests: 125 passed, 1 skipped (real engine, AI_CAD_LIBAICAD gate). Gate: PASS.

[~] bom_price/fw_sim_run/struct_check degrade to ESCALATE if WS-B/C/D not merged (mocked in tests per driver rules).
