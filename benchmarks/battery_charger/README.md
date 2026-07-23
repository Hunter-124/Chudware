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

```bash
CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so \
  /home/hunter/.local/share/Chudware/mcp-venv/bin/python \
  scripts/bench_battery_charger.py
```

Env:

- `CHUDWARE_BC_OUT` — output dir (default `benchmarks/battery_charger`)
- `CHUDWARE_BC_MARKERS` — marker dir
- `CHUDWARE_BC_SEED` — fixed seed (default 0)
- `CHUDWARE_BC_SIM_BUDGET_S` — Renode wall budget (default 120)
- `CHUDWARE_ECAD_HEAVY_BUDGET_S` — board_from_schematic budget (default 180)

Exit 0 when no leg is FAIL (SKIP/WARN allowed).

## Artifacts

- `summary.json` / `VERDICT.txt` — per-leg status + `wall_ms`
- `sim_charge.json` / `sim_lvd.json` / `sim_safety.json` — real sim assertions
- `battery_charger_sch.json` / `battery_charger_board.json`
- `power_budget.json`, `markers/*.marker`

## Gate / simsuite wiring (Main)

Suggested:

```bash
# scripts/gate.sh
stage_bench_battery_charger() {
  "$MCP_PYTHON" scripts/bench_battery_charger.py
}
```

```python
# benchmarks/simsuite/run_all.py — optional entry
("battery_charger", "scripts/bench_battery_charger.py"),
```

Do **not** hard-gate on wall-times yet; record only.
