# MegaRun19 Findings

## Run summary

- **Primary graded run ID:** `mega-19-fc-rocket-20260715` (**a2** after Fixer19 force-cascade rebind)
- **a1 archive:** `attempt-1-primary-graded-a19-fail/` (live 6/9; force cascade idle; Z3 FAIL)
- **Baseline commit:** `6d11238b`
- **Fixer peer:** Fixer19=`ColdMuskox` (D19/A19/B19/C19 + post-a1 rebind force-after power; 118p)
- **Harness peer:** MegaRun19=`UnemployedTuna`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running matches disk
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359`
- **Mid-fallback:** openrouter paid deepseek; **Leaves:** ollama
- **Spend Δ:** `$0.0` (guard $8 OK; frontier xai-sub)
- **Wall a2:** **594.81s**
- **Terminal a2:** `done` / `deliver_partial`

### Pre-flight: PASS (a1 + a2)

Account ACTIVE, hard→grok-4.5, leaf→ollama, hostd match, no other xai consumer, Fixer19 fixes bound (+ rebind).

---

## Attempt matrix

| Attempt | Wall | Terminal | Live AC | A19 | B19 | C19 | D19 | Z1 | Z3 | Z4 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a1 | 459.4s | done/deliver_partial | 6 | FAIL | FAIL | FAIL | PASS | PASS | FAIL | PASS | PASS | power×4; wire=0; empty-netlist AC5 |
| **a2** | **594.81s** | done/deliver_partial | **6** | **FAIL** | **FAIL** | **PASS** | **PASS** | **PASS** | **PASS** | **PASS** | **PASS** | power×8; wire=0 still; AC5 null V; Z3/X/C19 up |

---

## Axis scoreboard (a2 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **A19** | **FAIL** | ecad_add_power×8; ecad_wire=0; force_inject events=0 despite rebind unit green; ERC dirty; AC4 FAIL |
| **B19** | **FAIL** | AC4 still sch-not-board class: check tool reported error: ok=false; board_from_schematic=0 |
| **C19** | **PASS** | AC5 now probe null (not empty-netlist); simulate path graded; C19 PASS; AC1 still measure_between soft zero |
| **D19** | **PASS** | PASS unit/structural |
| **Z1** | **PASS** | 9/9 criteria |
| **Z2r** | **FAIL** | power live; wire/force still 0 |
| **Z3** | **PASS** | PASS — AC5 not empty-netlist; simulate evidence present |
| **Z4** | **PASS** | PASS — AC5 FAIL with null V(3V3) honest |
| **G18** | **PARTIAL** | live 6 < 7; xai-sub live |
| **Y** | **PASS** | mid deepseek enabled; free off |
| **W** | **FAIL** | no wire; AC4 FAIL |
| **X** | **PASS** | honest FAIL not empty-netlist |
| **S/P** | **FAIL/FAIL** | board_from_schematic=0 |
| **V** | **PASS** | no sig errors |
| **Fix8** | **PASS** | gate live |

**Live AC PASS:** **6**/9 — AC2, AC3, AC6, AC7, AC8, AC9. FAIL: AC1, AC4, AC5.

AC1: `soft_scores_zero=['measure_between']`
AC4: `check tool reported error: ok=false`
AC5: `probe 'v(3v3)' is null in ecad_simulate payload (net '3V3' not driven / no finite value)`

---

## Progress vs a1

Z3 FAIL→PASS; X FAIL→PASS; C19 FAIL→PASS; AC5 empty-netlist→null V(3V3); wire/force still idle

Force cascade rebind unit (`test_post_power_success_force_cascades_wire`) green, but **live a2 still never emitted `ecad_chain_force_inject` or `ecad_wire`**. Model path: place + add_power×8 then left ECAD incomplete; thrash/force-after threshold still not crossed in production turn, or gate force path not entered after successful power.

---

## TOP residual (next)

1. Live force cascade must actually run after model add_power success (not only unit) — emit wire power_in→rail refs (A19).
2. board_from_schematic + board_doc_id into DRC (B19).
3. AC5 finite V(3V3) needs driven net (source + wire to rail).
4. AC1 measure_between soft zero → real refs or honest SKIP.
5. Hold D19/Z1/Z3/Z4/Y/Fix8/C19; target ≥8 live AC.

---

## Artifacts

```
reports/runs/mega-19/
  attempt-1-primary-graded-a19-fail/   # a1 archive + NOTE.txt
  run_mega.py grade_run.py _preflight.py findings.md fixes.md
  preflight_*, spawn_probe, auto_pick_smoke, daemon_preflight
  events.jsonl ledger.json mission.json status.json  # a2
  criterion_AC1..AC9 grade_summary run_summary status_report
```
