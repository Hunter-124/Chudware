# B2 — Battery charger / controller E2E benchmark

Deterministic end-to-end exercise of the landed MCP surface for a Li-ion
charger/controller:

| Leg | What it proves |
|-----|----------------|
| `parts` | Starter-pack resolve: MCP73831, SwitchingCharger_SOP8, power MOSFETs, R_Sense, Load_Switch, LVD_Comparator, NTC, L_Power |
| `schematic` | Full charger schematic via `ecad_new_schematic` / `ecad_place_symbol` / `ecad_wire` |
| `sim_charge` | Real MNA transient: CC→CV clamp at 4.2 V + charge-current taper |
| `sim_lvd` | Real MNA DC sweep: load disconnects below V_LVD=3.0 V |
| `sim_safety` | Real MNA trips: OVP@4.35 V, OCP fold-back via R_Sense, OTP via NTC-divider ref |
| `board` | `board_from_schematic` + power-trace widths + `ecad_check_impedance` + `ecad_run_drc` |
| `thermal` | `ecad_thermal_handoff` on the pass device (dry-run envelope OK) |
| `firmware` | `fw_new_project` + charge state machine (PRECHARGE/CC/CV/DONE/FAULT) + `fw_build` |
| `safety_loop` | ADC over-temp / over-voltage via scenario → `fw_sim_run` asserts FAULT (Renode) |
| `power_report` | `power_budget` + consolidated PASS/WARN/FAIL/SKIP card + wall times |

## Run

From the repo root, with the services venv (any interpreter with the MCP service
requirements works; `CHUDWARE_LIBCHUDWARE` points at a built engine):

```bash
CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so" \
  ~/.local/share/Chudware/mcp-venv/bin/python scripts/bench_battery_charger.py
```

Env:

- `CHUDWARE_BC_OUT` — output dir (default `benchmarks/battery_charger`)
- `CHUDWARE_BC_MARKERS` — marker dir (default `$CHUDWARE_BC_OUT/markers`)
- `CHUDWARE_BC_SEED` — fixed seed (default 0)
- `CHUDWARE_BC_SIM_BUDGET_S` — Renode wall budget (default 120)
- `CHUDWARE_ECAD_HEAVY_BUDGET_S` — board_from_schematic budget (default 180)

Exit 0 when no leg is FAIL (SKIP/WARN allowed).

## Gate

`scripts/gate.sh` runs this script as the opt-in stage `stage_bench_battery_charger`
when `CHUDWARE_RUN_BENCH_CHG=1` (otherwise the stage is skipped). The stage uses the
gate's resolved Python, picks `core/build-occt-linux` or `core/build-linux`
`libchudware.so` when `CHUDWARE_LIBCHUDWARE` is unset, writes output and markers under
the gate temp dir (not this directory), and defaults `CHUDWARE_ECAD_HEAVY_BUDGET_S` to
300. Any FAIL marker or non-zero exit fails the gate; SKIP legs do not. Wall times are
recorded only, never gated.

## Known breakage

`scripts/bench_battery_charger.py` still imports the retired top-level `engine`
package (`engine.sim.circuit` in the MNA sim legs, `engine.ecad` in `parts` /
`schematic` / `board` / `thermal`, `engine.firmware` in `firmware`). Each leg that
reaches one of those imports records FAIL with `ModuleNotFoundError` (dependent legs
then SKIP) until the script is repointed at the live `services.*` modules, so an
opted-in gate run currently fails.

## Artifacts

- `summary.json` / `VERDICT.txt` — per-leg status + `wall_ms`
- `sim_charge.json` / `sim_lvd.json` / `sim_safety.json` — real sim assertions
- `battery_charger_sch.json` / `battery_charger_board.json`
- `power_budget.json`, `markers/*.marker`
