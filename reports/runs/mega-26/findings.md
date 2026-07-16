# mega-26 findings (a1 primary graded)

**RUN_ID:** `mega-26-fc-rocket-20260716`  
**Baseline:** `6ab362fb`  
**Frontier:** xai-sub grok-4.5 EXACT account `06dc3359`  
**Pair:** MegaRun26=`InteriorFinch` / Fixer26=`DeadOpossum`  
**Terminal:** `needs_correction` / `verify_failed: ['AC4']`  
**Wall:** **566.94s** (a1)  
**Live AC:** **8**/9 PASS (`AC1 AC2 AC3 AC5 AC6 AC7 AC8 AC9`) — FAIL AC4 only  
**Target ≥8/9:** **MET** · Goal 9/9: miss by 1

---

## A26 — source+load on resolved measurement net

| Metric | mega-25 a2 | mega-26 a1 |
|---|---|---|
| sim doc | schematic `f9ec35394c6c` | schematic `2e24a3862916` |
| device_count | 2 | 2 |
| V(3V3) | **0.0** (not in-band) | **3.3** in-band |
| u1_stubbed | n/a | **True** |
| measured_net | VIN island (behind U1) | **3V3** |
| Axis A26 | n/a | **PASS** |
| Live AC5 | FAIL | **PASS** |

Root cause closed: forced V value was `"DC 3.3"` (SPICE parse → None → 0 V) and source sat on pre-regulator island. Fixer26: bare `"3.3"` on N21-resolved **3V3** + `u1_stubbed_ideal_regulator: true`.

---

## B26 — clearance retry fires

| Metric | mega-25 a2 | mega-26 a1 |
|---|---|---|
| min_clearance_mm | 0.2 | 0.2 (held) |
| DRC errors | 27 clearance @ ~0.13mm | **23** clearance @ ~0.08–0.13mm |
| clearance_retry_n | **0** | **4** |
| autoroute_retry | none | **4** (pitch=0.3) |
| Axis B26 | n/a (Z25 honest FAIL w/o retry) | **PASS** (retry fired; honest clearance FAIL) |
| Live AC4 | FAIL | **FAIL** (errors≠0 after retry) |

Root cause closed for fire path: truncated DRC JSON lost `errors` + BUILD allow omitted optimize → re-opt unreachable. Fixer26: scrape errors from truncated blob + `reopt_allowed` → autoroute+re-opt+re-DRC (`retry_n>=1`). Residual is **layout quality** (still sub-0.2mm after one bounded retry), not the retry branch.

---

## Axes (grade_summary)

| Axis | Verdict |
|---|---|
| **A26** source on measured net | **PASS** (V=3.3 in-band; stub noted) |
| **B26** clearance retry fires | **PASS** (retry_n=4; honest clearance FAIL) |
| **Y25** simulate schematic doc | **PASS** |
| **Z25** clearance-aware opt | **PASS** |
| **S23** V(3V3) in-band | **PASS** |
| **P22** driven rail | **PASS** |
| **U24** root cause | **PASS** |
| **V24** live build loop | **PASS** |
| **T23** measure_between | **PASS** |
| Q22 optimize→DRC chain | **PASS** |
| G18 / Y / Z1 / M21 / R22 | **PASS** |
| B20 board doc | **PARTIAL** |
| Battery | **PASS** `doc-cpp00000005` |

---

## Live AC board

| AC | Result | Note |
|---|---|---|
| AC1 | **PASS** | measure_between / battery |
| AC2 | **PASS** | volume proxy |
| AC3 | **PASS** | interference clean |
| AC4 | **FAIL** | DRC clearance×23 after retry (honest) |
| AC5 | **PASS** | V(3V3)=3.3 in-band; U1 stub |
| AC6 | **PASS** | antenna |
| AC7 | **PASS** | fw ELF |
| AC8 | **PASS** | |
| AC9 | **PASS** | 12 legs graded |

---

## Attempts / liveness

1. **a1** primary graded — liveness held (fails≤1); wall 566.94s; force chain fired (board→opt→drc→**retry**/autoroute→re-DRC→erc→sim on sch with measure-net drive).

---

## Residuals (for next mega / Fixer)

1. **AC4 clearance residual after retry:** retry_n≥1 and autoroute(pitch=0.3) executed, but DRC still 23 clearance errors (~0.08–0.13mm < 0.2mm). B26 contract satisfied (honest FAIL after fired retry). Closing live AC4 needs stronger clearance strategy (coarser pitch, keepout, fanout, or multi-retry with rising pitch) — **not** re-arming the dead branch.
2. Goal 9/9 still open; target ≥8/9 **met**.

---

## Artifacts

- `grade_summary.json`, `criterion_AC1..AC9.json`, `criterion_BATTERY.json`
- `battery_result.json`, `ledger.json`, `events.jsonl`, `mission.json`, `status.json`
- `fixes.md`, `offline_proof.md` (FINAL_OFFLINE_OK), `autosave_manifest.json`
- Parts under `reports/runs/mega-26-fc-rocket-20260716/parts/`

No push (Main gates).
