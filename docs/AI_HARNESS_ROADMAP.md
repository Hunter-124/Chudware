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
  auto_pick depth-2 → ollama. Attempts 1–3 against **stale engine daemon**
  (pre-cycle-8 registry) → A FAIL. Main rebuilt OCCT daemon ~22:20; **attempt-4**
  (pid 2668492, wall **693.46s**, Fixer8 1–10 bound) reconnected mid-run and
  **flipped A**: `new_part`×2 + **`add_features` accepted ×5** (real
  `add_feature`+cylinder/shell/cone solids on doc-cpp…). Full scoreboard:
  **A PASS**, **B PASS** (delegate×1 + spawn; layout/plan typed-refusal gate;
  SPECIFY not false-gated), **C PASS** (`leaf_oneshot_ok`×10 / miss×1),
  **D PASS** (explicit `failed`/`dag_error: string or blob too big` — not
  running+empty; Fix11 compact node_states residual). Fixer8 Fixes 1–11 in
  `fixes.md`. Findings: `reports/runs/mega-8/findings.md`.
- 2026-07-14/15 mega-9 (WS5): quality cycle under
  `reports/runs/mega-9/` (`mega-9-fc-rocket-20260714`). Baseline `b8181b97`
  + Fixer9 F/E/G (unit-green 36p). Preflight PASS: daemon git_rev matches
  disk (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`; PLAN/layout
  cap-sep; auto_pick depth-2 → ollama. Attempt-1 pre-Fixer9 archived
  (`attempt-1-pre-fixer9/`). **Attempt-2** pid 2788930 wall **415.77s**
  terminal `needs_correction`/`verify_failed: [AC1…AC9]`. Scoreboard:
  **E PASS** (`battery_result` + 15 per-check + BATTERY criterion in ledger;
  residual doc_id=null / AC1 NOT-CHECKED), **F PASS** (clean stop_reason,
  no raw blob), **G PASS** (schema taught + unit E2E; live refuse envelope
  still 0), **A–D regression PASS** (add_features ok×4, delegate+spawn,
  oneshot_ok×7/miss×4, explicit terminal). Fixer9 `fixes.md`. Findings:
  `reports/runs/mega-9/findings.md`.


- 2026-07-14/15 mega-10 (WS5): AC-closure cycle under
  `reports/runs/mega-10/` (`mega-10-fc-rocket-20260714`). Baseline `df8fb2e5`.
  Fixer10 (ComprehensiveWhale) bound H AC grading + I battery doc_id pre-launch
  (50 tests green). Preflight PASS: hostd running==disk
  (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`; spawn_probe +
  auto_pick depth-2→ollama. Primary pid 2996834 wall **315.96s**, terminal
  `needs_correction`/`verify_failed: [AC1,AC2,AC4–AC9]` (**AC3 omitted — PASS**).
  Scoreboard: **H PASS** (AC3 PASS from live `check_interference`; AC1 FAIL
  from `battery_result` evidence — mega-9 NOT-CHECKED residual cleared),
  **I PASS** (`battery_result.doc_id=doc-cpp00000001`), **J PASS** (harness
  typed-refusal E2E; live envelope still 0), **A–G regression PASS**
  (add_features ok×4, delegate+spawn, oneshot_ok×11, clean stop, battery 15
  checks honest FAIL). Fixer10 `fixes.md`. Findings:
  `reports/runs/mega-10/findings.md`.

- 2026-07-14/15 mega-11 (WS5): multi-leg closure under
  `reports/runs/mega-11/` (`mega-11-fc-rocket-20260714`). Baseline `ea37fb68`.
  GradingPaths (LegitimateHeron) + LegProduction (LazyVulture) bound pre-launch
  (grading 29p + legs 14p). Preflight PASS: hostd running==disk
  (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`; spawn_probe with
  `ecad_engineer`/`firmware_engineer` domain tools; auto_pick depth-2→ollama.
  Primary pid 3075330 wall **795.82s**, terminal
  `needs_correction`/`verify_failed: [AC1,AC2,AC4,AC5,AC7,AC9]` (**AC3+AC6 PASS;
  AC8 honest SKIP**). Scoreboard: **K PASS** (9/9 AC tool-mapped via
  `mission_criteria` + `evaluate_pass_rule`), **L PASS** (AC3 interference +
  AC6 antenna analyze), **M PASS** (fw_build ELF ledger artifact + ECAD
  schematic/places), **A–J regression PASS** (add_features ok×2, delegate+spawn,
  oneshot_ok×9, battery doc_id, clean stop). Residuals: AC7 grader missed
  success/elf evidence; AC2 bay dims; AC5 doc routing; board DRC path incomplete.
  Findings: `reports/runs/mega-11/findings.md`.


- 2026-07-14/15 mega-12 (WS5): live AC expansion under
  `reports/runs/mega-12/` (`mega-12-fc-rocket-20260714`). Baseline `71dcea10`.
  Fixer12 (AbleCattle) bound AC7 parse + AC2 volume + AC5 domain doc_id + ECAD
  board product gate (52–61p). Preflight PASS: hostd running==disk
  (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`; spawn_probe +
  auto_pick depth-2→ollama; xai CLEAR from StraightFowl. Primary pid 3283812
  wall **741.38s**, terminal `needs_correction`/`verify_failed: [AC1,AC4,AC5,AC9]`
  (**AC2+AC3+AC6+AC7+AC8 PASS**). Scoreboard: **N PASS** (5 live PASS; AC3+AC6
  held + AC7 + AC2 volume proxy), **O FAIL** (schematic+place only; product gate
  blocked auto-done but board_from_schematic/DRC never ran), **A–M regression
  PASS** (add_features ok, fw ELF, battery doc_id, clean stop). Residuals: board
  force-tool chain; AC5 empty netlist; AC9 missing legs; typed-refusal live.
  Findings: `reports/runs/mega-12/findings.md`.


- 2026-07-15 mega-13 (WS5): board-chain + AC expansion under
  `reports/runs/mega-13/` (`mega-13-fc-rocket-20260715`). Baseline `6894f19d`.
  Fixer13 (DustyGibbon) bound ECAD force-chain + wire-before-SPICE + AC9 leg
  mapping + airframe BUILD brief (70p) + post-launch frontier rank floor /
  empty-tool product done block. Preflight PASS: hostd running==disk
  (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`; spawn_probe with ECAD
  chain tools; auto_pick leaves→ollama. Attempt-1 (ollama-all, measured
  grok-4.5 rank=1) and attempt-2 (API Model not found: grok-4-5) archived.
  **Attempt-3** pid 3500779 wall **610.86s** (harness user rank override
  grok-4.5=5), terminal `needs_correction`/`verify_failed: [AC1,AC4,AC5]`
  (**AC2+AC3+AC6+AC7+AC8+AC9 PASS**). Scoreboard: **Q PASS** (6 live PASS),
  **P FAIL** (schematic+place×6; board_from_schematic/DRC still 0; force-chain
  residual), **R FAIL** (empty-netlist SPICE; no wire tools), **N PASS** /
  **O FAIL**, **A–M regression PASS** (add_features ok×19, fw ELF
  `3c1ef7a802cd`, AC9 all 12 legs, battery doc_id). Residuals: live force-chain
  injection; wire-before-sim; AC1 soft zeros; auto_pick model-id/rank floor.
  Findings: `reports/runs/mega-13/findings.md`.

- 2026-07-15 mega-14 (WS5): hard ECAD chain + remaining AC gaps under
  `reports/runs/mega-14/` (`mega-14-fc-rocket-20260715`). Baseline `7a1e1e5d`.
  Fixer14 (ExceptionalYak) bound Fix8 force-inject after denial cap + AC1
  per-check diagnosis + frontier measured rank floor 5 (77p). Preflight PASS:
  hostd running==disk (`night2-3054-gcb65bfd9-dirty`); account `17d17fb1-…`;
  spawn_probe incl Fix8 gate wiring; auto_pick leaves→ollama, analysis→
  xai-sub/`grok-4.5` EXACT. Primary pid 3590131 wall **797.46s**, terminal
  `needs_correction`/`verify_failed: [AC1,AC4,AC5]` (**AC2+AC3+AC6+AC7+AC8+AC9
  PASS**). Scoreboard: **S PASS** (board_from_schematic + DRC errors=0 via
  hard gate + force_inject; P/O upgraded), **T FAIL** (AC5 still empty-netlist
  class — no `ecad_wire`, U* ICs lack sim models), **U PASS** (AC1 FAIL with
  per_check/zero_checks/brief_hints in criterion_AC1), **Q PASS** (6 live;
  ≥7 miss), **A–N/K–M regression PASS** (B/G/J live residual). Residuals:
  SPICE sim-model passives + wire; ERC power_not_driven; AC1 soft zeros;
  typed-refusal live; phase cursor honesty.
  Findings: `reports/runs/mega-14/findings.md`.

- 2026-07-15 mega-15 (WS5): schematic wire+power force-inject + battery
  invocation fixes under `reports/runs/mega-15/` (primary graded
  `mega-15-fc-rocket-20260715-a3`). Baseline `17b05635`. Fixer15
  (CloseLynx) bound battery arg adapters + honest SKIP + pin-aware
  `ecad_wire`/`ecad_add_power` force-inject + ERC-after-DRC (128p).
  Preflight PASS (hostd match; Fix8+Fix-wire gates; auto_pick).
  **a1** archived (`attempt-1-xai-quota-blocked/`): both SuperGrok
  accounts `personal-team-blocked:spending-limit`, wall 22s.
  **a2** archived (`attempt-2-openrouter-daily-cap/`): free frontier
  `nvidia/nemotron-3-ultra-550b-a55b:free` wall 240s; **V PASS**, **W
  FAIL** (OpenRouter free-models-per-day 429 on BUILD; force_inject=0),
  **X PASS**, live AC PASS=1 (AC6).
  **a3 LOCAL-ONLY** offline `local-24k-gemma3-qat` (llama-server :1234)
  wall **73.73s**, terminal `needs_correction`/`verify_failed:
  [AC1,AC2,AC4,AC5]` (**AC3+AC6+AC7+AC8+AC9 PASS** = 5 live). Scoreboard:
  **V PASS**, **W UNTESTABLE** (schematic open only; ctx slot 4096
  overflow before place thrash — force_inject never fired; inject still
  unit-bound), **X PASS**, **U PASS**, **N PASS**, live AC PASS=5
  (target ≥8 miss). Full quota story: xai both accounts + openrouter
  daily + local ctx. **grok-4.5 revalidation deferred**.
  Findings: `reports/runs/mega-15/findings.md`.

- 2026-07-15 mega-16 (WS5): quota-unblock validation under
  `reports/runs/mega-16/` (primary `mega-16-fc-rocket-20260715`).
  Baseline `c269e326`. Fixer16 (BoldSilverfish) bound deepseek paid
  rank-5 + `keys.openrouter` provider wiring + xai spending-limit →
  `provider_unavailable` fallthrough (80p). Preflight PASS (hostd match;
  Fix8+Fix-wire; auto_pick axis Y). Frontier
  `openrouter:deepseek/deepseek-chat-v3.1` PAID; spend Δ **$0.16** / guard $8.
  Wall **316.73s**, terminal `deliver_partial`. Scoreboard: **Y PASS**
  (hard→openrouter paid×11, leaf→ollama×13, xai×0), **W PARTIAL**
  (`ecad_add_power` ok; `ecad_wire` fail `#PWR1`; force_inject place_symbol;
  AC4 ungraded), **X FAIL** (no SPICE), **V UNTESTABLE** (verify skipped),
  live AC PASS=**0** (criterion_result absent — deliver without AC grading).
  Target ≥8 miss. **grok-4.5 revalidation deferred**.
  Findings: `reports/runs/mega-16/findings.md`.

- 2026-07-15 mega-17 (WS5): terminal grading + power-then-wire under
  `reports/runs/mega-17/` (primary graded **a3** `mega-17-fc-rocket-20260715`).
  Baseline `cd764e92`. Fixer17 (AlrightMosquito) bound Z1 VERIFY/terminal
  criterion_AC1–AC9 always, Z2 live-ref wire (no `#PWR1`) + power-before-wire,
  Z3 chain-need `ecad_simulate` (91p) then Z2 residual rebind (self-loop no
  board unlock; force prefers add_power→cross-ref wire; 94p). Preflight PASS.
  Frontier `openrouter:deepseek/deepseek-chat-v3.1` PAID; spend guard $8 held
  (a2 Δ~$0.055; a3 usage poll flat). **a2** wall 145.9s Z1 PASS / Z2 FAIL /
  Z3 PASS / live 4 (archived). **a3** wall **150.7s**, terminal
  `needs_correction`/`verify_failed:[AC1,AC2,AC3,AC5,AC7,AC9]` (**AC4+AC6
  PASS** = 2 live). Scoreboard a3: **Z1 PASS**, **Z2 PASS** (add_power×8
  before wire; AC4 ERC clean), **Z3 PASS** (OP/mna; V(3V3) null), **Y PASS**,
  **W/S/Fix8 PASS**, live ≥8 miss. **grok-4.5 deferred**.
  Findings: `reports/runs/mega-17/findings.md`.

- 2026-07-15 mega-18 (WS5): Z2r rework + grok-4.5 revalidation under
  `reports/runs/mega-18/` (primary `mega-18-fc-rocket-20260715`).
  Baseline `769f92c6`. Fixer18 (PlainLizard) bound Z2r cascade force-EXECUTE
  `ecad_add_power`→cross-ref wire + self-loop guard, Z4 AC5 null `V(3V3)`→FAIL
  (105p). Preflight PASS: account `06dc3359` ACTIVE, hard→xai-sub grok-4.5,
  mid openrouter deepseek enabled, leaf ollama. Primary wall ~551s terminal
  `needs_correction` live AC PASS **6**/9 (AC2/3/6/7/8/9). Axes: Z1 PASS,
  Z2r PARTIAL (power×2 live; force wire U1↔R1 not rail; ERC still dirty),
  Z3 PASS, **Z4 PASS** (NOT-CHECKED residual closed), G18 PARTIAL (6<7),
  Y PASS. Residuals: power-pin→rail force wire; AC4 DRC on sch not board;
  AC5 finite V(3V3); ≥8 live AC. Findings: `reports/runs/mega-18/findings.md`.
- 2026-07-15 mega-19 (WS5): close mega-18 residuals under
  `reports/runs/mega-19/` (primary `mega-19-fc-rocket-20260715`).
  Baseline `6d11238b`. Fixer19 (ColdMuskox) bound D19 power-satisfied,
  A19 rail wire prefs, B19 board doc_id, C19 V-source/measure; rebind1
  force-after-power; rebind2 cascade budget past thrash (119p). Preflight
  PASS: account `06dc3359`, hard→grok-4.5 EXACT, mid deepseek, leaf ollama.
  **a1** 459s live 6 (force idle; archived). **a2** 595s live 6 (budget
  starve pre-wire; archived). **a3** wall **510s** live AC **4**/9 (AC6–9;
  mech AC1–3 starved). Axes a3: **D19/C19 PASS**, A19/B19 **PARTIAL**
  (force cascade fires: power→R place→**U1↔R1 wire** not rail; board+DRC on
  board doc `5cc769570819` clean; ERC power_not_driven remains), Z1/Z3/Z4/Y/
  Fix8/W/X PASS, G18 PARTIAL. Residual → Fixer20 rail wire + ERC clean +
  finite V(3V3). Findings: `reports/runs/mega-19/findings.md`.



- 2026-07-15 mega-20 (WS5): close mega-19 A19/B19 live residuals under
  `reports/runs/mega-20/` (primary `mega-20-fc-rocket-20260715`).
  Baseline `b0e66f3d`. Fixer20 (LiberalWarbler) bound A20 power-pin rebond
  (no #PWR ref; unbonded power_in xy) + B20 board_doc_id on forced/model DRC
  (127p). DaemonFix (DrivingHippopotamus) H1+H3 hostd `night2-3075-gb0e66f3d-dirty`.
  Preflight PASS: account `06dc3359` ACTIVE, hard→xai-sub grok-4.5 EXACT, mid
  openrouter deepseek (key restored env-only), leaf ollama; hostd matches disk.
  **a1** wall **404s** terminal `done`/`deliver_partial` live AC PASS **5**/9
  (AC4/6/7/8/9). Axes a1: **A20 PASS** (VIN.+→R1.1 rail wire; power_not_driven=0;
  AC4 DRC+ERC clean), **B20 PASS** (board `c01d5499cdb8` DRC errors=0; S PASS),
  C19/D19/Z1/Z3/Z4/Y/Fix8 PASS; W/Z2/Z2r FAIL (no force/add_power — model VIN
  path); G18 PARTIAL (5<7). Residuals: mech AC1–3 (no new_part), finite V(3V3),
  force cascade optional when model skips add_power. Findings:
  `reports/runs/mega-20/findings.md`.


- 2026-07-15 mega-21 (WS5): kill mega-20 variance sinks (mech starvation + AC5
  net-name null) under `reports/runs/mega-21/` (primary
  `mega-21-fc-rocket-20260715`). Baseline `175c4b81`. Fixer21 (TroubledSlug)
  bound M21 deliver-gate mech force (`new_part`+`add_features` or typed refusal)
  + N21 AC5 live-net resolve/`resolved_net` evidence (142p). Preflight PASS:
  account `06dc3359` ACTIVE, hard→xai-sub grok-4.5 EXACT, mid openrouter
  deepseek (keys.openrouter merge-safe), leaf ollama; hostd
  `night2-3075-gb0e66f3d-dirty` matches disk. **a1** wall **434s**
  terminal `needs_correction`/`verify_failed: ['AC1', 'AC4', 'AC5']` live AC PASS **6**/9
  (AC2, AC3, AC6, AC7, AC8, AC9). Axes a1: **M21 PASS** (airframe product; AC2/AC3
  PASS), **N21 PASS** (`resolved_net=3V3`, finite V=0.0 honest FAIL vs band),
  C19/D19/Z1/Z2/Z3/Z4/Y/W/X/V/Fix8/A PASS; A20/B20/S **PARTIAL** (AC4 DRC
  clearance×31, ERC=0); G18 PARTIAL (6<7). Residuals: AC5 in-band V (source
  drive), AC4 clearance, AC1 measure_between side vocab. Findings:
  `reports/runs/mega-21/findings.md`.

- 2026-07-15 mega-22 (WS5): close mega-21 AC1/AC4/AC5 residuals under
  `reports/runs/mega-22/` (primary `mega-22-fc-rocket-20260715` **a2**).
  Baseline `0f43d126`. Fixer22 (GrossShark) P22/Q22/R22 (158p) + rebinds
  (mech soft_refusal, ecad_product_gate, V_source GND, side-only measure; 163p).
  Preflight PASS: account `06dc3359`, hard→xai-sub grok-4.5, mid deepseek, leaf
  ollama. **a1** blocked live 4/9. **a2** wall **676s** `done`/`deliver_partial`
  live AC **7**/9 (AC2/3/4/6/7/8/9). **Q22 PASS** (DRC errors=0 — clearance×31
  closed). **P22 PARTIAL** (V(3V3)=null). **R22 PARTIAL** (mb soft zero).
  M21/N21/B20/Z*/G18/Y/W/X/Fix8 PASS. a3 checkpoint_blob + a4 process death
  archived (infra). Residuals: finite V on 3V3; mb real score/SKIP. Findings:
  `reports/runs/mega-22/findings.md`.


- 2026-07-15 mega-24 (WS5): fix mega-23 live thrash under
  `reports/runs/mega-24/` (primary a3 `mega-24-fc-rocket-20260715`).
  Baseline `8ce92d74`. Fixer24 ComingOcelot + Fixer24c BurningWildcat:
  U24 root-cause (provider poison + tool amplifiers + terminal force skip —
  **not** S23/T23 logic); V24 restore BUILD tool loop; force-first-tool on
  zero-tool prose/junk-JSON; typed_refusal on plan done; fw `_err`; wire net
  strip; ledger_note coerce; 234p offline gate. a1/a2 liveness-killed
  (archives `221105Z`/`223717Z`); HOLD then offline FINAL_OK; **a3** wall
  **897s** terminal `needs_correction`/`verify_failed:[AC4,AC5]` live AC
  **7**/9 (AC1–3,6–9). **T23 PASS** (measure_between=1.0); **S23 FAIL**
  (simulate on PCB doc). **U24/V24 PASS**. Battery `doc-cpp00000005` PASS.
  Residuals: AC4 clearance×6; AC5 schematic doc routing. Findings:
  `reports/runs/mega-24/findings.md`.
