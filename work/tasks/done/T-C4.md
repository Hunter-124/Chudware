---
id: T-C4
title: Hardware path - probe-rs flash/debug and clangd LSP tools
workstream: C
owner: sonnet-orin
status: done
branch: task/T-C4-hardware-path
day: 4
depends_on: [T-C3]
lane: ecad-firmware
kind: new
---

## Context

Day 4 adds the hardware path: probe-rs (MIT/Apache-2.0) for DAP flash and debug, esptool subprocess for ESP32, and clangd (subprocess LSP `--query-driver='**/arm-none-eabi-*'`) for a code panel in the app. The `fw_flash` and `fw_debug_*` MCP tools enable agent-driven hardware verification when a probe is attached. clangd provides completions/diagnostics driven by `compile_commands.json` generated in T-C2.

## Files / areas touched

- `engine_oracle/firmware/hardware.py` — NEW: probe-rs and esptool subprocess wrappers
- `engine_oracle/firmware/lsp.py` — NEW: clangd subprocess launcher
- `engine_oracle/firmware/mcp_tools.py` — added fw_flash, fw_debug_start, fw_debug_halt, fw_debug_read_mem handlers
- `services/mcp/firmware_tools.py` — 4 new @mcp.tool() wrappers registered inside register_firmware_tools
- `contracts/licenses.md` — 3 new ledger rows: probe-rs, esptool, clangd
- (server.py NOT touched — new tools registered inside existing register_firmware_tools, per merge hot-spot guidance)

## Definition of done

- [x] `ProbeRsFlasher.flash()` calls probe-rs subprocess with correct args; graceful error if not in PATH
- [x] `EsptoolFlasher.flash()` calls esptool subprocess; graceful error if not in PATH
- [x] `ClangdManager.start()` launches clangd with `--query-driver` for arm-none-eabi
- [x] `fw_flash`, `fw_debug_start`, `fw_debug_halt`, `fw_debug_read_mem` MCP tools registered
- [x] `python -m pytest engine_oracle/firmware/test_hardware.py -q` passes (mock subprocess) — 31/31 green
- [~] Live flash (probe-rs): `probe-rs download --chip STM32F407VGTx <path/to/blink.elf>` — requires DAP probe (CMSIS-DAP or J-Link) connected; verify chip name with `probe-rs chip list | grep STM32`
- [~] Live flash (esptool): `esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash 0x0 <path/to/firmware.bin>` — requires ESP32 in bootloader mode (GPIO0 LOW at reset)
- [~] Live debug halt/read: `probe-rs reset --chip STM32F407VGTx --halt` then `probe-rs read --chip STM32F407VGTx 0x20000000 16` — requires DAP probe
- [~] Live clangd: `clangd --compile-commands-dir=<project>/build --query-driver='**/arm-none-eabi-*'` — clangd found at `/usr/bin/clangd`; needs `compile_commands.json` from a fw_build run

## Gate status

- check.py: pre-existing FAIL (feature_surface_audit, parity_audit) — not from T-C4
- core CMake/Ninja: PASS
- ctest: PASS
- pytest engine/doc services/agent: pre-existing FAIL (OCP module not installed in venv — `ModuleNotFoundError: No module named 'OCP'`) — not from T-C4; present on master before this branch
- ui/imgui: PASS
- firmware test suite: 118/118 passed (87 baseline C1-C3 + 31 new T-C4)

## Design notes

- `server.py` was NOT edited — the 4 new tools are appended inside `firmware_tools.py::register_firmware_tools()`, following the merge hot-spot guidance (minimal server.py footprint).
- `fw_debug_start`/`fw_debug_halt` are stateless one-shot subcommands (`probe-rs reset [--halt]`), not persistent debug sessions — testable, posture-consistent, no module-global Popen.
- `fw_debug_read_mem` returns `{success, data_hex, ...}` (hex-encoded bytes) because MCP tools return JSON strings; `ProbeRsDebugger.read_memory` returns raw `bytes` for direct Python callers.
- clangd has NO MCP tool surface — it serves the code panel only. `ClangdManager` is in lsp.py; `get_diagnostics` does the full LSP handshake (initialize → didOpen → publishDiagnostics). Tests mock Popen and assert `--query-driver` arg is present.
- esptool is GPL-2.0+ → subprocess-only enforced (same isolation as QEMU). licenses.md row added.
