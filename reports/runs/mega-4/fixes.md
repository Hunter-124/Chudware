# mega-4 harness fixes (Fixer4 = OperationalVulture)

Live mission: `mega-4-fc-rocket-20260714` (MegaRun4 = ComprehensivePigeon).
Runtime ledger: `~/.local/share/AI-CAD/cache/runs/mega-4-fc-rocket-20260714/ledger.json`.
Scope is harness enablement only; no grading changes.

## Fix 1 — BUILD part bootstrap is available

**Fingerprint / ledger evidence:** mega-2 TOP-5 `new_part_bootstrap_missing`; mega-2 BUILD/VERIFY/DELIVER tool results repeatedly returned `No current part. Call new_part(...) first`. Mega-3 reproduced the same result at ledger event lines 4880–4910. The `feature_modeler` role used for part BUILD nodes did not expose `new_part`, so the model could not obey the engine's recovery instruction.

**Fix:** `services/agent/roles/feature_modeler.yaml` now exposes `new_part` and instructs a BUILD worker with no part `doc_id` to create the part before describe/preflight/add-features, then retain the returned `doc_id`. To keep the role within the 14-tool contract, redundant `render_part` (covered by describe/measure) and BUILD-time `fit_calc` (fits must already be ratified in the ledger/advisory) were removed. This enables the engine contract; it does not create geometry or evidence automatically.

**Regression:** `test_feature_modeler_can_bootstrap_new_part` also asserts the role remains within `MAX_TOOLS_PER_ROLE`.

## Fix 2 — direct buildgraph planner payload stays non-empty

**Fingerprint / ledger evidence:** mega-3 ledger seq 162 called `buildgraph_plan` with `format=aicad.buildgraph/1`, 21 `nodes`, explicit `deps`, and `edges`; the tool returned `ok=true` but `graph.nodes=[]` plus blocker `unknown_plan_shape`. This is the known MegaRun4 watch-list contract mismatch.

**Fix:** `services/agent/buildgraph/planner.py` recognizes direct `schema` or `format` `aicad.buildgraph/1` payloads and normalizes compact planner fields (`title`, `deps`, `role`) into the frozen node shape. Explicit edges are merged and deduplicated with per-node dependencies; validation and cycle detection remain unchanged.

**Regression:** `test_buildgraph_plan_accepts_live_compact_buildgraph_shape` reproduces the live compact payload and asserts three retained nodes, correct joins/assignment, and no `unknown_plan_shape`/`empty_graph` advisory.

## Fix 3 — xAI textual tool-call rendering is dispatched

**Fingerprint / ledger evidence:** MegaRun4 ledger event around lines 3471–3474 (`n_spec_done`) recorded the model reply as plain text containing four calls in xAI's observed form: `ethan tool call: ledger_get with section is null budget_tokens is 2000 ...`. `_parse_directive` accepted JSON only, so zero tools dispatched and the join failed `escalated: no_code` after about two seconds.

**Fix:** `services/agent/agent_turn.py` keeps fenced/bare JSON canonical, then narrowly parses the observed `ethan tool call: NAME with ARG is VALUE` fallback. It returns only the first call so the existing bounded dispatch → result → next-model-turn loop remains in control; allowed-tool enforcement is unchanged.

**Regression:** `test_parse_xai_textual_tool_call_rendering` uses the exact live two-call prefix, verifies typed `null`/integer kwargs and first-call selection, then drives `run_turn` through dispatch and a completed handoff.

## Fix 4 — residual ledger vocabulary reaches canonical state

**Fingerprint / ledger evidence:** MegaRun4 live ledger recorded `unknown kind 'assumption'` (SPECIFY DL1, lines 2523–2535), `unknown kind 'verification'` (DELIVER DL5, lines 3711–3724), `unknown section 'criterion_result'` immediately after a successful AC9 write (lines 3730–3780), `unknown section 'mission_brief'` (LAYOUT, lines 4355–4367), and later `unknown section 'verification'` during VERIFY DL2.

**Fix:** mission-scoped and MCP-global ledger tools now map `assumption→decision`, `verification|verification_result→artifact`, and `mission_brief→brief`. The already-present `criterion_results` ledger state is now readable directly, with singular/plural/`ac_results` and `verification(s)` section aliases. Artifact aliases preserve their source kind and synthesize a stable id from the deliverable/name rather than creating an id-less row. No verdict or evidence is invented.

**Regression:** `test_live_ledger_aliases_and_criterion_result_section` writes the exact alias classes, checks canonical decision/artifact state, reads AC9 through both singular and verification aliases, and resolves mission brief.

## Fix 5 — DAG progress no longer fabricates passed phase gates

**Fingerprint / ledger evidence:** MegaRun4 finished `needs_correction/correct` with `n_plan`, `n_build_DL1`, and `n_build_DL2` failed and no `build_nodes` or `features`, yet end-of-run reconciliation wrote successful PLAN/BUILD gates (`graphs_valid_ratified`, `nodes_done_or_carded`) and listed both phases in `phases_completed`. The cursor correctly advanced past soft fan-in, but `_drive_phases_from_dag` treated downstream progress as proof that the earlier phase itself completed.

**Fix:** phase reconciliation now distinguishes real completion (join done or every worker done) from cursor-only downstream progress. Reconciled failed phases are entered so status reflects where the DAG ran, but are not exited, assigned a success gate, or appended to `phases_completed`. Real soft-join completion remains supported.

**Regression:** `test_failed_plan_and_build_are_reconciled_but_not_completed` reproduces MegaRun4's failed PLAN/BUILD with later VERIFY/DELIVER progress and verifies correction routing without false completion. The mega-3 downstream regression now explicitly checks that cursor reconciliation does not complete failed SPECIFY.

## Fix 6 — delegation guidance matches the live dispatcher contract

**Fingerprint / ledger evidence:** MegaRun4 exposed `delegate` in SPECIFY/PLAN/BUILD tool slots but recorded zero delegate calls, zero child spawns, and zero assignment events despite parallel-work guidance. The `orchestrator` and `spec_writer` prompts instructed `delegate(role, brief, budget)`, while the rendered `MissionDispatch.delegate` schema requires `budget_tokens`; the dispatch layer ignores the unknown `budget` kwarg and then rejects the resulting zero-token grant.

**Fix:** both delegating role prompts now name the dispatch-faithful `delegate(role, brief, budget_tokens)` signature. Delegation policy, grants, depth, and grading are unchanged.

**Regression:** `test_delegating_roles_use_dispatch_budget_tokens_contract` checks both roles expose `delegate`, use `budget_tokens`, and contain no stale three-argument `budget` signature.

## Verification

```text
python3 -m pytest services/agent/test_mega4_fixes.py services/agent/test_mega3_fixes.py -q
26 passed in 0.51s
```

## Live-run notes

- MegaRun4 exited normally after 1187.45s: mission `needs_correction/correct`, 19/19 nodes started, 9 done, 10 failed.
- Fixes 1–2 landed while MegaRun4 was still in INTAKE. The live BUILD DL1 tool slot confirmed `new_part` rendered and the 14-tool cap held, but both BUILD nodes timed out in the provider before a mutating call; direct buildgraph normalization was not re-exercised because PLAN also timed out.
- Fixes 3–6 require a fresh mission process because their Python modules/role prompts were already bound or consumed. The completed run remains the evidence source and was not interrupted.
- Delegation child-result joining and auto model assignment were not exercised: the run emitted zero delegate calls/spawns/assignments and intentionally bound its explicit xAI frontier provider to all nodes. Concurrent xAI fan-out still produced 180-second read timeouts; no delegate-fan-out starvation conclusion can be drawn from this run.
