---
id: T-FIN25
title: E2E golden demo + gate battery - MCU sensor node designed, verified, emulated, cooled
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN25-e2e-golden-demo
depends_on: [T-FIN3, T-FIN6, T-FIN9, T-FIN11, T-FIN17, T-FIN23, T-FIN24]
lane: simulation
kind: new
priority: 2
---

## Context

The proof the content is finished: one scripted mission builds an STM32F4 sensor-node
board (MCU + I2C temp sensor + regulator + FPGA-lite optional leg), runs the full
verification ladder (ERC, DRC, host-board pack, SI pack, FPGA bank check + constraint
export, PDN), generates firmware via fw_new_project (pinmap from the board), emulates it
with a simulated sensor scenario and GPIO assertions, runs fw_verify_board, hands the
board to thermal FEA, picks a heatsink for the regulator, runs the system power budget
with the emulation duty cycle, and emits a make_report deliverable. Wired into the gate as
the sim/fw content smoke (fast paths only; OpenFOAM/renode-gated legs skip honestly).

## Files / areas touched

- scripts/demo_sensor_node.py (or tests/e2e_) — the scripted mission
- scripts/gate.sh — new stage entry, marker-file pattern per gate conventions
- docs/ — demo narrative doc with artifacts
- fixture board + firmware committed as test data

## Definition of done

- Demo runs green end-to-end on this machine; each leg's artifact checked (violations empty, assertions pass, Tj under limit, budget verdict)
- Gate stage added; renode/OpenFOAM-dependent legs skip with honest markers when absent
- Total gate cost bounded (<5 min for the fast path)
- Demo doubles as the living worked example referenced from cad_guide
