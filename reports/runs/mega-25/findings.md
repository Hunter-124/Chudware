# mega-25 findings (a2 primary graded)

**RUN_ID:** `mega-25-fc-rocket-20260715`  
**Baseline:** `871e0fb9`  
**Frontier:** xai-sub grok-4.5 EXACT account `06dc3359`  
**Pair:** MegaRun25=`PresentClam` / Fixer25=`ExcellentPiranha`  
**Terminal:** `needs_correction` / `verify_failed: ['AC4', 'AC5']`  
**Wall:** **901.92s** (a2)  
**Live AC:** **7**/9 PASS (`AC1 AC2 AC3 AC6 AC7 AC8 AC9`) — FAIL AC4+AC5  
**Target ≥8/9:** miss by 1 (same live count as mega-24; axes improved)

---

## Y25 — simulate → schematic doc

| Metric | mega-24 a3 | mega-25 a2 |
|---|---|---|
| sim doc | board `767f0f0fa678` | schematic `f9ec35394c6c` |
| error | `doc is a pcb, not a schematic` | **gone** |
| V(3V3) | null / never measured | **0.0 finite** (not in-band) |
| Axis Y25 | n/a | **PARTIAL** (routing PASS, in-band FAIL) |

Evidence: forced `ecad_simulate` args `doc_id=f9ec35394c6c` (= sch from `ecad_new_schematic`); `sim_on_schematic=true`, `pcb_not_schematic_error=false`. Residual is **rail drive** (P22 class): `device_count=2`, `U1 no sim model`, OP converged to 0.0 V.

---

## Z25 — clearance-aware board chain

| Metric | mega-24 a3 | mega-25 a2 |
|---|---|---|
| optimize_traces | yes (no min_clearance) | yes **`min_clearance_mm=0.2`** |
| DRC errors | 6 clearance @ 0.1296 | **27** clearance @ ~0.1299 |
| clearance_retry / autoroute | none | **retry_n=0** (detect present, re-route not fired) |
| Axis Z25 | n/a | **PASS** (honest FAIL naming clearance after clearance-aware opt) |

Contract allows honest FAIL naming clearance when clearance-aware attempt made. Live AC4 still FAIL (errors≠0). Residual: bounded re-route (`ecad_autoroute pitch=0.3`) did not execute after dirty DRC.

---

## Axes (grade_summary)

| Axis | Verdict |
|---|---|
| **Y25** simulate schematic doc | **PARTIAL** (sch routed; V=0.0) |
| **Z25** clearance-aware opt | **PASS** (min_clearance=0.2; honest clearance FAIL) |
| **U24** root cause | **PASS** |
| **V24** live build loop | **PASS** (tools high, fails=0, live 7) |
| **T23** measure_between | **PASS** |
| **S23** V(3V3) in-band | **PARTIAL** (finite 0.0, not in-band) |
| Q22 optimize→DRC chain | **PASS** |
| G18 / Y / Z1 / M21 / R22 | **PASS** |
| B20 board doc | **PARTIAL** |
| P22 driven rail | **PARTIAL** |
| Battery | **PASS** `doc-cpp00000007` |

---

## Live AC board

| AC | Result | Note |
|---|---|---|
| AC1 | **PASS** | measure_between / battery |
| AC2 | **PASS** | volume proxy |
| AC3 | **PASS** | interference clean |
| AC4 | **FAIL** | DRC clearance×27 on board `7c4fae119443` |
| AC5 | **FAIL** | V(3V3)=0.0 (schematic sim OK routing) |
| AC6 | **PASS** | antenna |
| AC7 | **PASS** | fw ELF |
| AC8 | **PASS** | |
| AC9 | **PASS** | 12 legs graded |

---

## Attempts / liveness

1. **a1** thrash-kill `attempt-thrash-20260715T234708Z` — fails=4 (intake/DL3 no_code/DL2/deliver_DL1); force chain 0; dead mistral 404.
2. **HOLD** until Fixer25 thrash rebind: end_of_turn_ecad_force + no mistral; offline_proof_a2 FINAL_OFFLINE_OK.
3. **a2** primary graded — liveness held (fails=0); wall 901.92s; force chain fired (board→opt→drc→erc→sim).

---

## Residuals (for next mega / Fixer)

1. **AC5 rail drive:** V=0.0 with sim on schematic — need driven V_source + load topology that measures 3V3 in-band (P22 residual resurfaced after Y25 unblocked routing).
2. **AC4 clearance re-route:** min_clearance_mm recorded but autoroute retry not executed; DRC 27 clearance remain. Fire Z25 bounded `ecad_autoroute(pitch=0.3)` + re-opt when post-opt DRC clearance-dirty.
3. Goal 9/9 still open; target ≥8/9 missed by 1.

---

## Artifacts

- `grade_summary.json`, `criterion_AC1..AC9.json`, `criterion_BATTERY.json`
- `battery_result.json`, `ledger.json`, `events.jsonl`, `mission.json`, `status.json`
- `fixes.md`, `offline_proof_a2.md`, `autosave_manifest.json`
- `attempt-thrash-20260715T234708Z/`, `findings_a1_thrash.md`
- Parts under `reports/runs/mega-25-fc-rocket-20260715/parts/`

No push (Main gates).
