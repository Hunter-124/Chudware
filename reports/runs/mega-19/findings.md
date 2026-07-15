# MegaRun19 Findings

## Run summary

- **Primary graded run ID:** `mega-19-fc-rocket-20260715` (**a3** final; rebind2 cascade budget)
- **Archives:** `attempt-1-primary-graded-a19-fail/` (a1), `attempt-2-primary-graded-wire-zero/` (a2)
- **Baseline:** `6d11238b`
- **Fixer19:** ColdMuskox (D19/A19/B19/C19 + rebind1 force-after-power + rebind2 cascade budget; 119p) — **standing down** (mega-20 wave)
- **Harness:** MegaRun19=`UnemployedTuna`
- **Frontier:** xai-sub `grok-4.5` @ `06dc3359`; mid openrouter deepseek; leaves ollama
- **Spend Δ:** `$0.0` (guard OK)
- **Wall a3:** **509.8s** terminal `done`/`deliver_partial`
- **Hand-off residual code:** Fixer20=`LiberalWarbler` (A20/B20 vs a3 ledger)

### Pre-flight: PASS (all attempts)

Daemon matches disk; account ACTIVE; hard→grok-4.5; leaf→ollama; no other xai; Fixer19 fixes bound (+ rebinds).

---

## Attempt matrix

| Attempt | Wall | Live AC | A19 | B19 | C19 | D19 | Z1 | Z3 | Z4 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a1 | 459s | 6 | FAIL | FAIL | FAIL | PASS | PASS | FAIL | PASS | PASS | force idle; empty-netlist |
| a2 | 595s | 6 | FAIL | FAIL | PASS | PASS | PASS | PASS | PASS | PASS | cascade budget-starved pre-wire |
| **a3** | **509.8s** | **4** | **PARTIAL** | **PARTIAL** | **PASS** | **PASS** | **PASS** | **PASS** | **PASS** | **PASS** | cascade full; U1↔R1 not rail; DRC on board |

---

## Axis scoreboard (a3)

| Axis | Verdict | Evidence |
|---|---|---|
| **A19** | **PARTIAL** | force cascade live: wire U1.3↔R1.1 (cross-ref, not rail); rail_wire_n=0; power_not_driven remains; AC4 FAIL |
| **B19** | **PARTIAL** | force DRC used board doc 5cc769570819 (clean DRC errors=0); AC4 FAIL on ERC errors=1 not sch-not-board; grader board_doc_ids harvest empty → PARTIAL |
| **C19** | **PASS** | PASS: sim_n=2; honest AC5 null V; V invocation clean |
| **D19** | **PASS** | unit green |
| **Z1** | **PASS** | 9/9 criteria |
| **Z2r** | **PARTIAL** | force power+wire+board; not rail; AC4 FAIL |
| **Z3** | **PASS** | simulate live OP/mna |
| **Z4** | **PASS** | AC5 null V honest FAIL |
| **G18** | **PARTIAL** | live 4 < 7 |
| **Y** | **PASS** | mid deepseek enabled |
| **W** | **PASS** | wire+power executed (force path) |
| **X** | **PASS** | not empty-netlist |
| **S/P** | **PARTIAL/PARTIAL** | board+drc executed; DRC clean on board doc |
| **V** | **PASS** | no sig errors |
| **Fix8** | **PASS** | gate + force inject live |

**Live AC PASS a3:** **4**/9 — AC6, AC7, AC8, AC9. FAIL: AC1–AC5.

AC1: `critical_scores_zero=['eval_check', 'is_manifold', 'measure_part']`
AC4: `errors=1 (drc.errors=0, erc.errors=1)` — DRC clean on board `5cc769570819`; ERC errors=1 (power_not_driven class)
AC5: `probe 'v(3v3)' is null in ecad_simulate payload (net '3V3' not driven / no finite value)`

---

## a3 cascade evidence (ledger)

1. place U1 Buck_3V3 ok on sch `596497ba8ad5`
2. model `ecad_add_power(GND)` ok
3. force `ecad_add_power(3V3)` ok
4. force place `Device:R` 1k → R1
5. force **wire U1.3 ↔ R1.1** (cross-ref, **not** power pin→rail)
6. force `board_from_schematic` ok
7. force `ecad_run_drc` on board doc **`5cc769570819`** → **0 errors** (B19 path fixed vs sch-not-board)
8. force `ecad_simulate` OP probes V(3V3) → null (device_count=1; U1 no sim model)

Force inject counts (grader): power=1 wire=1 place=1 n≈7.

---

## Progress

| Residual | a1 | a2 | a3 |
|---|---|---|---|
| Force cascade fires | no | starved | **yes** |
| Wire live | 0 | 0 | **2** (U1-R1) |
| Rail wire | no | no | **no** |
| DRC on board doc | sch error | sch | **board clean** |
| Empty-netlist AC5 | yes | no | no |
| Live AC ≥8 | 6 | 6 | **4** (mech AC1–3 starved) |

---

## TOP residual → mega-20 / Fixer20

1. **A19/A20:** force wire must prefer **power_in → 3V3/GND port** refs from add_power, not U1↔R1 passive.
2. **ERC power_not_driven→0** after rail wires (AC4 full PASS).
3. **AC5 finite V(3V3):** Device:V + wired rail (C path partial).
4. **Mechanical AC1–3** regression this run (new_part/add_features=0) — restore parallel CAD path.
5. Hold **D19/C19/Z1/Z3/Z4/Y/Fix8/W**.

---

## Fingerprints

| ID | a3 |
|---|---|
| D19 power detect | **PASS** |
| C19 source/AC1 invoke | **PASS** |
| A19 rail wire | **PARTIAL** |
| B19 board DRC doc | **PARTIAL** (board DRC live; axis harvest/AC4 ERC) |
| Z2r force cascade | **PARTIAL** |
| Live ≥8 | **FAIL** (4) |
| Account 06dc3359 | ACTIVE |
| Spend $8 | held |

---

## Artifacts

```
reports/runs/mega-19/
  attempt-1-primary-graded-a19-fail/
  attempt-2-primary-graded-wire-zero/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md
  events.jsonl ledger.json mission.json status.json  # a3
  criterion_AC* grade_summary run_summary status_report
```
