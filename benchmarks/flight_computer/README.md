# B1 — Flight-computer E2E benchmark

Deterministic end-to-end exercise of the real MCP tool surface on a central
flight-computer design (MCU + 3V3 buck + MPU6050 + BMP280 + LIS3MDL + NEO-6M).

## Run

From the repo root, with the services venv (any interpreter with the MCP service
requirements works; `CHUDWARE_LIBCHUDWARE` points at a built engine):

```bash
CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so" \
  ~/.local/share/Chudware/mcp-venv/bin/python scripts/bench_flight_computer.py
```

Env: `CHUDWARE_FC_OUT`, `CHUDWARE_FC_MARKERS`, `CHUDWARE_FC_SEED` (default 0),
`CHUDWARE_FC_ANT_FULLWAVE_BUDGET_S` (openEMS, default 90), `CHUDWARE_FC_SIM_BUDGET_S`
(Renode, default 120), `CHUDWARE_ECAD_HEAVY_BUDGET_S` (default 180).

Exit code `0` when no leg is `FAIL` (`SKIP` / `WARN` allowed).

## Legs

| # | Leg | Tools |
|---|-----|-------|
| 1 | `parts` | `ecad_find_part` + library resolve |
| 2 | `schematic` | `ecad_new_schematic` / `ecad_place_symbol` / `ecad_wire` |
| 3 | `sim` | `ecad_simulate` OP on ideal 3V3 front-end |
| 4 | `board` | `board_from_schematic` (place+route+opt, seed=0) |
| 5 | `trace_opt` | report metrics + `ecad_optimize_traces` |
| 6 | `antenna` | `ecad_antenna_design` / `ecad_antenna_analyze` (IFA 2.4 GHz + GPS patch; openEMS under budget) |
| 7 | `checks` | impedance, DRC, DFM, PDN, `ecad_to_3d`, thermal handoff, vibe |
| 8 | `firmware` | `fw_new_project(stm32f4)` + `fw_build` |
| 9 | `sensor_loop` | `climb_turn_profile` → `fw_sensor_scenario` → `fw_sim_run` + `fw_verify_board` |
| 10 | `power` | `power_budget` rollup |
| 11 | `report` | consolidated verdict card |

## Artifacts

Written under this directory (override with `CHUDWARE_FC_OUT`):

- `summary.json` — machine-readable leg table + wall_ms
- `VERDICT.txt` — human card
- `flight_computer_sch.json` / `flight_computer_board.json`
- `sensor_scenario.json` / `power_budget.json` / `checks.json`
- `markers/*.marker` — per-leg PASS/WARN/FAIL/SKIP

## Gate

`scripts/gate.sh` runs this script as the opt-in stage `stage_bench_flight_computer`
when `CHUDWARE_RUN_BENCH_FC=1` (otherwise the stage is skipped). The stage uses the
gate's resolved Python, picks `core/build-occt-linux` or `core/build-linux`
`libchudware.so` when `CHUDWARE_LIBCHUDWARE` is unset, writes output and markers under
the gate temp dir (not this directory), and defaults `CHUDWARE_ECAD_HEAVY_BUDGET_S` to
300. Any FAIL marker or non-zero exit fails the gate; SKIP legs (absent
Renode/openEMS/build123d) do not.

## Known breakage

`scripts/bench_flight_computer.py` still imports the retired top-level `engine`
package (`engine.ecad` in `parts` / `schematic` / `board`, `engine.firmware` in
`sensor_loop`). Each leg that reaches one of those imports records FAIL with
`ModuleNotFoundError` (dependent legs then SKIP) until the script is repointed at the
live `services.*` modules, so an opted-in gate run currently fails.
