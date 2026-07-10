# T-FIN25 — STM32 sensor-node golden demo

**Date:** 2026-07-10 · **Workstream:** FIN · **Script:** `scripts/demo_sensor_node.py` ·
**Gate stage:** `stage_fin_content` in `scripts/gate.sh` (4.7)

## What it proves

One scripted mission builds and exercises the full FIN content surface:

| Leg | What runs | Honest degrade |
|-----|-----------|----------------|
| **board** | STM32F4 sensor-node PCB (regulator U1, MCU U2, I2C TMP103 U3, iCE40 FPGA-lite U4, decoupling, crystal, BOOT0 pull-down) | — |
| **erc** | Minimal schematic ERC | advisory counts |
| **drc** | Board DRC | advisory counts |
| **hostcheck** | MCU host-board rule pack (T-FIN9) | sequencing findings |
| **sicheck** | High-speed/SI pack (T-FIN10) | findings |
| **fpga** | Bank check + constraint export (LPF/CSV) on iCE40 symbol (T-FIN11) | symbol missing → SKIP |
| **pdn** | DC IR-drop on +3V3 (T-FIN13) | solver absent → SKIP |
| **firmware** | `fw_new_project` + pinmap + `fw_verify_board` static; build + `fw_sim_run` with `fixture:i2c_temp_tmp103` when toolchain+Renode present | Renode/QEMU/cross-toolchain → SKIP marker `fw_sim.marker` |
| **thermal** | `ecad_thermal_handoff` live FEA (T-FIN16) | no build123d / no libaicad → envelope-only or SKIP |
| **heatsink** | `heatsink_select` for U1 to Tj ≤ 85 °C (T-FIN17) | network_estimate without mesh |
| **power_budget** | System budget + battery life (T-FIN23) with duty-cycle profile | synthetic 1 % profile when no fw power_profile |
| **report** | `make_report` verification_report deliverable | deliverables deps absent → SKIP |

## Run locally

```bash
export AI_CAD_LIBAICAD=$PWD/core/build-occt-linux/bin/libaicad.so   # sim-enabled build
export PYTHONPATH=$PWD
# optional:
#   AICAD_FIN25_OUT=scratch/fin25_sensor_node
#   AICAD_FIN25_THERMAL_DRY=1   # skip live mesh/solve
~/.local/share/AI-CAD/mcp-venv/bin/python scripts/demo_sensor_node.py
```

Exit 0 iff every leg is PASS or SKIP (no FAIL). Markers land under
`$AICAD_FIN25_OUT/markers/*.marker`; full summary in `summary.json`.

## Gate wiring

`scripts/gate.sh` stage **4.7** runs the demo by default (bounded well under 5 min on
this machine — typically ~5 s with live thermal). Opt out: `AICAD_SKIP_FIN25=1`.

Per-leg SKIP markers keep Renode/OpenFOAM-gated paths honest without failing the gate
on an unprovisioned box.

## Artifacts (typical)

- `scratch/fin25_sensor_node/summary.json` — leg table
- `scratch/fin25_sensor_node/markers/*.marker` — PASS/FAIL/SKIP per leg
- `scratch/fin25_sensor_node/power_budget.json`
- `scratch/fin25_sensor_node/project/artifacts/t-fin25-sensor-node-golden-demo*.docx`
- FPGA constraints under `fpga_constraints/` when export succeeds

## Living worked example

This script is the end-to-end reference for the FIN tool surface. `cad_guide('ecad')`,
`cad_guide('firmware')`, and `cad_guide('simulation')` point agents at the same tools;
this demo is the one place that chains them all.
