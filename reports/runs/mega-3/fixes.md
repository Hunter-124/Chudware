# mega-3 harness fixes (Fixer3 = InquisitiveGrouse)

Live mission: `mega-3-fc-rocket` (MegaRun3 = DemocraticAntelope).
Driver: `reports/runs/mega-3/run_mega.py` (or mega-2 copy).
Scope: `services/agent/**`, `services/dag/**`, `services/mcp/**`,
`tools/flywheel/bench/gym/*.py`, `reports/runs/mega-3/fixes.md`.
No grading inflation — only contract / wiring / state-key enablement.

Tests green (this wave):
```
python3 -m pytest \
  services/agent/test_mega3_fixes.py \
  services/agent/pipeline/test_pipeline.py::test_specify_advances_when_overseer_join_failed \
  -q
# 15 passed
```

---

## Fix 1 — Mission phase gate vs DAG progress

**Fingerprint:** `specify_incomplete` while DAG progressed to layout/plan/build/deliver
(mega-2: `phase=specify`, `phases_completed=[intake]`, stop=`specify_incomplete`
with `n_build_*` / `n_deliver_*` done).
**Evidence:** mega-2 findings §Harness enablement gaps #1; TOP-5 rank 5.
**Root cause:** `_drive_phases_from_dag` required *every* `n_spec_*` worker
`status==done` (after filtering the advisory join). Partial SPECIFY + soft
fan-in left failed workers in the required set forever, so the mission-layer
phase never exited even when the DAG join completed and later phases ran.
**Fix:**
- `services/agent/loop.py` — `_PHASE_JOIN_NODES` (`specify→n_spec_done`,
  `plan→n_plan`)
- `services/agent/loop.py` — `_phase_nodes_complete`: phase completes when
  (a) its join node is `done`, OR (b) all workers (join excluded) are `done`,
  OR (c) any downstream phase node is already `done` (DAG soft-advance)
- Overseer join failure alone still does not block when workers finished
  (preserves WS0 soft-join test).
**Regression:**
- `test_phase_gate_advances_when_join_done_despite_failed_workers`
- `test_phase_gate_advances_when_dag_past_specify_with_failed_workers`
- `test_phase_gate_still_blocks_when_specify_and_no_downstream`
- existing `test_specify_advances_when_overseer_join_failed`
**Note:** live mega-3 pid started before this edit will not reimport; binds
on next mission process.

---

## Fix 2 — Subagent spawn exposure (tools_slot + nudge)

**Fingerprint:** `child_spawns=0` with spawn-depth policy max=8 never consulted
beyond root (mega-2 subagent stats); MegaRun3 GAP2 probe pre-launch:
`spawn_exposed_on_specify_and_plan=FALSE` — `spec_writer`/`layout_engineer`
lacked `delegate`; only `orchestrator` + `department_manager` had it.
**Evidence:** MegaRun3 `spawn_probe.json`; mega-2 findings §Subagent spawn stats.
**Root cause:** role allowlists for specify/plan/build workers omitted
`delegate`; tools_slot is role-allowlist gated via `RoleCard.concrete_tools`.
Policy already permits depth>0 for capability C (`max_spawn_depth=8`).
**Fix:**
- `services/agent/roles/spec_writer.yaml` — `tool: delegate` + soft one-line
  decomposition nudge in system_prompt
- `services/agent/roles/layout_engineer.yaml` — same
- `services/agent/roles/feature_modeler.yaml` — same (BUILD)
- `services/agent/roles/assembly_fit_engineer.yaml` — same (BUILD assembly)
- Orchestrator / department_manager already had `delegate` (unchanged)
**Regression:**
- `test_delegate_on_specify_plan_build_tools_slot` (parametrize 6 roles)
- `test_spawn_depth_policy_permits_depth_gt0_for_capability_c`
- `test_spec_writer_prompt_has_decomposition_nudge`
**Verify:** `load_role("spec_writer").concrete_tools` includes `delegate`.

---

## Fix 3 — xAI parallel read-timeout storm

**Fingerprint:** `provider_read_timeout_storm` — 7× retryable
`The read operation timed out` under 5-way SPECIFY fan-out → false
“budget exhausted” node deaths (mega-2 #9).
**Evidence:** mega-2 findings TOP-5 rank 2; Fixer2 deferred as ops.
**Root cause:** unbounded concurrent `XAISubProvider.complete_ex` →
parallel long reads against xAI OAuth API; urllib timeout 120s each.
**Fix:**
- `services/agent/providers/xai_sub_provider.py` — process-wide
  `threading.Semaphore` around `complete_ex` HTTP path
- Config knobs (env):
  - `AICAD_XAI_MAX_CONCURRENT` (default **2**)
  - `AICAD_XAI_READ_TIMEOUT` seconds (default **180**)
- Test hook `_reset_request_semaphore_for_tests()` for env rebind
**Regression:**
- `test_xai_max_concurrent_default_and_env`
- `test_xai_read_timeout_default_and_env`
- `test_xai_complete_ex_bounds_concurrency` (peak concurrency == 1 under cap=1)
**Note:** does not invent completions; only serializes sibling DL calls so
read timeouts stop cascading into node budget death.

---

## Fix 4 — residual ledger kind/section thrash (mega-3 t+630s live)

**Fingerprint:** `ledger_note kind=manifest_confirm` unknown; `ledger_get
section=interfaces|results|notes` unknown.
**Evidence:** MegaRun3 t+630s residual DM (live pid 1153063).
**Root cause:** Fixer2 covered all/events/manifest/verdict; models still invent
intake-confirm kinds and layout/results section names.
**Fix:**
- `mission_dispatch.py` — kind aliases `manifest_confirm`/`confirm`/`confirmation`
  → decision; section aliases `interfaces→dimensions`, `results→artifacts`,
  `notes→stage_log`, `plan→build_nodes`, `acceptance→artifacts`
- `services/mcp/ledger_tools.py` — same aliases on global MCP path
**Regression:** `test_ledger_note_kind_manifest_confirm_aliases`,
`test_ledger_get_section_aliases_mega3`

## Fix 5 — orchestrator spawn nudge (soft)

**Fingerprint:** tools_slot has `delegate` on n_spec_* (YAML live) but
`delegate_calls=0` / `child_spawns=0` still.
**Fix:** one-line strengthen on `orchestrator.yaml` PLANNING step 3 —
explicit `delegate(role, brief, budget)` preference. Soft only; no force-spawn.

---

## Suite status

```
python3 -m pytest services/agent/test_mega3_fixes.py \
  services/agent/pipeline/test_pipeline.py::test_specify_advances_when_overseer_join_failed \
  -q
# 21+ expected after residual wave
```

## Process note

Live mega-3 process (pid 1153063) was started before / during these edits.
In-process Python will not see module changes until MegaRun3 relaunches.
Fixes are verified by unit regression; next mega launch should stop the
fingerprints above. YAML role cards (delegate allowlist) *did* bind live
because roles load from disk each node — confirmed by MegaRun3 t+630s.

DM history to MegaRun3 (DemocraticAntelope):
- Fixer3 identity + ownership ack
- GAP2 probe received (`delegate` missing on specify/layout)
- GAP2 land ack (role YAML wire + nudge)
- Phase-gate + xAI semaphore landed + suite green
- t+630 residual: kind/section aliases + orchestrator nudge
