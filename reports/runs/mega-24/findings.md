# mega-24 findings (a3 primary graded)

**RUN_ID:** `mega-24-fc-rocket-20260715`  
**Baseline:** `8ce92d74`  
**Frontier:** xai-sub grok-4.5 EXACT account `06dc3359`  
**Pair:** MegaRun24=`DelightedCricket` / Fixer24=`ComingOcelot` + Fixer24c=`BurningWildcat`  
**Terminal:** `needs_correction` / `verify_failed: ['AC4', 'AC5']`  
**Wall:** **897.15s**  
**Live AC:** **7**/9 PASS (`AC1 AC2 AC3 AC6 AC7 AC8 AC9`) — FAIL AC4+AC5  
**Target ≥8/9:** miss by 1 (AC4 clearance×6 + AC5 simulate on PCB doc)

---

## U24 — root cause (mega-23 thrash)

**Not S23/T23 logic.** Evidence from `reports/runs/mega-23/` vs mega-22 a2:

1. **Primary (mega-23):** mid-run provider poison — xai `personal-team-blocked:spending-limit` then openrouter 402 → process-local `mark_provider_unavailable` → BUILD fell to ollama → 0 tools / `no_code` / 9 node fails / battery `doc_id=null`.
2. **Secondary amplifiers:** `fw_sensor_scenario` NameError `_err`; forced `ecad_wire` + `net=` TypeError; `ledger_note` string payload thrash; TypeError → empty `fn()`.
3. **Terminal product force skipped** on `verify_failed` / `needs_correction` stop shapes.

Bind: `reports/runs/mega-24/fixes.md` (ComingOcelot + residual rebinds + BurningWildcat 24c).

Exact edit class that broke the live loop: **not** the S23 GND-return / T23 measure scoring hunks themselves — those preserved AC5/AC1 contracts. The thrash surface was **provider fallthrough + tool-dispatch amplifiers + terminal force gate**, exposed after S23/T23 landed on the same tree as GrossShark handoffs.

---

## V24 — live tool loop restored

| Metric | mega-23 a1 | mega-24 a1 | mega-24 a2 | mega-24 a3 |
|---|---|---|---|---|
| tools | 91 | 147 | 48 | **152+** (ledger multi-count higher) |
| node fails | 9 | 4 (kill) | 4 (kill) | **3** (run completed) |
| add_features ok | 0 | 9 | 0 | **38** |
| new_part | 0 | 2 | 0 | **5** |
| battery doc | null | n/a kill | n/a kill | **doc-cpp00000005 PASS** |
| live AC | 4-class | kill | kill | **7/9** |

a3 completed full DAG terminal with BUILD product, battery, fw ELF, criteria graded.

---

## Axes (grade_summary)

| Axis | Verdict |
|---|---|
| **U24** root cause | **PASS** |
| **V24** live build loop | **PASS** (tools≥200 ledger-side, fails=3, live 7) |
| **T23** measure_between | **PASS** (score 1.0, AC1 PASS) |
| **S23** V(3V3) in-band | **FAIL** (AC5: simulate called on PCB doc `767f0f0fa678`, not schematic) |
| Q22 optimize→DRC chain | **PASS** (board path advanced; AC4 still clearance×6) |
| G18 / Y | **PASS** |
| M21 / R22 / Z1 / E / I | **PASS** |
| mega-22 green hold | partial — AC4/AC5 residual |

---

## Live AC board

| AC | Result | Note |
|---|---|---|
| AC1 | **PASS** | measure_between score 1.0 (T23) |
| AC2 | **PASS** | volume proxy |
| AC3 | **PASS** | interference clean |
| AC4 | **FAIL** | DRC clearance×6 (0.1296 < 0.2mm) on board `767f0f0fa678` |
| AC5 | **FAIL** | `doc is a pcb, not a schematic` — wrong doc for SPICE |
| AC6 | **PASS** | antenna |
| AC7 | **PASS** | fw ELF `5c61776df1db` |
| AC8 | **PASS** | |
| AC9 | **PASS** | 12 legs graded |

Battery: **PASS** `doc-cpp00000005` (12 checks).

---

## Attempts / liveness

1. **a1** thrash-kill `attempt-thrash-20260715T221105Z` — 4 fails after BUILD restored (intake/layout/plan/deliver leaf poison).
2. **a2** thrash-kill `attempt-thrash-20260715T223717Z` — no_code on xai-sub spec/layout (zero-tool first completion).
3. **HOLD a3** until Fixer24c offline proof (`offline_proof_a2.md` + FINAL_OFFLINE_OK junk-JSON force).
4. **a3** primary graded — liveness held (fails=3 ≤3); completed wall 897s.

---

## Residuals (for next mega)

1. **AC5 doc routing:** simulate must target schematic, not board doc after `board_from_schematic`.
2. **AC4 clearance×6:** optimize_traces ran but 0.13mm vs 0.2mm remain.
3. **S23** blocked by (1) — extraction path never saw a finite rail V this run.
4. Soft node fails: `n_spec_DL5`, `n_spec_done`, `n_verify_DL2` budget exhaust (non-fatal).

---

## Artifacts

- `grade_summary.json`, `criterion_AC1..AC9.json`, `criterion_BATTERY.json`
- `battery_result.json`, `ledger.json`, `events.jsonl`, `mission.json`, `status.json`
- `fixes.md`, `autosave_manifest.json`, `offline_proof_a2.md`
- `attempt-thrash-20260715T221105Z/`, `attempt-thrash-20260715T223717Z/`
- Autosaved parts under `reports/runs/mega-24-fc-rocket-20260715/parts/` + Documents/AI-CAD `*.step+`

No push (Main gates).
