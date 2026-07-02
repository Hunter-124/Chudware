---
id: T-C2
title: Compile loop and pin-map header from ECAD netlist
workstream: C
owner: opus-crag
status: doing
branch: task/T-C2-compile-loop-pinmap
day: 2
depends_on: [T-C1]
---

## Context

Day 2's killer feature: the MCU selected in the ECAD schematic BOM seeds the firmware target automatically, and the ECAD netlist (net names) generates a `#define` pin-map header — nobody currently links schematic nets to firmware pin constants automatically. The compile loop MCP tools (`fw_new_project`, `fw_build`, `fw_sources`) let the agent iterate: write source → build → read errors → fix → repeat.

## Files / areas touched

- `engine_oracle/firmware/pinmap.py` — new: ECAD netlist → pin-map header generator
- `engine_oracle/firmware/mcp_tools.py` — new: fw_new_project, fw_build, fw_sources handlers
- `engine_oracle/ecad/` — netlist export (existing); find the netlist API
- `services/mcp/server.py` — register fw_new_project, fw_build, fw_sources
- `engine_oracle/firmware/project.py` — link MCU from ECAD BOM (extends T-C1)

## Steps

1. Run `graphify query "ECAD netlist export net names"` to find how to access net names from an ECAD board doc.
2. Run `graphify query "ECAD BOM component MCU"` to find how components/BOM are accessed.
3. Implement `pinmap.py`:
   - `generate_pinmap(board_doc) -> str`: read net names from netlist; for each net with a GPIO-like name pattern (e.g. `PA0`, `GPIO_LED`, `SENSOR_DATA`), emit `#define PIN_<NETNAME_UPPERCASE> <port_number>`.
   - Write generated header to `project.build_dir/include/pinmap.h`.
4. Implement auto-MCU selection: when `new_project(board_doc_ref, ...)` is called without explicit MCU, look up the first MCU component in the ECAD BOM and map its MPN to `mcu_family` via a lookup table (`STM32*` → `cortex-m4`, `ESP32*` → `xtensa-lx6`, `ATmega*` → `avr`).
5. Implement `mcp_tools.py` handlers: `fw_new_project`, `fw_build` (runs `CMakeToolchain.build()`, returns `BuildResult` dict), `fw_sources` (lists source files in project).
6. Register all three tools in `server.py`.
7. Test: mock ECAD board with two nets `PA5` and `MISO`; assert generated pinmap.h contains `#define PIN_PA5` and `#define PIN_MISO`.

## Definition of done

- [ ] `generate_pinmap()` produces a valid C header from ECAD net names
- [ ] `new_project()` auto-selects MCU family from ECAD BOM when `mcu` param not specified
- [ ] `fw_new_project`, `fw_build`, `fw_sources` MCP tools registered and functional
- [ ] `fw_build` returns `{success, elf_path, stderr, warnings}` dict
- [ ] `python -m pytest engine_oracle/firmware/test_pinmap.py engine_oracle/firmware/test_mcp_tools.py -q` passes

## AI hints

- `graphify query "ECAD schematic netlist net board"` — find netlist access API
- `graphify query "ECAD component BOM part MCU chip"` — find how BOM components are accessed from board doc
- Relevant files: `engine_oracle/ecad/`, `engine_oracle/firmware/project.py`, `engine_oracle/firmware/toolchains.py`, `services/mcp/server.py`
- Net name → pin number mapping for STM32: parse `PA<N>` → port A pin N; for ESP32: `GPIO<N>` → N; store lookup in `engine_oracle/firmware/mcu_pinouts.py`
