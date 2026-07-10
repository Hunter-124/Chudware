---
id: T-FIN3
title: Sensor stimulus scenario system - simulated sensors/GPIO data driving emulated firmware
workstream: FIN
owner: fable-fin
status: doing
branch: task/T-FIN3-fw-sensor-scenarios
depends_on: [T-FIN2]
lane: ecad-firmware
kind: new
priority: 2
---

## Context

The `scenario` param of fw_sim_run is accepted but undocumented and unused. Define a
declarative scenario schema (aicad.fwscenario/1): time-series stimulus tracks bound to
peripherals — analog waveforms on ADC channels (sine/ramp/csv samples), I2C/SPI sensor
register models (e.g. temp sensor returning programmed values), GPIO edge sequences
(buttons, encoder quadrature), UART injection. Implement via Renode's Python peripherals /
hooked sensor models. Ship a small fixture library of common sensors (I2C temp, SPI IMU
stub, quadrature encoder, ADC thermistor curve) and extend the assertion vocabulary to
cover responses (uart contains X within t, gpio toggles N times, pin period ~ P).

## Files / areas touched

- contracts/firmware_rpc.md — aicad.fwscenario/1 schema (append-only)
- engine_oracle/firmware/scenario.py — schema parse + Renode stimulus binding
- engine_oracle/firmware/fixtures/ — sensor model library
- engine_oracle/firmware/emulator.py — scenario wiring into .resc generation
- services/mcp/firmware_tools.py + cad_guide('firmware') — document scenario usage

## Definition of done

- A demo firmware reading an I2C temp sensor + ADC channel passes assertions purely in emulation with programmed sensor data
- Encoder/button GPIO stimulus works with edge/count assertions
- Scenario schema documented in the contract; cad_guide('firmware') worked example added
- Fixture library covers >=4 sensor archetypes with tests
