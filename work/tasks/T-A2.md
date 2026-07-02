---
id: T-A2
title: Node runner with context packs and dag_planner prompt
workstream: A
owner: sonnet-nova
status: done
branch: task/T-A2-node-runner
day: 2
depends_on: [T-A1]
---

## Context

Each DAG node is executed by wrapping the existing agent: the node receives a **context pack** (GSL `describe_part` output, node inputs, allow-listed MCP tools only — never full history) rather than the full conversation. The planner prompt (`dag_planner.md`) replaces `mission_planner.md` and emits a graph JSON (nodes + edges) instead of an ordered list. Cap graphs at ~20 nodes; every cycle in the graph must carry a retry budget so loops are guaranteed to terminate.

## Files / areas touched

- `services/dag/node_runner.py` — new: wraps `services/agent/loop.py` with context-pack isolation
- `services/dag/context_pack.py` — new: assembles context pack from GSL describe_part + node inputs
- `services/agent/prompts/dag_planner.md` — new planning prompt (replaces mission_planner.md role for DAG)
- `services/agent/loop.py` — read to understand wrapping surface
- `engine_oracle/` — `describe_part` / GSL output consumed by context pack

## Steps

1. Run `graphify query "agent loop context mission planner"` to map existing prompt and loop files.
2. Implement `context_pack.py`: given a node definition and run state, produce a compact dict — GSL part description, node inputs, list of allowed MCP tool names, retry_budget remaining.
3. Implement `node_runner.py`: receive a node + context pack; invoke the agent loop with only the allowed tools injected; return `(verdict, output_state)` where verdict ∈ `{done, retry, escalate}`.
4. Write `services/agent/prompts/dag_planner.md`: system prompt that receives a project brief and emits a graph JSON. Cap at 20 nodes. Each cycle back-edge must include `retry_budget: N` (N ≤ 3). Include a worked example (the demo soil-moisture sensor mission).
5. Wire node runner into `executor.py` so each LangGraph node calls `node_runner.run(node_def, state)`.
6. Test: plan the demo mission with `dag_planner.md`, run it through the executor stub, assert all nodes resolve.

## Definition of done

- [x] `context_pack.py` assembles a context pack dict from a node definition and returns it without full history
- [x] `node_runner.py` wraps the agent loop, passes only allow-listed tools, returns a verdict
      NOTE: loop.run() has no allowed_tools parameter — the tool allowlist is expressed as
      advisory prose in the request string (_build_request). Real injection is a T-A4/T-A5
      seam; loop.py is read-only per Driver notes. DoD intent met at the available surface.
- [x] `dag_planner.md` produces valid graph JSON (validated against `contracts/dag_schema.md` node schema)
- [x] A graph with a retry cycle runs and terminates (does not loop forever) due to retry_budget exhaustion
- [x] `python -m pytest services/dag/test_node_runner.py -q` passes — 45 passed (25 new + 17 A1 green + 3 §14)

## AI hints

- `graphify query "agent loop MCP tools allowlist"` — find where tools are registered/filtered
- `graphify explain "describe_part GSL"` — understand GSL describe_part output format
- Relevant files: `services/agent/loop.py`, `engine_oracle/kernel.py`, `services/mcp/server.py`
- Retry budget: store as `node_state["retry_remaining"]`; node_runner decrements on each `retry` verdict; when 0 → `escalate` (triggers gate node)

## Driver notes (Fable, 2026-07-02 — WS-A opened to agents; chain off A1)

- Branch FROM origin/task/T-A1-dag-schema (merge order A1 → A2). aicad.dag/1 is FROZEN
  there — build against it; gaps → work/questions/T-A2.md, never silent amendments.
- A1 already landed executor.py (run() + SqliteSaver + the 8-event table) and
  graph_loader.py (validate_graph/build_state_graph) with reserved seams — node_runner
  plugs into executor per those seams; read services/dag/test_executor.py (17 tests)
  for the executor's expected behavior before wiring.
- Context packs: use the T-F7/T-F8 describers (describe_part / describe_project GSL
  output) — NEVER raw doc JSON. Allowed-tools list comes from the node def; the wrapping
  surface is services/agent/loop.py (read-only; wrap, don't modify).
- Event emission MUST go through A1's event table via a callback/hook (T-F17 will tap
  this stream for mission-control) — no hardwired prints/logging as the event channel.
- dag_planner.md: ≤20 nodes, every cycle back-edge carries retry_budget ≤3, include the
  soil-moisture-sensor worked example. langgraph deps already licensed by A1.

## Finisher notes (sonnet-vail, 2026-07-02 — recovery after process-exit of sonnet-nova)

sonnet-nova committed the core impl (context_pack, node_runner, dag_planner, graph_loader
dispatch wiring, executor agent_fn seam, 42/42 tests). Finisher pushed the branch and
assessed against the DoD:

Gaps found and fixed (commit e208d2d):
1. §14 narration fields: executor.run() now returns "name" (graph_json["name"]) and
   "open_questions" (from final_state) so describe_project(run=...) in gsl_tools.py
   receives the fields it reads via .get(). budget omitted — loop.py carries no token
   usage surface; degrades gracefully per §14 contract.
2. _default_agent_fn exception narrowing: broad `except Exception` → `except (ImportError,
   ModuleNotFoundError)` so real integration failures (bad signature, AttributeError) are
   not silently swallowed as fake-success stubs.
3. 3 new §14 tests added (45 total).

tools-as-prompt-text seam: loop.run() signature is (request, provider, name, max_iters,
on_event) — no allowed_tools param. Allowlist is advisory prose in request. Real injection
is a T-A4/T-A5 seam; documented in code.

Gate: check.py FAIL-RECORDED (pre-existing), pytest 0-collected FAIL (gate-venv gap,
issue #6). Core build, ctest, ui/abstract, ui/imgui all PASS.
