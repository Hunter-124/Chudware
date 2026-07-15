# AI Harness Roadmap — copilot, benchmarks, tiering, compliance

Owner directives (Hunter, 2026-07-14) organized into workstreams. Living document:
update status per wave. Driver: session manager agent; execution: task subagents.

## Ground truth so far (evidence)

- Harness: multi-provider (anthropic/openai/gemini/openrouter/llamacpp/vllm/xai(+sub)/claude-cli/codex-cli/subs), vision path, GSL text narration, inspect_part/look_around close-up tools.
- Guardrails: 5 HARD floors all tiers (battery-before-done, checkpoint-before-destructive, interference-before-assembly-done, no-orphan-sketch, ref-integrity). Capability profiles A/B/C wired (scaffolds for A/B, freedom for C).
- Benchmarks: gym C1–C15 + diag axes (tool_validity/vision_fix/feature_id/vc_hygiene) + divisions (local/open) + discipline index + instrumentation; leaderboard contract `aicad.leaderboard/1` + GUI (Settings + AI workspace).
- **Measured**: grok-4.5 diag = 1.0/0.8/1.0/1.0 (best on board); locals = perfect tool syntax, weak geometry. BUT C1 real: grok-4.5 0/3, discipline 1.0, **specify-phase mortality 1.0 across ALL model classes** → the SPECIFY phase machinery is the top harness defect, not the models.

## WS0 — SPECIFY-phase fix (P0, blocks everything)
Every model dies in specify. Diagnose from grok45-1 + local-1 ledgers: what the phase gate
demands vs what models emit; fix gate/prompt/handoff schema; re-run C1 on grok-4.5 + local.
Exit: C1 pass_rate > 0 for grok-4.5 OR a named, justified challenge-fixture defect fixed.

## WS1 — Benchmarks settings tab + full tool-category coverage (P1)
- Dedicated **Benchmarks tab** in Settings window (not inside AI tab): category rows →
  run/queue/status/history + last scores; wired via bench_rpc (exists) extended with categories.
- Categories = Modeling (gym C1–C15), ECAD (flight_computer/battery_charger benches),
  Simulation (benchmarks/simsuite + vv_benchmark), RevEng (E2E scan/datasheet tests),
  Parts Library research (datasheet/picture/online-reference → dims), and the
  **Round-trip pipeline**: Parts Library research → RevEng models it → Parts Library stores
  verified model + footprint + material props + into current assembly. Every MCP tool family
  must appear in ≥1 category test (coverage matrix from bench spec extends to ECAD/sim/parts).
- Live monitor: every real bench run gets a monitor that watches ledgers/events in-flight and
  files tooling-improvement findings (see WS5).

## WS2 — Model management: rankings, per-role assignment, auto mode (P1)
- **Rankings**: `services/agent/model_rankings.yaml` — shipped defaults from public data
  (LMArena/major evals; grok-4.5/claude/gpt-5/gemini top band; qwen3-VL/mistral mid; 4B locals
  utility band) + per-model user override (Settings). Our own leaderboard overrides shipped
  defaults as real cycles land.
- **Per-role assignment mode**: settings map role → provider+model for: advisor, task,
  planning, default, PLM/version-control, redaction (local-only enforced). Extends
  routing.yaml roles; GUI dropdowns per role sourced from configured providers.
- **Auto mode**: task-difficulty classifier (workflow_router TaskClass + node depth/risk) →
  pick model from rankings within user's enabled providers; per-node assignment for subagent
  spawns (node_runner/mission_dispatch).

## WS3 — Redaction / restricted-data compliance (P2, design-first)
ITAR-class users must use frontier models without leaking controlled data. Pipeline:
outbound cloud prompts pass through a LOCAL redaction stage (local llama.cpp model +
deterministic GSL-aware numeric/identifier scrubbing): exact dims/tolerances/materials/part
ids → stable placeholders; local mapping table rehydrates model responses. Modes:
off | redact | local-only. Per-provider data-policy notes surfaced in GUI. Redaction model is
a first-class role (WS2) and MUST resolve to a loopback provider (hard-enforced).

## WS4 — DAG/workflow refinement (P2)
Questioning-to-user batching, subagent spawn-depth policy (infinitely-branching guarded by
budget + depth + auto-model assignment), planner prompt refinement. Informed by WS0 findings
and instrumentation (phase mortality, verify-efficacy, budget curves).

## WS5 — Continuous bench→tooling loop (P1, process)
Every real cycle: monitor agent tails events.jsonl + ledgers, annotates WHERE models struggle
(tool errors, retries, phase stalls), and emits a tooling-findings report per run
(reports/runs/<cycle>/findings.md). Ship rule (frozen in harness spec WI-18): scaffolds
graduate only on ablation evidence; C-freedom regressions are harness bugs.

## Status log
- 2026-07-14: waves 1–4 + harness batches 1–2 landed (see REFACTOR_LOG). xAI OAuth live;
  grok-4.5 smoke + diag + C1 real executed; multi-account subscriptions; claude/codex CLI
  providers; bench-from-GUI RPC. This roadmap committed.
- 2026-07-14 WS0 SPECIFY fix: every model class died at specify_incomplete because
  (1) agent_turn only accepted `{"done":true,"handoff":…}` while role cards teach
  `{"status","summary","outputs"}` — perfect role handoffs became no_code;
  (2) default tool budget 8 + DEFAULT_RETRY_BUDGET=1 escalated productive
  ledger_note turns that never emitted a formal done; (3) n_spec_done overseer
  join matched the n_spec_ prefix and was required status==done, so a failed
  audit froze the phase even when workers finished. Fix: accept role-schema
  handoffs; auto-complete on ledger_note after budget/no_code; soft-filter
  n_spec_done from the required set; default budget 16. Live specify-fix-1
  C1×grok-4-5: specify mortality 1/3=0.33 (was 1.0); s1+s3 reach plan
  (plan_incomplete is next bottleneck). Regression: test_agent_turn
  role/auto-complete/replay + test_specify_advances_when_overseer_join_failed.
- 2026-07-14 mega-1 (WS5): grok-4.5 multi-domain mission (flight computer + model
  rocket plane, 5 deliverables, org layer + max spawn depth 8, maximum effort)
  run under `reports/runs/mega-1/`. Early death at wall 4.25s: config had
  `offline_mode=true` and empty `subscription_auth` so `xai:grok-4-5` fell through
  to API-key `XAIProvider` → 400 Incorrect API key on every node; parallel SPECIFY
  then crashed LangGraph with `INVALID_CONCURRENT_GRAPH_UPDATE` on `__root__`
  (multi-DL fan-in lacks Annotated reducer). Findings:
  `reports/runs/mega-1/findings.md` — top fixes: auth fail-closed for xai tiers,
  concurrent fan-in reducer, on_event dual-signature normalize, non-retryable
  provider taxonomy, ECAD/fw tools on analysis role slots. Infinitely-branching
  DelegationTree root landed; no child spawns (pre-model-success). Re-run after
  xai-sub login or API key restore.

- 2026-07-14 mega-2 (WS5): same 5-DL flight-computer + rocket-plane mission
  under `reports/runs/mega-2/` with SuperGrok OAuth live (`XAISubProvider`
  grok-4.5). Wall **879s**, mission `blocked`/`specify_incomplete` (phases_completed
  only intake) but **DAG progressed past plan** through BUILD/VERIFY/DELIVER and
  `dag.run.done` — no `INVALID_CONCURRENT_GRAPH_UPDATE` (mega-1 fan-in fix held).
  Live fingerprints: fw/em tools unresolved-but-callable, ledger_get section
  all/events/plan/acceptance, ledger_note decision `id` KeyError + kind=verdict,
  provider read-timeout storm under parallel fan-out, BUILD without `new_part`
  (hollow geometry), buildgraph_plan empty nodes. Child spawns still 0. Fixer2
  (FollowingTakin) landed enablement fixes 1–7 mid-run (`fixes.md`, 49 tests) —
  live pid pre-import; relaunch for verification. Findings + TOP5:
  `reports/runs/mega-2/findings.md` (relaunch Fixer2 wave; xAI concurrency
  timeout policy; new_part bootstrap; buildgraph_plan shape; mission phase gate
  vs DAG progress).

- 2026-07-14 mega-4 (WS5): current-module relaunch under
  `reports/runs/mega-4/` with xAI grok-4.5, maximum effort, org layer, and fresh
  `mega-4-fc-rocket-20260714` run ID. Preflight passed delegate exposure on
  `spec_writer`/`layout_engineer`/`feature_modeler`, but the 1,187s run made
  **0 delegate calls / 0 child spawns / depth 0**; all 19 role turns stayed
  frontier, so cheap-leaf auto assignment was not exercised. The DAG started
  19/19 nodes (9 done, 10 failed), but BUILD created no part, board, or firmware:
  0 features/build nodes and four retryable ~180s xAI read timeouts. Phase
  reconciliation did run after `dag.run.done`, yet falsely backfilled successful
  PLAN/BUILD gates despite failed nodes and no artifacts — liveness improved,
  correctness still fails. Provider usage tokens/cost and semaphore occupancy
  remain unobserved. Explicit verdicts, latency metrics, live Fixer4 fingerprints,
  and TOP5 are in `reports/runs/mega-4/findings.md`.
- 2026-07-14/15 mega-5 (WS5): fresh process on commit `4f98f625` under
  `reports/runs/mega-5/` (`mega-5-fc-rocket-20260714`) so all Fixer4 fixes bind.
  Preflight PASS: delegate on `spec_writer`/`layout_engineer`/`feature_modeler`
  and RENDERED tool schemas include `budget_tokens`. Wall **1,438.65s**, mission
  `blocked`/`deliver_incomplete`, `phases_completed=["intake"]` only. DAG 19/19
  (8 done, 11 failed). **Actual delegation partial flip:** 2 `delegate` calls,
  1 depth-1 `part_fixer` spawn (brief≤500 + `budget_tokens=8000`), join
  `executed=true` but 0 tokens spent / null handoff / no CAD. Auto assignment
  still unexercised — all 19 tools_slots frontier SuperGrok (owner quota:
  VERIFY/DELIVER/INTAKE leaves should have been cheap). BUILD still 0
  parts/boards/firmware. Phase gate no longer false-completes PLAN/BUILD
  (honesty fix held) but live stage_log stayed intake-only until terminal.
  **7×** xAI read timeouts under fan-out; SPECIFY slots were 41 tools/2284 tok.
  Fixer5 mid-run Fixes 1–7 (prompt contract, model_assignment, xAI retry,
  auto_pick-first SuperGrok quota, live phase cursor, tools_slot cap≤14) need
  fresh process. Findings + SuperGrok spend section + TOP5:
  `reports/runs/mega-5/findings.md`.
- 2026-07-14/15 mega-6 (WS5): conservation relaunch on commit `563fb2c0` under
  `reports/runs/mega-6/` (`mega-6-fc-rocket-20260714`) — first process with full
  Fixer5 wave bound and **no per-node frontier injection** (auto_pick-first).
  Preflight PASS: `budget_tokens` on rendered delegate schemas; auto_pick smoke
  depth-2 low-difficulty → `ollama/llama3.2` (non-xai). Attempt-1 died 19/19
  `provider_auth` (~4s) when keyless openrouter free was auto-picked at
  min_rank≥3 (archived `attempt-1-openrouter-auth-wipe/`); Fixer6 Fix1 key-gate
  + driver `enabled_providers=[ollama,xai-sub]`. Attempt-2 wall **888.68s**
  (−38% vs mega-5 1438s), mission `blocked`/`deliver_incomplete`,
  `phases_completed=["intake","specify","plan"]`. DAG 19/19 (**17 done, 2 failed**).
  **xAI timeouts 0** under semaphore=3 (mega-5 had 7); SPECIFY slots **14 tools /
  601 tok** (was 41/2284). First auditable SuperGrok spend: **192**
  `provider_usage` rows / **2.55M** total tokens (all xai-sub — depth-0 min_rank=3
  filters ollama rank1; cheap leaves need depth≥2 delegate, of which **0** fired —
  delegation regressed from mega-5's 2 calls/1 spawn). BUILD partial flip:
  **`new_part`×2** (empty solids) but **0 `add_features`/board/fw**. Live stage
  cursor advanced mid-run. Fixer6 Fixes 1–7 (keyless openrouter, join handoff,
  provider_usage, BUILD floor, ollama degrade, model_assignment on node resolve,
  add_features nudge) — Fix6–7 need relaunch. Findings + grok delta + TOP5:
  `reports/runs/mega-6/findings.md`.

- 2026-07-14/15 mega-7 (WS5): DELIVER + conservation validation under
  `reports/runs/mega-7/` (`mega-7-fc-rocket-20260714`). Baseline `84963165`.
  Attempt-1 (pre-Fixer7, account `79066dcc-…`) archived
  `attempt-1-pre-fixer7-rebind/` after Fix6 `model_assignment` proof; killed for
  relaunch. Attempt-2 primary: fresh SuperGrok `17d17fb1-…` + Fixer7 Fixes 1–4
  bound. Preflight PASS (spawn_probe budget_tokens; auto_pick depth2→ollama;
  subscription_accounts prefer newest). Wall **382.98s**. Mission no longer
  freezes on `deliver_incomplete` — soft-join advanced deliver when any
  `n_deliver_*` done (`phases_completed` includes deliver; stage exit
  `mission_report_ledger_closed`). DAG 19/19 (**9 done / 10 failed**).
  **Cheap-share live >0:** task_class stamps → 11/19 model_assignment rows
  ollama (leaf/simple_part); token share **0.72%** (21k/2.96M; ollama leaves
  die no_code cheaply while SuperGrok still owns long loops). **BUILD partial:**
  `new_part`×1 then **`add_features`×13** (Fix3 sequence) but all ops rejected
  (`unknown op` vocabulary) → hollow solid. **Delegation partial:** attempt-1 archive delegate×2/spawn×2 (part_designer,
  edit-part); attempt-2 primary 0 despite Fix2 (stickiness open). Phase honesty PASS. Explicit
  six verdicts + TOP5: `reports/runs/mega-7/findings.md`. Fixer7 fixes.md
  (deliver soft-join, delegate preference, add_features thrash block,
  task_class stamp).

- 2026-07-14/15 mega-8 (WS5): cycle-8 validation under
  `reports/runs/mega-8/` (`mega-8-fc-rocket-20260714`). Baseline `7ef7b577`
  (op-vocab registry + capability separation + cheap-leaf oneshot +
  `deliver_partial` terminal). Preflight PASS: account `17d17fb1-…` active;
  PLAN/layout tools_slot has delegate and **no** add_features/new_part;
  auto_pick depth-2 → ollama. Three processes: attempt-1 archived pre-Fixer8
  rebind; attempt-2 (Fix 1–3, wall **926s**) and attempt-3 (Fix 4–5, wall
  **1003.84s**). Axis scoreboard: **A FAIL** (0 accepted add_features ops;
  a2 `unknown op 'cylinder'` without observed once-per-mission cheatsheet;
  a3 new_part without add_features). **B PASS** (delegate×1 + depth-1 spawn
  on BUILD DL2; layout/plan `delegate_or_typed_refusal_required` gate; SPECIFY
  no longer false-gated). **C PASS** (`leaf_oneshot_ok`×3 + miss×8 telemetry
  both a2/a3 after OpenAI string-content fix). **D PASS** (terminal
  `needs_correction` / `verify_failed: [AC1…AC9]` — not running+empty;
  `deliver_partial` not selected because VERIFY→CORRECT won). Fixer8 Fixes
  1–6 in `fixes.md` (oneshot string crash, run_id pack seed, gate narrow,
  JSON salvage, payload ok:false, null-reason normalize). Open defects:
  thin unknown-op errors, cheatsheet inject not ledger-visible, typed-refusal
  success path unused. Findings: `reports/runs/mega-8/findings.md`.
