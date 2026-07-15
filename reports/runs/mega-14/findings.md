# MegaRun14 Findings

## Run summary

- **Run ID:** `mega-14-fc-rocket-20260715`
- **Baseline commit:** `7a1e1e5d` (repo HEAD `7a1e1e5d`)
- **Conservation:** auto_pick-first; no per-node frontier injection; xAI semaphore=3
- **Active SuperGrok account:** `17d17fb1-247a-4800-ba97-e7b5150bc53a`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Primary process:** pid **3590131**, wall **797.46s**
- **Fixer peer:** Fixer14=`ExceptionalYak` (Fix8 force-inject + AC1 per-check diagnosis + rank floor 5; 77 tests)
- **Harness peer:** MegaRun14=`WholeCatfish`
- **Model id:** `grok-4.5` EXACT (fallback tier `xai:grok-4.5`; hyphen form avoided)
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, battery_result.json, criterion_AC*.json, fw_artifact.json, grade_summary.json, fixes.md, progress.md

### Pre-flight: PASS

1. Daemon **running == disk hostd** (`night2-3054-gcb65bfd9-dirty`, pid 2980908)
2. Account **`17d17fb1-…`** active
3. **`spawn_probe` PASS** — PLAN/layout cap-sep; leg roles `ecad_engineer`+`firmware_engineer`; **ECAD chain tools present**; **Fix8 hard gate wiring PASS** (source + unit test + role tools)
4. **`auto_pick_smoke` PASS** — depth-2 low → `ollama/llama3.2:latest`; analysis → `xai-sub/grok-4.5` rank 5
5. **Fixer14 `fixes bound`** DM (77 tests) before primary launch
6. **No other xAI consumer** live at launch
7. Fix8 unit test green at baseline: `test_ecad_hard_gate_blocks_place_until_wire` PASS

### Terminal (primary)

- `status=needs_correction`
- `stop_reason=verify_failed: ['AC1', 'AC4', 'AC5']`
- **AC2+AC3+AC6+AC7+AC8+AC9 PASS** (6 live PASS); AC1/AC4/AC5 FAIL
- Wall **797.46s**; models: **xai-sub/grok-4.5 ×10**, **ollama/llama3.2:latest ×13**

---

## Axis scoreboard (honest — primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **S** board_from_schematic+DRC executed (P upgrade) | **PASS** | `board_from_schematic` ok + `ecad_run_drc` ok; DRC **errors=0** clean board `c8131464bfbe`. Fix8: place denied → `ecad_add_power` → place denied (need board) → **`ecad_chain_force_inject` board_from_schematic** after 2 denials → DRC. |
| **T** AC5 wired SPICE (R upgrade) | **FAIL** | empty-netlist / no simulatable devices. `ecad_wire`×0; only `ecad_add_power`×1. Placed U* ICs lack SPICE models (MCU/Buck/MPU6050 skipped); hint wants R/C/L/V with VALUES. |
| **U** AC1 battery PASS or per-check diagnosis | **PASS** (diagnosis) | AC1 still FAIL; `criterion_AC1.json` carries `per_check` / `zero_checks` / `geometry_evidence` / `brief_hints` (Fixer14 Fix2). Geometry OK: is_manifold+measure_part+volume AC2 PASS. |
| **P** board+DRC (reg) | **PASS** | upgraded by S |
| **Q** live AC PASS ≥6 | **PASS** | **6** PASS: AC2+AC3+AC6+AC7+AC8+AC9 (target ≥7 not met) |
| **R** AC5 wired (reg) | **FAIL** | same as T |
| **N** live AC PASS ≥4 (reg) | **PASS** | 6 ≥ 4; AC3+AC6 held + AC7 + AC2 |
| **O** ECAD board→DRC (reg) | **PASS** | board+DRC executed; DRC clean |
| **K** AC gradeable ≥6 (reg) | **PASS** | 9/9 AC tool-mapped results |
| **L** live PASS ≥2 (reg) | **PASS** | 6 ≥ 2 |
| **M** non-CAD artifact (reg) | **PASS** | fw ELF `/tmp/AI-CAD/firmware/24728a117438/build/firmware.elf` + schematic/board |
| **H** AC closure (reg) | **PASS** | ≥1 evidence-PASS (6) |
| **I** battery doc_id (reg) | **PASS** | `battery_result.doc_id=doc-cpp00000041` |
| **A** accepted ops (reg) | **PASS** | `new_part` ok + `add_features` ok×3 (events) / broader ledger ok counts |
| **B** delegate/gate (reg) | **PARTIAL/FAIL (tool path)** | preflight cap-sep PASS; live `delegate` tool_name count 0; layout/plan/build_DL2 failed typed-refusal/no_code residual |
| **C** leaf oneshot (reg) | **PASS** | grade ledger oneshot_ok×8 / miss×5 |
| **D** terminal (reg) | **PASS** | Explicit `needs_correction` + clean `verify_failed: [AC1,AC4,AC5]` |
| **E** battery ledger (reg) | **PASS** | 15 checks; honest FAIL |
| **F** clean stop_reason (reg) | **PASS** | Class string only |
| **G** typed-refusal teach (reg) | **PASS** (live residual) | layout/plan still max_iters/no_code without refuse envelope |
| **J** typed-refusal live (reg) | **PASS** (harness residual) | same class as prior cycles |
| **Fix8 live** | **PASS** | `ecad_chain_gate`×2 + `tool_denied`×2 + `ecad_chain_force_inject` board_from_schematic |

**Live AC PASS count:** 6 (target ≥7 **missed** by AC1/AC4/AC5).

---

## S) board_from_schematic → DRC: PASS

- Live BUILD DL3 (`ecad_engineer` / xai-sub/grok-4.5):
  - `ecad_new_schematic`×1 ok
  - `ecad_place_symbol`×3 (with hard denials on thrash)
  - `ecad_add_power`×1 ok
  - **`board_from_schematic`×1 ok** (force-injected after 2 denials)
  - **`ecad_run_drc`×1 ok**, DRC **errors=0** on board doc `c8131464bfbe`
- Ledger Fix8 events:
  1. `tool_denied` place — need wire/power
  2. `ecad_chain_gate` need=`ecad_wire|ecad_add_power` denials=1
  3. `tool_denied` place — need board
  4. `ecad_chain_gate` need=`board_from_schematic` has_wire=true denials=2
  5. **`ecad_chain_force_inject` tool=`board_from_schematic` reason=`hard_gate_cap`**
- **P/O upgraded to PASS** vs mega-13 place-only residual.

---

## T) AC5 wired SPICE: FAIL

- **No `ecad_wire` calls** in agent tool path (events: wire=0).
- `ecad_add_power`×1 satisfied hard-gate “has_wire” proxy (power port) enough to force board, but netlist still has **no simulatable R/C/L/V devices**.
- AC5 live recheck: `nothing to simulate — the netlist has no simulatable devices`
- Warnings: U1 MCU / U2 Buck / U3 MPU6050 — **no sim model — skipped**
- Hint: place parts with sim models (R/C/L/V/I/D/Q) and VALUES; wire into a circuit.
- Residual class shifted from pure place-only (mega-13) to **power-port without passive SPICE net** + missing sim models.

---

## U) AC1 battery diagnosis: PASS (diagnosis path)

AC1 result remains **FAIL** (battery not weakened). Diagnosis is gradeable from artifacts:

### Failing / zero checks (soft)

| Check | Score | Message / class |
|---|---|---|
| check_sketch | 0.0 | missing required arg `sketch` (arg miss, not hollow solid) |
| dim_coverage | 0.0 | no open drawing on airframe doc |
| ecad_check_diff_impedance | 0.0 | missing `net_p`/`net_n` (ECAD-on-CAD) |
| ecad_check_impedance | 0.0 | no open ECAD document on mechanical doc |
| ecad_run_erc | 0.0 | no open ECAD document on mechanical doc |
| measure_between | 0.0 | missing `a_query` arg |

### Geometry evidence (PASS)

| Check | Score |
|---|---|
| is_manifold | 1.0 |
| measure_part | 1.0 |
| eval_check / groundedness / layout / query / unit / power_budget | 1.0 |

- AC2 volume_mm3≈**196147** ≥ floor (payload bay proxy PASS).
- `brief_hints` in criterion_AC1: prefer feature-solid path; ignore ECAD soft zeros on mechanical doc; measure_between arg miss ≠ hollow solid.
- **Concrete next brief change:** keep wall≥1.5 / bay dims; solid feature path over free sketches; do not expect ECAD battery checks on airframe doc_id.

---

## AC4 residual (not S failure)

- AC4 FAIL: `errors=18 (drc.errors=0, erc.errors=18)`
- **DRC clean** (S holds); ERC **power_not_driven** on many nets (N$10/N$11/… no power_out / power port drive).
- Product board path executed; ERC power topology incomplete (related to wire/power residual for T).

---

## Fixer14 fix hold vs live

| Fix | Intent | Live |
|---|---|---|
| 1 hard-gate force-inject | S / T upgrade | **S HELD** (force_inject board + DRC clean). **T partial** — add_power fired, wire/sim models not |
| 2 AC1 per-check diagnosis | U gradeable | **HELD** — per_check/zero_checks/brief_hints in criterion_AC1 |
| 3 frontier rank floor 5 | routing | **HELD** — xai-sub/grok-4.5 on analysis BUILD/SPEC without ollama-all |

---

## A–Q regression notes

### A accepted ops: PASS
- new_part → doc-cpp00000041; add_features ok path live

### B capability separation: preflight PASS; live tool residual
- layout/plan failed without typed-refusal envelope (regressed residual class from mega-13)
- grade tool-path delegate=0 / spawn=0

### C oneshot: PASS (ledger)

### D/E/F/I: PASS

### N/O/P/Q: PASS (O/P upgraded)

### R/T: FAIL

---

## Token / conservation

- Wall **797.46s**
- Models: xai-sub/grok-4.5 ×10, ollama/llama3.2:latest ×13 (leaves)
- Account `17d17fb1-…` held
- Rank floor 5: analysis min_rank 3–4 selected grok-4.5 rank 5 (no user pin required for floor alone; harness still pins rankings for belt-and-suspenders)
- API model id **grok-4.5** only

---

## Post-primary Fixer14 Fix4 (not in this primary)

After primary grade, ExceptionalYak bound Fix4 (unit-green 77, fixes.md):
- Orphan `ecad_add_power` no longer counts as wire for board unlock.
- Require real `ecad_wire`; force Device:R value=1k when schematic is IC-only.
- Chain order: passive → wire → board → DRC.

Addresses T residual fingerprint for a subsequent launch; this primary remains
the graded evidence for S/U PASS and T FAIL.

---

## DAG node outcomes

**done (18):** n_intake, n_spec_DL1–DL5, n_deliver_DL5, n_build_DL4, n_build_DL3, n_build_DL1, n_verify_DL1–DL4, n_deliver_DL1–DL4

**failed (5):** n_spec_done (max_iters), n_organize (no_code), n_layout (delegate_or_typed_refusal), n_plan (no_code), n_build_DL2 (delegate_or_typed_refusal)

---

## TOP residual (next cycle)

1. **T / AC5 sim models + nets** — force-inject `ecad_wire` + place R/C/V with VALUES for SPICE (not only power port / IC symbols). Empty-netlist class persists after board product success.
2. **AC4 ERC power_not_driven** — DRC clean but ERC 18 errors; need driven power ports / net labels on power_in pins.
3. **AC1 soft zeros** — geometry product OK; battery still FAIL on arg-miss + ECAD-on-CAD checks. Diagnosis now gradeable (U); optional next: battery profile skip ECAD checks on mechanical docs without weakening scores.
4. **Live PASS ≥7** — need one of AC1/AC4/AC5 to flip while holding 6.
5. **Typed-refusal live** — layout/plan/organize still budget-exhaust without refuse envelope.
6. **Phase cursor honesty** — phases_completed stayed `["intake","specify"]` while DAG advanced through BUILD/VERIFY/DELIVER.

---

## Fingerprints

| ID | Status |
|---|---|
| S board+DRC executed | **PASS** (force_inject + DRC errors=0) |
| T AC5 wired SPICE | **FAIL** (no wire; U* no sim model) |
| U AC1 diagnosis | **PASS** (per_check artifacts) |
| Q live PASS ≥6 | **PASS** (6; ≥7 miss) |
| P/O upgrade | **PASS** |
| R | **FAIL** |
| A–N / K–M regression | **PASS** (B/G/J live residual) |
| Fix8 live gate | **PASS** |

---

## Artifacts

```
reports/runs/mega-14/
  run_mega.py grade_run.py _preflight.py findings.md progress.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json fw_artifact.json grade_summary.json
  criterion_AC1.json … criterion_AC9.json criterion_BATTERY.json
```
