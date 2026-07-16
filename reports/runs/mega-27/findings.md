# mega-27 findings (a1 primary graded)

**RUN_ID:** `mega-27-fc-rocket-20260716`  
**Baseline:** `dce2e872`  
**Frontier:** xai-sub grok-4.5 EXACT account `06dc3359`  
**Pair:** MegaRun27=`CertainCatshark` / Fixer27=`AshamedLungfish`  
**Terminal:** `done` / `mission_complete`  
**Wall:** **654.22s** (a1)  
**Live AC:** **8**/9 PASS (`AC1 AC2 AC3 AC5 AC6 AC7 AC8 AC9`) — FAIL AC4 only  
**Target 9/9:** miss by 1 (AC4) — **C27/D27 contract axes PASS**

---

## C27 — board routing room (≥3× component bbox or margin≥15)

| Metric | mega-26 a1 | mega-27 a1 |
|---|---|---|
| board_size args on force | none | **None** |
| outline_margin | 5 (hard-coded AUTO) | **15.0** |
| outline_area_mm2 | packed auto | **7200.0** |
| component_bbox_area_mm2 | n/a | **432.0** |
| outline_area_ratio | n/a | **16.667** |
| Axis C27 | n/a | **PASS** |

Root cause closed for room path: force-time `board_from_schematic` now carries `board_size` + `outline_margin≥15` + area diagnostics; MCP/board_flow plumb knobs (no invented engine clearance).

---

## D27 — AC4 clearance zero or reduced vs mega-26

| Metric | mega-26 a1 | mega-27 a1 |
|---|---|---|
| min_clearance_mm | 0.2 | 0.2 (held) |
| DRC errors | **23** clearance | **6** clearance |
| errors reduced | n/a | **True** |
| clearance_retry / grow | retry_n≥1 pitch=0.3 | grow×1.5 + autoroute pitch=0.3 |
| Axis D27 | n/a | **PASS** |
| Live AC4 | FAIL | **FAIL** (errors≠0; honest reduced) |

Contract: D27 PASS on clean **or** honest FAIL with error count reduced vs mega-26's 23. Live AC4 still requires errors==0 for 9/9.

---

## Regression (mega-26 green axes)

| Axis | Verdict |
|---|---|
| **C27** board routing room | **PASS** |
| **D27** AC4 reduced/clean | **PASS** |
| **A26** source on measured net | **PASS** |
| **B26** clearance retry fires | **PASS** |
| Y25 / Z25 / S23 / P22 / U24 / V24 / T23 / Q22 / G18 | see grade_summary |

---

## Live AC board

| AC | Result | Note |
|---|---|---|
| AC1 | **PASS** | battery / measure |
| AC2 | **PASS** | volume proxy |
| AC3 | **PASS** | interference clean |
| AC4 | **FAIL** | DRC clearance×6 (was 23) after room+retry |
| AC5 | **PASS** | V(3V3) in-band |
| AC6 | **PASS** | antenna |
| AC7 | **PASS** | fw ELF |
| AC8 | **PASS** | |
| AC9 | **PASS** | 12 legs graded |

---

## Attempts / liveness

1. **a1** primary graded — liveness held; wall 654.22s; force chain board_size/outline_margin ≥3× room + grow ladder; terminal `done`/`mission_complete`.

---

## Residuals

1. **AC4 still errors=6** after C27 room + D27 grow/retry (reduced from 23). Goal 9/9 remains open until DRC errors==0; D27 contract satisfied as reduced honest FAIL.
2. No thrash / no archive this attempt.

---

## Artifacts

- `grade_summary.json`, `criterion_AC1..AC9.json`, `criterion_BATTERY.json`
- `battery_result.json`, `ledger.json`, `events.jsonl`, `mission.json`, `status.json`
- `fixes.md`, `offline_proof.md` (FINAL_OFFLINE_OK), `autosave_manifest.json`
- Parts under `reports/runs/mega-27-fc-rocket-20260716/` (if present)

No push (Main gates).
