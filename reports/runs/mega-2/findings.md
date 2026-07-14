# Findings — mega-2 (flight computer + model rocket plane × xai:grok-4-5)

Source: real `run_mission` via `reports/runs/mega-2/run_mega.py` (org layer ON,
maximum effort in-process, `autonomy_default=auto`, spawn-depth policy max=8).
Ledger: `reports/runs/mega-2/ledger.json` (copy of
`~/.local/share/AI-CAD/cache/runs/mega-2-fc-rocket/ledger.json`).
Events: `reports/runs/mega-2/events.jsonl`. Status: `status.json` /
`mission.json`. Fixer2 live log: `fixes.md` (FollowingTakin).

**Result: mission-layer blocked at specify; DAG progressed far past plan.**
`status=blocked`, reported `phase=specify`, `phases_completed=["intake"]`,
`stop_reason=specify_incomplete`, wall **879.06 s**. DAG nevertheless ran
SPECIFY → DELIVER(spec) → ORGANIZE/LAYOUT/PLAN → **BUILD DL1/DL2 → VERIFY →
DELIVER DL1/DL2** and `dag.run.done` with **no**
`INVALID_CONCURRENT_GRAPH_UPDATE` (mega-1 fix held under 5-way fan-in).

Auth path healthy: `provider_for_tier("xai:grok-4-5")` → `XAISubProvider`
`grok-4.5` (preflight no raise). Real tool_calls and model completions
throughout (contrast mega-1 auth death at 4.25 s).

## Mission setup (what we ran)

| Knob | Value |
|------|-------|
| Request | Model rocket plane airframe + STM32F4 flight computer E2E (plan → parts → schematic → SPICE → PCB/trace-opt → antenna/RF → firmware → Renode SITL → report) |
| Manifest | 5 deliverables (part / assembly / analysis×2 / report), 9 ACs — `manifest.json` |
| Tier | `xai:grok-4-5` → wire model `grok-4.5` via **xai-sub** OAuth |
| Effort | maximum (deliberation=full, ladder=mandatory, delegation_depth=8, worker_fan_out=4) via in-process `load_config` patch |
| Org / spawn | `AICAD_ORG_LAYER=1`, `AICAD_OVERSEER=1`, `max_spawn_depth=8` |
| Graph | 19 nodes; 5 parallel `n_spec_DL*` + later BUILD/VERIFY/DELIVER pairs |
| Process | pid 1051012, exit 0; Fixer2 harness fixes landed mid-run (live modules pre-fix — fingerprints below still observed) |

## Per-phase mortality

| Phase / node class | Outcome | Evidence |
|--------------------|---------|----------|
| **INTAKE** (fixture) | PASS stage | `manifest_valid`, artifact note `5 deliverables` |
| **n_intake** role | FAIL `no_code` budget | model prose + tools, no formal handoff (seq early agent_turn) |
| **SPECIFY DL2, DL3** | DONE | confirmed ~142 s / ~95 s; ledger_note + ecad_find_part work |
| **SPECIFY DL1, DL4, DL5** | FAIL budget | provider `The read operation timed out` (retryable) under parallel fan-out |
| **n_spec_done** overseer | DONE (blocked handoff) | gaps_found; ask_user for power/radio/MCU/envelope |
| **DELIVER DL4, DL5** (early) | DONE | honest FAIL/SKIP report card from empty ledger; AC9 labeled |
| **DELIVER DL3** | FAIL timeout | same read-timeout class |
| **n_organize, n_layout, n_plan** | FAIL budget | timeouts + plan thrash; `buildgraph_plan` once ok with **empty nodes** + `unknown_plan_shape` |
| **BUILD DL1, DL2** | DONE (hollow) | completed without geometry — `describe_part`/`add_features`/`list_world_bodies` all `No current part. Call new_part(...) first` |
| **VERIFY DL2** | DONE (blocked) | review_verdict note: AC3 NOT-CHECKED |
| **VERIFY DL1** | FAIL `no_code` | budget |
| **DELIVER DL1, DL2** | DONE (blocked) | verify_battery scores 0; no doc |
| BUILD/VERIFY ECAD/FW | never material | fw_* unresolved at SPECIFY; no schematic/board artifacts |
| Antenna/openEMS / Renode | SKIP / not exercised | DL5 report SKIP antenna + Renode; no openEMS path |

**Mission-layer phase mortality:** intake structural 0.0; reported specify
incomplete **1.0** (gate), despite DAG BUILD/DELIVER nodes running.
**DAG node fail rate (failed / started):** intake role 1/1; specify 3/5;
early deliver 1/3; organize/layout/plan 3/3; build 0/2; verify 1/2;
late deliver 0/2.

## Tool-misuse / harness fingerprints (live ledger)

| # | Fingerprint | Evidence |
|---|-------------|----------|
| 1 | **tools_slot_unresolved_still_callable** | tools_slot `unresolved[]` lists `fw_*`/`em_*`/`ecad_antenna_*` but model still calls them → `tool 'fw_status' could not be resolved` (DL4) |
| 2 | **ledger_get_section_all** | `ledger_get(section="all")` → unknown section (DL1/2/plan/BUILD) |
| 3 | **intake_role_no_code** | n_intake `escalated: no_code` after fixture already advanced stage |
| 4 | **manifest_not_in_ledger_sections** | requirements/build_nodes empty; models thrash sections |
| 5 | **ledger_note_decision_id_keyerror** | kind=decision/dimension → `ledger write failed: 'id'` (9+ fails SPECIFY/BUILD); kind=note works |
| 6 | **ledger_get_section_events** | DELIVER DL3 `section=events` unknown |
| 7 | **partial SPECIFY + soft-join** | 2/5 SPECIFY done; fan-in **no concurrent crash**; soft advance to DELIVER/PLAN |
| 8 | **ledger_note_kind_verdict** | kind=`verdict` / `review_verdict` unknown (DELIVER DL5, VERIFY/DELIVER DL1/2) |
| 9 | **provider_read_timeout_storm** | 7× retryable `The read operation timed out` under parallel nodes → false “budget exhausted” |
| 10 | hard_guardrail ×28 | all `outcome=passed` (battery not required) — noise, not defect (Fixer2) |
| 11 | **ledger_get_section_plan/acceptance** | PLAN/DELIVER unknown sections `plan`, `acceptance` |
| 12 | **new_part_bootstrap_missing** | BUILD/VERIFY/DELIVER: describe_part, add_features, list_world_bodies, describe_assembly, check_interference all fail without `new_part` first (classic C1/grok45 T1) |
| 13 | **dimension_provenance_required** | ledger_note dimension rejected without `provenance` field |
| 14 | **buildgraph_plan_empty_nodes** | `buildgraph_plan` ok=true but `nodes: []` + advisory `unknown_plan_shape` — plan_json shape not accepted into executable graph |

Fixer2 (FollowingTakin) landed fixes **1–7 mid-run** (see `fixes.md`, 49 tests);
live pid did not reimport. Expected clear on next mega launch.

## Subagent spawn stats

| Metric | mega-2 |
|--------|--------|
| Delegation root | 1 × `orchestrator` depth=0 auto-approved (`dg_0d2d445c1b73`, budget 50k) |
| Child spawns / joins | **0** |
| Max depth reached | 0 |
| Spawn-depth policy | active (max 8); never consulted beyond root |
| Parallel DAG role nodes | 5 SPECIFY + multi DELIVER/BUILD pairs concurrent |
| Org layer | ORGANIZE node started then timeout-failed; no department tree materialised |

**Conclusion:** infinitely-branching mode remains **wired but unexercised** by
the model (no `delegate` tool_calls). Parallelism is still **DAG-level**, not
DelegationTree children. BUILD “success” is hollow (no geometry).

## Token / latency profile

| | mega-2 |
|--|--------|
| Wall | **879.06 s** (~14.7 min) end-to-end |
| Ledger events | 220 |
| Stream events | 164 lines `events.jsonl` |
| tools_slot tokens (sum rendered) | ~15.8k across slots |
| tool_calls | 120 (ledger_get 62, ledger_note 32, ecad_find_part 9, …) |
| tool_call latency_ms (stream) | sum ~37 ms (local tools); provider turns dominate wall |
| Provider read timeouts | 7 |
| Successful model completions | multiple (SPECIFY DL2/3, DELIVER, BUILD, VERIFY DL2, …) |
| Effort | maximum / frontier (ledger `effort_profile`) |

## Harness enablement gaps (remaining after Fixer2 wave)

1. **Mission phase accounting vs DAG progress** — mission reports
   `phase=specify` / `specify_incomplete` while DAG completed BUILD+DELIVER
   nodes. Gate does not observe multi-DL partial success or soft-failed SPECIFY
   majority.
2. **Provider parallel timeout / concurrency** — 5-way SPECIFY + later pairs
   produce read timeouts; retryable but still burn node budgets as
   `budget exhausted`. Need per-provider concurrency limit, longer read timeout
   for xai-sub, or serialise heavy frontier nodes.
3. **BUILD bootstrap `new_part`** — still not injected; models call describe /
   add_features first (fingerprint 12). Same class as grok45-1 T1.
4. **buildgraph_plan contract** — accepts free-form plan_json but emits empty
   graph + `unknown_plan_shape`; PLAN cannot hand a real build graph to BUILD.
5. **ledger_note kind/section vocabulary** — Fixer2 aliases help; still missing
   `plan` / `acceptance` sections and first-class criterion_result surface for
   graders.
6. **fw/em/antenna resolve** — fixed in tree (Fixer2 #1) but unproven live until
   relaunch; SPECIFY DL4 died without firmware tools.
7. **Child spawn never triggered** — org layer + depth-8 rails idle; prompts /
   tools may not expose `delegate` clearly enough under auto autonomy.

## TOP 5 next tooling fixes (by unblock value)

| Rank | Fix | Unblocks | Evidence |
|------|-----|----------|----------|
| **1** | **Relaunch-verify Fixer2 wave** (fw/em resolve, ledger section all/events/manifest, decision id synth, plan/intake auto-complete, kind=verdict aliases) on a fresh mega-3 process | Clears fingerprints 1–8 on next run; this run was pre-import | `fixes.md`; live fails still show old errors |
| **2** | **xAI parallel read-timeout policy** — raise timeout and/or cap concurrent frontier agent_turns; map timeout storms to `provider_degraded` not node budget death | SPECIFY DL1/4/5, ORGANIZE, LAYOUT, PLAN mortality | 7× `The read operation timed out` → budget exhausted |
| **3** | **BUILD bootstrap: require/auto `new_part` before describe/measure/add_features** (tool hint + agent_turn preflight) | Mechanical DL1/DL2 ever produce geometry | BUILD/VERIFY/DELIVER “No current part” chain |
| **4** | **buildgraph_plan accept role/mission plan_json → non-empty nodes** (or adapter from deliverables manifest) | PLAN→BUILD handoff; empty graph advisory | `unknown_plan_shape`, `nodes: []` |
| **5** | **Mission phase gate vs DAG**: specify_incomplete should not ignore majority SPECIFY done + BUILD execution; surface partial multi-DL progress | Honest status / phases_completed; mega mortality metrics | mission `phase=specify` while `n_build_*`/`n_deliver_*` done |

Honorable: dimension `provenance` ergonomics; `ledger_get(section=acceptance|plan)`;
expose `delegate` in PLAN/ORGANIZE tools_slot; ECAD library STM32F4 part name
hints (`ecad_find_part('STM32F4')` empty vs `MCU` hit).

## Manual verification steps

1. Confirm auth: `provider_for_tier("xai:grok-4-5")` → XAISubProvider (done).
2. After Fixer2 modules are importable, re-run:
   `~/.local/share/AI-CAD/mcp-venv/bin/python reports/runs/mega-2/run_mega.py`
   (or mega-3 copy) — expect no `could not be resolved` for `fw_status`, no
   `section=all` errors, no decision `'id'` KeyError.
3. Expect `status.json` state=finished; ideally phases_completed beyond intake
   and at least one `new_part` + non-empty geometry for DL1.
4. Monitor: ledger + `progress.md` snapshots; Fixer2 IRC for new fingerprints.
5. openEMS/Renode legs must SKIP honestly when unprovisioned (DL5 already did).

## Deferred

- True multi-hour spawn-depth / token curves once BUILD creates parts and
  firmware tools resolve live.
- No source edits by MegaRun2 (AestheticGrouse) outside `reports/runs/mega-2/**`
  and roadmap status append.
- `run_mega.py` is report-local only.
