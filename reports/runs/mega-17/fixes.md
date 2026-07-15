# mega-17 harness fixes (Fixer17 = AlrightMosquito)

Baseline: `cd764e92`. Pair: MegaRun17=`RequiredWoodpecker` owns
`reports/runs/mega-17/**` + roadmap primary; Fixer17 owns
`services/agent/{loop,agent_turn,reviewer_exec}` + tests + this file.

Axes targeted:
- **Z1** terminal grading unconditional — every mission terminal
  (`done` / `deliver_partial` / `needs_correction`) produces
  `criterion_result` for ALL 9 ACs (honest FAIL/SKIP/NOT-CHECKED when
  artifacts absent). mega-16 repro: 0/9 graded.
- **Z2** power-then-wire force-inject — `ecad_add_power` before any wire
  inject touching power nets; wire args derive from LIVE place/add_power
  refs (never hardcoded `#PWR1`); denial→reinject once on missing ref.
- **Z3** chain need schedules `ecad_simulate` after ERC (or when ERC not
  allowed) so AC5 has a chance on a wired netlist.
- Target: ≥8 live AC PASS. Regression: A–Y hold (esp Y paid routing + Fix8
  board chain).

Criteria were **not** weakened.

---

## Fix 1 — Z1 terminal grading unconditional (`loop.py`)

**Fingerprint (mega-16):** `stop_reason=deliver_partial`, stage_log entered
build/verify but never exited them; `criterion_results=[]`; grade_run wrote
0 `criterion_ACn.json`. Soft-reconcile past incomplete BUILD/VERIFY skipped
`_run_reviewer_checks` because that call lived *after* the
`if not all_done: break` / `if reconciled: continue` gates.

**Fix:**
1. Run `_run_reviewer_checks` at **VERIFY entry before** all_done/reconciled
   gates so soft-join still grades.
2. `_ensure_terminal_criterion_grading` at every driver return: if any declared
   AC missing, re-run reviewer from artifacts; stub remaining with honest
   `NOT-CHECKED` + `source=terminal_grading`. Empty manifests fall back to
   rocket-plane AC1–AC9 shells so live PASS counts are always defined.

**Regression:** deliver soft-join still `deliver_partial`; silence never
fabricates PASS.

---

## Fix 2 — Z2 power-then-wire + live refs (`agent_turn.py`)

**Fingerprint (mega-16):**
- `ecad_add_power` ok (orphan net_size=0).
- force_inject once with `tool=ecad_place_symbol` (passive R — correct mid-step).
- Model/force path then `ecad_wire(from_ref=R1, to_ref=#PWR1)` →
  `no symbol '#PWR1' on the schematic` (placed: U1, R1).
- Engine `ecad_add_power` returns net/pos, **not** a `#PWR*` symbol ref.

**Fix:**
1. Chain need already prefers `ecad_add_power` before `ecad_wire` when power
   missing — held.
2. `_ecad_power_refs` / `_ecad_all_live_refs` harvest live place + add_power
   results only (never invent `#PWR1`).
3. `_ecad_forced_tool_args("ecad_wire")` endpoints = live place refs (passive↔IC
   or pin1↔pin2); uses a power ref **only if the engine returned one**.
4. Pre-dispatch `_ecad_sanitize_wire_args` rewrites any endpoint not in the live
   ref set.
5. Post-dispatch denial→reinject once: on `no symbol '…'` re-derive and retry
   (`wire_ref_retry` / `ecad_wire_ref_retry` ledger events). Same path on
   bootstrap force-inject.

**Regression:** orphan power still does not unlock board; real wire required
(Fix8/mega-14 held).

---

## Fix 3 — Z3 AC5 simulate after ERC (`agent_turn.py`)

**Fingerprint (mega-16):** `ecad_simulate=0`; AC5 never graded.

**Fix:** `_ecad_chain_need` after DRC+ERC (or wire when ERC not on allowlist)
returns `ecad_simulate`. Forced args: `{analysis: "op", probes: ["V(3V3)"]}`.

---

## Tests

`services/agent/test_mega17_fixes.py` — 11 cases:
| Test | Axis |
|---|---|
| `test_terminal_grading_on_deliver_partial` | Z1 soft-join grades 9 ACs |
| `test_terminal_grading_stub_when_no_manifest_acs` | Z1 empty → 9 stubs |
| `test_terminal_grading_skips_mid_run` | Z1 no mid-run grade |
| `test_mega16_ledger_terminal_grading_replay` | Z1 fixture replay |
| `test_chain_need_power_before_wire` | Z2 order |
| `test_forced_wire_args_never_hardcode_pwr1` | Z2 no #PWR1 |
| `test_sanitize_rewrites_invented_pwr1` | Z2 sanitize |
| `test_wire_missing_ref_retry_once` | Z2 denial→reinject |
| `test_power_refs_from_live_add_power` | Z2 live power ref |
| `test_chain_need_schedules_simulate_after_erc` | Z3 |
| `test_chain_need_simulate_without_erc_in_allow` | Z3 |

Also green: `test_mega11_legs` + `test_mega11_grading` + `test_mega16_fixes`.

**Counts (last run):** mega17 **11 passed**; mega11 legs+grading + mega16 + mega17
**~90+ passed**, 0 failed.

---

## Bind one-liners (for MegaRun17)

1. **Z1** — VERIFY grades at entry even when soft-reconciled; terminal always
   emits criterion_AC1…AC9 (honest NOT-CHECKED if no evidence).
2. **Z2** — wire inject uses live U1/R1/… only; `#PWR1` sanitized + one
   missing-ref retry; add_power still before wire in chain need.
3. **Z3** — after ERC (or wire-only allow), force-inject `ecad_simulate(op)`.

No push (Main gates). Ready for primary after this bind.

---

## Residual rebind (post a2 primary) — Z2 power-before-board

**Live a2 residual:** force_inject still preferred Device:R place; model then
self-looped R1.1↔R1.2; board+DRC advanced **without** `ecad_add_power`
(ERC power_not_driven → AC4 FAIL).

**Additional fix:**
1. `_ecad_has_cross_wire` — same-ref self-loop is NOT product wiring when ≥2
   symbols are placed (single-ref self-loop still counts).
2. `_ecad_nets_ready_for_board` — requires `ecad_add_power` (when allowed) **and**
   product wire before board unlock.
3. Hard gate / chain need / soft nudges all use nets-ready (not bare
   "any wire ok").
4. Force wire args prefer passive↔IC when ≥2 live refs (never self-loop first).

**Tests added:** `test_self_loop_does_not_unlock_board`,
`test_power_then_cross_wire_unlocks_board`,
`test_force_inject_after_passive_is_add_power`. Suite: **82+** green with
mega11 legs/grading.
