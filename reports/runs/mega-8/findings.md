# MegaRun8 Findings

## Run summary

- **Run ID:** `mega-8-fc-rocket-20260714`
- **Baseline commit:** `7ef7b577` (cycle-8 wave) + Fixer8 Fixes 1–11 mid-cycle
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Engine note (critical for axis A):** attempt-1–3 ran against a **stale engine daemon** predating cycle-8 registry. Main rebuilt OCCT tree + replaced daemon ~22:20; **attempt-4 reconnected mid-run** to the fixed engine — first live accepted solids.
- **Primary process:** attempt-4 pid **2668492**, wall **693.46s**
- **Fixer peer:** CollectiveElk (Fixer8) — `fixes.md` Fixes 1–11
- **Evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `status.json`, preflight JSONs, attempt archives, `progress.md`

### Pre-flight: PASS

1. Account **`17d17fb1-…`** active (2 xai-sub accounts)
2. **`spawn_probe` PASS** — PLAN/layout **no** `add_features`/`new_part`; **with** `delegate`; leaf keeps exec
3. **`auto_pick_smoke` PASS** — depth-2 low → `ollama/llama3.2:latest`

### Attempt map

| # | Pid | Bound | Wall | Terminal | Archive |
|---|---:|---|---:|---|---|
| 1 | 2410582 | baseline only | ~6m38s killed | incomplete | `attempt-1-pre-fixer8-rebind/` |
| 2 | 2453640 | Fix 1–3 | 926s | `needs_correction` / verify_failed ACs | `attempt-2-pre-fix4-5/` |
| 3 | 2544922 | Fix 1–5 | 1003.84s | `needs_correction` / verify_failed ACs | `attempt-3-fix4-5/` |
| **4** | **2668492** | **Fix 1–10 + live engine** | **693.46s** | `failed` / `dag_error: string or blob too big` | **primary OUT** + `attempt-4-engine-rebind/` |

---

## Axis scoreboard (honest — attempt-4 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **A** accepted ops + vocab | **PASS** | **`add_features` accepted batches ×5** after `new_part`×2; real ops `add_feature`+`cylinder`/`shell`/`cone` on `doc-cpp00000002` / `doc-cpp0000000a`; revisions/body_count advanced; volume mentions in ledger. One rejected cone (param shape) then retry ok — teaching path via Fix10 nudge + live engine. Cheatsheet inject still 0 (no invented top-level op name this run). |
| **B** delegate / typed refuse | **PASS** | **delegate×1** + **spawn×1**; `n_layout`/`n_plan` still `delegate_or_typed_refusal_required` (gate live; typed-refusal success envelope still unused). SPECIFY not false-gated. |
| **C** oneshot + miss | **PASS** | **`leaf_oneshot_ok`×10**, **`leaf_oneshot_miss`×1** (Fix4/6 working — was ×8 miss on a2/a3). |
| **D** terminal stop_reason | **PASS** | `status=failed`, **`stop_reason=dag_error: string or blob too big`** — explicit, not `running`+empty. Residual harness SqliteSaver blob limit (Fix11 landed post-run). |

---

## A) add_features accepted ops: PASS (attempt-4)

### Root cause of a1–a3 FAIL

Stale engine daemon lacked cycle-8 op registry in-process. Models that invented `op:"cylinder"` got thin `unknown op` without closest suggestions; accepted solids impossible regardless of middleware.

### attempt-4 live (post daemon rebuild + Fix10 re-nudge)

| Call | Result |
|---|---|
| `new_part` rocket airframe docs | ok (`doc-cpp00000002`, `doc-cpp0000000a`) |
| `add_feature` cylinder `body_outer` r=25 h=420 | **ok** |
| `add_feature` cylinder bore cut | **ok** |
| `add_feature` shell thickness 2 | **ok** |
| `add_feature` cone (bad params radius/top_radius) | **ok:false** validation (not unknown-op) |
| `add_feature` cone r1/r2 retry | **ok** |
| `add_feature` cylinder BodyTube_OD | **ok** |

- Event-level: add_features **ok×5 / fail×1**
- **Accepted solid-producing ops > 0** — contract met
- Op vocabulary teaching: model used correct `op: add_feature` + feature types (registry intent). Once-per-mission cheatsheet **not fired** (no unknown top-level op) — acceptable; inject path still unproven live but A axis is **accepted ops**, not inject alone.

**Verdict: PASS.**

---

## B) Capability separation: PASS

- Preflight: PLAN/layout cards lose exec tools, keep delegate.
- Live: **delegate×1**, **agent.spawn×1** (depth 1).
- Gate: layout + plan die `delegate_or_typed_refusal_required` when they free-form without refuse envelope (F9 scaffold present; models still don't emit typed_refusal successfully).
- Forcing function holds; escape-hatch success remains residual.

**Verdict: PASS.**

---

## C) Cheap-leaf oneshot: PASS

| Attempt | oneshot_ok | oneshot_miss |
|---:|---:|---:|
| 1 | 0 (TypeError) | many |
| 2–3 | 3 | 8 |
| **4** | **10** | **1** |

Miss telemetry still recorded; Fix1/4/6 stack collapsed miss rate.

**Verdict: PASS.**

---

## D) Terminal status: PASS (with residual crash class)

- a4: `failed` / `dag_error: string or blob too big` (LangGraph SqliteSaver node_states blob) — **explicit stop_reason**
- a2/a3: `needs_correction` / `verify_failed: [AC1…AC9]` also explicit
- Contract: **not** `running`+empty — satisfied
- `deliver_partial` not selected (crash/VERIFY paths won)
- Fix11 (compact agent_result) post-a4 for next process

**Verdict: PASS.**

---

## Token / conservation (attempt-4)

- model_assignment: **11 ollama / 9 xai**
- Wall 693s (faster than a2/a3)
- xAI semaphore capacity=3 observed in driver log
- oneshot_ok×10 proves cheap leaf path without thrashing tool loops

---

## TOP 5 residual

1. **Fix11 bind** — prevent `string or blob too big` crash after productive BUILD (compact node_states).
2. **Typed-refusal success** — gate kills layout/plan; still 0 successful refuse envelopes despite F9 scaffold.
3. **Prove cheatsheet inject live** — needs a deliberate unknown top-level op; ledger event `op_vocab_cheatsheet_inject` still 0.
4. **Rich unknown-op on live daemon** — verified by Main offline; re-confirm if model invents bare `cylinder` again.
5. **Product ACs** — solids exist but AC1–9 still not mission-complete (verify/correct path interrupted by blob error).

---

## Fingerprints

| ID | Status |
|---|---|
| F1–F6 | Fixed + bound by a4 |
| F7 thin unknown-op | Fixed in source; stale daemon was real a2 cause |
| F8 cheatsheet unobserved | Inject path ready; not exercised a4 (no unknown top-level op) |
| F9 typed-refusal unused | Scaffold bound; models still budget-die |
| F10 new_part→add_features skip | **Fixed live a4** (×5 add_features after new_part) |
| F11 SqliteSaver blob too big | **Landed post-a4** — needs relaunch to prove |

---

## Axis vs mega-7

| Axis | mega-7 | mega-8 a4 | Flip? |
|---|---|---|---|
| A accepted ops | 0 | **≥5 accepted** | **FLIP PASS** |
| B delegate | partial | **delegate+spawn+gate** | **FLIP PASS** |
| C oneshot | n/a | **ok×10 miss×1** | **FLIP PASS** |
| D stop_reason | running+empty | **explicit** (failed/dag_error) | **FLIP PASS** |

---

## Artifacts

```
reports/runs/mega-8/
  run_mega.py findings.md progress.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json  # attempt-4 primary
  attempt-1-pre-fixer8-rebind/
  attempt-2-pre-fix4-5/
  attempt-3-fix4-5/
  attempt-4-engine-rebind/
```
