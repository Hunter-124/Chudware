# MegaRun4 Findings

## Run summary

- **Run ID:** `mega-4-fc-rocket-20260714`
- **Provider:** xAI `grok-4.5`, tier `frontier`, effort `maximum`
- **Wall time:** 1,187.45s (19m47s)
- **Mission result:** `needs_correction`, phase `correct`
- **DAG result:** 19/19 nodes started; 9 done, 10 failed; run status `paused`
- **Acceptance:** AC1–AC9 all failed or had no execution evidence
- **Primary evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `spawn_probe.json`

## Pre-flight spawn verdict: PASS exposure, FAIL behavior

`spawn_probe.json` was generated against the current role cards before launch.
`spec_writer`, `layout_engineer`, and `feature_modeler` each contained `delegate`
in both the raw allowlist and the resolved concrete tool list. Live tools-slot ledger
events also showed `delegate` on SPECIFY, PLAN, and BUILD roles; BUILD DL1's
`feature_modeler` slot additionally contained `new_part`.

**Actual Grok delegation verdict: FAIL.** Across 142 tool calls there were **0
`delegate` calls**, **0 child spawns**, and **maximum depth 0**. The only
`aicad.delegation/1` record is the auto-approved root orchestrator at depth 0.
Its budget was 50,000 tokens granted, 0 spent, 0 attempts. Exposure is fixed;
model use of delegation is not.

## Phase-gate verdict: FAIL/PARTIAL

The phase gate is no longer permanently stuck at INTAKE, so the reconciliation
code did execute. However it did not track live DAG progress:

1. During the whole 19-node DAG execution, `stage_log` remained INTAKE-only.
2. After `dag.run.done`, the mission emitted SPECIFY → PLAN → BUILD → VERIFY →
   CORRECT within about 50ms and backfilled those ledger stages.
3. It recorded successful exit gates `graphs_valid_ratified` and
   `nodes_done_or_carded`, and `mission.json` lists PLAN and BUILD in
   `phases_completed`, even though `n_plan`, `n_build_DL1`, and `n_build_DL2`
   all failed and final `build_nodes=[]`, `features=[]`.

Thus reconciliation repaired liveness but introduced/retained false-positive
phase completion semantics. A failed or never-materialized PLAN/BUILD must not be
reported as satisfying a success gate.

## Auto model assignment verdict: NOT EXERCISED / COST-TIER FAIL

There were no delegate spawns, so the required per-spawn assignment table is
empty:

| depth | task_class | min_rank | picked model | cost tier |
|---:|---|---:|---|---|
| — | — | — | — | No delegate spawn occurred |

No `role_model`, `task_class`, `min_rank`, `auto_pick`, or cost-assignment event
was emitted. All 19 direct role tools-slot records used `tier=frontier`; the
configured provider was `xai:grok-4.5`. Consequently cheap models received no
leaf work, while frontier handled even VERIFY/DELIVER report leaves. No budget
model was misassigned to a frontier task because no budget model was selected,
but the inverse cost mismatch—frontier on trivial leaf work—was pervasive.

## BUILD depth

BUILD did not create a product:

- `n_build_DL1` and `n_build_DL2` started, exposed the expected tools, then each
  failed after an approximately 180s retryable provider read timeout.
- `new_part` calls: 0; `add_features` calls: 0.
- Schematic/PCB creation calls: 0; board/trace optimization calls: 0.
- `fw_new_project`/`fw_build` calls: 0.
- Final ledger: 0 features, 0 geometry specs, 0 build nodes.
- The 13 `ecad_find_part` calls occurred during SPECIFY only; they did not become
  a board, schematic, or provenance-backed BOM artifact.
- Final artifacts are the intake manifest, an all-FAIL mission report card, and a
  generic battery artifact. The battery artifact says `verify_battery PASS`, but
  its underlying criterion evidence reports no current document and failed
  geometry/ECAD checks; it is not evidence that a part was built.

## Failure fingerprint

- SPECIFY DL2/DL3: `max_iters`; DL1/DL4 auto-completed after 16 calls without a
  formal handoff.
- SPECIFY join: `no_code` after xAI returned plain text containing
  `ethan tool call: ...` rather than canonical JSON.
- PLAN layout: `max_iters`.
- PLAN mission DAG, BUILD DL1, BUILD DL2, and DELIVER DL2: retryable provider read
  timeout, approximately 180s each.
- VERIFY DL1 and DELIVER DL3: `no_code`.
- Downstream VERIFY/DELIVER nodes still ran after empty/failed BUILD and produced
  failure reports rather than build evidence.

Fixer4's textual-call fallback, ledger aliases, compact buildgraph acceptance,
and `feature_modeler.new_part` bootstrap fixes landed during this run. The live
process had already imported affected modules; only the tool-card changes visible
at later node assembly bound reliably. A fresh process is required to evaluate
all landed fixes.

## Token and latency

- End-to-end wall: **1,187.45s**.
- Tool calls: **142**; cumulative tool execution 1,900.07ms; median 0.34ms; p95
  4.56ms; max 1,253.91ms.
- Node latency was dominated by model waits: PLAN 180.19s, BUILD DL1 180.30s,
  BUILD DL2 185.06s, DELIVER DL2 184.28s; all ended in read timeout.
- Tools-slot prompt renderings: 19 records, **20,085 rendered tokens** total,
  median 794.
- Delegation budget: 50,000 granted, 0 spent.
- **Provider input/output token usage and monetary cost were not recorded**, so a
  true model-token/cost rollup is unavailable. This is itself a telemetry gap.
- The xAI semaphore produced no observable occupancy/queue events. No 429/rate
  limit error appeared, but four 180s read timeouts remain; the run cannot prove
  whether semaphore concurrency behaved as intended.

## TOP 5 next

1. **Make phase reconciliation truthful and live.** Emit phase progress as DAG
   milestones settle, and only record a success exit gate when required nodes
   succeeded or were explicitly carded. Preserve failed/paused status instead of
   backfilling PLAN/BUILD success after `dag.run.done`.
2. **Close the delegation behavior gap.** Align every prompt/schema on
   `budget_tokens`, surface rejected/ignored delegate args, and require or strongly
   bias at least one legitimate spawn for broad multi-domain SPECIFY/PLAN/BUILD
   work. Record spawn depth/count in mission telemetry.
3. **Exercise and audit auto model assignment.** Emit `{depth, task_class,
   min_rank, picked_model, cost_tier}` for every spawn; route trivial leaf report
   work to cheap models while retaining frontier for orchestration/high-risk
   design. Alert on either direction of rank mismatch.
4. **Harden xAI timeout/concurrency behavior.** Add observable semaphore queue/
   occupancy timing and bounded retry/backoff for `retryable=true` read timeouts;
   a single 180s timeout should not consume and fail an entire PLAN/BUILD node.
   Capture provider token usage and cost on every response/error.
5. **Prevent phantom downstream success/evidence, then rerun fresh.** Do not run
   geometry verification or emit a PASS battery artifact with no current document;
   gate VERIFY/DELIVER on real BUILD artifacts. Relaunch after the live Fixer4
   parser/ledger/buildgraph fixes bind and confirm actual part, board, firmware,
   delegation, and assignment events.
