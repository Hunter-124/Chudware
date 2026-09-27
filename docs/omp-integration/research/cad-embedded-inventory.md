# CAD Embedded/Firmware Inventory

## TL;DR
CAD already ships a **mature, non-trivial firmware pillar** (the "T-FIN"/"T-C"/"T-RF15" task
series). This is NOT greenfield — it's an existing subsystem to extend, not bootstrap. Original
implementation was Python (`engine_oracle/firmware/*`, now deleted/ported — see T-RF15 note in
`services/mcp/firmware_tools.py:3`), fully rewritten as native C++ in `core/firmware/` and driven
through the same JSON-RPC worker + MCP-tool architecture as ECAD/CAM/reveng. Docs:
`contracts/firmware_rpc.md` (342 lines, canonical contract), `.claude/skills/gui-workspaces/SKILL.md:29`
(GUI), `contracts/licenses.md:78-80,90-91` (toolchain license ledger).

## 1. Existing firmware/embedded/MCU/debugger code

All under `CAD/core/firmware/` (C++ engine, 33 files) + `CAD/host/src/ui/firmware/` (GUI) +
`CAD/services/mcp/firmware_tools.py` (MCP surface) + `CAD/core/tests/test_firmware.cpp`.

- `core/firmware/project.{h,cpp}` — `FirmwareProject` record, JSON sidecar persistence
  (atomic tmp+replace), `fw_new_project`/`fw_sources` logic. Schema `aicad.firmware.project/1`.
- `core/firmware/toolchains.{h,cpp}` — build backends: **CMake + arm-none-eabi-gcc** (default,
  BSD-3/GPL-3-runtime-exception), **ESP-IDF** `idf.py` (Apache-2.0), **arduino-cli** (GPL-3). All
  subprocess-only, PATH-discovered, never linked (`contracts/firmware_rpc.md` §1). Returns
  `BuildResult{success, elf_path, map_path, stderr, warnings, returncode, cmd, ms}`.
- `core/firmware/emulator.{h,cpp}` — **Renode** (MIT, `--disable-xwt`, primary — instruction-accurate,
  GPIO/UART/I2C/SPI peripheral stimulus) and **QEMU** (GPL-2.0+, `qemu-system-arm -M mps2-an385`,
  semihosting-only fallback) subprocess wrappers. `SimResult{passed, uart_log, gpio_states,
  assertion_results, ms, gpio_events, trace_path}`.
- `core/firmware/hardware.{h,cpp}` — **real hardware flash/debug**: `probe_rs_flash/reset/halt/
  read_memory` (probe-rs, MIT/Apache-2.0, CMSIS-DAP/J-Link — `probe-rs download/reset/read`
  subcommands) and `esptool_flash` (esptool.py, GPL-2.0+, `write_flash`). **No GDB/OpenOCD/JTAG-SWD
  raw-protocol code** — debug is exposed only via probe-rs's high-level CLI (reset/halt/read-mem),
  not a GDB-remote-serial-protocol client or direct SWD bit-banging. This is the clearest gap vs.
  the ask (GDB integration, OpenOCD, direct JTAG/SWD).
- `core/firmware/mcu_pinouts.{h,cpp}` — MCU family table: `cortex-m0..m7`, `stm32f1/f4`, `esp32`
  (`xtensa-lx6`), **`avr`** (`atmega*`/`attiny*` → family `"avr"`, strap pins `RESET/NRESET/PC6`).
  AVR is recognized in the pin/strap model but **has no avr-gcc toolchain backend and no avrdude
  flash path** — `toolchains.cpp`/`hardware.cpp` only wire cmake/espidf/arduino build and
  probe-rs/esptool flash; AVR would currently fail `toolchain_not_found`/no flasher. Real gap:
  avr-gcc + avrdude glue.
- `core/firmware/platform_db.{h,cpp}` — MCU family → Renode `.repl` platform file lookup +
  capability matrix (uart/gpio/gpio_observe/adc/i2c/spi/timers) per family: stm32f4, stm32f1,
  nrf52, rp2040, esp32 (build+flash only, no emulation), generic cortex-m.
  `engine_oracle/firmware/templates/renode_platforms/*.repl` — bundled Renode platform
  descriptions (MIT, attributed).
- `core/firmware/scenario.{h,cpp}`, `sensor_models.{h,cpp}`, `sim_sensor_bridge.{h,cpp}` —
  declarative sensor/GPIO stimulus (`aicad.fwscenario/1`): i2c_temp_tmp103, spi_imu_stub,
  quadrature_encoder, adc_thermistor, user_button fixtures compiled into generated Renode `.resc`.
- `core/firmware/coverify.{h,cpp}` — firmware↔board co-verification (`fw_verify_board`): static
  pinmap/strap/direction checks + dynamic (trace or live emulation) pin-activity diff against the
  ECAD netlist. ERC-shaped findings.
- `core/firmware/powerprofile.{h,cpp}` — duty-cycle power bookkeeping from emulation traces +
  ECAD rail metadata (not SPICE).
- `core/firmware/pinmap.{h,cpp}` — generates `pinmap.h` (`#define PIN_<NET> <n>`) from an
  ECAD board's netlist for `board_doc_ref`-linked projects.
- `core/firmware/lsp.py` equivalent: `contracts/licenses.md:80` references `engine/firmware/lsp.py`
  `ClangdManager` (spawns `clangd --query-driver='**/arm-none-eabi-*'` for the firmware code panel;
  subprocess Popen + JSON-RPC over stdio) — **this is the one part still Python-side / not yet
  confirmed ported to `core/firmware/`** (no `core/firmware/lsp.*` in the current file listing —
  worth confirming before assuming it's live in the C++ port).
- `core/firmware/provision.{h,cpp}` — `fw_status` provisioning doctor: detects cmake, ninja,
  arm-none-eabi-gcc, renode, qemu-system-arm, probe-rs, esptool, idf.py, arduino-cli on PATH;
  never fails, returns install hints (`banners`) and per-family `can_build/can_emulate/can_flash`.
- `core/firmware/fw_cli.cpp` — presumably a CLI entry for the firmware engine (headless use).
- `core/firmware/subprocess.{h,cpp}` — shared subprocess-run plumbing (never raises; missing
  binary/timeout → `returncode=-1`), C++ port of the Python `_run` helpers.
- `core/firmware/templates_embedded.cpp` / `templates.h` — embedded CMake/source project
  templates baked into the binary (no external template repo needed).
- `host/src/ui/firmware/fw_workspace.{h,cpp}` — GUI workspace ("T-FIN5"): project rail + sources,
  build console (parsed warnings), **emulation + live UART console**, **GPIO trace/waveform
  viewer**, `fw_status`-gated Flash/Debug button rail. Progressive disclosure via
  `firmwareTabVisible()`. `CHUDWARE_SHOT=firmware` headless-shot fixture support.
- `core/tests/test_firmware.cpp` — mirrors the old Python test suites (`test_project.py`,
  `test_pinmap.py`, `test_coverify.py`, `test_mcp_tools.py`); external-tool legs (arm-none-eabi-gcc/
  probe-rs/esptool) **skip honestly** when absent.

**No AVR-GCC/avrdude, no GDB, no OpenOCD, no raw JTAG/SWD driver, no USB-device enumeration code**
exist anywhere in CAD. `grep` for those exact terms in `CAD/` (excluding scratch/omp-src) returns
zero hits beyond the probe-rs/esptool subprocess wrappers above and a `/dev/ttyUSB0` test fixture
string in `core/tests/test_firmware.cpp:385`.

## 2. ECAD/CAM/reveng subsystem maturity (for comparison — firmware follows the same pattern)

- **ECAD** (`services/mcp/ecad_*.py`, `services/ecad/*` [schema, drc, si_rules, place, zonefill,
  antenna, fpga, pdn, impedance, stackup, autosave, fab/{gerber,excellon,bom}, library/{kicad_sym,
  kicad_mod,sexpr,native,starter}]) — very mature: schematic+PCB editing, DRC/ERC, SI/PDN rules,
  Gerber/Excellon fab output, KiCad library import, antenna EM bridge, FPGA support. This is the
  reference precedent the firmware pillar explicitly mirrors (`firmware_rpc.md:5`: "Mirrors the
  ECAD precedent").
- **CAM** (`services/mcp/cam_tools.py`, README.md:19) — milling/turning/laser-plasma-waterjet,
  G-code for GRBL/LinuxCNC/Fanuc/Haas/Marlin/Siemens. Mature, sibling pillar.
- **reveng** (`services/mcp/reveng_*.py`, `contracts/reveng.md`) — reverse-engineering/point-cloud
  → part pipeline. Mature.
- **Firmware** — youngest pillar (`T-FIN*`/`T-C*`/`T-RF15` task IDs, i.e. added after ECAD/CAM were
  established), but already has: project/build/sim/flash/debug-read/verify-vs-board/power-profile/
  sensor-scenario tools, a GUI workspace, and a full C++ port off the original Python engine.
  Genuinely missing: real JTAG/SWD/GDB protocol work, AVR toolchain, OpenOCD, live external-MCP
  integration, and any "magic keyword"/slash-command triggers specific to firmware (see §5).

## 3. How the C++ host launches external processes

- `core/firmware/subprocess.{h,cpp}` — the canonical subprocess-run helper used by every firmware
  toolchain/emulator/flasher call (cross-platform, `contracts/firmware_rpc.md` §1 posture: never
  raises, timeout/absent-binary → `returncode=-1`). **This is the natural host seam for a new
  debugger/programmer process** — a JTAG/SWD tool (openocd, gdb, avrdude) would be wired exactly
  like `probe_rs_flash`/`esptool_flash` in `core/firmware/hardware.cpp`, using this same helper.
- `host/src/bootstrap.cpp:61,411` — `popen`/`_popen` wrappers (POSIX/Win32) for one-shot shell
  commands captured to string; `bootstrap.cpp:90-93` — raw Win32 `CreateProcessA` path.
- `host/src/ui/file_dialog.cpp:398` — another `popen` use (native file-dialog helper invocation).
- `host/CMakeLists.txt:380-383`, `cmake/cef.cmake:108` + `host/src/ui/widgets/cef/cef_app.cpp` /
  `cef_helper_main.cpp` / `cef_platform.h` — CEF embedded-browser subprocess-helper machinery
  (multi-process Chromium model); **not directly reusable for MCU tooling** but shows the host
  already manages a persistent side-process lifecycle (helper binary staged next to the main exe,
  argv/env control) — a template for a long-lived debug-server (openocd/probe-rs gdb-server)
  process if one is added.
- `services/installer/provision.py`, `services/installer/ngspice_backend.py` — Python-side
  precedent for "detect tool on PATH, subprocess it, degrade gracefully" (same idiom
  `core/firmware/provision.cpp` follows for `fw_status`).
- **No general plugin system** was found in `host/` beyond CEF's process model and the
  worker-RPC/MCP tool-registration pattern; CAD's "plugin" surface for new capabilities is the
  MCP-tool-family pattern (§5), not a dynamic native-plugin ABI.

## 4. Serial/USB/device communication code

- **None found directly** in CAD's own code — no `pyserial`/`serial.Serial`, no `libusb`, no
  raw `termios` ioctl for a serial port (the one `termios` hit, `Webservice/gateway/terminal.py:38-103`,
  is a PTY-resize helper for a web terminal, unrelated to embedded UART).
  All hardware I/O goes through **external subprocess CLIs** that own the transport themselves:
  probe-rs (USB HID/WinUSB to CMSIS-DAP/J-Link probes) and esptool.py (pyserial-based, but that
  dependency lives inside esptool's own process, never imported by CAD). `port` is passed through
  as a bare string (e.g. `/dev/ttyUSB0`) to the subprocess argv (`fw_flash` param, `hardware.cpp`
  `esptool_flash`), i.e. CAD does zero direct device enumeration or raw serial framing today.
- If real-time UART console / GDB-remote-serial-protocol / raw SWD bit-banging is wanted, that is
  entirely new code — either glue another CLI (openocd + its gdbserver, avrdude for AVR ISP/UPDI)
  the same subprocess way, or add an in-process serial library (not currently a dependency).

## 5. How CAD exposes tools to its agent (tool-registration seam for new firmware tools)

- `services/mcp/firmware_tools.py` — `register_firmware_tools(mcp)`: thin `@mcp.tool()` FastMCP
  wrappers, each JSON-dumps the result of `core_bridge.call(<rpc_method>, params)`
  (`services/mcp/core_bridge.py` bridges to the C++ core). Currently registers: `fw_status`,
  `fw_new_project`, `fw_build`, `fw_sources`, `fw_sim_run`, `fw_verify_board`, `fw_power_profile`,
  `fw_flash`, `fw_debug_start`, `fw_debug_halt`, (+ `fw_debug_read_mem`, `fw_sensor_scenario` per
  `firmware_rpc.md` §8/§9, file continues past line 300 — not fully re-read but referenced
  throughout `agent_turn.py`/`reviewer_exec.py`). **New MCU-debug/program/comms tools slot in here**
  as additional `@mcp.tool()` functions in this same file, following the exact
  `params → cb.call(rpc_method, params) → _dumps(result)` pattern — zero new plumbing needed.
- `services/mcp/server.py` — append-only registration point; firmware tools are wired in via
  `register_firmware_tools(mcp)` (confirmed call site: `services/agent/reviewer_exec.py:202-203`).
- **No `tool_registry.py`/`tool_schemas.py` exist under `services/agent/`** in the form the task
  description assumed (`services/agent/tool_schemas.py` exists but wasn't the primary
  registration mechanism found — CAD's actual pattern is the MCP `register_<family>_tools(mcp)`
  convention across `services/mcp/*.py`, not a hand-rolled schema/registry file). Grep for
  `tool_registry.py` under `services/agent` returned nothing.
- Agent role/permission wiring for firmware: `services/agent/roles.py:81-88` (`"fw"`/`"firmware"`
  role tool allowlists), `services/agent/agent_turn.py:2279-2286,6571-6665` (bootstrap-opener +
  mega-rule gating: `fw_new_project` must be followed by `fw_build` to count as product success),
  `services/agent/prompts/firmware.md`, and
  `services/agent/roles/firmware_engineer.yaml` (dedicated agent role/system prompt).
  The mission-specific AC7/AC8 declaration module mentioned in the original inventory
  had no production importer and was removed; generic grading stays in `reviewer_exec.py`.
- **No firmware-specific trigger/magic-keyword entries** exist in
  `services/agent/advisor/triggers.yaml` (grep returned zero hits for
  firmware/fw_/embedded/mcu/debug) — this is a genuine gap: the "magic keywords" ask (slash
  commands / auto-triggers for firmware work) has no wiring yet, unlike whatever
  triggers.yaml does for other domains.
- **No external third-party MCP-server client** was found wired into CAD's own agent (CAD's MCP
  server is exposed *outward* via `services/mcp/server.py`, i.e. CAD IS an MCP server, not an
  MCP client of other servers) — connecting to e.g. a community OpenOCD-MCP or a hardware-vendor
  MCP server would be new integration work, separate from the OMP side.

## What exists vs. greenfield (summary)

| Capability | Status |
|---|---|
| Project scaffold / build (CMake+arm-none-eabi, ESP-IDF, arduino-cli) | **EXISTS**, mature |
| Emulation (Renode primary, QEMU fallback) w/ UART+GPIO+sensor scenarios | **EXISTS**, mature |
| Flash (probe-rs, esptool) | **EXISTS** |
| High-level debug (reset/halt/read-mem via probe-rs) | **EXISTS**, shallow (no breakpoints/step/registers/disasm) |
| GDB / GDB-remote-serial-protocol integration | **GREENFIELD** |
| OpenOCD integration | **GREENFIELD** |
| Raw JTAG/SWD driver | **GREENFIELD** (rely on probe-rs/openocd instead) |
| AVR toolchain (avr-gcc) + avrdude flashing | **GREENFIELD** (AVR is modeled in pinouts only) |
| Direct serial/USB device comms in-process | **GREENFIELD** (currently delegated wholesale to subprocess CLIs) |
| GUI workspace (build console, UART console, GPIO trace, flash/debug rail) | **EXISTS**, extensible |
| ECAD↔firmware co-verification, power profiling | **EXISTS**, mature, novel |
| MCP tool registration seam for new tools | **EXISTS**, trivial to extend (`services/mcp/firmware_tools.py`) |
| Agent role/prompt and generic grading wiring for firmware | **EXISTS** (`roles.py`, `firmware_engineer.yaml`, `reviewer_exec.py`) |
| Magic-keyword/slash-command triggers for firmware | **GREENFIELD** (`triggers.yaml` has none) |
| External MCP-server client (consuming 3rd-party hardware MCP servers) | **GREENFIELD** |
| Host process-spawn seam for a new debugger/programmer process | **EXISTS AND REUSABLE** (`core/firmware/subprocess.{h,cpp}`) |
</content>
