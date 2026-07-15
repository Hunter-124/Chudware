# MegaRun11 Findings

## Run summary

- **Run ID:** `mega-11-fc-rocket-20260714`
- **Baseline commit:** `ea37fb68` (repo HEAD `ea37fb68-dirty`)
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running `git_rev` matches disk binary
- **Primary process:** pid **3075330**, wall **795.82s**
- **Fixer peers:** GradingPaths=`LegitimateHeron` (29 grading tests); LegProduction=`LazyVulture` (14 leg tests)
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, battery_result.json, criterion_AC*.json, fw_artifact.json, progress.md

### Pre-flight: PASS

1. Daemon **running == disk hostd** (`night2-3054-gcb65bfd9-dirty`)
2. Account **`17d17fb1-…`** active
3. **`spawn_probe` PASS** — PLAN/layout cap-sep; leg roles `ecad_engineer`+`firmware_engineer` with domain tools
4. **`auto_pick_smoke` PASS** — depth-2 low → `ollama/llama3.2:latest`
5. **BOTH bound DMs** received before primary launch

### Terminal

- `status=needs_correction`
- `stop_reason=verify_failed: ['AC1', 'AC2', 'AC4', 'AC5', 'AC7', 'AC9']`
- **AC3 + AC6 absent from fail list** (PASS); **AC8 SKIP** (honest Renode/backend absence)

---

## Axis scoreboard (honest — primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **K** AC gradeable ≥6 | **PASS** | All **9** AC1–AC9 tool-mapped via `mission_criteria.apply_rocket_plane_checks`; unit `test_mega11_grading.py` **29 passed**; live criterion_results for every AC from named tools (not manual prose) |
| **L** AC live PASS ≥2 | **PASS** | **AC3 PASS** (`check_interference`, interferences empty, doc `doc-cpp0000000b`) + **AC6 PASS** (`ecad_antenna_analyze`, fullwave-openems + analytic). Live PASS count = **2** |
| **M** non-CAD leg artifact | **PASS** | **fw_build** ELF ledger artifact (`project_id=bc70b9ea15b4`, `elf_path=/tmp/AI-CAD/firmware/.../firmware.elf`, success true). Also **ecad_new_schematic** + **ecad_place_symbol×7** |
| **H** AC closure (reg) | **PASS** | ≥1 evidence-PASS (AC3+AC6); AC1 evidence-FAIL from battery |
| **I** battery doc_id (reg) | **PASS** | `battery_result.doc_id=doc-cpp0000000b` |
| **J** typed-refusal (reg) | **PASS** (harness residual) | layout/plan still `delegate_or_typed_refusal_required`; live refuse envelope 0 (same class as mega-10) |
| **A** accepted ops (reg) | **PASS** | `new_part`×1 ok → `doc-cpp0000000b`; **`add_features` ok×2** |
| **B** delegate/gate (reg) | **PASS** | **delegate×2** + **spawn×1** (`researcher`, budget 4000); cap-sep preflight PASS |
| **C** leaf oneshot (reg) | **PASS** | **leaf_oneshot_ok×9** / miss×4 |
| **D** terminal (reg) | **PASS** | Explicit `needs_correction` + clean `verify_failed: […]` |
| **E** battery ledger (reg) | **PASS** | reviewer `battery_result` 15 checks, honest FAIL; BATTERY criterion |
| **F** clean stop_reason (reg) | **PASS** | Class string only |
| **G** typed-refusal teach (reg) | **PASS** | Schema/teach path retained; live residual |

---

## K) AC gradeable count: PASS (≥6)

| AC | Tool | pass_rule | Live result | Notes |
|---|---|---|---|---|
| AC1 | verify_battery | PASS | **FAIL** | evidence from battery_result; soft ECAD/sketch zeros |
| AC2 | measure_part | bay envelope ≥ board outline | **NOT-CHECKED** | volume_mm3 present (170198) but rule wants bay/board dims — grader residual |
| AC3 | check_interference | interferences empty or volume==0 | **PASS** | empty interferences |
| AC4 | ecad_run_drc+ecad_run_erc | errors==0 | **FAIL** | tool ok=false (no clean board DRC path completed) |
| AC5 | ecad_simulate | V(3V3) in [3.135,3.465] | **FAIL** | mechanical doc_id passed to ECAD sim |
| AC6 | ecad_antenna_analyze | PASS\|SKIP | **PASS** | openEMS + analytic evidence |
| AC7 | fw_build | returncode==0 | **NOT-CHECKED** | **defect**: live ELF success artifact exists; grader saw text-wrapped ledger_tool_call without success parse |
| AC8 | fw_sim_run | PASS\|SKIP(renode) | **SKIP** | honest unresolved/backend absence |
| AC9 | report_card | all legs PASS\|WARN\|FAIL\|SKIP | **FAIL** | legs missing: trace_opt, power |

**Gradeable count:** 9/9 tool-mapped + unit-tested pass_rules (K ≥6).

---

## L) AC live PASS count: PASS (=2)

- AC3 PASS + AC6 PASS. (AC8 SKIP does not count as PASS.)

---

## M) non-CAD leg artifact: PASS

Ledger artifact (actor FIRMWARE ENGINEER):

- project_id: `bc70b9ea15b4`
- elf_path: `/tmp/AI-CAD/firmware/bc70b9ea15b4/build/firmware.elf`
- success: true, verdict: PASS, ac: AC7

ECAD: `ecad_new_schematic(flight_computer)` + place_symbol×7 + find_part×6.

---

## A–J regression

### A accepted ops: PASS
- new_part rocket_plane_airframe → doc-cpp0000000b
- add_features ok×2; preflight_ops×3

### B capability separation: PASS
- Preflight cap-sep + leg roles
- Live delegate×2, spawn researcher

### C cheap-leaf oneshot: PASS
- oneshot_ok×9 / miss×4

### D terminal: PASS
- needs_correction + verify_failed list

### E battery: PASS
- 15 checks; is_manifold=1.0; doc_id threaded

### F clean stop: PASS

### G teach path: PASS (live residual)

### H/I/J: PASS (see scoreboard)

---

## Token / conservation

- model_assignment: **13 ollama / 11 xai-sub**
- Wall **795.82s**
- xAI semaphore capacity=3 observed
- oneshot path active on leaves

---

## DAG node outcomes

**done (17):** n_intake, n_spec_DL1/3/5, n_organize, n_build_DL1/3/4, n_verify_DL1–4, n_deliver_DL1–5

**failed (6):** n_spec_DL2 (max_iters), n_spec_DL4 (no_code), n_spec_done (no_code), n_layout (delegate_or_typed_refusal_required), n_plan (same), n_build_DL2 (max_iters)

---

## TOP residual (next cycle)

1. **AC7 grader parse** — success/elf_path in ledger evidence must grade returncode==0 / success (NOT-CHECKED today despite real ELF).
2. **AC2 bay/board dims** — measure_part returns volume_mm3; mechanical bay-vs-board needs named dims or volume fallback.
3. **AC5 doc routing** — ECAD simulate must not receive mechanical doc_id.
4. **Board completion** — schematic placed but no board_from_schematic / DRC/ERC path → AC4 FAIL.
5. **Live typed-refusal** — layout/plan still gate-die without envelope.
6. **AC9 report_card completeness** — trace_opt + power legs missing verdicts.
7. **Phase cursor honesty** — phases_completed stayed `["intake"]` while DAG advanced (prior residual class).

---

## Fingerprints

| ID | Status |
|---|---|
| K AC gradeable ≥6 | **PASS** (9/9 tool-mapped) |
| L live PASS ≥2 | **PASS** (AC3+AC6) |
| M non-CAD artifact | **PASS** (fw ELF + schematic) |
| A–J regression | **PASS** |

---

## Artifacts

```
reports/runs/mega-11/
  run_mega.py findings.md progress.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json fw_artifact.json
  criterion_AC1.json … criterion_AC9.json criterion_BATTERY.json
```
