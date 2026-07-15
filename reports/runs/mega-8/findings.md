# MegaRun8 Findings

## Run summary

- **Run ID:** `mega-8-fc-rocket-20260714`
- **Baseline commit:** `7ef7b577` (cycle-8 wave: op-vocab registry + capability separation + cheap-leaf oneshot + deliver_partial)
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3; fallback `xai:grok-4-5`
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a` (preflight active; switched=False)
- **Org layer:** ON, max spawn depth 8
- **Fixer peer:** CollectiveElk (Fixer8) — Fixes 1–6 mid-cycle (`fixes.md`)
- **Primary attempt for grading:** attempt-2 (Fixer8 1–3 bound) + attempt-3 (Fixer8 4–5 bound); attempt-1 pre-fix archive
- **Primary evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `status.json`, `spawn_probe.json`, `auto_pick_smoke.json`, `subscription_accounts.json`, attempt archives, `progress.md`, `fixes.md`

### Pre-flight: PASS

1. **`subscription_accounts.json`** — 2 xai-sub accounts; **active=`17d17fb1-…`**
2. **`spawn_probe.json` verdict PASS** — PLAN/layout (`orchestrator`, `layout_engineer`, `department_manager`) resolve **WITHOUT** `add_features`/`new_part`/`preflight_ops` and **WITH** `delegate`; `feature_modeler` keeps exec tools; `budget_tokens` rendered on required roles
3. **`auto_pick_smoke.json` verdict PASS** — depth-2 low-difficulty → `ollama/llama3.2:latest` (non-xai); depth-0 architecture → xai-sub

### Attempt map

| Attempt | Pid | Bound fixes | Wall | Terminal | Archive |
|---|---:|---|---:|---|---|
| 1 | 2410582 | baseline only | ~6m38s (killed) | incomplete | `attempt-1-pre-fixer8-rebind/` |
| 2 | 2453640 | Fixer8 1–3 | **926.0s** | `needs_correction` / `verify_failed: [AC1…AC9]` | `attempt-2-pre-fix4-5/` |
| 3 | 2544922 | Fixer8 1–5 (+6 mid) | **1003.84s** | `needs_correction` / `verify_failed: [AC1…AC9]` | `attempt-3-fix4-5/` + live OUT |

---

## Axis scoreboard (honest)

| Axis | Verdict | Best evidence |
|---|---|---|
| **A** add_features accepted ops > 0 + cheatsheet ≤1 | **FAIL** | attempt-2: `new_part`×1 + `add_features`×1 with `unknown op 'cylinder'` → **0 accepted ops**; hollow auto-complete (pre-Fix5). Cheatsheet inject **never observed** (`op vocabulary assist` count 0). attempt-3: `new_part`×1, **`add_features`×0** (BUILD DL1 no_code) — Fix5 not exercised live. |
| **B** delegate >0 OR typed refusal | **PASS** | attempt-2/3: **delegate×1** on `n_build_DL2` + **agent.spawn** depth-1 (source-parts / feature_modeler). `n_layout` (and a2 `n_plan`) failed `delegate_or_typed_refusal_required` — gate live; no typed-refusal envelopes emitted (gate without successful refuse path). SPECIFY not false-gated after Fix3. |
| **C** cheap-leaf oneshot + leaf_miss | **PASS** | attempt-2/3: **`leaf_oneshot_ok`×3** each (n_ops 2–10); **`leaf_oneshot_miss`×8** each (json parse / schema) — miss telemetry recorded. Fix1 unbound a1 (TypeError string indices). |
| **D** terminal done\|blocked + stop_reason | **PASS** | attempt-2/3: **`status=needs_correction`**, **`stop_reason=verify_failed: [AC1…AC9]`** — explicit, not `running`+empty. `deliver_partial` not the terminal class (VERIFY→CORRECT beat soft-join deliver). |

---

## A) add_features / op vocabulary: FAIL

### What bound

- Cycle-8 registry + rich unknown-op path + once-per-mission middleware present at baseline `7ef7b577`.
- attempt-2 BUILD DL1 sequence: **`new_part(rocket_plane_airframe)` ok** → **`add_features`** with invented op:

```json
{"op": "cylinder", "name": "body_tube_outer", "r": 25, "h": 400, "axis": "z", "origin": [0, 0, 0]}
```

- Engine result: `{"ok": false, "error": "op[0]: unknown op 'cylinder'"}` — **thin** error (no `closest:` suggestions, no `cad_guide()` pointer in live string).
- Call-record `ok: true` (pre-Fix5) → `productive_tools` included `add_features` → max_iters auto-complete on hollow solid.
- **Accepted solid-producing ops: 0.**

### Cheatsheet once-per-mission

- Grep across attempt-2/3 ledgers + events: **`op vocabulary assist` = 0**, **`once per mission` = 0**.
- Unknown-op path was hit once (a2) but middleware inject did not land in observable mission memory (defect fingerprint for Fixer8 — possible run_id/context_pack miss pre-Fix2, or inject only into ephemeral turn messages not ledgered; Fix2 run_id seed bound on a2/a3 but still no inject trail).

### attempt-3 regression on sequence

- `new_part`×1, **`add_features`×0**, BUILD DL1 `no_code` — model did not re-fire mutator path; Fix5 honest ok:false **not live-exercised**.
- Spawn on a3: `feature_modeler` with geometry brief (budget 12000) after BUILD DL2 delegate — still no accepted ops in parent ledger.

**Verdict: FAIL** — cycle-8 intent (accepted ops >0 + teaching) not met. Sequence partial on a2 only.

**Defects logged for Fixer8:**

1. `UNKNOWN_OP_THIN_ERROR` — live error lacks `closest:` / `cad_guide()` despite registry tests.
2. `OP_VOCAB_CHEATSHEET_NOT_OBSERVED` — once-per-mission inject not visible after real unknown-op.
3. `ADD_FEATURES_OK_TRUE_ON_REJECT` — fixed as Fix5 (bound a3; not re-hit).
4. `HOLLOW_AUTOCOMPLETE_ON_REJECTED_OPS` — consequence of (3) on a2.

---

## B) Capability separation / delegate / typed refusal: PASS

### Preflight

- PLAN/layout cards: no exec tools + delegate present (spawn_probe PASS).
- Leaf `feature_modeler` retains `add_features`/`new_part`.

### Live forcing function

| Signal | attempt-2 | attempt-3 |
|---|---|---|
| `delegate` tool calls | **1** (`n_build_DL2` → `source-parts`, budget 3000, ok) | **1** (`n_build_DL2` → `edit-part`/`feature_modeler` path, ok=false after join) |
| `agent.spawn` | **1** depth-1 worker | **1** depth-1 `feature_modeler` budget 12000 |
| Gate `delegate_or_typed_refusal_required` | `n_layout`, `n_plan` | `n_layout`, `n_build_DL2` |
| Typed refusal envelopes | 0 | 0 |
| SPECIFY false-gate | a1 yes; **a2/a3 no** (Fix3) | no |

- Gate **forces** multi-domain PLAN/layout/BUILD coordinators: they either spawn or die on budget when they free-form complete without typed refusal.
- Actual **delegate > 0** on BUILD multi-domain → axis B contract met (delegate OR typed refusal).
- Residual: models prefer burning budget over emitting `{reason, domains_considered, why_flat}`; typed-refusal **path unproven as successful escape hatch**.

**Verdict: PASS** (forcing function + live delegate/spawn).

---

## C) Cheap-leaf oneshot + leaf_miss: PASS

### attempt-1 (unbound)

- All ollama leaves: `leaf_oneshot_miss` with `string indices must be integers, not 'str'` (OpenAI/ollama `_message`).

### attempt-2/3 (Fix1+)

| Metric | attempt-2 | attempt-3 |
|---|---:|---:|
| `leaf_oneshot_ok` | **3** | **3** |
| `leaf_oneshot_miss` | **8** | **8** |
| Sample ok | INTAKE n_ops=3; simple_part n_ops=3; leaf n_ops=2 | simple_part n_ops=2; leaf n_ops=10; leaf n_ops=6 |
| Miss classes | truncated JSON, control chars, string artifacts | null `reason`, string artifacts/ops (Fix6 mid-a3) |

- Miss telemetry is the live `leaf_miss` stat (ledger `leaf_oneshot_miss` events). Process-local `leaf_miss_count()` not persisted across process end — event trail is the contract evidence.
- model_assignment: **11 ollama / 9 xai** both a2 and a3 (20 nodes incl. spawn child).

**Verdict: PASS** — completions >0 and miss telemetry recorded.

---

## D) Terminal mission status: PASS

| Attempt | status | phase | stop_reason | phases_completed |
|---|---|---|---|---|
| 2 | `needs_correction` | `correct` | `verify_failed: ['AC1'…'AC9']` | `["intake"]` |
| 3 | `needs_correction` | `correct` | `verify_failed: ['AC1'…'AC9']` | `["intake","specify"]` |

- **Not** `running` + empty stop_reason (mega-7 residual class).
- `deliver_partial` specifically not selected — VERIFY failure routed to CORRECT with explicit AC list (acceptable under contract: explicit stop_reason; deliver_partial optional).
- Soft-join deliver workers still completed mid-run (a2: DL1/3/4/5 done; a3: DL1/2/3/5 done).

**Verdict: PASS.**

---

## Token / latency / conservation

| Metric | attempt-2 | attempt-3 |
|---|---:|---:|
| Wall | 926.0s | 1003.84s |
| DAG | 19 started; 13 done / 6 failed | 19-ish; 11 done / 8 failed |
| model_assignment | 11 ollama / 9 xai | 11 ollama / 9 xai |
| provider_usage rows (xai-sub) | 190 / **2.22M** tok | 172 / **1.67M** tok |
| ollama provider_usage rows | 0 ledgered (oneshot path may not emit same usage event) | 0 |
| xAI semaphore | capacity=3 observed | capacity=3 |
| Tool calls (events) | ~173 | ~216 |

Cheap **assignment** share remains strong (~55% ollama rows). Cheap **token** share still ~0 in provider_usage ledger (ollama oneshot not dual-counted like mega-7 multi-complete rows) — conservation routing holds; token accounting for oneshot is a residual observability gap.

---

## Phase honesty

- Live stage_log advances intake → specify → plan/build/verify/deliver cursor (and correct after verify fail).
- a3: specify **exit_gate `spec_audited_criteria_covered`** (first mega with clean specify exit observed this series).
- plan/build not falsely marked success when nodes failed.
- Product ACs all FAIL → honest `needs_correction` (not forged done).

---

## TOP 5 next

1. **Make unknown-op teaching actually land.** Live a2 hit `unknown op 'cylinder'` without `closest:` / cheatsheet trail. Wire C++/MCP error through rich `unknown_doc_op_message`; ensure `_maybe_inject_op_vocab_cheatsheet` append is ledger-visible (or force next-user-turn feedback event).
2. **Prove Fix5 on a real reject path.** a3 never re-called `add_features` after new_part — BUILD prompt / thrash block may over-suppress retries once hollow; require at least one honest ok:false → replan/cad_guide loop.
3. **Typed-refusal success path.** Gate kills layout/plan on budget; models never emit valid `{reason, domains_considered, why_flat}`. Add one-shot scaffold or GBNF for the refuse envelope when multi-domain and depth-limited.
4. **Leaf envelope schema soft-normalize.** Fix4/6 reduce miss rate; residual string ops/artifacts and null reason still dominate ollama misses — keep salvage + few-shot envelope exemplar in oneshot system text.
5. **Oneshot provider_usage telemetry.** Assignment proves cheap routing; token share looks 0% without ollama usage rows — emit provider_usage on oneshot complete for conservation dashboards.

---

## Failure fingerprints (for Fixer8)

| ID | Fingerprint | Attempt | Status |
|---|---|---|---|
| F1 | `leaf_oneshot` TypeError string indices (openai `_message`) | 1 | **Fixed** Fix1 |
| F2 | `run_id` missing on context_pack (cheatsheet keying) | 1 latent | **Fixed** Fix2 |
| F3 | typed-refusal gate on `n_spec_*` (`type=design`) | 1 | **Fixed** Fix3 |
| F4 | truncated ollama JSON oneshot miss | 2 | **Fixed** Fix4 (partial residual) |
| F5 | `add_features` ok:true on `{ok:false}` payload | 2 | **Fixed** Fix5 (not re-hit a3) |
| F6 | `reason: null` / string artifacts envelope miss | 3 | **Fixed** Fix6 (next process) |
| F7 | `UNKNOWN_OP_THIN_ERROR` no closest/cad_guide | 2 | **OPEN** |
| F8 | `OP_VOCAB_CHEATSHEET_NOT_OBSERVED` after unknown-op | 2 | **OPEN** |
| F9 | layout/plan budget death without typed-refusal success | 2–3 | **OPEN** (gate works; escape hatch weak) |
| F10 | a3 BUILD DL1 `new_part` then no `add_features` | 3 | **OPEN** |

---

## Axis scoreboard vs mega-7

| Axis | mega-7 attempt-2 | mega-8 (best) | Flip? |
|---|---|---|---|
| A accepted ops / vocab teach | 0 accepted; unknown thrash ×13 | 0 accepted; 1 unknown; cheatsheet unobserved | **Hold FAIL** (partial sequence only) |
| B delegate / cap sep | partial (a1 only) | **delegate×1 + spawn + gate** (a2/a3) | **FLIP PASS** |
| C cheap leaf oneshot | n/a (no envelope) | **ok×3 + miss×8** | **FLIP PASS** |
| D terminal stop_reason | `running` + empty | **`needs_correction` + verify_failed** | **FLIP PASS** |
| conservation assignment | 11/19 ollama | 11/20 ollama | Hold |
| SuperGrok wall | 383s | 926–1004s | slower (more oneshot+delegate work) |

---

## Artifacts

```
reports/runs/mega-8/
  run_mega.py
  progress.md findings.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json   # attempt-3 primary
  attempt-1-pre-fixer8-rebind/
  attempt-2-pre-fix4-5/
  attempt-3-fix4-5/
```
