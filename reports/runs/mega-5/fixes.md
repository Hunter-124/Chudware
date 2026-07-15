# mega-5 harness fixes (Fixer5 = AllegedDeer)

Live mission: `mega-5-fc-rocket-20260714` (MegaRun5 = PlannedJay).
Runtime ledger: `~/.local/share/AI-CAD/cache/runs/mega-5-fc-rocket-20260714/ledger.json`.
Baseline commit: `4f98f625` (first run with all Fixer4 fixes bound).
Scope is harness enablement only; no grading changes.

## Fix 1 — all delegate-capable role prompts name `budget_tokens`

**Fingerprint / ledger evidence:** mega-5 `spawn_probe.json` preflight
`budget_tokens_preflight.detail` showed `layout_engineer`, `feature_modeler`,
`assembly_fit_engineer`, and `department_manager` with
`prompt_budget_tokens=false` / `prompt_sig_ok=false` while the rendered schema
already required `budget_tokens`. Mega-4 Fix 6 only patched `orchestrator` +
`spec_writer`. Live mega-4 still had 0 `delegate` calls; a role that only sees
"prefer delegate" without the dispatch-faithful signature can invent
`budget=…` and get a zero-token grant rejection.

**Fix:** role system prompts for the four remaining delegate roles now name
`delegate(role, brief, budget_tokens)` (department_manager dispatch step
likewise). Schema path unchanged.

**Regression:** `test_all_delegate_roles_use_budget_tokens_contract`.

## Fix 2 — auto model assignment emits audit events on every executed spawn

**Fingerprint / ledger evidence:** mega-4 findings — zero assignment rows:
no `role_model` / `task_class` / `min_rank` / `auto_pick` / cost-assignment
events despite `MissionDispatch._run_subagent` already resolving via
`role_models`. Assignment was written only onto the in-memory node dict, so
graders and the mission ledger could not prove min_rank was respected.

**Fix:**
- `agent_events.model_assignment_event` builds
  `{depth, task_class, min_rank, picked_model, picked_provider, cost_tier, mode, rank, reason, role, subagent_id}`.
- `role_models.cost_tier_for` exposes the cost class for telemetry.
- `_run_subagent` emits `agent.model_assignment` on the live sink and appends a
  ledger `model_assignment` note; `role_model` is also returned on the
  delegate result (done / failed / terminated).

**Regression:** `test_delegate_emits_model_assignment_event`,
`test_model_assignment_event_builder_shape`.

## Fix 3 — xAI semaphore occupancy telemetry + default concurrency 3 + read-timeout retry

**Fingerprint / ledger evidence:** mega-4 PLAN / BUILD DL1 / BUILD DL2 /
DELIVER DL2 each failed after ~180s with
`provider_error: The read operation timed out`, `retryable=true`. Default
semaphore was 2 with no occupancy/queue events, so the run could not prove
serialization vs remote load. A single timeout escalated the whole node
(`stop_reason=failed` → escalate). Mega-5 live: **7×** read-timeout
`provider_error` rows (SPECIFY DL1/2/4, DELIVER DL3, n_spec_done, PLAN layout,
PLAN mission) escalated budget exhausted on the pre-fix process.

**Fix:**
- Default `AICAD_XAI_MAX_CONCURRENT` raised 2 → 3 (still env-overridable).
- Process-wide `semaphore_stats()` tracks capacity / in_flight /
  peak_in_flight / wait_count / total_wait_ms; queue waits log at INFO.
- One bounded in-provider retry on read timeout
  (`AICAD_XAI_READ_TIMEOUT_RETRIES`, default 1).
- `node_runner`: when `stop_reason=failed` and
  `agent_result.result["retryable"] is True`, map to `VERDICT_RETRY` so the
  existing retry budget is consumed instead of instant escalate
  (`retry_cheap` stamped for the next attempt).

**Regression:** `test_xai_default_max_concurrent_is_three`,
`test_xai_semaphore_stats_track_occupancy`,
`test_xai_read_timeout_retries_once`,
`test_retryable_provider_failed_maps_to_retry_verdict`,
`test_non_retryable_provider_failed_still_escalates`.
Mega-3 concurrent default assertion updated to 3.

## Fix 4 — assignment works without config.json `ai_roles`

**Fingerprint / ledger evidence:** mega-4/5 host config has no `ai_roles` key
(`provider=xai-sub`, `model=grok-4.5` only). `resolve_for_node` returns `None`
when `ai_roles` is unset (legacy path), so even after Fix 2 the spawn path
never built an assignment and never emitted rows — auto model assignment was
structurally unexercisable on mega runs.

**Fix:** `_run_subagent` always falls through to `resolve_role_model(..., min_rank=)`
when `resolve_for_node` is None or when spawn policy supplies `min_rank`.
Telemetry emission is unchanged from Fix 2.

**Regression:** `test_delegate_assigns_without_ai_roles_config`.

## Fix 5 — SuperGrok quota: stop binding frontier xAI to every node

**Fingerprint / ledger evidence (owner priority):** mega-4 finding — mission
driver injects frontier `xai:grok-4.5` and `node_runner._resolve_provider`
returned the injected provider **first**, so every DAG node + most work burned
SuperGrok. Auto_pick min_rank thresholds already encode leaf→cheap /
frontier→hard, but the run config overrode per node. Mega-5: all 19 tools_slots
`tier=frontier`; SuperGrok wall 1438.65s.

**Fix:**
- `_resolve_provider` priority is now: **difficulty-aware auto_pick first**
  (full enabled set, depth/task_class/min_rank), then injected mission
  provider as fallback only, then tier, then ambient config.
- Enabled set includes live **ollama** (`llama3.2:latest` @ 11434) when present;
  free OpenRouter models cost-ranked below subscription (`cost=1` for `:free`).
- Non-frontier ranks get `reasoning_effort` low/medium; `retry_cheap` / low
  `retry_remaining` forces low effort.
- Spawn path (`MissionDispatch._run_subagent`) maps ollama→openai local endpoint
  and applies the same effort caps.
- Rankings: `llama3.2:latest` / `llama3.2` rank 1.

**Regression:** `test_depth2_low_difficulty_spawn_not_xai` (depth-2 simple_part
must not resolve to xai when a cheaper enabled provider meets min_rank),
`test_retry_cheap_forces_low_effort`.

## Fix 6 — live phase cursor during DAG execution

**Fingerprint / ledger evidence:** mega-5 live run stage_log stayed
**INTAKE-only** until post-`dag.run.done` reconciliation entered
specify/plan/build/verify/deliver with null exit gates.
`phases_completed=["intake"]` only (honest — no false PLAN/BUILD complete).

**Fix:** `run_mission` wraps the DAG `emit` with
`_live_phase_cursor_on_dag_event`. On `dag.node.started|done|failed|paused` it
maps `node_id` → phase and `ledger.log_stage`s any intermediate phases up to
the furthest started phase, emitting `mission_phase` events. **Cursor only** —
never invents exit gates or `phases_completed` (false-completion rules remain
in `_drive_phases_from_dag`).

**Regression:** `test_live_phase_cursor_advances_on_dag_node_started`.

## Fix 7 — SPECIFY tools_slot family expansion respects MAX_TOOLS_PER_ROLE

**Fingerprint / ledger evidence:** mega-5 tools_slot for every SPECIFY DL*
rendered **41 tools / 2284 tokens** (`budget_tokens=10000`, `trimmed=false`)
because `family:ecad` + `family:em` + `family:fw` expanded unconstrained in
`RoleCard.concrete_tools` while contract `MAX_TOOLS_PER_ROLE=14`. Five-way
SPECIFY fan-out with ~2.3k-token tool schemas is a strong root cause for the
xAI 180s read timeouts under load.

**Fix:**
- `concrete_tools` always returns ≤ `MAX_TOOLS_PER_ROLE`.
- Explicit `tool:` entries take seats first; family expansions fill remaining
  seats by **round-robin** across families so ecad/em/fw each get coverage.
- Absolute `out[:MAX_TOOLS_PER_ROLE]` ceiling for over-authored allowlists.
- `layout_engineer` allowlist trimmed to 14 (dropped redundant `render_part`).

**Regression:** `test_spec_writer_concrete_tools_capped_at_max`,
`test_all_roles_concrete_tools_within_cap` (mega-1 domain-family test still
passes with interleaved ecad/em/fw).

## Fix 8 — productive delegation: brief soft-truncate + role alias + token spend

**Fingerprint / ledger evidence (mega-5 findings §1):** first real spawns —
(1) `delegate(role=part_fixer, brief_len=1048, budget_tokens=8000)` → hard
`brief must be ≤500 chars` (correct contract, wasted BUILD turn);
(2) shortened brief → spawn `part_fixer` depth-1 `executed=true` /
`subagent_status=done` but **unknown role** (no card → bare sub-agent, no
`new_part`), **tokens_spent=0**, empty model fields, child tools
`skill_activate`/`list_tools` unresolved, only `cad_guide`.

**Fix:**
- Soft-truncate briefs to 500 chars (ellipsis) with `brief_truncated` /
  `brief_len_original` on the result instead of hard-failing the spawn.
- Role aliases: `part_fixer` / `geometry_fixer` / `cad_fixer` /
  `part_modeler` → `feature_modeler` (has `new_part`); `mech_fixer` /
  `feature_fixer` → `fixer`.
- After a successful child turn, accrue a lower-bound `tokens_used`
  estimate (`1000 + 200×tool_calls`) flagged `tokens_used_estimate=true` so
  the tree meter is not stuck at 0 when providers omit usage.

**Regression:** `test_delegate_soft_truncates_overlong_brief`,
`test_delegate_role_alias_part_fixer_to_feature_modeler`.

## Verification

```text
python3 -m pytest services/agent/test_mega5_fixes.py \
  services/agent/test_mega4_fixes.py services/agent/test_mega3_fixes.py \
  services/agent/test_mega1_top5.py services/agent/test_role_models.py -q
86 passed in 1.78s
```

## Live-run notes

- MegaRun5 finished wall **1438.65s**, `blocked/deliver_incomplete`,
  `phases_completed=[intake]`, 2 delegate calls / 1 depth-1 spawn, 0
  model_assignment, 7× read timeouts, 0 BUILD features. Process
  (pid 1785399) predated Python Fix 2–8.
- **Re-launch required** to bind Fix 2–8. YAML Fix 1/7 rebind on `load_role`.
- Watch-list for next process: ≤14 tools on SPECIFY; soft-truncated briefs +
  `feature_modeler` child with `new_part`; non-xai leaf `model_assignment`;
  live stage_log mid-run; retryable timeouts → DAG retry; tokens_used > 0 on
  child complete; BUILD geometry evidence.
