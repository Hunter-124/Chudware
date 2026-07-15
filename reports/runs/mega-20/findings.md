# MegaRun20 Findings

## Run summary

- **Primary graded run ID:** `mega-20-fc-rocket-20260715` (**a1**)
- **Baseline commit:** `b0e66f3d`
- **Fixer peer:** Fixer20=`LiberalWarbler` (A20/B20 rebond + board doc_id; 127p)
- **Harness peer:** MegaRun20=`HugeAlpaca`
- **DaemonFix:** DrivingHippopotamus H1+H3 → hostd `night2-3075-gb0e66f3d-dirty` matches disk
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359`
- **Mid-fallback:** openrouter paid deepseek (key restored env-only from omp history for Y); **Leaves:** ollama
- **Spend Δ:** `$0.0` (guard $8 OK; frontier xai-sub)
- **Wall a1:** **404.47s**
- **Terminal a1:** `done` / `deliver_partial`

### Pre-flight: PASS

Account ACTIVE, hard→grok-4.5, mid openrouter deepseek enabled, leaf→ollama, hostd match, no other xai consumer, Fixer20 fixes bound + DaemonFix rebuild-done.

---

## Axis scoreboard (a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **A20** | **PASS** | wire×2 rail (VIN.+→R1.1); power_not_driven=0; AC4 PASS |
| **B20** | **PASS** | board_from_schematic minted `c01d5499cdb8`; DRC args doc_id=board; errors=0; S PASS |
| **C19** | **PASS** | AC5 honest null V with source/VIN path; AC1 diagnosis present |
| **D19** | **PASS** | unit/structural |
| **Z1** | **PASS** | 9/9 criteria |
| **Z2/Z2r** | **FAIL** | force_inject=0; no ecad_add_power (model VIN path) |
| **Z3** | **PASS** | simulate executed; not empty-netlist |
| **Z4** | **PASS** | AC5 FAIL with null V(3V3) honest |
| **G18** | **PARTIAL** | live 5 < 7; xai-sub live |
| **Y** | **PASS** | mid deepseek enabled |
| **W** | **FAIL** | no ecad_add_power; wire ok + AC4 clean but W contract wants add_power |
| **X** | **PASS** | honest FAIL not empty-netlist |
| **S/P** | **PASS** | board+DRC executed; errors=0 |
| **V** | **PASS** | no sig errors |
| **Fix8** | **PASS** | gate live |
| **A** | **FAIL** | new_part=0 / add_features=0 (mech starved) |

**Live AC PASS:** **5**/9 — AC4, AC6, AC7, AC8, AC9. FAIL: AC1, AC2, AC3, AC5.

AC1: `critical_scores_zero=['eval_check', 'is_manifold', 'measure_part']` (no part doc)
AC2/AC3: `No current part. Call new_part(...) first`
AC4: `errors==0 (drc.errors=0, erc.errors=0)` on board `c01d5499cdb8`
AC5: `probe 'v(3v3)' is null ... net '3V3' not driven`

---

## A20/B20 vs mega-19 a2 residual

mega-19 a2: power×8 no ref, wire=0, force idle, DRC on sch.
mega-20 a1: model placed VIN + Device:R, wired VIN.+→R1.1 (rail), board_from_schematic + DRC on board doc clean. **A20+B20 PASS live.** Force cascade unused.

---

## TOP residual (next)

1. Mechanical path: force/ensure new_part+airframe before verify (AC1–3) — live ≥8 blocked here.
2. AC5 finite V(3V3): VIN path did not drive 3V3 probe; still need bonded 3V3/source on rail or honest sim source on 3V3 net.
3. W/Z2r: either count VIN rail path as power-satisfied for W, or ensure force rebond still runs when model skips add_power.
4. Hold A20/B20/C19/D19/Z1/Z3/Z4/Y/Fix8/S.

---

## Artifacts

```
reports/runs/mega-20/
  run_mega.py grade_run.py _preflight.py fixes.md findings.md
  preflight_*, spawn_probe, auto_pick_smoke, daemon_preflight
  events.jsonl ledger.json mission.json status.json primary.log
  criterion_AC1..AC9 grade_summary run_summary status_report
```
