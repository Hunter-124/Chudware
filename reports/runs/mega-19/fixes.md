# mega-19 harness fixes (Fixer19 = ColdMuskox)

Baseline: `6d11238b`. Pair: MegaRun19=`UnemployedTuna` owns
`reports/runs/mega-19/**` + roadmap primary; Fixer19 owns
`services/agent/agent_turn.py`, `services/verify/battery.py`,
`services/agent/test_mega19_fixes.py` + this file.

Axes targeted:
- **D19** power-satisfied = `ecad_add_power` executed OR power symbols/nets
  already present in ledger scan (stop forever-`add_power` on fixtures with
  manual power). Clears the 6 reds mega-18 commit exposed.
- **A19** forced `ecad_wire` prefers power_in → rail refs **returned by**
  `ecad_add_power` (never invent `#PWR1`; no passive-passive when rail refs
  exist). Replay mega-18 residual shape as fixture.
- **B19** forced DRC/trace/autoroute args carry `board_doc_id` harvested from
  `board_from_schematic` result (incl. truncated ledger JSON strings).
- **C19** sourceless netlist: force-place `Device:V` (DC 3.3) before
  `ecad_simulate` in cascade; `measure_between` derives two real feature refs
  or honest SKIP (no signature-zero).
- Regression: Z1/Z3/Z4/Y/Fix8 + mega-11/16/17/18 suites hold. Criteria not
  weakened.

---

## Fix D — power-satisfied (unblocks 6 reds)

**Fingerprint:** PlainLizard Z2r cascade required bonded/`net_size≥1` power and
re-forced `ecad_add_power` forever when power symbols already existed; also
broke IC-only → Device:R order and post-power → wire / simulate schedule.

**Fix (`agent_turn.py`):**
1. `_ecad_power_satisfied` — True when any successful `ecad_add_power` ran, OR
   `_ecad_existing_power_symbols_or_nets` finds `#PWR*` / `power:` / labeled
   rails / V sources in the ledger.
2. `_ecad_chain_need` — order:
   - IC-only + orphan off-pin power → rebond `add_power` (mega-18 a1)
   - IC-only + no power → `place_symbol` Device:R (mega-11)
   - else first power when not satisfied → `add_power`
   - then cross wire → board (power-satisfied) → DRC → ERC → simulate
3. `_ecad_nets_ready_for_board` — power-satisfied (when allowed) + cross wire;
   orphan ports alone still do not unlock board (no cross).
4. Hard-gate `_has_power_port` uses `_ecad_power_satisfied`.

**6 reds green (original assertions):**
`test_chain_need_power_before_wire`,
`test_chain_need_schedules_simulate_after_erc`,
`test_chain_need_simulate_without_erc_in_allow`,
`test_power_then_cross_wire_unlocks_board`,
`test_ecad_hard_gate_force_injects_wire_after_thrash`,
`test_ecad_chain_need_helpers`.

---

## Fix A — rail-correct wire pairs

**Fingerprint (mega-18 live):** force wire paired U1↔R1, not power-pin→rail.

**Fix:**
1. `_ecad_derive_wire_args` — when `_ecad_power_refs` yields engine refs, pair
   IC power_in (net-hint matched) → that ref; never invent `#PWR1`.
2. Prefer non-passive endpoints when no rail ref (avoid pure R↔R).
3. Fixture: mega-18 ledger residual shape + synthetic rail refs proves ends
   include returned `#PWR*` refs.

---

## Fix B — board doc_id threading

**Fingerprint (mega-18 live):** `board_from_schematic` → `08cfb42d9b98` but
forced DRC ran against sch `0ce20163ee10`.

**Fix:**
1. `_ecad_board_doc_id` / `_ecad_sch_doc_id` harvest structured + truncated
   JSON (`board_doc_id` / `doc_id` / `sch_doc_id`).
2. `_ecad_forced_tool_args` for `ecad_run_drc`, `ecad_autoroute`, `ecad_to_3d`,
   `ecad_run_pdn`, `ecad_export` set `doc_id` to the board id.

---

## Fix C — SPICE source + measure_between

**Fingerprint:** AC5 null V(3V3) with ICs only / no source; AC1
`measure_between` soft zero.

**Fix:**
1. Cascade: before forced `ecad_simulate`, if `_ecad_has_sim_source` is false,
   force-EXECUTE `ecad_place_symbol` Device:V `DC 3.3` on sch doc.
2. `battery._adapt_check_kwargs(measure_between)` — derive two feature ids from
   `doc_tree`, or top/bottom faces for a single solid feature, else honest SKIP
   (never empty queries → signature zero).

---

## Tests

`services/agent/test_mega19_fixes.py` — D/A/B/C cases including mega-18 ledger
rail-pair fixture, truncated board-doc DRC args, V-inject cascade,
measure_between SKIP/derive.

Gate command:

```bash
python3 -m pytest \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py -q
```

**Counts (this bind):** **117 passed**, 0 failed.

---

## Bind one-liners (for MegaRun19)

1. **D19** — power-satisfied = add_power executed OR power symbols/nets already
   present; board still needs cross-ref wire.
2. **A19** — forced wire uses add_power-returned rail refs (power_in→rail).
3. **B19** — forced DRC/trace carry board doc_id from board_from_schematic.
4. **C19** — Device:V inject pre-simulate when sourceless; measure_between
   real refs or honest SKIP.
5. Frontier: xai-sub grok-4.5 on account `06dc3359`; conservation auto_pick.

No push (Main gates). Ready for primary after this bind.

---

## Residual rebind (post a1 primary)

**Live a1 fingerprint:** model executed `ecad_add_power`×2 then left ECAD
(wire=0, board=0, force_inject=0). Gate only logged denials=1 — cascade never
armed. Soft enablement inject only.

**Additional fix:**
1. **Post-success cascade** after `ecad_add_power` / wire / board / DRC / ERC —
   force-EXECUTE remaining chain need in-process (reason
   `post_success_cascade:*`). Does **not** cascade after `place_symbol` so
   thrash hard-gate denial tests still see blocked place.
2. **Done incomplete force** — when status=done with bootstrap incomplete,
   force cascade (`done_incomplete_force`) before accepting handoff.
3. **Hard-gate force-after=1** when power already satisfied and need is
   wire/board/drc (live only hit denials=1 then switched domains).
4. `_ECAD_FORCE_CASCADE_MAX` 4→8 so power→wire→board→drc→erc→V→sim fits.
5. Regression test `test_post_power_success_force_cascades_wire` replays a1
   residual (sch→place→power→power→done) and asserts forced wire + board +
   DRC with board doc_id.

**Gate after rebind:** **118 passed**, 0 failed.

---

## Residual rebind 2 (post a2 primary)

**Live a2 fingerprint:** hard_gate_cap DID fire (`ecad_chain_force_inject`×3)
but cascade only completed add_power×2 + Device:R place — **stopped before
wire** because turn budget was already ~13/16. Forced wire/board never ran.
U1 place result truncated at 1000 chars so pin xy harvest failed → rebond
args landed at (0,10) not U1 power pins.

**Additional fix:**
1. `_ecad_force_chain_cascade` uses `cascade_budget =
   max(budget, tool_calls_made + max_steps + 2)` so product chain finishes
   even after model thrash burned the turn budget.
2. In-process `calls[]` keep **full** tool results (ledger still truncated)
   so pin lists survive for rail wire derivation.
3. `_ecad_place_pin_records` regex-harvests pin xy from truncated JSON
   strings when parse fails.
4. Regression: `test_cascade_extends_budget_to_finish_wire_board` (budget
   13/16 residual → wire + board + DRC with board doc_id).

**Gate after rebind2:** **119 passed**, 0 failed.
