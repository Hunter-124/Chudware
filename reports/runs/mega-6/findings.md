# MegaRun6 Findings

## Run summary

- **Run ID:** `mega-6-fc-rocket-20260714` (attempt-2 primary evidence)
- **Baseline commit:** `563fb2c0` (first process where full Fixer5 wave binds)
- **Conservation config:** auto_pick-first; **no per-node frontier injection**; xAI only as fallback / where min_rank demands
- **Fallback provider:** SuperGrok live `XAISubProvider` / `grok-4.5` (not forced onto every node)
- **Org layer:** ON, max spawn depth 8; xAI semaphore=3
- **Wall time (attempt-2):** **888.68s** (14m49s)
- **Mission result:** `blocked` / phase `deliver` / `stop_reason=deliver_incomplete`
- **`phases_completed`:** `["intake", "specify", "plan"]` (build/verify/deliver entered live; success gates not closed)
- **DAG result:** 19/19 nodes started; **17 done, 2 failed**; `dag.run.done` status `paused`
- **Primary evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `spawn_probe.json`, `auto_pick_smoke.json`, `progress.md`, `attempt-1-openrouter-auth-wipe/`
- **Fixer peer:** StaticOwl (Fixer6) — Fixes 1–7 in source + `fixes.md` (14 tests); Fix6–7 need **fresh process** for rebind

### Attempt-1 (auth wipe — archived)

- Wall **3.91s**, **19/19** nodes `provider_auth` (keyless openrouter free auto-picked at min_rank≥3 → HTTP 401)
- Artifacts: `reports/runs/mega-6/attempt-1-openrouter-auth-wipe/`
- Drove Fixer6 Fix 1 (keyless openrouter skip) + driver `enabled_providers=[ollama, xai-sub]`

### Pre-flight: PASS

1. **`spawn_probe.json` verdict PASS** — `spec_writer` / `layout_engineer` / `feature_modeler` have `delegate` in raw allowlist **and** concrete tools; **all three** rendered tool schemas include `budget_tokens` in `delegate(...)`.
2. **`auto_pick_smoke.json` verdict PASS** — depth-2 low-difficulty → **`ollama/llama3.2:latest` (non-xai)**; depth-0 hard → `xai-sub/grok-4.5` (after keyless openrouter exclusion).

---

## 1) Delegation verdict: FAIL (regression vs mega-5)

| Metric | mega-5 | mega-6 attempt-2 |
|---|---:|---:|
| `delegate` tool calls | **2** | **0** |
| Child spawns | **1** (depth 1) | **0** |
| Max depth | **1** | **0** |
| Join handoff content | null / empty (mega-5) | n/a |

- SPECIFY / PLAN / BUILD tools slots **did** render `delegate` (SPECIFY 601 tok / 14 tools incl. delegate; layout 650 tok with delegate; BUILD DL1 590 tok with `new_part`+`add_features`+`delegate`).
- Model never called `delegate`. No spawn, no join, no handoff-content proof path this run.
- Fixer6 Fix 2 (join handoff synth + real tokens) is in tree but **unexercised** (no spawn).

**Verdict: FAIL** — exposure + `budget_tokens` contract still live; actual delegation regressed from mega-5's partial flip to zero.

---

## 2) Auto model assignment verdict: PARTIAL PASS (routing works; telemetry unbound)

### Preflight / smoke table (live `auto_pick_smoke.json`)

| depth | task_class | min_rank | model | cost tier |
|---:|---|---:|---|---:|
| 2 | simple | 1 | `ollama/llama3.2:latest` | 0 (local) |
| 2 | leaf | 1 | `ollama/llama3.2:latest` | 0 |
| 2 | (none)/risk=low | 1 | `ollama/llama3.2:latest` | 0 |
| 0 | architecture / high | 4 | `xai-sub/grok-4.5` | 2 (sub) |
| 0 | (none) | 3 | `xai-sub/grok-4.5` | 2 |

### Live DAG assignment table (attempt-2)

| node | depth | task_class | min_rank | model | cost tier |
|---|---:|---|---:|---|---:|
| all 19 DAG nodes | 0 | *(unset)* | ≥3 (default) | **`xai-sub/grok-4.5`** (provider_usage) | 2 |

- **0** `agent.model_assignment` event / ledger rows (Fixer6 Fix 6 emits on every `_resolve_provider` — **process unbound**, pre-Fix6 import).
- **192** `provider_usage` rows, **all** `xai-sub/grok-4.5` (Fixer6 Fix 3 **bound** — first auditable SuperGrok spend).
- tools_slot `tier` field is `null` (no longer forced `frontier` string) but every complete was SuperGrok.

**Why leaves stayed on SuperGrok (conservation truth):** mission template nodes run at **depth 0**. `min_rank_for_difficulty(depth=0)` = **3**. Ollama `llama3.2` is rank **1** → filtered out. With keyless openrouter excluded, only `xai-sub` rank 5 remains. Cheap routing is **correct for depth≥2** (smoke PASS) but **no depth≥2 work was spawned**.

### Grok vs cheap token split (attempt-2)

| Provider/model | Completes | Prompt tok | Completion tok | Total tok |
|---|---:|---:|---:|---:|
| `xai-sub/grok-4.5` | **192** | 2,340,378 | 157,800 | **2,545,663** |
| ollama / openrouter free | **0** | 0 | 0 | **0** |

**Cheap share of live tokens: 0%.** Conservation infrastructure works; exercise path (delegate depth≥2 or leaf min_rank on VERIFY/DELIVER) did not fire.

### Grok spend delta vs mega-5

| Proxy | mega-5 | mega-6 attempt-2 | Δ |
|---|---:|---:|---:|
| Wall | 1,438.65s | **888.68s** | **−550s (−38%)** |
| Explicit xAI read timeouts | **7** | **0** | **−7** |
| DAG nodes done / failed | 8 / 11 | **17 / 2** | +9 done / −9 fail |
| Provider usage tokens recorded | **0** (unobserved) | **2,545,663** | first measurement |
| Tools-slot SPECIFY tokens | **2,284** / 41 tools | **601** / 14 tools | **−74% slot size** (Fixer5 tools cap bound) |
| Tools-slot total (19 slots) | 20,085 | ~10.5k (sum of slot tokens) | lower |
| Role completes on SuperGrok | 19/19 + 1 child | 19/19 (no child) | still all depth0 xAI |
| Openrouter 401 wipe | n/a | attempt-1 only (fixed) | — |

Wall and timeout deltas are the clearest SuperGrok conservation wins this cycle (semaphore=3 + tools cap + no 180s death spiral). Absolute SuperGrok token spend is now **measured** (~2.55M) vs mega-5 unknown; remaining waste is **depth-0 min_rank policy**, not injection of frontier provider (injection is gone).

**Verdict: PARTIAL PASS** — auto_pick-first + non-xai leaf smoke PASS; live DAG still 100% SuperGrok at depth 0; model_assignment telemetry unbound this process.

---

## 3) BUILD outputs verdict: PARTIAL PASS (first `new_part` of mega series; still hollow)

| Artifact | mega-5 | mega-6 |
|---|---:|---:|
| `new_part` calls | 0 | **2** (`DL1_skeleton_rocket_plane` doc-cpp0000008c; `rocket_plane_airframe` doc-cpp00000091) |
| `add_features` calls | 0 | **0** |
| Schematic / board create | 0 | 0 |
| `fw_new_project` / `fw_build` | 0 | 0 |
| Ledger geometry features | 0 | 0 |
| Bodies on airframe docs | — | **0** (empty solids) |

- BUILD DL1 tools slot **had** `new_part` + `add_features` + `preflight_ops` (Fixer5/6 enablement). Sequence: `new_part` ok → `preflight_ops`×7 (ops planned cylinders/boxes) → **max_iters auto-complete without `add_features`**.
- PLAN layout also called `new_part` (skeleton) + preflight thrash.
- BUILD DL2 assembly role: no mutators in allowlist; ledger thrash + empty scene.
- VERIFY DL1: AC1 **FAIL** — empty solid, 0 bodies; VERIFY DL2 AC3 **NOT-CHECKED** / blocked.
- Fixer6 Fix 7 (nudge add_features; empty solid ≠ auto-done) landed **after** this process — needs rebind.

**Verdict: PARTIAL PASS** — first real `new_part` bootstrap of the mega series; still **no solid geometry / board / firmware**.

---

## 4) Phase-gate honesty verdict: PASS (live cursor + honest incomplete)

End-of-run stage_log (success exits):

1. `intake` → exit_gate `manifest_valid`
2. `specify` → exit_gate `spec_audited_criteria_covered` (terminal reconciliation)
3. `plan` → exit_gate `graphs_valid_ratified`
4. `build` / `verify` / `deliver` **entered** (live mid-run + terminal) with **no success exit_gate**

Mission `phases_completed = ["intake", "specify", "plan"]` — **did not** claim build/verify/deliver success despite DAG visiting those nodes. Live stage_log advanced during SPECIFY/PLAN (Fixer5 live cursor **bound**), unlike mega-5 intake-only mid-run.

**Verdict: PASS** — no false completion of unfinished product phases; live progress visible.

---

## 5) xAI timeout under semaphore=3: PASS

| Metric | mega-5 | mega-6 attempt-2 |
|---|---:|---:|
| Read-timeout provider_errors | **7** | **0** |
| `provider_auth` deaths | 0 | 0 (attempt-2); 19 (attempt-1 only) |
| Semaphore capacity | unbound / unobserved | **3** (`AICAD_XAI_MAX_CONCURRENT=3`) |
| SPECIFY tools_slot size | 41 tools / 2284 tok | **14 tools / 601 tok** |

Five-way SPECIFY fan-out completed 4/5 without timeout (DL5 `no_code` only). No 180s-class read-timeout storms.

**Verdict: PASS** — fan-out no longer kills the mission on xAI read timeouts under semaphore=3 + tools cap.

---

## 6) Token / latency + TOP 5

| Metric | Value |
|---|---|
| Wall | **888.68s** |
| Tool calls | **170** |
| Tool latency sum | 6,495.91ms |
| Tool median / p95 / max | 0.15ms / 3.89ms / **4,105ms** |
| Provider usage | **192** completes; **2,545,663** total tokens (all SuperGrok) |
| Delegation spent | 0 |
| Notable node walls | intake 37s; SPECIFY DL2 217s; n_spec_done 124s; PLAN layout 115s; BUILD DL2 178s; BUILD DL1 107s |

### TOP 5 next

1. **Relaunch for Fixer6 Fix 6–7 bind.** Prove `agent.model_assignment` on every DAG node; prove `new_part` → `add_features` (no empty-solid auto-done).
2. **Exercise cheap leaves for real.** Either (a) force/nudge depth≥2 `delegate` on BUILD/SPECIFY, or (b) lower min_rank / set leaf `task_class` on VERIFY/DELIVER/INTAKE so auto_pick selects ollama without waiting for spawns. Target: ≥20% of completes non-xai.
3. **Productive BUILD gate.** Require at least one successful `add_features` (or board/fw create) before BUILD node can mark done; stop counting max_iters auto-complete as product success.
4. **Restore actual delegation.** mega-5 had 2 calls / 1 spawn; mega-6 had 0 — reinforce role prompts + allowlist for known roles (`feature_modeler`, not invented roles); keep Fix 2 join handoff synth ready for the next spawn.
5. **Keep openrouter key-gate + usage telemetry.** Never regress attempt-1 wipe; continue recording provider_usage every complete so SuperGrok $ / tokens stay auditable.

---

## Failure fingerprint (DAG attempt-2)

| Node | Result |
|---|---|
| n_intake | done (37s) |
| n_spec_DL1–4 | done (83–217s) |
| n_spec_DL5 | failed — no_code |
| n_spec_done | done (124s, audit gaps) |
| n_deliver_DL3–5 | done (honest FAIL/blocked reports) |
| n_organize | failed — no_code |
| n_layout / n_plan | done |
| n_build_DL1 | done auto-complete — **new_part only**, 0 add_features |
| n_build_DL2 | done auto-complete — no geometry |
| n_verify_DL1/2 | done — AC FAIL / NOT-CHECKED |
| n_deliver_DL1/2 | done — empty product honesty |

---

## Fixer6 mid/post-run (source landed)

See `reports/runs/mega-6/fixes.md` (StaticOwl):  
(1) keyless openrouter skip + construction refuse  
(2) join handoff synth + real tokens  
(3) provider_usage ledger (bound this process — 192 rows)  
(4) BUILD tool floor ≥8 + new_part seed  
(5) ollama → openrouter/xai degrade  
(6) model_assignment on every node_runner resolve (**needs rebind**)  
(7) add_features nudge; empty solid ≠ auto-done (**needs rebind**)

---

## Axis scoreboard (mega-6 vs mega-5)

| Axis | mega-5 | mega-6 attempt-2 | Flip? |
|---|---|---|---|
| actual_delegation | 2 calls / 1 spawn / depth 1 | **0 / 0 / 0** | **Regress** |
| auto model assignment | frontier×19, 0 events | smoke PASS non-xai leaf; live still xai×19; 0 events | **Partial** (routing path proven) |
| BUILD outputs | 0 new_part | **2 new_part, 0 add_features** | **Partial flip** |
| phase-gate honesty | intake-only completed; live lag | **intake+specify+plan; live cursor** | **Improve** |
| xAI timeout under fan-out | 7× | **0×** under sem=3 | **Flip PASS** |
| SuperGrok spend | unobserved; 1439s wall | **2.55M tok measured; 889s wall (−38%)** | **Measurable + wall win** |
| budget_tokens contract | PASS preflight | PASS preflight | Hold |
| tools_slot SPECIFY | 41 tools / 2284 tok | **14 tools / 601 tok** | **Cap bound** |
