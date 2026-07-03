---
id: T-C2
title: Compile loop and pin-map header from ECAD netlist
workstream: C
owner: opus-crag
status: done
branch: task/T-C2-compile-loop
day: 2
depends_on: [T-C1]
lane: ecad-firmware
kind: new
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

- [x] `generate_pinmap()` produces a valid C header from ECAD net names — `pinmap.py`; board
  `nets` list or schematic `build_netlist`; power/ground rails skipped; positional names (`PA5`→5,
  `GPIO4`→4, `D13`→13) decode via `mcu_pinouts.resolve_pin`, named nets get logical indices (≥1000);
  guarded `#ifndef AICAD_PINMAP_H` header.
- [x] `new_project()` auto-selects MCU family from ECAD BOM when `mcu` param not specified —
  `project.new_project` (`mcu` now optional) + `mcu_pinouts.select_mcu_family`/`resolve_board_doc`
  (resolves an open doc_id or a `.ecad+`/`.json` path). STM32*→cortex-m4, ESP32*→xtensa-lx6,
  ATmega*→avr. Errors (never guesses) when neither `mcu` nor a derivable BOM MCU is present.
- [x] `fw_new_project`, `fw_build`, `fw_sources` MCP tools registered and functional —
  framework-free handlers in `engine/firmware/mcp_tools.py`; FastMCP wrappers in
  `services/mcp/firmware_tools.py`; registered append-only in `services/mcp/server.py` (stub-register
  verified: exactly the 3 tools). `fw_new_project` also seeds `build/include/pinmap.h`.
- [x] `fw_build` returns `{success, elf_path, stderr, warnings}` dict — returns the full
  `BuildResult` dict (§3); tool-absent path is graceful (`success=False`, `returncode=-1`), not an
  error envelope — see Q2 in `work/questions/T-C2.md` (DoD-over-§5 interpretation, non-blocking).
- [x] `python -m pytest engine_oracle/firmware/test_pinmap.py engine_oracle/firmware/test_mcp_tools.py -q`
  passes — **40 passed** (mcp-venv); full `engine/firmware/` suite **50/50** (incl. C1's 10).

## Gate & closeout notes (opus-crag)

`bash scripts/gate.sh` → **FAIL**, but every failing stage is pre-existing / out-of-lane; none is mine:
- `ui/imgui` build fails on `PanelField::selMulti` — the KNOWN break fixed on master at `b569020`.
  This branch is chained off `task/T-C1-firmware-scaffold`, which predates that fix; I touched **zero**
  C++/ui files (`git diff --stat …-- '*.cpp' '*.h'` is empty). Rebases away once C1→C2 land post-b569020.
- gate `pytest` FAILs because it runs system `python3 -m pytest` (deselects all 25 — the known
  venv-skip trap). Under `~/.local/share/AI-CAD/mcp-venv` my paths are green (above). The wider
  `engine/ecad` suite has pre-existing failures/errors under py3.14 (`test_impedance` import,
  `test_sync`) in files I never touched.
- `check.py` (feature_surface_audit + parity_audit) = FAIL-RECORDED non-fatal (pre-existing, per the
  gate summary). `core build` / `ui/abstract` / `ctest` all PASS.

Open questions (both non-blocking, recommended answers implemented): `work/questions/T-C2.md`.
`branch:` corrected to the actual worktree branch `task/T-C2-compile-loop`.

## AI hints

- `graphify query "ECAD schematic netlist net board"` — find netlist access API
- `graphify query "ECAD component BOM part MCU chip"` — find how BOM components are accessed from board doc
- Relevant files: `engine_oracle/ecad/`, `engine_oracle/firmware/project.py`, `engine_oracle/firmware/toolchains.py`, `services/mcp/server.py`
- Net name → pin number mapping for STM32: parse `PA<N>` → port A pin N; for ESP32: `GPIO<N>` → N; store lookup in `engine_oracle/firmware/mcu_pinouts.py`
