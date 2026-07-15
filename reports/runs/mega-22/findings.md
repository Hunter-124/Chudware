# MegaRun22 Findings

## Run summary

- **Primary graded run ID:** `mega-22-fc-rocket-20260715` (**a2**)
- **Baseline commit:** `0f43d126`
- **Fixer peer:** Fixer22=`GrossShark` (P22/Q22/R22; rebinds a1+a2; up to 163p)
- **Harness peer:** MegaRun22=`UnusualBaboon`
- **Daemon:** hostd matched disk at launch (FINE; Python-only baseline)
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359`
- **Mid-fallback:** openrouter paid deepseek; **Leaves:** ollama
- **Spend Δ:** `$0.0` (guard $8 OK)
- **Wall a2:** **675.98s**
- **Terminal a2:** `done` / `deliver_partial`

### Pre-flight: PASS

Account ACTIVE, hard→grok-4.5, mid deepseek, leaf ollama, hostd match, no other xai, Fixer22 bound.

### Attempts

| Attempt | Wall | Terminal | Live AC | Notes |
|---|---|---|---|---|
| a1 | 657s | blocked mech_force | 4/9 | archived |
| **a2** | **676s** | **done/deliver_partial** | **7**/9 | **PRIMARY** Q22 PASS |
| a3 | 697s | dag_error:checkpoint_blob | 0 | infra archive |
| a4 | ~8min | process died no terminal | 0 | infra archive |

---

## Axis scoreboard (a2 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **P22** | **PARTIAL** | device_count=2 driven; AC5 V(3V3)=**null** |
| **Q22** | **PASS** | board→optimize_traces→drc; **DRC errors=0** ERC=0 |
| **R22** | **PARTIAL** | no `'top face'`; soft_zero measure_between score=0 |
| **M21** | **PASS** | airframe product; AC2/AC3 PASS |
| **N21** | **PASS** | resolved_net=3V3 honest null FAIL |
| **A20** | **PARTIAL** | wires+ERC clean; rail_wire residual |
| **B20** | **PASS** | board doc DRC clean |
| **C19/D19** | **PASS** | |
| **Z1–Z4** | **PASS** | |
| **G18** | **PASS** | live 7≥7; xai-sub |
| **Y/W/X/S/P/V/Fix8/A** | **PASS** | |

**Live AC PASS:** **7**/9 — AC2, AC3, AC4, AC6, AC7, AC8, AC9. FAIL: AC1, AC5.

- AC4: **PASS** errors==0 (mega-21 clearance×31 **closed** via Q22)
- AC5: V(3V3) null — not in-band
- AC1: soft_scores_zero measure_between (face vocab residual closed)

---

## TOP residual

1. P22/AC5: finite non-null V on 3V3 in [3.135,3.465] or honest numeric fail
2. R22/AC1: measure_between real score or honest SKIP
3. Target ≥8/9 not met (7/9 best)

---

## Artifacts

```
reports/runs/mega-22/
  attempt-1-primary-graded-blocked-mech/
  attempt-2-primary-graded-live7/   # PRIMARY
  attempt-3-primary-failed-checkpoint-blob/
  attempt-4-primary-died-no-terminal/
  run_mega.py grade_run.py _preflight.py wait_and_launch.sh fixes.md findings.md
  criterion_AC* status*.json grade_summary events ledger (from a2)
```
