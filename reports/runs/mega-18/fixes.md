# mega-18 harness fixes (Fixer18 = PlainLizard)

Baseline: `769f92c6`. Pair: MegaRun18=`ValidBovid` owns
`reports/runs/mega-18/**` + roadmap primary; Fixer18 owns
`services/agent/{agent_turn,reviewer_exec}` + `test_mega18_fixes.py` + this file.

Axes targeted:
- **Z2r** force-EXECUTE `ecad_add_power` when parts are placed and power is
  missing (cascade, not schedule-only) — same inject class as mega-14
  `board_from_schematic`; returned refs feed derived `ecad_wire`; self-loop
  guard rejects same-ref pairs when ≥2 live refs. Replay mega-17 a2 residual.
- **Z4** AC5 grader reads live `ecad_simulate` payload; null `V(3V3)` → honest
  **FAIL** naming the missing net (never NOT-CHECKED when the tool ran).
- **G18** (runner) grok-4.5 revalidation on fresh SuperGrok account
  `06dc3359` — not owned by Fixer18.
- Regression: A–Z1/Z3/Y hold (mega-11/16/17 suites green).

Criteria were **not** weakened.

---

## Fix 1 — Z2r cascade force-EXECUTE (`agent_turn.py`)

**Fingerprint (mega-17 a2):** force_inject fired `ecad_place_symbol` only;
`ecad_add_power` was **never** force-executed; model self-looped R1.1↔R1.2;
board+DRC advanced → ERC `power_not_driven`×4 (AC4 FAIL).

**Fix:**
1. `_ecad_force_chain_cascade` / `_ecad_dispatch_forced_step` — after denial
   cap or bootstrap incomplete cap, **EXECUTE** successive chain needs
   (`ecad_add_power` → `ecad_wire` → board/drc…) in-process (up to
   `_ECAD_FORCE_CASCADE_MAX=4`), reusing the mega-14 force-inject path.
2. Hard-gate + bootstrap incomplete paths call the cascade (no more single-step
   schedule that only rewrites `tool`/`args` then falls through once).
3. `_ecad_derive_wire_args` — net-aware pairs: power pin → rail ref when
   `ecad_add_power` returned a ref; else IC↔passive; never same-ref when ≥2
   live refs.
4. `_ecad_reject_self_loop_args` + sanitize pre-dispatch — reject R1↔R1 and
   re-derive.
5. `_ecad_last_place_refs` / `_ecad_has_cross_wire` — harvest refs from
   truncated ledger JSON strings; treat `n_place≥2` as multi-symbol so
   self-loops never unlock board when U1 result was truncated.

**Regression:** orphan power still does not unlock board; real cross-ref wire
required (Fix8/mega-14/17 held).

---

## Fix 2 — Z4 AC5 null probe → FAIL (`reviewer_exec.py`)

**Fingerprint (mega-17 a2 AC5):**
```json
{"ok": true, "analysis": "op", "backend": "mna", "converged": true,
 "device_count": 1, "op": {"V(3V3)": null},
 "reading": "no finite probe values — check connectivity / source values"}
```
→ graded **NOT-CHECKED** (`no numeric field matching 'v(3v3)'`).

**Fix:**
1. `_interval_null_probe_detail` — explicit null / non-finite probe under
   `op`/`readings` → **FAIL** naming net (`3V3`).
2. `_spice_tool_ran` — when simulate evidence exists (ok/converged/backend/op)
   but probe absent → **FAIL** missing net, not NOT-CHECKED.
3. Numeric in-band still PASS; out-of-band still FAIL; empty unrelated payload
   still NOT-CHECKED.

---

## Tests

`services/agent/test_mega18_fixes.py` — 11 cases:

| Test | Axis |
|---|---|
| `test_mega17_residual_chain_need_is_add_power` | Z2r residual shape |
| `test_force_cascade_executes_add_power_then_cross_wire` | Z2r cascade EXECUTE |
| `test_wire_self_loop_guard_rederives` | Z2r self-loop guard |
| `test_forced_wire_after_power_prefers_power_pin` | Z2r power-pin → rail |
| `test_mega17_ledger_replay_produces_add_power_and_valid_wire` | Z2r ledger fixture |
| `test_live_force_inject_turn_hits_add_power` | Z2r end-to-end turn |
| `test_ac5_null_v3v3_is_fail_not_not_checked` | Z4 mega-17 payload |
| `test_ac5_numeric_v3v3_still_pass` | Z4 PASS hold |
| `test_ac5_out_of_range_still_fail` | Z4 FAIL hold |
| `test_ac5_tool_never_ran_still_not_checked` | Z4 silence hold |
| `test_ac5_missing_probe_with_converged_is_fail` | Z4 missing probe |

Also green: `test_mega11_legs` + `test_mega11_grading` + `test_mega16_fixes` +
`test_mega17_fixes` + `test_mega18_fixes`.

**Counts (last run):** mega18 **11 passed**; mega11 legs+grading + mega16 +
mega17 + mega18 **105 passed**, 0 failed.

---

## Bind one-liners (for MegaRun18)

1. **Z2r** — after place thrash / incomplete bootstrap, harness
   force-EXECUTES `ecad_add_power` then net-aware `ecad_wire` (no self-loop);
   board stays gated until power + cross-ref wire.
2. **Z4** — AC5: null/missing `V(3V3)` after `ecad_simulate` ran → FAIL
   naming net `3V3`, never NOT-CHECKED.
3. **G18** — runner: frontier `xai-sub` grok-4.5 on account `06dc3359`,
   auto_pick-first conservation (no per-node frontier injection), openrouter
   deepseek mid-fallback remains.

No push (Main gates). Ready for primary after this bind.
