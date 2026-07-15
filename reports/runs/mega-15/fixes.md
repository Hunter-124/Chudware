# mega-15 harness fixes (Fixer15 = CloseLynx)

Baseline: `17b05635`. Pair: MegaRun15=`PrecisePanda` owns `reports/runs/mega-15/**` +
roadmap; Fixer15 owns `services/agent|dag|mcp` + gym + this file.

Axes targeted:
- **V** battery invocation bugs fixed — zero checks from signature errors eliminated
  (each formerly-broken check either scores real or honest-SKIPs with reason).
- **W** `ecad_wire` + `ecad_add_power` executed live with concrete args; ERC
  `power_not_driven` → 0; AC4 PASS.
- **X** AC5 SPICE runs on the wired netlist (V(3V3) measured; pass or honest numeric fail).
- Target: ≥8 live AC passes. Regression: A–U hold.

Criteria were **not** weakened. Battery never weakened: a check that CAN run must run.

---

## Fix 1 — Battery argument adapters + honest SKIP (axis V / AC1)

**Fingerprint (mega-14 AC1):** soft zeros from invocation defects, not geometry:
| Check | Class |
|---|---|
| check_sketch | missing required arg `sketch` |
| measure_between | missing required arg `a_query` |
| ecad_check_impedance | wrong doc / no board nets |
| ecad_check_diff_impedance | missing `net_p`/`net_n` |
| dim_coverage | no open drawing (mission has no drawing leg) |
| ecad_run_erc | (product residual — power_not_driven; fixed by wire inject) |

**Fix (`services/verify/battery.py`):**
1. `_adapt_check_kwargs` fills required args from the open document when possible:
   - `check_sketch` → first sketch feature id from `doc_tree`, else SKIP `"no sketches on document"`
   - `measure_between` → `top face` / `bottom face` when features exist, else SKIP `"no features to measure between"`
   - `ecad_check_impedance` → open board session + first non-power routed net, else SKIP
   - `ecad_check_diff_impedance` → board + differential pair heuristic, else SKIP
2. `dim_coverage` → honest SKIP `"no drawing in mission scope"` when doc is not a drawing
3. Honest SKIP omits the check from `scores` (no signature-zero) and records
   `findings[{check, ok:true, skipped:true, detail:reason}]` + `skipped[]`
4. A check that CAN run still runs and scores for real (never skip-when-runnable)

**Regression:** unit tests in `services/mcp/test_verify_battery.py` (score path + SKIP path
per adapter).

---

## Fix 2 — Concrete force-inject for `ecad_wire` + `ecad_add_power` (axis W / X)

**Fingerprint (mega-14):** force-inject advanced to `board_from_schematic` after orphan
`ecad_add_power(net=GND)` with `net_size=0`; **zero** `ecad_wire`; IC-only places → empty
SPICE netlist; ERC `power_not_driven` ×18.

**Fix (`services/agent/agent_turn.py`):**
1. Keep Fix4 order: passive Device:R → power → **real wire** → board → DRC.
2. `_ecad_place_pin_records` reads pin lists + world `xy` from place results.
3. `_ecad_forced_tool_args`:
   - `ecad_add_power` bonds **onto pin world xy** (GND first, then 3V3 on power_in pins) —
     never orphan `(0,0)` when pins are known
   - `ecad_wire` uses concrete `from_ref/from_pin/to_ref/to_pin` from place pin numbers
     (prefer R*→U* with real pin numbers)
4. Soft enablement + chain need still force wire before board (orphan power does not count)

**Regression:**
- `test_forced_power_args_use_pin_xy`
- `test_forced_wire_args_use_place_refs_and_pins`
- existing `test_ecad_hard_gate_force_injects_wire_after_thrash`
- `test_ecad_chain_need_helpers`

---

## Fix 3 — ERC re-runs after wiring so AC4 grades post-wire state (axis W / AC4)

**Fingerprint:** AC4 multi-tool `ecad_run_drc+ecad_run_erc` could grade stale pre-wire ERC
(or place-only ERC) while DRC was clean.

**Fix:**
1. `_ecad_chain_need`: after board+DRC+real wire, force `ecad_run_erc` if not yet ok.
2. Soft enablement nudge after DRC/board/wire/power: re-run ERC so power_not_driven clears.

**Regression:** `test_chain_need_forces_erc_after_drc` + chain helper update.

---

## Test command

```text
python3 -m pytest services/mcp/test_verify_battery.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py \
  services/agent/test_mega10_fixes.py -q
```

---

## Files touched

| Path | Change |
|---|---|
| `services/verify/battery.py` | arg adapters + honest SKIP for sketch/measure/dim/impedance |
| `services/agent/agent_turn.py` | pin-aware forced wire/power args; ERC after DRC chain |
| `services/mcp/test_verify_battery.py` | adapter unit tests + force-arg/chain ERC tests |
| `services/agent/test_mega11_legs.py` | chain need ERC after DRC |
| `reports/runs/mega-15/fixes.md` | this log |

---

## Ready for MegaRun15 primary launch

Fixes 1–3 are **bound and unit-green**. DM `PrecisePanda` `fixes bound`.

Expected on primary (account `17d17fb1`, model id **grok-4.5** exact):
- Battery no longer zeros on signature misses (SKIP or real score)
- Force-inject places R, bonds power on pin xy, wires pin-to-pin, board, DRC, ERC
- AC4 grades post-wire ERC (path to errors==0)
- AC5 gets a wired netlist with simulatable Device:R
- Path to **≥8 live AC PASS** while A–U hold
