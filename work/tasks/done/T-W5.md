---
id: T-W5
title: Advisory engine (foreman) - stage tracking, check batteries, advisory injection
workstream: W
owner: opus-hale
status: done
branch: task/T-W5-advisory-engine
depends_on: [T-W1]
lane: ai-copilot
kind: new
priority: 1
---

## Context

The heart of Pillar 20 (docs/ADVISOR_ARCHITECTURE.md §2.2, contracts/advisor.md §1 §3
§9 — frozen). A deterministic engine that tracks the process stage, runs the stage's
read-only check battery after tool calls, converts findings into aicad.advisory/1
blocks with kernel/table-computed candidate fixes, and injects them into the next
model turn in both hosts (services/agent/loop.py and the DAG node runner). No LLM in
the hot path.

## Files / areas touched

- `services/agent/advisor/` — new package: `stages.py` (vocabulary, inference map,
  check batteries, exit gates), `engine.py` (run checks → advisory items → render),
  `render.py` (budgeted block rendering, drop-count line, §9 precedence rules)
- `services/agent/loop.py` — injection seam in message assembly + post-build hook
- `services/dag/` node runner — same seam per node step
- `services/mcp/advisor_tools.py` — `advise(doc_id)` (on-demand battery for external
  MCP clients like Claude Code), `stage_set`, `advisory_dispute`
- tests: `services/agent/advisor/test_engine.py`

## Steps

1. Read the architecture doc + contract fully. Schemas are frozen; implement exactly.
2. `stages.py`: stage enum, tool-name → stage inference table, per-stage
   `check_battery` mapping to EXISTING read tools (sketch: sketch_health [T-W2, may
   land in parallel — code against its contract shape, stub if unmerged]; feature:
   check_feature_overlap + doc_eval_check; assembly: check_interference with
   fit-aware min_clearance; part_verify: verify_battery subset; sim: describe_results
   presence; cam: cam_simulate summary), exit gates per contract §1.
3. `engine.py`: `on_tool_result(ctx, tool, result)` — infer/update stage (ledger
   stage_log via T-W1), run the battery (cheap checks only; budget ~1s, skip
   heavy checks unless stage-exit), map findings → advisory items. Candidate-fix
   computation v1: interference → fit_calc-based bore/shaft suggestion when two
   cylindrical faces are involved (T-W3), overlap → feature reorder/param hints from
   doc_eval_feature_overlap classification, open profile → auto_constrain proposal
   (T-W2), rebuild errors → wrap regen_diagnose. Where no computed fix exists, emit
   finding + helper_tools only (severity rules per contract §3).
4. `render.py`: budgeted rendering per tier profile stub (hardcode budgets until
   T-W10; read the layout from docs §3), lowest-severity-first drops, always append
   drop-count, [ADVISORY]…[/ADVISORY] delimiters, §9 precedence (user-override
   downgrade).
5. Wire both hosts behind `CHUDWARE_ADVISOR=1` (default on): loop.py — after each
   build/tool result, engine runs and the block is prepended to the next user-role
   message; DAG runner — per node step. Advisories also logged to the ledger.
6. `advise`/`stage_set`/`advisory_dispute` MCP tools (dispute logs to ledger; kernel-
   sourced disputes require ask_user waiver per §9.3).
7. Tests: stage inference sequences; interference→fit fix on a two-body fixture;
   budget rendering drops correctly; blocker gates stage exit but not next action;
   dispute flow; loop.py injection golden test (message assembly contains the block).
8. MCP freshness: server instructions describe the advisory loop; `graphify update .`.

## Definition of done

- [x] Stage inference + per-stage batteries + exit gates per contract §1
      (services/agent/advisor/stages.py)
- [x] aicad.advisory/1 items with computed candidate_fixes for interference/overlap/
      open-profile/rebuild classes (engine.py; interference→fit_calc on the doc path,
      heuristic on the loop path where no diameters exist — §3-compliant)
- [x] Budgeted rendering with §9 precedence + no silent truncation (render.py)
- [x] Injection live in loop.py AND dag node runner behind default-on CHUDWARE_ADVISOR
- [x] advise/stage_set/advisory_dispute registered; tests pass (19 new, 223 agent
      suite pass; 2 pre-existing unrelated failures documented below)

## Notes / deferrals (opus-hale, 2026-07-04)

- Loop.py has no doc_id (build123d-code path): only res.fit (interference) +
  res.error (rebuild) fire there. Interference on the loop path carries NO
  cylindrical-face diameters (kernel _fit_report gives {a,b,volume}), so the
  flagship interference→fit_calc computed fix runs on the DOC path (advise /
  DAG nodes, where doc_eval_interference can surface bore/shaft Ø + feature id).
  On the loop path the interference item stays §3-compliant via a heuristic
  move/resize fix + fit_calc advertised as a helper. Full stage vocabulary +
  batteries are defined in stages.py regardless (T-W7 / pipeline cite them).
- DAG-path advisories are injected (output_state["advisory"]) but NOT yet logged
  to the ledger — node_runner has no run_id/ledger handle. Offline replay of
  DAG-injected blocks (§3/§9.4) is deferred to the T-W19 pipeline glue that
  threads a ledger handle through the context pack. Loop.py advisories ARE logged.
- Pre-existing agent-suite failures (confirmed on clean master via stash+compare,
  204/2 clean → 223/2 with T-W5): test_providers_conformance::
  test_neutral_part_and_message_helpers and test_h7_shadow offline-mode
  (order-dependent pollution; passes in isolation on clean master). Not T-W5.
  test_loop_mock.py has a pre-existing collection error (missing sample path in
  the worktree checkout) — also not T-W5.

## AI hints

- `graphify query "dag node runner context packs"` and `graphify query "run_turn
  message assembly"` before wiring seams.
- Keep engine synchronous + deterministic; the optional LLM "senior advisor" pass is
  OUT of scope (future task).
- T-W2/T-W3 may merge mid-task: integrate via their contract shapes, feature-flag the
  candidate-fix providers individually.

## Scope update 2026-07-04 (in-app pivot — Hunter directive)

Priority raised to 1: the in-app copilot harness (services/agent + GUI) is the sole
integration target for this task. Changes vs the original spec:
- SKIP the MCP freshness rule steps (server instructions, cad_guide topics, manual
  regen) — external MCP surface polish is parked in T-W20 (deferred batch task).
- Registering tools with the MCP server is OPTIONAL — do it only when it is one
  decorator on the same function and costs nothing; never let it drive design.
- Acceptance = the capability working through the IN-APP path (services/agent hosts,
  loop.py / dag runner / pipeline), proven by python tests. graphify update . still
  required (that serves our own agents).

## Progress log
- 2026-07-05 02:43 UTC [opus-hale] Advisory engine (foreman) landed on task/T-W5-advisory-engine. New pkg services/agent/advisor/{stages,engine,render,adapters}.py: deterministic stage vocab + tool-stage inference, pure engine over normalized observations -> aicad.advisory/1 with kernel/table candidate fixes, budgeted §9 render w/ never-silent drops. Seams live behind CHUDWARE_ADVISOR (default on) in loop.py (res.fit/res.error, ledger-logged) AND dag node_runner (doc battery -> output_state.advisory). MCP: advise/stage_set/advisory_dispute. Tests: 19 pass (stage inference, two-body interference->fit_calc fix, budget drops, blocker gates exit not action, §3 non-info-requires-fix invariant, loop+dag injection goldens, dispute waiver). Full suite: 223 passed, 1 skipped, 2 FAILED — both confirmed pre-existing on clean master via stash+compare (test_providers_conformance neutral helpers; test_h7_shadow offline order-pollution). Deferred: DAG-path advisories injected but not ledger-logged (no run_id handle; T-W19 glue). graphify updated.
