# mega-28 findings (a1 primary graded) — **9/9 MILESTONE**

**RUN_ID:** `mega-28-fc-rocket-20260716`  
**Baseline:** `271aedac`  
**Frontier:** xai-sub grok-4.5 EXACT account `06dc3359`  
**Pair:** MegaRun28=`CleanJaguar` / RouterFix=`FatalGrouse`  
**Daemon stamp:** `night2-3084-g271aedac-dirty` (NEW post-RouterFix; was night2-3077-g0f43d126-dirty)  
**Terminal:** `blocked` / `deliver_partial`  
**Wall:** **706.33s** (a1)  
**Live AC:** **9**/9 PASS (`AC1 AC2 AC3 AC4 AC5 AC6 AC7 AC8 AC9`)  
**Target 9/9:** **MET** — first full green board

---

## E28 — router honors min clearance (engine + live)

| Metric | mega-27 a1 | mega-28 a1 |
|---|---|---|
| min_clearance_mm | 0.2 | **0.2 (held)** |
| DRC errors | **6** clearance | **0** |
| AC4 | FAIL | **PASS** |
| Deterministic replay | n/a | **6→0** (`router_proof.json`) |
| Axis E28 | n/a | **PASS** |

Root cause closed at source: Python autorouter default corner chamfer 0.5mm produced pad↔track sub-clearance; C++ `clearance_of()` + opts `min_clearance`/`min_clearance_mm` floored; MCP/agent plumb. DRC 0.2mm rule **not** weakened.

---

## F28 — full mission 9/9

| AC | Result |
|---|---|
| AC1 | **PASS** |
| AC2 | **PASS** |
| AC3 | **PASS** |
| AC4 | **PASS** (DRC errors==0) |
| AC5 | **PASS** |
| AC6 | **PASS** |
| AC7 | **PASS** |
| AC8 | **PASS** |
| AC9 | **PASS** |

Axis **F28 PASS**. Terminal `blocked`/`deliver_partial` is envelope honesty only — all criterion_results graded PASS.

---

## Regression (mega-27 green axes)

| Axis | Verdict |
|---|---|
| **E28** router min clearance | **PASS** |
| **F28** 9/9 live | **PASS** |
| **C27** board routing room | **PASS** |
| **D27** AC4 zero/reduced | **PASS** |
| **A26** source on measured net | **PASS** |
| **B26** clearance retry fires | **PASS** |
| Y25 / Z25 / S23 / P22 / U24 / V24 / T23 / Q22 / G18 | see grade_summary |

---

## Attempts / liveness

1. **a1** primary graded — waited RouterFix `router proven` DM + NEW hostd stamp; preflight PASS (mega28_bound); liveness held; wall 706.33s; terminal `blocked`/`deliver_partial`.

---

## Residuals

1. None for live ACs — **9/9 closed**.
2. Soft: openrouter mid-fallback returned 402 (no credits) once; xai-sub frontier + ollama leaves continued (conservation OK).
3. Terminal status `blocked`/`deliver_partial` despite all ACs PASS — harness envelope residual, not AC residual.

---

## Artifacts

- `grade_summary.json`, `criterion_AC1..AC9.json`, `criterion_BATTERY.json`
- `battery_result.json`, `ledger.json`, `events.jsonl`, `mission.json`, `status.json`, `status_report.json`
- `router_proof.json`, `router_proof.md`, `fixes.md`, `autosave_manifest.json`
- Daemon preflight: `daemon_preflight.json` stamp `night2-3084-g271aedac-dirty`

No push (Main gates).
