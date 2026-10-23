# STM32 sensor-node demo

**Script:** `scripts/demo_sensor_node.py` · **Gate stage:** `stage_sensor_node_demo` in
`scripts/gate.sh`

## What it proves

One scripted mission builds and exercises the ECAD, firmware and simulation tool surface:

| Leg | What runs | Honest degrade |
|-----|-----------|----------------|
| **board** | STM32F4 sensor-node PCB (regulator U1, MCU U2, I2C TMP103 U3, iCE40 FPGA-lite U4, decoupling, crystal, BOOT0 pull-down) | — |
| **erc** | Minimal schematic ERC | advisory counts |
| **drc** | Board DRC | advisory counts |
| **hostcheck** | MCU host-board rule pack | sequencing findings |
| **sicheck** | High-speed/SI pack | findings |
| **fpga** | Bank check + constraint export (LPF/CSV) on the iCE40 symbol | symbol missing → SKIP |
| **pdn** | DC IR-drop on +3V3 | solver absent → SKIP |
| **firmware** | `fw_new_project` + pinmap + `fw_verify_board` static; build + `fw_sim_run` with `fixture:i2c_temp_tmp103` when toolchain+Renode present | Renode/QEMU/cross-toolchain → SKIP marker `fw_sim.marker` |
| **thermal** | `ecad_thermal_handoff` live FEA | no build123d / no libchudware → envelope-only or SKIP |
| **heatsink** | `heatsink_select` for U1 to Tj ≤ 85 °C | network_estimate without mesh |
| **power_budget** | System budget + battery life with a duty-cycle profile | synthetic 1 % profile when no fw power_profile |
| **report** | `make_report` verification_report deliverable | deliverables deps absent → SKIP |

## Run locally

```bash
export CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so   # sim-enabled build
export PYTHONPATH=$PWD
# optional:
#   CHUDWARE_SENSOR_DEMO_OUT=scratch/sensor_node_demo
#   CHUDWARE_SENSOR_DEMO_THERMAL_DRY=1   # skip live mesh/solve
~/.local/share/Chudware/mcp-venv/bin/python scripts/demo_sensor_node.py
```

Exit 0 iff every leg is PASS or SKIP (no FAIL). Markers land under
`$CHUDWARE_SENSOR_DEMO_OUT/markers/*.marker`; the full summary is in `summary.json`.

## Gate wiring

`scripts/gate.sh` runs the demo by default (opt out with `CHUDWARE_SKIP_SENSOR_DEMO=1`).
Per-leg SKIP markers keep Renode/OpenFOAM-gated paths honest without failing the gate on
an unprovisioned box.

## Artifacts (typical)

- `scratch/sensor_node_demo/summary.json` — leg table
- `scratch/sensor_node_demo/markers/*.marker` — PASS/FAIL/SKIP per leg
- `scratch/sensor_node_demo/power_budget.json`
- `scratch/sensor_node_demo/project/artifacts/` — the verification report (`.docx`)
- FPGA constraints under `fpga_constraints/` when export succeeds

## Worked example

This script is the end-to-end reference for the ECAD/firmware/simulation tool surface.
`cad_guide('ecad')`, `cad_guide('firmware')`, and `cad_guide('simulation')` point agents at
the same tools; this demo is the one place that chains them all.
