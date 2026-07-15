# MegaRun13 Findings

## Run summary

- **Run ID:** `mega-13-fc-rocket-20260715`
- **Baseline commit:** `6894f19d` (repo HEAD `6894f19d`)
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Primary process (attempt-3):** pid **3500779**, wall **610.86s**
- **Fixer peer:** Fixer13=`DustyGibbon` (fixes 1–4 bound + post-launch rank floor / empty-tool hardenings)
- **Harness peer:** MegaRun13=`CurlyTermite`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, battery_result.json, criterion_AC*.json, fw_artifact.json, grade_summary.json, fixes.md, progress.md; archived attempt-1/2 under `attempt-1-ollama-all/`, `attempt-2-model-not-found/`

### Pre-flight: PASS

1. Daemon **running == disk hostd** (`night2-3054-gcb65bfd9-dirty`, pid 2980908)
2. Account **`17d17fb1-…`** active
3. **`spawn_probe` PASS** — PLAN/layout cap-sep; leg roles `ecad_engineer`+`firmware_engineer` with domain tools; **ECAD chain tools present** (`board_from_schematic`, `ecad_run_drc`, `ecad_wire`, …)
4. **`auto_pick_smoke` PASS** — depth-2 low → `ollama/llama3.2:latest`
5. **Fixer13 `fixes bound`** DM (70 tests) before primary launch
6. **No other xAI consumer** live at launch

### Attempts

| Attempt | Wall | Terminal | Note |
|---|---|---|---|
| 1 | 162.9s | hollow | enabled model `grok-4.5` measured rank=1 → auto_pick ollama for all 23 nodes; archived |
| 2 | 222.4s | provider_auth | enabled `grok-4-5` rank=5 but API **Model not found: grok-4-5**; archived |
| **3 (primary)** | **610.86s** | needs_correction | user rank override `grok-4.5`=5; real xAI BUILD; **graded below** |

### Terminal (attempt-3)

- `status=needs_correction`
- `stop_reason=verify_failed: ['AC1', 'AC4', 'AC5']`
- **AC2+AC3+AC6+AC7+AC8+AC9 PASS** (6 live PASS); AC1/AC4/AC5 FAIL

---

## Axis scoreboard (honest — attempt-3 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **P** board_from_schematic+DRC executed | **FAIL** | `board_from_schematic=0`, `ecad_run_drc=0` in agent tool path. Schematic+place×6 then `n_build_DL3` max_iters. Gate blocked false auto-done; force-chain scaffold did **not** inject board tools live. |
| **Q** live AC PASS ≥6 | **PASS** | **6** PASS: AC2+AC3+AC6+AC7+AC8+AC9 |
| **R** AC5 wired SPICE | **FAIL** | empty-netlist / no simulatable devices after place-only (no `ecad_wire`/`ecad_add_power` calls); U* warnings no sim model |
| **N** live AC PASS ≥4 (reg) | **PASS** | 6 ≥ 4; AC3+AC6 held + AC7 + AC2 |
| **O** ECAD board→DRC (reg) | **FAIL** | same as P — no board/DRC product calls |
| **K** AC gradeable ≥6 (reg) | **PASS** | 9/9 AC tool-mapped results |
| **L** live PASS ≥2 (reg) | **PASS** | 6 ≥ 2 |
| **M** non-CAD artifact (reg) | **PASS** | fw ELF `3c1ef7a802cd` + schematic/places |
| **H** AC closure (reg) | **PASS** | ≥1 evidence-PASS (6) |
| **I** battery doc_id (reg) | **PASS** | `battery_result.doc_id=doc-cpp00000035` |
| **J** typed-refusal (reg) | **PASS** (harness residual) | layout/plan still no live refuse envelope |
| **A** accepted ops (reg) | **PASS** | `new_part` ok + **`add_features` ok×19** / fail×10 |
| **B** delegate/gate (reg) | **PASS** | delegate + spawn present |
| **C** leaf oneshot (reg) | **PASS** | oneshot_ok×12 / miss×1 |
| **D** terminal (reg) | **PASS** | Explicit `needs_correction` + clean `verify_failed: [AC1,AC4,AC5]` |
| **E** battery ledger (reg) | **PASS** | reviewer battery_result 15 checks, honest FAIL |
| **F** clean stop_reason (reg) | **PASS** | Class string only |
| **G** typed-refusal teach (reg) | **PASS** | Schema/teach path retained; live residual |

---

## Q) AC live PASS count: PASS (=6 ≥6)

| AC | Tool | pass_rule | Live result | Notes |
|---|---|---|---|---|
| AC1 | verify_battery | PASS | **FAIL** | soft zeros: sketch/dim_coverage/ECAD-on-CAD/measure_between; manifold/eval improved vs mega-12 |
| AC2 | measure_part | bay envelope | **PASS** | volume_mm3≈228755.85 ≥ floor 5000 |
| AC3 | check_interference | empty/vol0 | **PASS** | empty interferences |
| AC4 | ecad_run_drc+erc | errors==0 | **FAIL** | no board product; live recheck: sch doc not board |
| AC5 | ecad_simulate | V(3V3) band | **FAIL** | empty-netlist / no simulatable devices (place×6, no wire) |
| AC6 | ecad_antenna_analyze | PASS\|SKIP | **PASS** | openEMS fullwave + analytic |
| AC7 | fw_build | returncode==0 | **PASS** | ELF `/tmp/AI-CAD/firmware/3c1ef7a802cd/build/firmware.elf` |
| AC8 | fw_sim_run | PASS\|SKIP | **PASS** | Renode path passed |
| AC9 | report_card | all legs | **PASS** | all 12 legs verdicts (Fix3: plan/power/trace_opt mapped) |

**Contract check:** count 6≥6 → **Q PASS**. N also PASS (AC3+AC6+AC7+AC2).

---

## P) board_from_schematic → DRC: FAIL

- Live BUILD DL3 (`ecad_engineer` / xai-sub/grok-4.5): `ecad_new_schematic`×1 ok, `ecad_find_part`×7, `ecad_place_symbol`×6 ok, then **max_iters**
- **`board_from_schematic`×0**, **`ecad_run_drc`×0**, **`ecad_wire`×0**, **`ecad_add_power`×0**
- Product gate correctly blocked schematic-only auto-done (node failed, not false done)
- Force-chain scaffold from Fix1 did not fire live tool injections (no `[ECAD enablement]` in events)
- AC4 live recheck: `doc '7465f4d07b42' is a sch, not a board`

---

## R) AC5 wired SPICE: FAIL

- No wire/power tools executed in BUILD
- AC5: `nothing to simulate — the netlist has no simulatable devices` (+ U* no sim model warnings)
- Domain doc routing no longer mechanical-doc class (mega-12 Fix3 held); residual is place-without-wire product

---

## Fixer13 fix hold vs live

| Fix | Intent | Live |
|---|---|---|
| 1 force board chain | P / O upgrade | **Gate held**; **chain not executed** (scaffold residual) |
| 2 wire before SPICE | R / AC5 | **Not held** — zero wire tools |
| 3 AC9 plan/trace_opt/power | AC9 complete | **HELD** — AC9 PASS all 12 legs |
| 4 airframe BUILD brief | AC1 geometry | **Partial** — real solid + AC2 PASS; AC1 still soft FAIL |

---

## A–J / K–M regression

### A accepted ops: PASS
- new_part → doc-cpp00000035; add_features ok×19

### B capability separation: PASS
- Preflight cap-sep + live delegate/spawn

### C cheap-leaf oneshot: PASS

### D terminal: PASS
- needs_correction + verify_failed list (AC1,AC4,AC5 only)

### E battery: PASS
- 15 checks; doc_id=doc-cpp00000035; honest FAIL

### F clean stop: PASS

### G teach path: PASS (live residual on layout/plan)

### H/I/J/K/L/M: PASS (see scoreboard)

---

## Token / conservation

- Wall **610.86s**
- Models: xai-sub/grok-4.5 ×11, ollama/llama3.2:latest ×13 (leaves)
- Account `17d17fb1-…` held
- oneshot path active
- Harness override: `model_rankings["grok-4.5"]=rank 5` so analysis min_rank=5 selects xAI (measured floor alone was 4)

---

## DAG node outcomes (attempt-3)

**done (18):** n_intake, n_spec_DL5, n_spec_DL4, n_spec_DL1, n_spec_DL2, n_spec_done, n_deliver_DL5, n_organize, n_build_DL4, n_build_DL1, n_verify_DL3, n_verify_DL1, n_verify_DL2, n_verify_DL4, n_deliver_DL4, n_deliver_DL1, n_deliver_DL2, n_deliver_DL3

**failed (5):** n_spec_DL3 (max_iters), n_layout (delegate_or_typed_refusal), n_plan (no_code), n_build_DL3 (max_iters), n_build_DL2 (max_iters)

---

## TOP residual (next cycle)

1. **P / O board path** — force-chain scaffold still does not produce live `board_from_schematic`/`ecad_run_drc` after place; need harder injection or auto-chain after N places.
2. **R / AC5 wire** — place without `ecad_wire`/`ecad_add_power`; empty-netlist SPICE remains.
3. **AC1 battery soft zeros** — geometry exists (AC2 volume PASS) but sketch/dim_coverage/ECAD-on-CAD soft fails.
4. **auto_pick model id / rank** — API id `grok-4.5` vs ranking alias `grok-4-5`; measured demotion to rank 1; floor must be ≥5 for analysis min_rank.
5. **Live typed-refusal** — layout/plan still max_iters/no_code without refuse envelope.
6. **Phase cursor honesty** — phases_completed stayed `["intake","specify"]` while DAG advanced through BUILD/VERIFY/DELIVER.

---

## Fingerprints

| ID | Status |
|---|---|
| P board+DRC executed | **FAIL** (0 board/DRC agent calls) |
| Q live PASS ≥6 | **PASS** (6: AC2,AC3,AC6,AC7,AC8,AC9) |
| R AC5 wired SPICE | **FAIL** (empty-netlist class) |
| N ≥4 (reg) | **PASS** |
| O board→DRC (reg) | **FAIL** |
| A–M regression | **PASS** (with J/G live residual) |

---

## Artifacts

```
reports/runs/mega-13/
  run_mega.py grade_run.py findings.md progress.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json fw_artifact.json grade_summary.json
  criterion_AC1.json … criterion_AC9.json criterion_BATTERY.json
  attempt-1-ollama-all/ attempt-2-model-not-found/
```
