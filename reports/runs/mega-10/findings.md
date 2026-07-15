# MegaRun10 Findings

## Run summary

- **Run ID:** `mega-10-fc-rocket-20260714`
- **Baseline commit:** `df8fb2e5` (repo HEAD `night2-3060-gdf8fb2e5`)
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running `git_rev` matches disk binary (staleness guard OK; Python Fixer10 binds without hostd rebuild)
- **Primary process:** pid **2996834**, wall **315.96s**
- **Fixer peer:** ComprehensiveWhale (Fixer10) — fixes 1–2 bound pre-launch; `fixes.md` + 50 tests green
- **Evidence:** `events.jsonl`, `ledger.json`, `mission.json`, `status.json`, preflight JSONs, `battery_result.json`, `criterion_AC1.json`, `criterion_AC3.json`, `fixes.md`, `progress.md`

### Pre-flight: PASS

1. Daemon **running == disk hostd** (`night2-3054-gcb65bfd9-dirty`)
2. Account **`17d17fb1-…`** active (2 xai-sub accounts)
3. **`spawn_probe` PASS** — PLAN/layout no exec tools; leaf keeps exec
4. **`auto_pick_smoke` PASS** — depth-2 low → `ollama/llama3.2:latest`
5. Fixer10 DM: **fixes 1–2 BOUND** before primary launch

### Terminal

- `status=needs_correction`
- `stop_reason=verify_failed: ['AC1', 'AC2', 'AC4', 'AC5', 'AC6', 'AC7', 'AC8', 'AC9']`
- **AC3 absent from fail list** (graded PASS live)

---

## Axis scoreboard (honest — primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **H** AC closure | **PASS** | **AC3 = PASS** from real `check_interference` artifact (`interferences=[]`, `doc_id=doc-cpp00000001`). **AC1 = FAIL** graded from `battery_result` evidence (`source=battery_result`, scores/findings) — mega-9 residual “AC1 NOT-CHECKED despite battery” **cleared**. ≥1 mission AC evidence-PASS. |
| **I** battery doc_id multi-part | **PASS** | `battery_result.doc_id = doc-cpp00000001` (geometry-bearing BUILD doc). Battery artifact + AC1 evidence also carry same doc_id. Only one `new_part` this run; resolver still threaded explicit id (no null). |
| **J** typed-refusal live or unit | **PASS** (harness E2E) | Fixer10 `test_typed_refusal_flat_multi_domain_gate_e2e` + mega-8/9 suite in 50-pass batch. Live: **0** validating refuse envelopes; `n_layout` / `n_plan` / `n_build_DL2` still `delegate_or_typed_refusal_required`. Contract allows unit arm. |
| **A** accepted ops (reg) | **PASS** | `new_part`×1 ok → `doc-cpp00000001`; **`add_features` ok×4** / fail×3 (batch rejects + unknown `set_param` with cheatsheet inject). Real volumes (e.g. 84867 mm³ shell). |
| **B** delegate/gate (reg) | **PASS** | **delegate×1** + **spawn×1** (`part_engineer` worker, `budget_tokens=8000`); capability gate live on layout/plan/build_DL2. |
| **C** leaf oneshot (reg) | **PASS** | **`leaf_oneshot_ok`×11**, miss×0 in ledger agent_turn events. |
| **D** terminal (reg) | **PASS** | Explicit `needs_correction` + clean `verify_failed: […]` (not `running`+empty). |
| **E** battery ledger (reg) | **PASS** | `battery_result` actor=reviewer source=`run_verify_battery`, **15 per-check** rows, honest overall **FAIL**; synthetic `criterion_result` id=`BATTERY` FAIL. |
| **F** clean stop_reason (reg) | **PASS** | Class string only; no Sqlite/blob text. |
| **G** typed-refusal teach (reg) | **PASS** | Schema teach path retained; live emission residual (same as mega-9 G). |

---

## H) AC closure: PASS

### Mission AC table (ledger `criterion_result`)

| AC | Result | Tool | Evidence |
|---|---|---|---|
| **AC1** | **FAIL** | verify_battery | `evidence.source=battery_result`, `doc_id=doc-cpp00000001`, scores include `is_manifold=1.0` / several 0.0 ECAD/sketch checks → honest FAIL (not NOT-CHECKED) |
| **AC2** | NOT-CHECKED | measure_part | Tool ran (`ok=true`, mass/body) but prose pass_rule not mechanical (Fixer10 flagged, not weakened) |
| **AC3** | **PASS** | check_interference | `interferences=[]`, summary “No interference detected.”, single body parts list, `doc_id=doc-cpp00000001` |
| AC4–AC9 | NOT-CHECKED | manual | Manual by construction |

**Closure count:** ≥1 PASS from real artifacts (**AC3**). Grader wiring residual fixed: AC1 no longer NOT-CHECKED when battery evidence exists.

Artifacts: `criterion_AC3.json`, `criterion_AC1.json`, `battery_result.json`.

---

## I) battery doc_id: PASS

```text
battery_result.doc_id = "doc-cpp00000001"
artifact kind=battery doc_id = "doc-cpp00000001"
AC1.evidence.doc_id = "doc-cpp00000001"
```

Mega-9 residual was `doc_id: null` with 2 parts open. This run: explicit geometry doc threaded; no ambiguous “2 parts are open” battery failure mode on measure/query for the mission doc.

---

## J) typed-refusal: PASS (unit / harness)

| Path | Evidence |
|---|---|
| Unit/harness | Fixer10 `test_typed_refusal_flat_multi_domain_gate_e2e` + prior suite; **50 passed** |
| Live success envelope | **0** — layout/plan/build_DL2 budget-die on gate |
| Gate | Still enforces `delegate_or_typed_refusal_required` |

---

## A–G regression

### A accepted ops: PASS

| Call | Result |
|---|---|
| `new_part` rocket_plane_airframe | ok → `doc-cpp00000001` |
| `add_features` | **ok×4**, fail×3 (batch reject / empty delete / unknown `set_param` + cheatsheet inject) |

### B capability separation: PASS

- Preflight cap-sep OK
- Live: delegate + depth-1 spawn (`part_engineer`, budget 8000)
- Coordinators still gated without exec tools

### C cheap-leaf oneshot: PASS

| Event | Count |
|---|---:|
| leaf_oneshot_ok | 11 |
| leaf_oneshot_miss | 0 |

### D terminal: PASS

Explicit terminal after productive BUILD/VERIFY. `phases_completed` still honesty-limited (`intake`,`specify`) while DAG advanced — same phase-cursor residual class as prior megas; not a D fail.

### E battery: PASS

15 checks; honest FAIL aggregate; `is_manifold=1.0` this run (geometry solid). Failures mostly missing-arg ECAD/sketch/drawing checks on a mechanical-only doc — battery still ran and logged.

### F clean stop: PASS

`verify_failed: [AC…]` only.

### G teach path: PASS (live residual)

---

## Token / conservation

- model_assignment: **11 ollama / 9 xai-sub**
- Wall **315.96s** (faster than mega-9 416s / mega-8 693s)
- xAI semaphore capacity=3 observed
- oneshot path active on leaves

---

## DAG node outcomes

**done (14):** n_intake, n_spec_DL1/3/5, n_spec_done, n_organize, n_build_DL1, n_verify_DL1/2, n_deliver_DL1–5  

**failed (5):** n_spec_DL2 (no_code), n_spec_DL4 (no_code), n_layout (delegate_or_typed_refusal_required), n_plan (same), n_build_DL2 (same)

---

## TOP residual (next cycle)

1. **Live typed-refusal emission** — unit/harness green; models still don't emit envelope under gate (J residual).
2. **AC2 mechanical rule** — measure_part runs but prose pass_rule not evaluable; map to volume/bbox rule or fit tool (Fixer10 proposal).
3. **AC4–AC9 manual** — need executable tools (ecad_simulate, fw_build, report grader) without weakening.
4. **Battery on mechanical-only doc** — ECAD/sketch checks FAIL for missing args/docs; consider role-scoped battery subsets so overall FAIL reflects true geometry when ECAD legs absent.
5. **Phase cursor honesty** — DAG reached build/verify/deliver but `phases_completed` stayed intake+specify.

---

## Fingerprints

| ID | Status |
|---|---|
| H AC closure | **PASS live** (AC3 PASS; AC1 evidence-FAIL) |
| I battery doc_id | **PASS live** (`doc-cpp00000001`) |
| J typed-refusal | harness E2E PASS; live 0 |
| A–G regression | **PASS** |

---

## Artifacts

```
reports/runs/mega-10/
  run_mega.py findings.md progress.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json criterion_AC1.json criterion_AC3.json criterion_BATTERY.json
```
