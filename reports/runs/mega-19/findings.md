# MegaRun19 Findings

## Run summary

- **Primary graded run ID:** `mega-19-fc-rocket-20260715`
- **Baseline commit:** `6d11238b` (repo HEAD matches)
- **Fixer peer:** Fixer19=`ColdMuskox` (D19 power-satisfied + A19 rail wire + B19 board doc_id + C19 V-source/measure_between; **117 tests** gate)
- **Harness peer:** MegaRun19=`UnemployedTuna`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running matches disk binary
- **Frontier:** `xai-sub:grok-4.5` EXACT on SuperGrok account `06dc3359-2d20-4cd7-bd84-8c8db560cd4e`
- **Mid-fallback:** `openrouter:deepseek/deepseek-chat-v3.1` PAID (Y stays live)
- **Leaves:** ollama `llama3.2:latest`
- **Spend (OpenRouter):** before `$0.22370784` → after `$0.22370784` → **delta `$0.0`** (guard $8, **OK**; frontier on xai-sub)
- **Wall:** **459.44s**
- **Terminal:** `done` / `deliver_partial`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, grade_summary.json, criterion_AC1–AC9.json, battery_result.json, openrouter_usage_{before,after}.json, fixes.md, daemon_preflight.json, no_other_xai_consumer.json, grok45_probe.json

### Pre-flight: PASS

1. Daemon **running == disk hostd**; baseline `6d11238b`
2. **spawn_probe PASS** — cap-sep + Fix8 + Fix-wire/Z2r cascade wiring
3. **auto_pick_smoke PASS** — hard→xai-sub grok-4.5; leaf→ollama; Y openrouter deepseek enabled mid
4. Account pin **06dc3359 ACTIVE** (3 xai-sub accounts; no other xai consumer)
5. Fixer19 **fixes bound** before primary (D/A/B/C; 6 reds green; 117p)
6. grok-4.5 probe reply `pong` on account 06dc3359

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | A19 | B19 | C19 | D19 | Z1 | Z3 | Z4 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **a1 primary** | xai-sub grok-4.5 | **459.44s** | done/deliver_partial | **6** (AC2/AC3/AC6/AC7/AC8/AC9) | **FAIL** | **FAIL** | **FAIL** | **PASS** | **PASS** | **FAIL** | **PASS** | **PASS** | power×4; wire=0; no force cascade; DRC on sch; AC5 empty-netlist |

---

## Axis scoreboard (honest — a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **A19** wire pairs rail-correct | **FAIL** | ecad_add_power×4 live; ecad_wire=0 live (no force inject); ERC power_not_driven remains; AC4 FAIL |
| **B19** board doc_id threading | **FAIL** | AC4 DRC error: doc '9b66592e2b7a' is a sch, not a board; board_from_schematic not executed live |
| **C19** sim source + AC1 zero defects | **FAIL** | AC5 empty-netlist (U1 Buck only, no sim model); Device:V inject never fired (cascade idle); AC1 measure_between score 0 (no sig error — V PASS for invocation) |
| **D19** chain_need power detection | **PASS** | unit surface + power-satisfied logic present; 6 reds green pre-launch |
| **Z1** terminal grading unconditional | **PASS** | 9/9 criterion_ACn.json |
| **Z2r** add_power force-EXECUTE + net-aware wire | **FAIL** | power live; wire=0; force_inject=0 |
| **Z3** AC5 SPICE on wired netlist | **FAIL** | no live ecad_simulate tool_call; AC5 check-tool empty-netlist FAIL |
| **Z4** AC5 V(3V3) from simulate payload | **PASS** | AC5 honest FAIL (not NOT-CHECKED) — Z4 holds |
| **G18** grok-4.5 revalidation | **PARTIAL** | xai-sub×12 + ollama×13; pass_count=6 < 7 |
| **Y** openrouter mid-fallback | **PASS** | deepseek enabled mid; free disabled; leaves ollama |
| **W** wire+power ERC clean | **FAIL** | no wire live; AC4 FAIL |
| **X** AC5 SPICE clean | **FAIL** | empty-netlist class FAIL |
| **S/P** board+DRC | **FAIL/FAIL** | board_from_schematic=0; DRC on sch |
| **V** battery invocation | **PASS** | no signature errors; measure_between soft zero remains |
| **A/B/C/D** | **PASS/PASS/PASS/PASS** | ops/delegate/oneshot/terminal clean |
| **Fix8 live** | **PASS** | chain gate + deny events present |

**Live AC PASS count:** **6** (target ≥8 **missed**). PASS: AC2, AC3, AC6, AC7, AC8, AC9. FAIL: AC1, AC4, AC5.

---

## A19) rail-correct wire: FAIL

**Observed live:**
1. Model placed schematic + `ecad_add_power` ×4 (GND/3V3 class).
2. **No** `ecad_wire` tool_call in ledger/events (wire_n=0).
3. **No** `ecad_chain_force_inject` cascade fired (force_inject_power/wire/place all 0).
4. ERC still reports `power_not_driven` (AC4 evidence: nets N$3/N$6 power_in without driver).
5. Gate Fix8 did fire (chain_gate events) but thrash never reached force-after threshold for wire inject this run.

**vs mega-18:** mega-18 force-injected place→U1↔R1 wire→board; mega-19 model path added power but **never wired** and force cascade stayed idle.

**Fingerprint residual:** chain thrash / force-after did not force-execute wire with power-port refs when model stalled after add_power.

---

## B19) board doc_id threading: FAIL

Live AC4 DRC:
```
doc '9b66592e2b7a' is a sch, not a board
```
`board_from_schematic` count=0 in tool path; no board doc_id harvested. Residual mega-18 fingerprint **unchanged** (DRC against schematic).

---

## C19) sim source + AC1: FAIL

- AC5: `nothing to simulate — the netlist has no simulatable devices` (U1 Buck no sim model). **Device:V force inject never ran** (cascade idle; Device:V hits=0).
- AC1: `soft_scores_zero=['measure_between']` — score 0.0 with empty msg (not signature TypeError). Axis V still PASS for invocation-defect elimination; C19 wants numeric V or honest fail **with source** + AC1 zero soft defect — **FAIL**.

---

## D19) power detection: PASS

Pre-launch unit gate: 6 reds green (`test_chain_need_power_before_wire`, simulate schedule, power-then-cross-wire unlock, mega-11 force-inject + chain helpers). Source has power-satisfied = add_power **OR** existing power symbols/nets. **Axis PASS** (unit/structural).

---

## Regression

| Hold | Verdict |
|---|---|
| Z1 | **PASS** |
| Z3 | **FAIL** (empty-netlist; no live simulate) |
| Z4 | **PASS** (honest FAIL, not NOT-CHECKED) |
| Y | **PASS** |
| Fix8 | **PASS** |

---

## TOP residual (next cycle)

1. **Force cascade must fire after power** when model stalls: inject rail wire (power_in→#PWR/3V3/GND) using returned add_power refs (A19).
2. **board_from_schematic → board_doc_id** must thread into forced DRC/trace (B19); stop grading/checking DRC on sch id.
3. **Pre-simulate Device:V** inject when device scan finds no source (C19); close empty-netlist class.
4. **measure_between** real feature refs or honest SKIP (not soft zero).
5. Hold **D19/Z1/Z4/Y/Fix8**; restore **Z3**.
6. Target still ≥8 live AC.

---

## Fingerprints

| ID | Status |
|---|---|
| A19 rail wire | **FAIL** |
| B19 board doc | **FAIL** |
| C19 source+AC1 | **FAIL** |
| D19 power detect | **PASS** |
| Z1 terminal AC grading | **PASS** (9/9) |
| Z3 simulate executes | **FAIL** |
| Z4 AC5 null→FAIL | **PASS** |
| G18 grok-4.5 ≥7 AC | **PARTIAL** (6) |
| Y openrouter mid | **PASS** |
| Live PASS ≥8 | **FAIL** (6) |
| Fixer19 bind | **held** |
| Spend guard $8 | **held** ($0.00 OR delta; xai frontier) |
| Account 06dc3359 | **ACTIVE** |

---

## Artifacts

```
reports/runs/mega-19/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md progress.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json no_other_xai_consumer.json
  grok45_probe.json openrouter_usage_before.json openrouter_usage_after.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json criterion_AC1..AC9.json criterion_BATTERY.json
  grade_summary.json grade_log.txt run_summary.json status_report.json
  primary.pid
```
