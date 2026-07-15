# MegaRun16 Findings

## Run summary

- **Primary graded run ID:** `mega-16-fc-rocket-20260715`
- **Baseline commit:** `c269e326` (repo HEAD matches)
- **Fixer peer:** Fixer16=`BoldSilverfish` (deepseek paid rank-5 + keys.openrouter wiring + xai spending-limit → provider_unavailable fallthrough; **80 tests**)
- **Harness peer:** MegaRun16=`NeutralCatfish`
- **Engine:** hostd `night2-3054-gcb65bfd9-dirty` — running git_rev matches disk binary
- **Frontier:** `openrouter:deepseek/deepseek-chat-v3.1` (PAID, no `:free`)
- **Spend:** before `$0.000005` → after `$0.160200` → **delta `$0.160194`** (guard $8, **OK**)
- **Wall:** **316.73s**
- **Terminal:** `done` / `deliver` / `deliver_partial`
- **Evidence:** events.jsonl, ledger.json, mission.json, status.json, preflight JSONs, grade_summary.json, openrouter_usage_{before,after}.json, fixes.md, daemon_preflight.json

### Pre-flight: PASS

1. Daemon **running == disk hostd**; baseline `c269e326`
2. **spawn_probe PASS** — cap-sep + Fix8 + Fix-wire gate wiring
3. **auto_pick_smoke PASS** — axis Y: hard→openrouter paid deepseek rank5; leaf→ollama
4. Fixer16 **fixes bound** before primary
5. OpenRouter usage recorded; xai left blocked (not used as frontier)

---

## Attempt matrix

| Attempt | Frontier | Wall | Terminal | Live AC PASS | W | X | Y | Notes |
|---|---|---|---|---|---|---|---|---|
| **a1 primary** | openrouter `deepseek/deepseek-chat-v3.1` paid | **316.7s** | deliver_partial | **0** (verify skipped) | **PARTIAL** | **FAIL** | **PASS** | BUILD reached schematic/place/power/wire attempt; wire fail `#PWR1`; force_inject place_symbol; no AC grading |

**grok-4.5 revalidation deferred:** xAI still spending-limit-blocked. Do not attempt.

---

## Axis scoreboard (honest — a1 primary)

| Axis | Verdict | Evidence |
|---|---|---|
| **Y** auto_pick openrouter paid when xai blocked | **PASS** | Preflight: depth0 hard → `openrouter:deepseek/deepseek-chat-v3.1` rank5; leaf → ollama. Live: openrouter×11, ollama×13, xai×0, free×0. No refusal loops, no ollama-all collapse. |
| **W** ecad_wire + power; ERC clean; AC4 | **PARTIAL** | `ecad_add_power` ok×1; `ecad_wire` attempted ok=false (`no symbol '#PWR1'`); force_inject fired once (tool=`ecad_place_symbol` after denials, not wire); chain gate denied place/erc/board until wire; **AC4 never graded** (verify skipped). power_not_driven not measurable. |
| **X / X-live** AC5 SPICE on wired netlist | **FAIL** | `ecad_simulate`=0; no AC5 criterion_result; wire never produced simulatable nets. |
| **V** battery invocation bugs fixed | **UNTESTABLE** | No `verify_battery` / battery_result this run (verify phase skipped). Prior mega-15 unit+live held; no regression evidence either way. |
| **A** accepted ops | **PASS** | new_part_ok×5; add_features_ok×34 |
| **B** delegate | **PASS** | delegate×12; spawn×2 |
| **C** oneshot | **PASS** | oneshot_ok×7 |
| **D/F** terminal | **PASS** | deliver_partial clean |
| **E/I** battery | **FAIL** | no battery_result / doc_id |
| **M** non-CAD | **PASS** | schematic×2; fw path present |
| **K/L/N/Q** live AC counts | **FAIL** | 0 gradeable AC results |
| **S/P/O** board+DRC | **FAIL** | board_from_schematic gated/denied until wire; drc=0 |
| **Fix8 live** | **PASS** | ecad_chain_gate×3; tool_denied ECAD×3 |

**Live AC PASS count:** **0** (target ≥8 **missed**). Root cause: mission `deliver_partial` closed without emitting `criterion_result` events (verify entered but no AC grading).

---

## Y) auto_pick openrouter paid: PASS

| Case | Provider | Model |
|---|---|---|
| depth0 architecture/analysis hard | openrouter | deepseek/deepseek-chat-v3.1 rank5 |
| depth2 leaf/simple | ollama | llama3.2:latest |
| Live assignments | openrouter×11 + ollama×13 | xai×0 free×0 |

Fixer16 bind held: spending-limit marks xai unavailable; paid deepseek offered before free pool; keys.openrouter wired through provider_for_tier/get_provider.

---

## W) wire + power force-inject: PARTIAL

**Observed live:**

1. Schematic opened (`ecad_new_schematic` ok).
2. Place thrash → **ECAD chain gate** denials on further place / erc / board (`need: ecad_wire`).
3. `ecad_add_power` **ok**.
4. `ecad_wire` **executed but failed**: `no symbol '#PWR1' on the schematic` (placed refs: U1, R1).
5. `ecad_chain_force_inject` fired once with **tool=ecad_place_symbol** (Device:R) after denials — **not** force-inject of `ecad_wire`/`ecad_add_power` this path.
6. No successful pin-to-pin wire → ERC/board remained gated → AC4 ungraded.

**Fingerprint for Fixer:** force-inject after thrash still injects place_symbol in some paths; wire args used power ref `#PWR1` that was never placed as a symbol. Pin-aware inject should emit wire with real placed refs (U1/R1/power symbol actual ref).

---

## X-live) AC5 SPICE: FAIL

No `ecad_simulate` call. Empty-netlist class not even reached — no wired netlist, no AC5 criterion.

---

## Verify / AC grading residual (critical)

Mission phases_completed: `intake, specify, plan, deliver` — **build/verify not in phases_completed** despite stage_log entries. Terminal `deliver_partial` with **zero** `criterion_result` / `battery_result` in ledger.

This makes live AC count un-gradeable (0/9) independent of product tool success. Next cycle must either:
1. Force verify phase to run AC checks before deliver_partial, or
2. Post-hoc grade from artifacts when criteria events missing.

---

## Spend

| | USD |
|---|---|
| Before | 0.000005 |
| After | 0.160200 |
| **Delta** | **0.160194** |
| Guard | 8.00 |
| Abort? | **no** |

OpenRouter key remaining ≈ $9.84 of $10 limit.

---

## TOP residual (next cycle)

1. **Verify skip on deliver_partial** → 0 live AC; force criterion evaluation.
2. **W inject path:** force-inject should fire `ecad_wire` (pin-aware against placed refs), not only place_symbol; power symbol ref must match wire endpoints.
3. Close wire → ERC → board → SPICE for W full + X-live + AC4/AC5.
4. Hold Y (openrouter paid + ollama leaves) under spend guard.
5. **grok-4.5 revalidation still deferred** (xai spending-limit).

---

## Fingerprints

| ID | Status |
|---|---|
| Y openrouter paid auto_pick | **PASS** |
| W wire+power force-inject live | **PARTIAL** (tools called; wire fail; AC4 ungraded) |
| X-live AC5 SPICE | **FAIL** |
| V battery invocation | **UNTESTABLE** (no battery this run) |
| Live PASS ≥8 | **FAIL** (0; verify skipped) |
| Fixer16 bind | **held** |
| Spend guard $8 | **held** ($0.16) |
| grok-4.5 primary | **deferred** |

---

## Artifacts

```
reports/runs/mega-16/
  run_mega.py grade_run.py _preflight.py findings.md fixes.md
  spawn_probe.json auto_pick_smoke.json subscription_accounts.json
  daemon_preflight.json preflight_summary.json
  openrouter_usage_before.json openrouter_usage_after.json
  request.txt manifest.json mission_meta.json
  events.jsonl ledger.json mission.json status.json
  fw_artifact.json grade_summary.json
```

No attempt archive subdirectory (single primary; not a hard provider block).
