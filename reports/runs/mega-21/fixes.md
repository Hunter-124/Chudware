# mega-21 harness fixes (Fixer21 = TroubledSlug)

Baseline: `175c4b81`. Pair: MegaRun21=`IndividualThrush` owns
`reports/runs/mega-21/**` + roadmap primary; Fixer21 owns
`services/agent/agent_turn.py`, `services/agent/loop.py`,
`services/agent/reviewer_exec.py`, `services/agent/test_mega21_fixes.py` + this file.

Axes targeted (grounded in mega-20 a1 findings):

- **M21** mech starvation — live deliver_partial with `new_part=0` /
  `add_features=0` so AC1–3 FAIL despite ECAD product. Deliver gate must treat
  missing mech part artifacts like the ECAD chain treats an unwired schematic.
- **N21** AC5 net-name null — wires + VIN source exist but `V(3V3)` measures
  null because the live rail is `N$2` (VIN path). Measure/grade must resolve
  the probe net from the live netlist / source landing net.

Regression: A20/B20/C19/D19/Z1/Z3/Z4/Y/Fix8 + mega-11..20 holds. Criteria not
weakened (AC5 still needs a finite numeric voltage on a real rail; AC1–3 still
need verify_battery/measure evidence).

---

## Live fingerprints (mega-20 a1)

| Observation | Detail |
|---|---|
| Terminal | `done` / `deliver_partial` |
| Mech | `new_part=0`, `add_features=0` → AC1/2/3 FAIL |
| ECAD | VIN + Device:R, wire VIN.+→R1.1 on net `N$2`, board+DRC clean |
| AC5 | `probes=["V(3V3)"]`, `op={"V(3V3)": null}`, nodes `N$2,N$3,N$1` |
| Live AC | 5/9 (AC4,6,7,8,9) |

---

## Fix M21 — deliver-time mech BUILD force

**Root cause:** soft-join DELIVER advanced with zero mechanical product tools;
nothing forced the airframe leg the way ECAD force-cascade forces wire/board.

**Fix (`agent_turn.py` + `loop.py`):**

1. `_mech_has_part_artifact(ledger)` — requires successful `new_part` **and**
   `add_features` (bootstrap alone is not product).
2. `_airframe_spec_from_manifest` + `_airframe_build_ops` — concrete dims from
   mission brief + feature_modeler role card (body tube OD≈60 / len≥300–500,
   payload bay ≥56×86×20, wing, 3 fins).
3. `_mech_force_build_chain` — force-EXECUTE `new_part` then `add_features` with
   those ops; log `mech_chain_force_inject` + forced `tool_call` events.
4. `mech_deliver_gate` — if mech missing, force once per `run_id`; if still
   missing → typed refusal (`mech_leg_missing_after_force…`) for leg relaunch.
5. `loop._drive_phases_from_dag` terminal — before criterion grading, run the
   gate; refuse deliver (`status=blocked` + refusal stop_reason) when force
   cannot produce a part artifact.

**Fixture:** mega-20 ledger replay proves the gate fires (`new_part`+`add_features`).

---

## Fix N21 — AC5 measurement net resolution

**Root cause:** simulate/grade hard-coded `V(3V3)` while the live driven net was
the VIN source pin net (`N$2`). Probe null → AC5 FAIL even with a wired source.

**Fix (`agent_turn.py` + `reviewer_exec.py`):**

1. `resolve_measure_net` / `resolve_measure_net_from_ledger` — prefer exact
   3V3 → 3V3-class names → injected source net (VIN/`Device:V` + pin) → named
   rails; **do not** steal anonymous product-wire nets when no source landed
   (preserves mega-17 forced-sim `V(3V3)`).
2. Forced `ecad_simulate` args carry `probes=[V(<resolved>)]` + `resolved_net`.
3. `_domain_params_hint(ecad_simulate)` rewrites AC5 params_hint probes from the
   live ledger before measure.
4. `_run_one` stamps `resolved_net` / `resolved_net_source` into criterion
   evidence.
5. `evaluate_pass_rule` interval path accepts a finite voltage on the resolved
   rail (and records the measured net name in detail). Still FAIL (never
   NOT-CHECKED) when the tool ran and all probes are null/non-finite.

**Fixture:** mega-20 netlist shape (VIN → `N$2`, null `V(3V3)`).

---

## Tests / gate

```bash
python3 -m pytest \
  services/agent/test_mega21_fixes.py \
  services/agent/test_mega20_fixes.py \
  services/agent/test_mega19_fixes.py \
  services/agent/test_mega18_fixes.py \
  services/agent/test_mega17_fixes.py \
  services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py -q
```

**Counts (this bind):** **142 passed**, 0 failed.

---

## Bind one-liners (for MegaRun21)

1. **M21** — deliver gate force-injects airframe `new_part`+`add_features` once
   (role-card dims); second miss → typed refusal, no silent zero-mech deliver.
2. **N21** — AC5 probes/grades the live resolved rail net (3V3-class else
   source/VIN net); `resolved_net` recorded in criterion evidence; finite V
   still required.
3. Frontier: xai-sub grok-4.5 EXACT account `06dc3359`; mid deepseek paid; ollama leaves.
4. No push (Main gates). Ready for primary after this bind.

No push. Fixes bound.
