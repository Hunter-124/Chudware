---
id: T-C5
title: firmware.md agent prompt and demo firmware in Renode
workstream: C
owner: unassigned
status: todo
branch: task/T-C5-firmware-agent-demo
day: 5
priority: 3
depends_on: [T-C4]
---

## Context

Day 5 delivers the `firmware.md` agent prompt that generates HAL-level C from a behavior spec and pin map, then proves it in Renode. The demo board is the soil-moisture sensor PCB from the sprint demo: the agent writes sensor-read firmware using the generated pin-map header (T-C2), builds it, runs it in Renode, asserts on UART output, and the DAG orchestrator's firmware verify node calls `fw_sim_run` as its end condition.

## Files / areas touched

- `services/agent/prompts/firmware.md` — new agent system prompt
- `engine_oracle/firmware/` — full stack from T-C1..T-C4
- `engine_oracle/firmware/templates/renode_platforms/` — add demo board platform if needed
- `docs/sessions/demo_sprint_day5/` — demo run artifacts

## Steps

1. Run `graphify query "agent system prompt behavior spec HAL"` to understand prompt patterns used in other workstreams.
2. Write `firmware.md` system prompt: given a behavior spec (e.g., "read soil moisture via ADC every 5s, transmit over UART at 115200"), a pin-map header path, and MCU family, generate minimal HAL-level C; use the available MCP tools (`fw_build`, `fw_sim_run`) in a write → build → emulate → fix loop.
3. Add a Renode platform file for the demo MCU (STM32F4 or equivalent) if not already present in templates/.
4. Run the demo: call `dag_plan` for the firmware subtask; let the agent write `main.c` using the pin map; call `fw_build`; call `fw_sim_run` with assertion `"SOIL: [0-9]+"` on UART; iterate until passing.
5. Capture UART log and assertion results; save to `docs/sessions/demo_sprint_day5/firmware_run.log`.
6. Verify the DAG verify node calls `fw_sim_run` and its result propagates correctly.

## Definition of done

- [ ] `firmware.md` prompt exists at `services/agent/prompts/firmware.md`
- [ ] Agent can generate, build, and emulate a minimal soil-moisture sensor-read loop using the prompt
- [ ] `fw_sim_run` assertion `"SOIL: [0-9]+"` passes in Renode (or is documented as a stub with reason)
- [ ] DAG verify node using `fw_sim_run` as end condition works end-to-end
- [ ] UART log saved to `docs/sessions/demo_sprint_day5/firmware_run.log`

## AI hints

- `graphify query "agent prompt system firmware MCU HAL"` — check for existing firmware prompt patterns
- `graphify query "DAG verify node end condition fw_sim_run"` — confirm wiring from T-A4
- Relevant files: `services/agent/prompts/`, `engine_oracle/firmware/`, `services/dag/verify.py`
- STM32 HAL pattern for ADC: `HAL_ADC_Start()`, `HAL_ADC_PollForConversion()`, `HAL_ADC_GetValue()` — agent should know this; the pin map provides the ADC channel number
