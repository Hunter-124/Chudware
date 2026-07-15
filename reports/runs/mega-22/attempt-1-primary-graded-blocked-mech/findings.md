# MegaRun22 Findings

## Run summary

- **Primary graded run ID:** `mega-22-fc-rocket-20260715` (**a1**)
- **Baseline commit:** `0f43d126`
- **Fixer peer:** Fixer22=`GrossShark` (P22 SPICE driven rail + Q22 board→optimize_traces→drc + R22 measure_between faces; 158p)
- **Harness peer:** MegaRun22=`UnusualBaboon`
- **Daemon:** hostd `night2-3075-gb0e66f3d-dirty` matches disk (FINE; Python-only baseline)
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359`
- **Mid-fallback:** openrouter paid deepseek enabled; **Leaves:** ollama
- **Spend Δ:** `$0.0` (guard $8 OK; frontier xai-sub)
- **Wall a1:** **657.2s**
- **Terminal a1:** `blocked` / `mech_force_add_features_failed` (airframe box batch rejected f5; rolled_back)

### Pre-flight: PASS

Account ACTIVE, hard→grok-4.5, mid openrouter deepseek enabled, leaf→ollama, hostd match, no other xai consumer, Fixer22 **fixes bound** (P22/Q22/R22 probe PASS; 158p).

---

## Axis scoreboard (a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **P22** | **FAIL** | AC5: no active schematic — sim never ran; force_n=0; no finite V |
| **Q22** | **FAIL** | board_from_schematic=0, ecad_run_drc=0; chain incomplete (ECAD build starved) |
| **R22** | **PARTIAL** | no bad-face `'top face'` residual; AC1 FAIL on critical zeros (empty geom after rollback) |
| **M21** | **PASS** | force attempted (new_part ok) — but force add_features rejected → terminal block |
| **N21** | **FAIL** | no resolved_net / finite V (sim never executed) |
| **A20** | **FAIL** | no rail wires (no ECAD place/wire path) |
| **B20** | **FAIL** | no board doc / DRC |
| **C19** | **FAIL** | no sim source path live; AC1 not zero-defect product |
| **D19** | **PASS** | unit surface |
| **Z1** | **PASS** | 9/9 criteria graded |
| **Z2** | **FAIL** | no power-then-wire live |
| **Z2r** | **FAIL** | no add_power force execute live |
| **Z3** | **FAIL** | no SPICE on wired netlist |
| **Z4** | **PASS** | AC5 FAIL with tool-level detail (not NOT-CHECKED) |
| **G18** | **PARTIAL** | xai-sub live; live AC 4 < 7 |
| **Y** | **PASS** | mid deepseek enabled; leaf ollama |
| **W** | **FAIL** | no wire+power ERC path |
| **X** | **PASS** | AC5 honest FAIL not empty-netlist shape |
| **S/P** | **FAIL** | board+DRC not executed |
| **V** | **PASS** | no signature-error zeros |
| **Fix8** | **UNKNOWN** | no chain-gate events (ECAD never advanced) |
| **A** | **FAIL** | add_features_ok=0 (force batch rejected) |
| **B/C/D** | **PASS** | delegate/oneshot/terminal |

**Live AC PASS:** **4**/9 — AC3, AC6, AC7, AC8. FAIL: AC1, AC2, AC4, AC5, AC9.

- AC1: critical_scores_zero=`eval_check,is_manifold,measure_part` (empty/rolled-back airframe; R22 face-query residual closed)
- AC2: document has no bodies to evaluate
- AC3: **PASS** (interference empty)
- AC4: check ok=false (no board/schematic product)
- AC5: `no active schematic — call ecad_new_schematic(name) first`
- AC6–AC8: **PASS** (antenna + fw_build ELF + Renode/sensor path)
- AC9: legs missing verdicts: `schematic`

---

## P22/Q22/R22 vs mega-21 residual

mega-21 a1: live 6/9; N21 resolved_net=3V3 with V=0.0; AC4 DRC clearance×31 (no optimize); AC1 soft measure_between `'top face'`.

mega-22 a1: Fixer22 unit-bound P22/Q22/R22 (158p) but **primary never exercised ECAD build** — budget exhaustion on `n_build_DL1/DL2/DL3` + layout/plan; deliver hard-stopped on **mech_force_add_features_failed** (box f5). R22 face-vocab residual appears closed in battery adapter (no `'top face'` soft zero), but product geom absent.

---

## TOP residual (next)

1. **Unblock deliver:** mech force `add_features` must land airframe (or typed refusal relaunch) — box batch f5 rejection must not hard-block after ECAD/FW legs.
2. **ECAD budget / path:** ensure schematic→wire→sim and board→optimize_traces→drc actually run before budget exhaust; P22/Q22 force inject only fire on a live ECAD session.
3. **P22 live:** source V + load R on resolved rail; netlist readback; V(3V3) in-band or honest non-zero numeric fail.
4. **Q22 live:** full chain with DRC errors==0 or honest FAIL + error class.
5. Hold M21/D19/Z1/Z4/Y/V/X; restore N21/A20/B20/S via ECAD path.

---

## Artifacts

```
reports/runs/mega-22/
  run_mega.py grade_run.py _preflight.py wait_and_launch.sh fixes.md findings.md
  preflight_*, spawn_probe, auto_pick_smoke, daemon_preflight, no_other_xai_consumer
  events.jsonl ledger.json mission.json status.json primary.log
  criterion_AC1..AC9 grade_summary run_summary status_report status_yield axes_short
```
