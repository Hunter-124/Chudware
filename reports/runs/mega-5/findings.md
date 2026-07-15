# MegaRun5 Findings

## Run summary

- **Run ID:** `mega-5-fc-rocket-20260714`
- **Baseline commit:** `4f98f625` (first process where all Fixer4 fixes bind)
- **Provider:** SuperGrok live `XAISubProvider` / `grok-4.5`, tier `frontier`, effort `maximum`
- **Org layer:** ON, max spawn depth 8
- **Wall time:** **1,438.65s** (23m59s)
- **Mission result:** `blocked` / phase `deliver` / `stop_reason=deliver_incomplete`
- **`phases_completed`:** `["intake"]` only
- **DAG result:** 19/19 nodes started; **8 done, 11 failed**; `dag.run.done` status `paused`
- **Primary evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `spawn_probe.json`, `progress.md`
- **Fixer peer:** AllegedDeer (Fixer5) landed Fixes 1–7 mid-run in source + `fixes.md` (84 tests); Python binds require a **fresh process**

## Pre-flight: PASS (Fixer4 contracts live)

1. **Delegate exposure:** `spawn_probe.json` verdict **PASS** — `spec_writer`, `layout_engineer`, `feature_modeler` each have `delegate` in raw allowlist **and** concrete tools. Live tools slots confirmed delegate on SPECIFY×5, ORGANIZE, PLAN layout, PLAN mission, BUILD DL1/DL2.
2. **`budget_tokens` live in RENDERED prompts:** all three required roles' rendered tool schemas include  
   `delegate(role: str = '', brief: str = '', budget_tokens: int = 0, …)`.  
   `orchestrator` + `spec_writer` system prompts name `delegate(role, brief, budget_tokens)`.  
   Dispatch signature: `MissionDispatch.delegate(..., budget_tokens: int = 0, ...)`.
3. **`feature_modeler` has `new_part`** (Fixer4 bootstrap) — BUILD DL1 tools slot rendered `new_part`.

---

## 1) Delegation verdict: PARTIAL PASS (first real spawn)

| Metric | Value |
|---|---|
| `delegate` tool calls | **2** (both from BUILD DL2) |
| Child spawns | **1** (`dg_7486c9fcf0bb`, role `part_fixer`) |
| Max depth | **1** (root depth 0 + one child) |
| Root grant | 50,000 tokens, 0 spent |
| Child grant | 8,000 tokens, **0 spent** |

**Call 1:** `role=part_fixer`, `budget_tokens=8000`, brief **1048 chars** →  
`status=failed`, `error=brief must be ≤500 chars (contract §12)`.  
**Correct contract enforcement** — `budget_tokens` kwarg was accepted (Fixer4 Fix 6 signature fix held).

**Call 2:** shortened brief (~280 chars), `budget_tokens=8000` →  
`status=approved`, `executed=true`, `subagent_status=done`, `handoff=null`.  
Lifecycle: pending → approved(auto) → running → done (~7s wall; tool latency 7174.69ms).  
Child tools attempted: `skill_activate` (unresolved), `list_tools` (unresolved), `cad_guide`×2 (ok). **No `new_part` / `add_features`.** Tokens spent recorded as 0; empty model `{provider,id,tier}`.

**Join:** parent received structured result (`ok=true`, `executed=true`, `handoff=null`, partial `result.calls`). Parent then continued and auto-completed at max_iters without assembly geometry.

**vs mega-4:** mega-4 had **0** delegate calls / depth 0. Mega-5 flips actual_delegation from zero to non-zero, but spawn quality is still weak (unknown role `part_fixer`, no CAD mutations, 0 token accounting, no model_assignment row).

**Verdict: PARTIAL PASS** — exposure + `budget_tokens` + one depth-1 spawn/join work; productive multi-domain fan-out still absent.

---

## 2) Auto model assignment verdict: FAIL (not exercised / frontier-everywhere)

Required per-spawn table:

| depth | task_class | min_rank | picked model | cost tier |
|---:|---|---:|---|---|
| 1 | *(unset)* | *(unset)* | *(empty provider/id/tier)* | unknown |

- **0** `agent.model_assignment` / ledger `model_assignment` / `role_model` / `task_class` / `min_rank` / `auto_pick` events.
- All **19** tools_slot records: `tier=frontier` (SuperGrok).
- Child spawn model fields blank; no cheap leaf routing.

**Root cause (this process):** `run_mega.py` injects `provider_for_tier("xai:grok-4-5")` and pre-Fixer5 `node_runner` preferred the injected provider. Fixer5 Fix 4–5 (assignment without `ai_roles`, auto_pick-first over injected frontier, ollama+openrouter free) landed **after import** and need a fresh process.

**Owner priority — SuperGrok spend + which nodes could have been cheaper**

Provider input/output token usage and $ cost were **not recorded** (telemetry gap persists). Observable proxies:

| Proxy | Value |
|---|---|
| Wall on SuperGrok | 1,438.65s of frontier `complete_ex` |
| Tools-slot render tokens | **20,085** across 19 slots (median 794; SPECIFY slots **2,284 each** / 41 tools) |
| Role turns on frontier | **19/19** DAG nodes + 1 child subagent |
| Explicit xAI read timeouts | **7** × ~180s-class failures (see §5) |
| Delegation budget spent | 0 / 58,000 granted |

**Nodes that should NOT have burned SuperGrok** (leaf/simple; auto_pick min_rank should prefer openrouter free / ollama):

| Node | Why cheaper |
|---|---|
| INTAKE | Fixture-backed manifest confirm; low difficulty |
| SPECIFY DL5 (report card) | Report scaffolding / ledger notes |
| ORGANIZE | Department instantiation |
| VERIFY DL1, VERIFY DL2 | Acceptance checks / ledger reads |
| DELIVER DL1–DL5 | Report packaging leaves |
| Child `part_fixer` depth-1 | Leaf geometry worker if role were real |

**Nodes that may legitimately stay frontier/hard** (still should not all run in parallel unbounded):

| Node | Why hard |
|---|---|
| SPECIFY DL1–DL4 | Multi-domain specs |
| PLAN layout / PLAN mission DAG | Architecture + buildgraph |
| BUILD DL1 / BUILD DL2 | Geometry / assembly creation |
| SPECIFY join (`n_spec_done`) | Overseer audit |

**Future-run rule (owner):** only top-level/hard nodes bind `xai:grok-4-5`; children route via auto assignment with openrouter free + local tiers enabled. Fixer5 Fix 5 implements this; **mega-5 evidence process did not bind it**.

**Verdict: FAIL** — assignment unobserved; inverse cost mismatch (frontier on every leaf) pervasive.

---

## 3) BUILD outputs verdict: FAIL (zero product)

| Artifact | Count |
|---|---:|
| `new_part` calls | 0 |
| `add_features` calls | 0 |
| Schematic / board create | 0 |
| `fw_new_project` / `fw_build` | 0 |
| Ledger features | 0 |
| Ledger build_nodes | 0 |
| Geometry specs | 0 |
| Product artifacts | 0 (only intake manifests) |

- BUILD DL1 tools slot **had** `new_part` + `delegate` but ended `no_code` / budget exhausted (textual turn, no mutating tools).
- BUILD DL2 “done” via max_iters auto-complete after ledger thrash + failed/empty child spawn; `list_world_bodies` / `describe_assembly` → `No current part. Call new_part(...) first`.
- VERIFY DL2 AC3: `NOT-CHECKED` — no assembly doc.
- SPECIFY made 15× `ecad_find_part` + 1× `ecad_antenna_design` but never a schematic/board/BOM artifact.

**Verdict: FAIL** — no part, board, or firmware created.

---

## 4) Phase-gate honesty verdict: PARTIAL PASS (no false completion; still not live)

End-of-run `stage_log`:

1. `intake` exited `manifest_valid` (start)
2. `specify` / `plan` / `build` / `verify` / `deliver` **entered** at `2026-07-15T00:01:50Z` (≈ post-`dag.run.done`) with **`exit_gate: null`**, **`exited: null`**

Mission `phases_completed = ["intake"]` only — **did not** claim PLAN/BUILD success despite DAG visiting those nodes. This is the Fixer4 Fix 5 win vs mega-4 (which falsely completed PLAN/BUILD).

However:

- During the live ~24m run, stage_log stayed **INTAKE-only** until terminal reconciliation (Fixer5 Fix 6 live cursor was **not** bound in this process).
- Cursor-only enter without success gates is honest about non-completion, but mission control still looked stuck mid-run.

**Verdict: PARTIAL PASS** — false-completion fixed; live progress still missing in this process.

---

## 5) xAI timeout behavior under fan-out: FAIL (still storms; no retry bind)

**7** ledger `provider_error: The read operation timed out`:

| Actor | Outcome |
|---|---|
| SPECIFY DL1 | failed / budget exhausted |
| SPECIFY DL2 | failed / budget exhausted |
| SPECIFY DL4 | failed / budget exhausted |
| DELIVER DL3 | failed / budget exhausted |
| SPECIFY complete (`n_spec_done`) | failed / budget exhausted |
| PLAN layout | failed / budget exhausted |
| PLAN mission DAG | failed / budget exhausted |

Also: SPECIFY DL3 `max_iters`; ORGANIZE/BUILD DL1 `no_code`.

- No semaphore occupancy / queue telemetry in events (Fixer5 Fix 3 unbound).
- No in-provider read-timeout retry observed; retryable failures still escalated to node death (old mapping).
- Strong correlating fingerprint: SPECIFY tools slots rendered **41 tools / 2,284 tokens** each under 5-way fan-out (Fixer5 Fix 7 caps at 14 — unbound here).

**Verdict: FAIL** — fan-out still produces multi-node ~180s read-timeout deaths; observability/retry/cap need fresh process.

---

## 6) Token / latency

| Metric | Value |
|---|---|
| Wall | **1,438.65s** |
| Tool calls | **127** |
| Tool latency sum | 7,269.62ms |
| Tool median / p95 / max | 0.2ms / 1.99ms / **7,174.69ms** (delegate child execution) |
| Tools-slot render tokens | 20,085 (19 slots) |
| Provider usage tokens / $ | **not recorded** |
| Delegation spent | 0 |
| Notable node walls | intake 103s; SPECIFY DL5 398s; DELIVER DL4 233s; DELIVER DL5 269s; BUILD DL2 43s; DELIVER DL1 80s |

Node latency dominated by model waits and timeouts, not tool execution.

---

## Failure fingerprint (DAG)

| Node | Result |
|---|---|
| n_intake | done (103s) |
| n_spec_DL1/2/4 | failed — provider read timeout → budget exhausted |
| n_spec_DL3 | failed — max_iters |
| n_spec_DL5 | done (398s, auto-complete) |
| n_spec_done | failed — read timeout |
| n_deliver_DL3 | failed — read timeout |
| n_deliver_DL4/5 | done (report/blocked honesty) |
| n_organize | failed — no_code |
| n_layout / n_plan | failed — read timeout |
| n_build_DL1 | failed — no_code |
| n_build_DL2 | done (auto-complete; no geometry) |
| n_verify_DL1 | failed — max_iters |
| n_verify_DL2 | done (AC3 NOT-CHECKED) |
| n_deliver_DL1/2 | done (empty product reports) |

---

## Fixer5 mid-run (source landed; process unbound)

See `reports/runs/mega-5/fixes.md` (AllegedDeer):  
(1) all delegate prompts name `budget_tokens`  
(2) `agent.model_assignment` on spawn  
(3) xAI concurrent=3, semaphore_stats, read-timeout retry, retryable→DAG retry  
(4) assignment without config `ai_roles`  
(5) auto_pick-first / SuperGrok quota (cheap leaves)  
(6) live stage_log cursor on dag.node.*  
(7) SPECIFY concrete_tools cap ≤14 with family round-robin  

YAML may rebind per `load_role`; **Python requires relaunch**.

---

## TOP 5 next

1. **Relaunch with Fixer5 bound + SuperGrok quota policy.** Fresh process only; inject frontier only as fallback. Prove tools_slot ≤14 on SPECIFY, live stage_log during SPECIFY, retryable timeout → retry, and non-xai `model_assignment` on leaves.
2. **Make delegation productive.** Prefer known roles (`feature_modeler`/`spec_writer`/…) over invented `part_fixer`; surface brief≤500 earlier in schema/prompt; require child tool allowlist that includes `new_part`/`add_features`; emit model+tokens_spent honestly on join.
3. **Close assignment telemetry + cost routing.** Every spawn emits `{depth, task_class, min_rank, picked_model, cost_tier}`; alert when leaves take frontier or hard nodes take rank-0.
4. **xAI fan-out hardening.** Keep semaphore stats + bounded read-timeout retry; reduce parallel frontier complete_ex (cap concurrent hard nodes); record provider usage tokens/$ on every response/error.
5. **BUILD artifact gate.** Do not mark BUILD/VERIFY/DELIVER “done” without `new_part`/board/fw evidence; gate VERIFY on real docs; stop max_iters auto-complete from counting as product success.

---

## Axis scoreboard (mega-5 vs mega-4 open verdicts)

| Axis | mega-4 | mega-5 | Flip? |
|---|---|---|---|
| actual_delegation | 0 calls / depth 0 | **2 calls / 1 spawn / depth 1** | **Partial flip** |
| auto model assignment | not exercised | still not exercised (frontier×19) | No |
| BUILD outputs | 0 parts/boards/fw | 0 parts/boards/fw | No |
| phase-gate honesty | false PLAN/BUILD complete | **intake-only completed; no false exits** | **Honesty flip; live still lag** |
| xAI timeout under fan-out | 4× ~180s | **7×** timeouts; no retry | No (worse count) |
| token/latency + TOP5 | recorded | recorded + SuperGrok spend section | Yes (docs) |
