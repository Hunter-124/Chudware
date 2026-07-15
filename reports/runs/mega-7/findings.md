# MegaRun7 Findings

## Run summary

- **Run ID:** `mega-7-fc-rocket-20260714` (**attempt-2 primary**)
- **Baseline commit:** `84963165` (Fixer6 Fix 6–7 rebind; Fixer7 Fixes 1–4 bound on attempt-2)
- **Conservation config:** auto_pick-first; **no per-node frontier injection**; xAI only as fallback / where min_rank demands
- **Fallback provider:** SuperGrok live `XAISubProvider` / `grok-4.5`
- **Active SuperGrok account (attempt-2):** `17d17fb1-247a-4800-ba97-e7b5150bc53a` (fresh device-flow; `79066dcc-…` parked)
- **Org layer:** ON, max spawn depth 8; xAI semaphore=3
- **Wall time (attempt-2):** **382.98s** (~6m23s)
- **Mission result:** `status=running` / phase `deliver` / **`stop_reason=""`** (not `deliver_incomplete`); `phases_completed=["intake","build","deliver"]`
- **DAG result:** 19/19 nodes started; **9 done, 10 failed**; `dag.run.done` status `paused`
- **Primary evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `spawn_probe.json`, `auto_pick_smoke.json`, `subscription_accounts.json`, `progress.md`, `attempt-1-pre-fixer7-rebind/`
- **Fixer peer:** HomelessBadger (Fixer7) — Fixes 1–4 in source + `fixes.md` (32 pytest); bound on attempt-2 relaunch

### Attempt-1 (pre-Fixer7 rebind) — archived, not primary

- Pid 2101380, account `79066dcc-…`, wall ~5–10m (killed for relaunch)
- Preflight PASS; **Fix6 model_assignment ×12–21** already bound (all `task_class=unknown` → xai-sub)
- Mid-run: some `add_features` calls appeared under Fixer6 Fix7, but Fixer7 task_class / deliver soft-join / stronger nudges **not imported**
- Artifacts: `attempt-1-pre-fixer7-rebind/`

### Pre-flight (attempt-2): PASS

1. **`subscription_accounts.json`** — 2 xai-sub accounts; **active=`17d17fb1-…`** (newest); switched=False (already active)
2. **`spawn_probe.json` verdict PASS** — `spec_writer` / `layout_engineer` / `feature_modeler` have `delegate` + rendered `budget_tokens`
3. **`auto_pick_smoke.json` verdict PASS** — depth-2 low-difficulty → `ollama/llama3.2:latest` (non-xai)

---

## 1) DELIVER completion verdict: PASS (soft-join frontier flipped)

| Metric | mega-6 attempt-2 | mega-7 attempt-2 |
|---|---|---|
| Mission `stop_reason` | **`deliver_incomplete`** | **`""` (empty)** |
| Mission `status` | `blocked` | `running` (terminal driver exit; phase cursor on deliver) |
| `phases_completed` | `intake, specify, plan` | **`intake, build, deliver`** |
| Stage exit_gate deliver | none | **`mission_report_ledger_closed`** |
| `n_deliver_*` done | DL3/4/5 done (still incomplete freeze) | **DL1/3/4/5 done**, DL2 failed |
| Soft-join | missing | **Fixer7 Fix1 bound** |

- DELIVER is terminal fan-out with no join node. Fixer7 Fix1: **any** `n_deliver_*` done advances the phase.
- Live: four of five deliver workers `done` → phase completed; mission **did not** freeze on `deliver_incomplete`.
- Residual: mission `status` string stayed `running` (driver/loop bookkeeping quirk) rather than a clean `done`/`blocked`, but the **deliver_incomplete class is gone** — the owner frontier for this cycle.

**Verdict: PASS** — DELIVER soft-join holds; `deliver_incomplete` no longer the terminal stop_reason when any deliver worker succeeds.

---

## 2) Live delegation / spawns verdict: FAIL (still 0; mega-6 regression holds)

| Metric | mega-5 | mega-6 | mega-7 attempt-2 |
|---|---:|---:|---:|
| `delegate` tool calls | **2** | **0** | **0** |
| Child spawns | **1** (depth 1) | **0** | **0** |
| Max depth | **1** | **0** | **0** |
| Ledger `delegation` rows | — | root only | root only (`tokens_spent=0`) |

- Fixer7 Fix2 strengthened MUST-spawn language on role cards; tools_slot still renders `delegate` + `budget_tokens` (preflight PASS).
- Models never called `delegate` on any of 19 nodes. Max observed depth on `model_assignment` = **0**.
- Why (same structural trap as mega-6, prompts alone insufficient):
  1. Mission template nodes are **flat depth-0** — no forced fan-out edges for mechanical vs ECAD vs firmware.
  2. Productive roles that could spawn (`feature_modeler`, `layout_engineer`) spent budget on ledger thrash / failed geometry ops instead of `delegate`.
  3. Ollama leaves fail `no_code` with **0 tool calls** — never reach a spawn decision.
  4. Prompt preference is not a hard gate; without a scheduler-side “spawn or explain” requirement, SuperGrok still single-threads.

**Verdict: FAIL** — exposure + contract live; actual delegation still zero (no recovery from mega-6 regression).

---

## 3) Cheap-share % of tokens verdict: PASS (live >0; assignment flip strong)

### Preflight / smoke (unchanged PASS)

| depth | task_class | min_rank | model | cost tier |
|---:|---|---:|---|---:|
| 2 | simple / leaf / low | 1 | `ollama/llama3.2:latest` | 0 |
| 0 | architecture / high | 4 | `xai-sub/grok-4.5` | 2 |

### Live `agent.model_assignment` (Fix6+Fix4 bound) — **19/19 nodes**

| task_class | count | typical pick |
|---|---:|---|
| `simple_part` | 6 | ollama (DELIVER) |
| `leaf` | 5 | ollama (INTAKE / VERIFY / organize / overseer) |
| `multi_feature_part` | 3 | xai-sub (BUILD DL1, PLAN, SPEC DL1) |
| `assembly` | 3 | xai-sub (SPEC DL2, BUILD DL2, layout) |
| `analysis` | 2 | xai-sub (SPEC DL3/4) |

- **11/19 assignments → ollama** (57.9% of nodes) — first live depth-0 cheap routing of the mega series (mega-6 was 0/19).
- **8/19 assignments → xai-sub** for hard classes (correct conservation: not every node forced off SuperGrok).

### Token totals (from per-node `agent_result.usage.providers`)

| Provider/model | Completes (provider rows) | Prompt tok | Completion tok | Total tok | Share |
|---|---:|---:|---:|---:|---:|
| `xai-sub/grok-4.5` | **294** | 2,780,207 | 104,526 | **2,943,476** | **99.28%** |
| `openai/llama3.2:latest` (ollama) | **8** | 16,400 | 5,053 | **21,453** | **0.72%** |
| **Grand** | | | | **2,964,929** | 100% |

**Cheap share of live tokens: 0.72% (>0 target met).**

Caveats:

- Assignment share ≫ token share: ollama nodes mostly `no_code` in one short turn (~2–3k tok); SuperGrok nodes run long multi-complete loops (100k–400k tok each).
- Several nodes **assigned** ollama later show xai-sub usage rows (degrade path / multi-complete) — assignment event is the routing proof; tokens measure residual SuperGrok gravity.
- Still far from a healthy ≥20% cheap target, but the **live path is open** for the first time.

**Verdict: PASS** — live cheap-share **>0%** (0.72% tokens; 57.9% of model_assignment rows). Infrastructure + task_class stamp proven.

---

## 4) BUILD outputs verdict: PARTIAL PASS (`new_part`→`add_features` sequence; geometry still hollow)

| Artifact | mega-6 | mega-7 attempt-2 |
|---|---:|---:|
| `new_part` calls | 2 | **1** (`rocket_plane_airframe`, ok) |
| `add_features` calls | **0** | **13** (event-level ok=true) |
| Successful solid-producing `add_features` | 0 | **0** (all op payloads rejected) |
| Schematic / board / fw | 0 | 0 |

- Fixer7 Fix3 bound: BUILD DL1 sequence is **`new_part` → `add_features`×6** (no preflight thrash monopoly). Productive_tools includes `add_features`.
- Payload quality fail: every `add_features` result `ok:false` with `unknown op` (`sketch`, `cylinder`, `box`, `add_box`, `sketch_create`, `extrude`) — model inventing op names instead of engine vocabulary (`cad_guide` / real feature ops).
- Doc ids observed: `doc-cpp0000009b` (new_part), event targets also `doc-cpp000000a4` — solids remain non-product without accepted ops.
- BUILD DL2: ledger thrash only; no geometry mutators.

**Verdict: PARTIAL PASS** — sequence gate flipped (`add_features` after `new_part`); **solid geometry still hollow** (op vocabulary, not missing nudge).

---

## 5) Phase-gate honesty verdict: PASS (with soft-join nuance)

End-of-run stage_log (ledger):

1. `intake` → exit_gate **`manifest_valid`**
2. `specify` entered (live cursor); no clean success exit (several SPECIFY workers failed)
3. `plan` entered; no success exit (`n_plan` no_code)
4. `build` → exit_gate **`nodes_done_or_carded`**
5. `verify` entered; mixed (DL1 done, DL2 no_code)
6. `deliver` → exit_gate **`mission_report_ledger_closed`**

`phases_completed = ["intake","build","deliver"]`:

- **Honest about specify/plan/verify** not being fully closed.
- **Deliver listed because soft-join succeeded** (any deliver worker done) — intentional Fix1 semantics, not a false product claim.
- Live stage cursor advanced mid-run across all six phases (Fixer5 live cursor still bound).

**Verdict: PASS** — no false PLAN success; DELIVER completion is soft-join by design, not silent artifact forgery.

---

## 6) Token / latency + TOP 5

| Metric | Value |
|---|---|
| Wall | **382.98s** (−57% vs mega-6 889s; −73% vs mega-5 1439s) |
| Tool calls | **71** |
| Tool latency sum | 1,604ms |
| Tool median / p95 / max | 0.18ms / 15.9ms / **935ms** |
| Model assignments | **19** (all nodes; Fix6 telemetry bound) |
| Provider tokens | **2.96M** total (99.28% SuperGrok / **0.72% ollama**) |
| Delegation spent | 0 |
| DAG | 9 done / 10 failed |
| xAI semaphore | capacity=3 observed in driver log (queue_wait present; no timeout storm) |

### TOP 5 next

1. **Force or score actual `delegate`.** Prompt MUST is insufficient after two cycles at 0 calls. Add a soft gate on multi-domain PLAN/BUILD: if ≥2 independent slices and depth&lt;max, require `delegate` or an explicit `ledger_note` refusal with reason before auto-complete.
2. **`add_features` op vocabulary assist.** After `unknown op`, inject `cad_guide` / allowed-ops snippet; reject-loop should not burn the full 16-iter budget on invented names (`box`/`cylinder`/`sketch`).
3. **Raise cheap token share without starving hard work.** Ollama leaves dying `no_code` with 0 tools wastes the routing win — either (a) tiny tool floor for leaf roles, or (b) one-shot structured handoff template so leaves complete in ≤1k tok with a real envelope.
4. **Clean mission terminal status.** Soft-join deliver should land `status=done|blocked` with explicit `stop_reason` (`deliver_partial` vs empty/`running`), not driver-exit `running`.
5. **Keep Fix4 task_class stamps + Fix6 model_assignment telemetry.** Never regress to `task_class=unknown`/min_rank=3 on VERIFY/DELIVER/INTAKE; continue auditing assignment rows every mega cycle.

---

## Failure fingerprint (DAG attempt-2 final)

| Node | Result | Model (assignment) | Notes |
|---|---|---|---|
| n_intake | failed no_code | ollama leaf | 0 tools |
| n_spec_DL1 | done | xai multi_feature | ledger_note productive |
| n_spec_DL2 | done | xai assembly | |
| n_spec_DL3 | failed no_code | xai analysis | usage present; stop no_code |
| n_spec_DL4 | failed no_code | xai analysis | |
| n_spec_DL5 | failed no_code | ollama simple_part | |
| n_spec_done | failed no_code | ollama leaf | |
| n_organize | failed no_code | ollama leaf | |
| n_layout | failed max_iters | xai assembly | |
| n_plan | failed no_code | xai multi_feature | |
| n_build_DL1 | done | xai multi_feature | **new_part + add_features×6 (ops rejected)** |
| n_build_DL2 | done | xai assembly | ledger only |
| n_verify_DL1 | done | ollama leaf* | *usage shows xai completes (degrade) |
| n_verify_DL2 | failed no_code | ollama leaf | |
| n_deliver_DL1 | done | ollama simple* | soft-join contributor |
| n_deliver_DL2 | failed no_code | ollama simple | |
| n_deliver_DL3–5 | done | ollama simple* | soft-join contributors |

\* assignment ollama; provider usage rows include xai-sub (degrade / multi-turn).

---

## Fixer7 mid/post-run (source landed + attempt-2 bound)

See `reports/runs/mega-7/fixes.md` (HomelessBadger):

1. **DELIVER soft-join any-done** — **bound; PASS this run**
2. **Delegate MUST on role cards** — bound; **unexercised (0 calls)**
3. **Harder new_part→add_features thrash block** — **bound; sequence PASS, solids FAIL**
4. **task_class stamp on mission nodes** — **bound; cheap-share path PASS**

Fixer6 Fix6 (model_assignment on node resolve) also confirmed bound (19 events).

---

## Axis scoreboard (mega-7 vs mega-6)

| Axis | mega-6 attempt-2 | mega-7 attempt-2 | Flip? |
|---|---|---|---|
| **DELIVER completion** | `deliver_incomplete` freeze | **soft-join; deliver in phases_completed; stop_reason empty** | **FLIP PASS** |
| actual_delegation | 0 / 0 / depth 0 | **0 / 0 / depth 0** | Hold FAIL |
| cheap-share tokens | **0%** | **0.72% live** (+57.9% ollama assignments) | **FLIP PASS (>0)** |
| BUILD outputs | new_part×2, add_features×0 | **new_part×1, add_features×13 (0 accepted ops)** | **Partial flip** |
| phase-gate honesty | intake+specify+plan; live cursor | intake+build+deliver; live cursor; soft-join explicit | Hold PASS |
| model_assignment telemetry | 0 events (unbound) | **19/19 events + task_class** | **FLIP PASS** |
| SuperGrok wall | 889s / 2.55M tok | **383s / 2.96M tok** | Wall win; spend similar |
| budget_tokens contract | PASS preflight | PASS preflight | Hold |
