# MegaRun18 Findings

## Run summary

- **Primary graded run ID:** `mega-18-fc-rocket-20260715`
- **Baseline commit:** `769f92c6` (repo HEAD matches)
- **Fixer peer:** Fixer18=`PlainLizard` (Z2r cascade force-EXECUTE add_power→cross wire + self-loop guard; Z4 AC5 null V(3V3)→FAIL; **105 tests**)
- **Harness peer:** MegaRun18=`ValidBovid`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running matches disk binary
- **Frontier:** `xai-sub:grok-4.5` EXACT on SuperGrok account `06dc3359-2d20-4cd7-bd84-8c8db560cd4e`
- **Mid-fallback:** `openrouter:deepseek/deepseek-chat-v3.1` PAID (Y stays live)
- **Leaves:** ollama `llama3.2:latest`
- **Spend (OpenRouter):** before `$0.22370784` → after `$0.22370784` → **delta `$0.0`** (guard $8, **OK**; frontier on xai-sub)
- **Wall:** **551.01s**
- **Terminal:** `needs_correction` / `verify_failed: ['AC1', 'AC4', 'AC5']`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, grade_summary.json, criterion_AC1–AC9.json, battery_result.json, openrouter_usage_{before,after}.json, fixes.md, daemon_preflight.json, no_other_xai_consumer.json

### Pre-flight: PASS

1. Daemon **running == disk hostd**; baseline `769f92c6`
2. **spawn_probe PASS** — cap-sep + Fix8 + Fix-wire/Z2r cascade wiring
3. **auto_pick_smoke PASS** — G18 hard→xai-sub grok-4.5; leaf→ollama; Y openrouter deepseek enabled mid
4. Account pin **06dc3359 ACTIVE** (3 xai-sub accounts; no other xai consumer)
5. Fixer18 **fixes bound** before primary (Z2r/Z4)
6. grok-4.5 probe reply `pong` on account 06dc3359

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | Z1 | Z2r | Z3 | Z4 | G18 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **a1 primary** | xai-sub grok-4.5 | **551.01s** | needs_correction | **6** (AC2/AC3/AC6/AC7/AC8/AC9) | **PASS** | **PARTIAL** | **PASS** | **PASS** | **PARTIAL** | **PASS** | power×2 model-executed; force place→wire U1-R1→board; AC4 sch-not-board; AC5 honest FAIL |

---

## Axis scoreboard (honest — a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **Z1** terminal grading unconditional | **PASS** | 9/9 criterion_ACn.json; terminal needs_correction still graded |
| **Z2** power-then-wire live refs | **PARTIAL** | power before wire; no #PWR1; wire ok; ERC not fully clean |
| **Z2r** add_power force-EXECUTE + net-aware wire | **PARTIAL** | `ecad_add_power`×2 ok (GND+3V3) model-path; force_inject_power=0 (already present); force cascade: place Device:R → wire U1.1↔R1.1 (cross-ref, not self-loop) → board; **not** power-pin→rail; power_not_driven remains; AC4 FAIL |
| **Z3** AC5 SPICE on wired netlist | **PASS** | simulate OP/mna ran; device_count=1; not empty-netlist |
| **Z4** AC5 V(3V3) from simulate payload | **PASS** | AC5 **FAIL** with `probe 'v(3v3)' is null…` — **NOT-CHECKED residual closed** |
| **G18** grok-4.5 revalidation | **PARTIAL** | preflight hard→xai-sub grok-4.5; live xai-sub×11 + ollama×13; **live AC PASS 6 < 7** |
| **Y** openrouter mid-fallback | **PASS** | deepseek enabled mid; free disabled; leaves ollama; live no free |
| **W** wire+power ERC clean | **PARTIAL** | wire+power executed; AC4 FAIL / power_not_driven |
| **X** AC5 SPICE clean | **PASS** | honest FAIL (not empty-netlist) |
| **S/P** board+DRC | **FAIL** | board_from_schematic ok (`08cfb42d9b98`) but AC4 DRC against **sch** `0ce20163ee10` ("is a sch, not a board") |
| **V** battery invocation | **PASS** | battery_result doc_id present |
| **A/B/C/D** | **PASS** | ops/delegate/oneshot/terminal clean |
| **Fix8 live** | **PASS** | chain gate + force inject cascade |

**Live AC PASS count:** **6** (target ≥8 **missed**; G18 ≥7 **missed**). PASS: AC2, AC3, AC6, AC7, AC8, AC9. FAIL: AC1, AC4, AC5.

---

## Z2r) power force-EXECUTE + net-aware wire: PARTIAL

**Observed live:**

1. Place U1 (MCU:Generic_QFP48 / STM32F4) ok.
2. Chain gate denied further place — need=`ecad_add_power`.
3. **Model** called `ecad_add_power(GND)` + `ecad_add_power(3V3)` ok (orphan net_size=0 each).
4. Gate again; need=`ecad_wire`; denials=2.
5. Force cascade: **`ecad_place_symbol` Device:R 1k** → **`ecad_wire` U1.1↔R1.1** (cross-ref, good vs self-loop) → **`board_from_schematic`**.
6. **No force_inject of `ecad_add_power`** (already executed by model) — grader force_inject_power=0.
7. Wire did **not** bond power ports to IC power pins → ERC power_not_driven remains; AC4 FAIL.
8. Board created but subsequent DRC path still on schematic doc.

**Progress vs mega-17:**
- mega-17: add_power=0; force place only; R1 self-loop.
- mega-18: add_power×2 live; force wire is cross-ref U1↔R1 (self-loop guard held); board force-injected.

**Fingerprint for Fixer18 residual:**
1. Cascade after power ports still injects passive R + IC↔R wire instead of **power_in pin → rail/power port**.
2. Orphan power ports (net_size=0) satisfy "has power" for board unlock without driven nets.
3. AC4/DRC doc selection uses sch after board exists.

---

## Z4) AC5 V(3V3) extraction: PASS

mega-17 residual **closed**. Live AC5:

```
result: FAIL
detail: probe 'v(3v3)' is null in ecad_simulate payload (net '3V3' not driven / no finite value)
op: {"V(3V3)": null}  device_count=1  U1 no sim model skipped
```

Not NOT-CHECKED. Axis X also grades honest FAIL (wired netlist class).

---

## G18) grok-4.5 revalidation: PARTIAL

| Case | Provider | Model |
|---|---|---|
| depth0 architecture/analysis hard | xai-sub | grok-4.5 rank5 |
| depth2 leaf/simple | ollama | llama3.2:latest |
| Live assignments | xai-sub×11 + ollama×13 | openrouter×0 free×0 |

Account `06dc3359` ACTIVE throughout. Probe `pong` pre-launch. Live AC PASS **6** (need ≥7 for G18 PASS). OpenRouter mid unused this run (no xai 429 observed as abort; occasional OpenRouter HTTP 404 in log — non-fatal).

---

## AC table

| AC | Result | Notes |
|---|---|---|
| AC1 | FAIL | soft_scores_zero measure_between |
| AC2 | PASS | volume/measure path |
| AC3 | PASS | interference empty |
| AC4 | FAIL | DRC on sch not board; ERC power issues |
| AC5 | FAIL | V(3V3) null honest FAIL (Z4) |
| AC6 | PASS | antenna (openEMS FDTD ran in log) |
| AC7 | PASS | fw ELF |
| AC8 | PASS | renode path ok/skip honest |
| AC9 | PASS | report card legs |

---

## TOP residual (next cycle)

1. **Z2r residual:** after add_power, force wire must prefer **IC power_in → power port (3V3/GND)**; do not unlock board on orphan ports + IC↔R only.
2. **AC4 doc:** DRC/ERC product path must target board_doc_id from board_from_schematic.
3. **AC5 measurement:** need V source + load with sim models so V(3V3) finite (upgrade X beyond honest FAIL).
4. **AC1 geometry** soft zeros.
5. Hold **Z1/Z3/Z4/Y/Fix8** + G18 account path.
6. Target still ≥8 live AC; G18 needs ≥7.

---

## Fingerprints

| ID | Status |
|---|---|
| Z1 terminal AC grading | **PASS** (9/9) |
| Z2r power force + net-aware wire | **PARTIAL** |
| Z3 simulate executes | **PASS** |
| Z4 AC5 null→FAIL | **PASS** (closed) |
| G18 grok-4.5 ≥7 AC | **PARTIAL** (6) |
| Y openrouter mid enabled | **PASS** |
| W ERC clean | **PARTIAL/FAIL** |
| Live PASS ≥8 | **FAIL** (6) |
| Fixer18 bind | **held** |
| Spend guard $8 | **held** ($0.00 OR delta; xai frontier) |
| Account 06dc3359 | **ACTIVE** |

---

## Artifacts

```
reports/runs/mega-18/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json no_other_xai_consumer.json
  openrouter_usage_before.json openrouter_usage_after.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json criterion_AC1..AC9.json criterion_BATTERY.json
  grade_summary.json run_summary.json status_report.json progress.md
  primary.log primary.pid
```
