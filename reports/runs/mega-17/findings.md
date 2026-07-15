# MegaRun17 Findings

## Run summary

- **Primary graded run ID:** `mega-17-fc-rocket-20260715`
- **Baseline commit:** `cd764e92` (repo HEAD matches)
- **Fixer peer:** Fixer17=`AlrightMosquito` (Z1 terminal grading + Z2 power-then-wire live refs + Z3 simulate chain need; **91 tests**)
- **Harness peer:** MegaRun17=`RequiredWoodpecker`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Frontier:** `openrouter:deepseek/deepseek-chat-v3.1` (PAID, no `:free`)
- **Spend:** before `$0.168983` → after `$0.223708` → **delta `$0.054725`** (guard $8, **OK**)
- **Wall:** **145.86s**
- **Terminal:** `needs_correction` / `verify_failed: ['AC1','AC2','AC4','AC5','AC9']`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, grade_summary.json, criterion_AC1–AC9.json, battery_result.json, openrouter_usage_{before,after}.json, fixes.md, daemon_preflight.json

### Pre-flight: PASS

1. Daemon **running == disk hostd**; baseline `cd764e92`
2. **spawn_probe PASS** — cap-sep + Fix8 + Fix-wire gate wiring
3. **auto_pick_smoke PASS** — axis Y: hard→openrouter paid deepseek rank5; leaf→ollama
4. Fixer17 **fixes bound** before primary (Z1/Z2/Z3)
5. OpenRouter usage recorded; xai left blocked (not used as frontier)

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | Z1 | Z2 | Z3 | Y | Notes |
|---|---|---|---|---|---|---|---|---|---|
| a1 aborted | openrouter paid | ~30s partial | killed | n/a | — | — | — | — | Double-launch race; archived `attempt-1-aborted-double-launch/` |
| **a2 primary** | openrouter `deepseek/deepseek-chat-v3.1` paid | **145.9s** | needs_correction | **4** (AC3/6/7/8) | **PASS** | **FAIL** | **PASS** | **PASS** | 9/9 ACs graded; wire without power; simulate via AC5 evidence |

**grok-4.5 revalidation deferred:** xAI still spending-limit-blocked. Do not attempt.

---

## Axis scoreboard (honest — a2 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **Z1** terminal grading unconditional | **PASS** | All 9 `criterion_ACn.json` written; ledger `criterion_result`×10 (+BATTERY); terminal `needs_correction` still graded (mega-16 had 0) |
| **Z2** power-then-wire live refs | **FAIL** | `ecad_add_power`=0; force_inject still `ecad_place_symbol` (Device:R); wire×5 with kwargs thrash then R1.1↔R1.2 self-loop; **no `#PWR1`** (mega-16 residual cleared); ERC power_not_driven still present (AC4 errors=4) |
| **Z3** AC5 SPICE on wired netlist | **PASS** | Live AC5 evidence: `ecad_simulate` OP/mna ran (`device_count=1`, converged); not empty-netlist class; V(3V3)=null → AC5 **NOT-CHECKED** (probe not finite) |
| **Y** auto_pick openrouter paid | **PASS** | Preflight hard→openrouter paid; live openrouter×10 + ollama×19; xai×0 free×0 |
| **W** wire+power ERC clean | **FAIL** | wire executed; power never; AC4 FAIL |
| **X** AC5 SPICE clean | **FAIL** | simulate evidence but V(3V3) not measured |
| **V** battery invocation | **PASS** | battery_result doc_id=`doc-cpp00000019`; no signature-error zeros |
| **A** accepted ops | **PASS** | new_part_ok + add_features_ok |
| **B** delegate | **PASS** | delegate×12; spawn×12 |
| **C** oneshot | **PASS** | oneshot_ok×10 |
| **D/F** terminal | **PASS** | clean `needs_correction` / verify_failed list |
| **E/I** battery | **PASS** | battery_result + doc_id |
| **S/P** board+DRC | **PASS** | board_from_schematic + DRC errors=0 |
| **Fix8 live** | **PASS** | chain gate + force_inject place after denials |
| **K/L/M** | **PASS** | 9 gradeable; 4 live PASS; fw ELF + schematic |

**Live AC PASS count:** **4** (target ≥8 **missed**). PASS: AC3, AC6, AC7, AC8. FAIL: AC1, AC2, AC4, AC9. NOT-CHECKED: AC5.

---

## Z1) terminal grading unconditional: PASS

mega-16 residual **closed**. Mission terminal always emits criterion results:

| AC | Result | Notes |
|---|---|---|
| AC1 | FAIL | battery critical_scores_zero (eval_check/is_manifold/measure_part) |
| AC2 | FAIL | no bodies to evaluate |
| AC3 | PASS | interference empty |
| AC4 | FAIL | erc.errors=4 (drc.errors=0) |
| AC5 | NOT-CHECKED | OP ran; V(3V3) null / no finite probe |
| AC6 | PASS | antenna analyze |
| AC7 | PASS | fw ELF `…/e030e21f7573/build/firmware.elf` |
| AC8 | PASS | renode path ok/skip honest |
| AC9 | FAIL | legs missing `sim` |

---

## Z2) power-then-wire: FAIL (partial progress)

**Observed live:**

1. Place U1 (BMP280) ok.
2. Model wire kwargs thrash: `from_reference` / `from_pins` / missing required args → ok=false (signature errors, not missing #PWR1).
3. Wire to missing R1 → `no symbol 'R1'` (placed: U1 only) — **not** `#PWR1`.
4. Chain gate denied further place; **force_inject `ecad_place_symbol` Device:R** (not power, not wire).
5. Place R1 ok; wire R1.1↔R1.2 self-loop ok (net N$9).
6. **Never called `ecad_add_power`.** ERC still power_not_driven (AC4).
7. Board+DRC advanced on the self-loop netlist (DRC clean) — product path incomplete for power.

**Fingerprint for Fixer17 residual:**

1. Force-inject after place thrash still prefers passive R place; does not inject `ecad_add_power` then pin-aware wire to power/IC pins.
2. Wire force path did not fire after R placed; model self-looped passive pins.
3. No power symbol / power port → ERC power_not_driven remains.
4. Good: hardcoded `#PWR1` residual is gone (wire_fail_missing_pwr=0).

---

## Z3) AC5 SPICE: PASS (execution), X still FAIL (measurement)

Reviewer/AC path executed OP simulate on doc `fb6e6e4b586d`:

- backend mna, converged, device_count=1 (R only; U1 no sim model skipped)
- probe V(3V3)=null → AC5 NOT-CHECKED (not empty-netlist class)

Axis Z3 contract = **simulate executes on wired netlist** → **PASS**. Axis X (measured V(3V3)) remains **FAIL**.

Note: chain force-inject for `ecad_simulate` was **not** observed as agent tool_call (count via AC5 evidence). Residual: BUILD-time force-inject of simulate after ERC still weak when ERC never runs.

---

## Y) auto_pick openrouter paid: PASS (regression held)

| Case | Provider | Model |
|---|---|---|
| depth0 architecture/analysis hard | openrouter | deepseek/deepseek-chat-v3.1 rank5 |
| depth2 leaf/simple | ollama | llama3.2:latest |
| Live assignments | openrouter×10 + ollama×19 | xai×0 free×0 |

Mid-run log showed occasional OpenRouter **HTTP 404** on chat completions; mission continued (ollama leaves + partial openrouter). Not fatal this cycle; residual to watch if hard nodes start failing.

---

## Spend

| | USD |
|---|---|
| Before (primary) | 0.168983 |
| After | 0.223708 |
| **Delta** | **0.054725** |
| Guard | 8.00 |
| Abort? | **no** |

OpenRouter remaining ≈ $9.78 of $10 limit. (Attempt-1 partial + mega-16 prior spend already on key.)

---

## TOP residual (next cycle)

1. **Z2 residual:** force-inject order must be `ecad_add_power` → pin-aware `ecad_wire` (live U*/R* + power port refs), not only Device:R place; prevent passive self-loop as “wired enough” for board unlock without power nets.
2. **ERC path:** `ecad_run_erc` never ran as tool_call (AC4 used summary path with erc.errors=4); schedule ERC after real power+wire.
3. **AC5 measurement:** need 3V3 source + load with sim models so V(3V3) finite (upgrade X).
4. **AC1/AC2 geometry:** airframe still hollow/no bodies for some checks.
5. Hold **Z1** + **Y** + Fix8/S board chain.
6. **grok-4.5 revalidation still deferred** (xai spending-limit).

---

## Fingerprints

| ID | Status |
|---|---|
| Z1 terminal AC grading | **PASS** (9/9) |
| Z2 power-then-wire live | **FAIL** (no add_power; inject place_symbol; no #PWR1) |
| Z3 simulate executes | **PASS** (AC5 evidence OP/mna) |
| Y openrouter paid auto_pick | **PASS** |
| W wire+power ERC clean | **FAIL** |
| X-live AC5 V(3V3) | **FAIL** |
| Live PASS ≥8 | **FAIL** (4) |
| Fixer17 bind | **held** |
| Spend guard $8 | **held** ($0.055) |
| grok-4.5 primary | **deferred** |

---

## Artifacts

```
reports/runs/mega-17/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  openrouter_usage_before.json openrouter_usage_after.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  battery_result.json criterion_AC1..AC9.json criterion_BATTERY.json
  grade_summary.json run_summary.json progress.md
  attempt-1-aborted-double-launch/NOTE.txt
```
