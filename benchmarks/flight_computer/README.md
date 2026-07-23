# B1 — Flight-computer E2E benchmark

Deterministic end-to-end exercise of the real MCP tool surface on a central
flight-computer design (MCU + 3V3 buck + MPU6050 + BMP280 + LIS3MDL + NEO-6M).

## Run

```bash
CHUDWARE_ECAD_HEAVY_BUDGET_S=180 \
  /home/hunter/.local/share/Chudware/mcp-venv/bin/python \
  scripts/bench_flight_computer.py
```

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

## Gate / simsuite wiring (Main)

- Env-gate: `CHUDWARE_RUN_BENCH_FC=1` → run `scripts/bench_flight_computer.py`
- Do **not** hard-fail the gate on `SKIP` (Renode/openEMS/build123d optional paths)
- simsuite: optional thin wrapper that shells this script and maps
  `summary.json` legs into the simsuite case list
