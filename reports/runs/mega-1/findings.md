# Findings — mega-1 (flight computer + model rocket plane × xai:grok-4-5)

Source: real `run_mission` via `reports/runs/mega-1/run_mega.py` (org layer ON,
maximum effort in-process, `autonomy_default=auto`, spawn-depth policy max=8).
Ledger: `reports/runs/mega-1/ledger.json` (copy of
`~/.local/share/AI-CAD/cache/runs/mega-1-fc-rocket/ledger.json`).
Events: `reports/runs/mega-1/events.jsonl`. Machine summary:
`reports/runs/mega-1/analysis.json`, `mission.json`, `mission_meta.json`.

**Result: early death.** `status=failed`, `phase=plan`,
`phases_completed=["intake"]`, wall **4.25 s**.
`stop_reason` = LangGraph `INVALID_CONCURRENT_GRAPH_UPDATE` on key `__root__`
after parallel SPECIFY nodes each burned one provider turn on a hard auth error.

This is **not** a model-quality failure. The infinitely-branching multi-domain
mission never got a single successful completion call. Findings below are
evidence-cited harness defects exposed by the attempt (plus baseline from
same-day grok-4.5 gym cells).

## Mission setup (what we ran)

| Knob | Value |
|------|-------|
| Request | Model rocket plane airframe + STM32F4 flight computer E2E (plan → parts → schematic → SPICE → PCB/trace-opt → antenna/RF → firmware → Renode SITL → report) |
| Manifest | 5 deliverables (part / assembly / analysis×2 / report), 9 ACs — `manifest.json` |
| Tier | `xai:grok-4-5` → wire model `grok-4.5` |
| Effort | maximum (deliberation=full, ladder=mandatory, delegation_depth=8, worker_fan_out=4) via in-process `load_config` patch — no disk config write |
| Org / spawn | `AICAD_ORG_LAYER=1`, `AICAD_OVERSEER=1`, `max_spawn_depth=8` (capability C) |
| Graph | 19 nodes / 22 edges; 5 parallel `n_spec_DL*` fan into `n_spec_done` |

## Per-phase mortality

| Phase | Outcome | Evidence |
|-------|---------|----------|
| **INTAKE** | PASS (fixture intake_fn) | stage exit_gate `manifest_valid`; artifact `manifest` note `5 deliverables` |
| **DAG n_intake role** | FAIL / budget exhausted | `dag.node.failed` n_intake `escalated: failed (budget exhausted)` after `provider_error` Incorrect API key (events.jsonl) |
| **SPECIFY (5-way parallel)** | FAIL all 5 | `n_spec_DL1..DL5` each: tools_slot → provider_error → budget exhausted (~200 ms) |
| **SPECIFY join / PLAN** | Harness crash | `dag_error: At key '__root__': Can receive only one value per step… INVALID_CONCURRENT_GRAPH_UPDATE` |
| BUILD / VERIFY / DELIVER | never reached | phases_completed only `intake` |
| Antenna/openEMS / Renode | not exercised | auth death pre-tool |

**Phase mortality (this run):** intake structural 0.0; model-backed intake+specify **1.0**;
plan join **1.0** (infrastructure). Baseline same-day C1×grok-4.5 (gym, auth OK):
specify-fix-1 mortality specify 1/3, plan 2/3; grok45-2 specify 1/3, plan 2/3
(`analysis.json` prior_cells).

## Tool-misuse fingerprints

On **this** run the model never chose a tool — every agent_turn after tools_slot
is `provider_error`. Fingerprints are therefore **auth / dispatch**, not misuse:

1. **`provider_error` ×5** (INTAKE role + 4 of 5 SPECIFY in ledger; 5th SPECIFY
   failed in events stream before ledger fold of last error):
   `Error code: 400 - Incorrect API key provided… console.x.ai`
   — actors: `INTAKE: build manifest`, `SPECIFY DL1..DL5` (ledger events
   `agent_turn` / `provider_error`).
2. **Silent tier→provider downgrade:** config `provider=xai-sub` but
   `provider_for_tier("xai:grok-4-5")` returned `XAIProvider` (API-key path)
   because `subscription_auth` has **no** xai-sub credential and `offline_mode=true`.
   No ledger event records “fell back from xai-sub to xai”; the model just 400s.
3. **Parallel fail-amplification:** one bad key × N parallel SPECIFY nodes =
   N identical hard errors + join crash (see gaps).

### Baseline misuse (when auth worked earlier today — grok45-2 / specify-fix-1)

Still relevant for the mega-mission once auth is restored:

| Fingerprint | Evidence (prior cells) |
|-------------|------------------------|
| `ledger_get` thrash / empty `{}` | grok45-2 s3: repeated ledger_get |
| `buildgraph_status` “run not found” retry storm | grok45-2 findings T2 |
| Missing tools: `dag_status`, `channel_send`, `council_convene`, `dag_run`, `dag_replan` | grok45-2 findings T3 |
| Invalid `ledger_note` kinds (`review_verdict`, `audit`, `deliverables_manifest`) | grok45-2 findings T4 |
| Offline research tools mid-SPECIFY | grok45-2 findings T5 |
| No `new_part` before describe/measure | grok45-2 / grok45-1 T1 — BUILD never materializes geometry |

## Subagent spawn stats

| Metric | mega-1 (this run) | Prior C1 grok cells (same day) |
|--------|-------------------|--------------------------------|
| Delegation root | 1 × `orchestrator` depth=0 auto-approved (`dg_2e3ea473a407`, budget 50k) | 1 root per cell (~3/cycle) |
| Child spawns / joins | **0** (died before role agents could `delegate`) | 0 model-driven children observed in C1 cells (roles did not fan out) |
| Max depth reached | 0 | 0 |
| Spawn-depth policy | active (max 8); never consulted beyond root | same |
| Parallel DAG role nodes | 5 SPECIFY workers started concurrently | C1 usually 1 DL → less fan-in pressure |

**Conclusion:** infinitely-branching mode is **wired** (org layer constructs
`DelegationTree`, root delegation event lands) but was **not exercised** by the
model because provider calls never succeeded. The multi-deliverable graph *did*
exercise **DAG-level** parallelism (5 SPECIFY nodes), which is what triggered the
LangGraph concurrent-update crash.

## Token / latency profile

| | mega-1 |
|--|--------|
| Wall | **4.25 s** end-to-end |
| Ledger events | 17 |
| Stream events | 15 lines in `events.jsonl` |
| tools_slot tokens (frontier) | INTAKE 343 / 10000; each SPECIFY 676 / 10000 |
| Successful completion tokens | **0** (all complete_ex → 400) |
| Per-node fail latency | ~0.2 s from node.started → node.failed after provider_error |

No useful latency/token curve for reasoning=high: the xAI path never returned
content or `reasoning_content`. Effort snapshot shows maximum preset applied
to active_tier `frontier` (ledger `effort_profile`).

## Harness enablement gaps (contract mismatches)

1. **Auth resolution honesty (blocker for this mission)**  
   - `config.provider=xai-sub` + empty `subscription_auth` + `offline_mode=true` +
     no `XAI_API_KEY` → `provider_for_tier("xai:…")` still constructs `XAIProvider`
     with a missing/empty key → opaque 400.  
   - Offline mode refuses `get_provider("xai-sub")` with a clear error, but the
     tier path **does not** surface that; it falls through to the API-key provider.
   - **Contract gap:** tier resolution should fail closed with a structured
     `{ok:false, code:"auth_missing", tried:["xai-sub","xai"]}` ledger event
     instead of N parallel 400s.

2. **LangGraph parallel fan-in without reducer (`INVALID_CONCURRENT_GRAPH_UPDATE`)**  
   - Multi-deliverable missions compile 5× `n_spec_DL*` → `n_spec_done`.  
   - On concurrent completion/failure LangGraph rejects multiple writes to
     `__root__` (“Can receive only one value per step. Use an Annotated key…”).  
   - **This is a harness bug independent of auth** — any multi-DL mission that
     reaches parallel node completion will hit it. mega-1 is the first live
     multi-DL stress that made it obvious.

3. **`on_event` dual signature**  
   - `loop._emit` uses kwargs dicts; `services.dag.executor` uses
     `emit(event_type: str, payload: dict)`.  
   - A single-arg EventFn (as used by gym-style sinks) **raises** and becomes
     `dag_error` (first mega-1 attempt, wall 2.05 s).  
   - Gym cells “work” only if they ignore/partial-wrap emits; mission drivers
   need `*args, **kwargs` adapters. Harness should normalize at
   `run_mission` boundary.

4. **Retry budget vs hard auth errors**  
   - Provider 400 “Incorrect API key” is not retryable, yet nodes still run the
     escalate/budget path (`escalated: failed (budget exhausted)`).  
   - Should short-circuit the whole mission on `auth_invalid` / 401 / permanent 400.

5. **WS0 specify/plan mortality still open (when auth works)**  
   - specify-fix-1 improved specify mortality 1.0 → 0.33 but plan_incomplete
     dominates; mega-mission would still face that after auth+fan-in fixes.

6. **ECAD / firmware tools not on default SPECIFY tools_slot**  
   - SPECIFY tools_slot rendered research + ledger (+ fit helpers in C1); flight-
     computer mega needs `ecad_*`, `board_from_schematic`, `fw_*`, `em_*` offered
     on the analysis deliverable roles or BUILD will never see them. Not hit this
     run (pre-tool death) but will be the next cliff for this mission shape.

## TOP 5 tooling fixes (by unblock value)

| Rank | Fix | Unblocks | Evidence |
|------|-----|----------|----------|
| **1** | **Auth fail-closed for `xai:` tiers** — if xai-sub has no token and no API key, refuse mission start with one structured error; respect/document `offline_mode` vs subscription. | All live xAI missions on this host; prevents parallel 400 storms | mega-1 provider_error ×5; auth_probe in analysis.json |
| **2** | **LangGraph Annotated reducer / serial join for multi-DL SPECIFY fan-in** on `__root__` (and any shared state keys). | Every multi-deliverable mission (mega ECAD+mech); currently hard-crashes | stop_reason INVALID_CONCURRENT_GRAPH_UPDATE; 5 preds → n_spec_done |
| **3** | **Normalize `on_event` at `run_mission`** to accept both `(dict)` and `(str, dict)` and never let emit exceptions become `dag_error`. | Custom mission runners / monitors; first mega attempt | stop_reason `on_event() takes 1 positional argument but 2 were given` |
| **4** | **Non-retryable provider error taxonomy** (auth, 401, invalid_api_key) → abort mission + single ledger `provider_fatal` | Token waste, log noise, false “budget exhausted” | node errors all `budget exhausted` wrapping auth 400 |
| **5** | **Role tools_slot for analysis/ECAD/firmware deliverables** must include `ecad_*` / `fw_*` / `em_*` / `board_from_schematic` (or explicit handoff to a tool-rich BUILD role). | Flight-computer mega path after auth | tools_slot lists on SPECIFY DL3 were research-heavy (ledger); B1 bench tools never appear |

Honorable mentions (from same-day grok45-2, still open): bootstrap `new_part`,
`buildgraph_status` next-step hints, ledger_note kind aliases, research offline
messaging — see `tools/flywheel/bench/gym/reports/runs/grok45-2/findings.md`.

## Manual verification steps

1. Restore xAI auth: Settings → Sign in with xAI (device code) **or** set a valid
   `XAI_API_KEY` and `offline_mode=false` for cloud.
2. Re-run:  
   `~/.local/share/AI-CAD/mcp-venv/bin/python reports/runs/mega-1/run_mega.py`
3. Expect: `status.json` state=finished with phases beyond intake; ledger
   `agent_turn` with `tool_call` / non-empty replies; **no**
   `INVALID_CONCURRENT_GRAPH_UPDATE` when 5 SPECIFY nodes complete.
4. Monitor:  
   `python3 -m tools.flywheel.bench.gym.monitor reports/runs/mega-1 --follow`
   (or static scan after).
5. Confirm openEMS/Renode legs SKIP honestly when unprovisioned (not FAIL).

## Deferred

- Live multi-hour mega once auth + fan-in reducer land (true spawn-depth / token
  curves, antenna openEMS path, Renode SITL).
- No source edits performed outside `reports/runs/mega-1/**` and roadmap status
  append (per owner write-set).
- `run_mega.py` is a **report-local** driver (allowed under reports/**), not a
  product CLI.
