# MegaRun22 Findings

## Run summary

- **Primary graded run ID:** `mega-22-fc-rocket-20260715` (**a2**; a1 archived)
- **Baseline commit:** `0f43d126`
- **Fixer peer:** Fixer22=`GrossShark` (P22/Q22/R22 + a1 rebind: mech soft_refusal + ecad_product_gate; 162p)
- **Harness peer:** MegaRun22=`UnusualBaboon`
- **Daemon:** hostd `night2-3075-gb0e66f3d-dirty` matches disk (FINE; Python-only baseline)
- **Frontier:** `xai-sub:grok-4.5` EXACT on `06dc3359`
- **Mid-fallback:** openrouter paid deepseek enabled; **Leaves:** ollama
- **Spend Δ:** `$0.0` (guard $8 OK; frontier xai-sub)
- **Wall a2:** **675.98s**
- **Terminal a2:** `done` / `deliver_partial`

### Pre-flight: PASS

Account ACTIVE, hard→grok-4.5, mid openrouter deepseek, leaf→ollama, hostd match, no other xai consumer, Fixer22 fixes bound (initial 158p + rebind 162p).

### Attempts

| Attempt | Wall | Terminal | Live AC | Notes |
|---|---|---|---|---|
| **a1** | 657s | `blocked` / mech_force_add_features_failed | 4/9 | ECAD starved; archived `attempt-1-primary-graded-blocked-mech/` |
| **a2** | **676s** | `done` / `deliver_partial` | **7**/9 | Q22 PASS (DRC clean); P22/R22 PARTIAL; FAIL AC1+AC5 |

---

## Axis scoreboard (a2 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **P22** | **PARTIAL** | device_count=2 + source present; AC5 `V(3V3)=null` (not in-band) |
| **Q22** | **PASS** | board→optimize_traces→drc order OK; **DRC errors=0**, ERC=0 |
| **R22** | **PARTIAL** | no bad-face `'top face'`; still soft_zero measure_between score=0 (mb×4, qg×2) |
| **M21** | **PASS** | new_part+add_features live (ok×24); AC2/AC3 PASS |
| **N21** | **PASS** | resolved net `3V3` recorded; honest null FAIL |
| **A20** | **PARTIAL** | wires live; power_not_driven=0; AC4 PASS; rail_wire_n=0 residual |
| **B20** | **PASS** | board doc `f02aaad89536` DRC on board doc; S PASS |
| **C19** | **PASS** | sim source path; AC1 zero-invocation defects for face vocab |
| **D19** | **PASS** | unit surface |
| **Z1** | **PASS** | 9/9 criteria |
| **Z2** | **PASS** | power then wire live |
| **Z2r** | **PASS** | add_power force execute live |
| **Z3** | **PASS** | simulate executed; not empty-netlist |
| **Z4** | **PASS** | AC5 explicit null FAIL |
| **G18** | **PASS** | xai-sub×11; live 7≥7 |
| **Y** | **PASS** | mid deepseek enabled; leaf ollama |
| **W** | **PASS** | wire+power; ERC clean; AC4 PASS |
| **X** | **PASS** | honest FAIL not empty-netlist |
| **S/P** | **PASS** | board+DRC executed clean |
| **V** | **PASS** | no sig errors |
| **Fix8** | **PASS** | gate live |
| **A** | **PASS** | new_part+add_features accepted |

**Live AC PASS:** **7**/9 — AC2, AC3, AC4, AC6, AC7, AC8, AC9. FAIL: AC1, AC5.

- AC1: `soft_scores_zero=['measure_between']` (score=0; no `'top face'` bad-face residual)
- AC2/AC3: **PASS** (volume + interference)
- AC4: **PASS** `errors==0` (drc+erc) — mega-21 clearance×31 residual **closed** via Q22 optimize chain
- AC5: `probe 'v(3v3)' is null` — net `3V3` not driven / no finite value (device_count=2)
- AC6–AC9: **PASS**

---

## P22/Q22/R22 vs mega-21 / a1

| Residual | mega-21 a1 | mega-22 a2 |
|---|---|---|
| AC4 DRC clearance | errors=31, no optimize | **errors=0** (Q22 PASS) |
| AC5 V | V=0.0 on 3V3 | **null** on 3V3 (still out of band; P22 PARTIAL) |
| AC1 measure_between | bad side `'top face'` | soft zero remains; bad-face residual closed (R22 PARTIAL) |
| Live AC | 6/9 | **7**/9 (target ≥8) |

---

## TOP residual (next)

1. **P22/AC5:** drive finite non-null V on resolved `3V3` (source+load topology readback) so V ∈ [3.135,3.465] or honest numeric fail naming actual voltage — null still fails band.
2. **R22/AC1:** measure_between real score (>0) or honest SKIP — soft zero remains despite valid-face path.
3. Hold Q22/M21/N21/B20/Z*/Y/W/X/Fix8/G18; lift live AC ≥8.

---

## Artifacts

```
reports/runs/mega-22/
  attempt-1-primary-graded-blocked-mech/   # a1 archive
  run_mega.py grade_run.py _preflight.py wait_and_launch.sh fixes.md findings.md
  events.jsonl ledger.json mission.json status.json primary.log
  criterion_AC1..AC9 grade_summary run_summary status_report status_yield axes_short
```
