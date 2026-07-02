---
id: T-C4
title: Hardware path - probe-rs flash/debug and clangd LSP tools
workstream: C
owner: unassigned
status: todo
branch: task/T-C4-hardware-path
day: 4
depends_on: [T-C3]
---

## Context

Day 4 adds the hardware path: probe-rs (MIT, bundleable) for DAP flash and debug, esptool subprocess for ESP32, and clangd (subprocess LSP `--query-driver='**/arm-none-eabi-*'`) for a code panel in the app. The `fw_flash` and `fw_debug_*` MCP tools enable agent-driven hardware verification when a probe is attached. clangd provides completions/diagnostics driven by `compile_commands.json` generated in T-C2.

## Files / areas touched

- `engine_oracle/firmware/hardware.py` — new: probe-rs and esptool subprocess wrappers
- `engine_oracle/firmware/lsp.py` — new: clangd subprocess launcher
- `engine_oracle/firmware/mcp_tools.py` — add fw_flash, fw_debug_start, fw_debug_halt, fw_debug_read_mem
- `services/mcp/server.py` — register hardware and LSP tools

## Steps

1. Implement `hardware.py`:
   - `ProbeRsFlasher.flash(elf_path, chip: str) -> FlashResult`: `probe-rs download --chip <chip> <elf>` subprocess; return `{success, output}`.
   - `ProbeRsDebugger.read_memory(address, length, chip) -> bytes`: `probe-rs read --chip <chip> <address> <length>`.
   - `EsptoolFlasher.flash(bin_path, port, baud=460800) -> FlashResult`: `esptool.py --port <port> write_flash ...` subprocess.
   - Graceful error if neither probe-rs nor esptool is in PATH.
2. Implement `lsp.py`:
   - `ClangdManager.start(project_dir) -> subprocess.Popen`: launch `clangd --compile-commands-dir=<build_dir> --query-driver='**/arm-none-eabi-*'`; expose stdin/stdout for JSON-RPC.
   - `ClangdManager.get_diagnostics(file_path) -> list[dict]`: send LSP textDocument/diagnostic request; parse response.
3. Add MCP tool handlers: `fw_flash`, `fw_debug_start`, `fw_debug_halt`, `fw_debug_read_mem`; register in `server.py`.
4. Test: mock probe-rs subprocess; assert `FlashResult.success=True` on zero exit code; `FlashResult.success=False` on nonzero.

## Definition of done

- [ ] `ProbeRsFlasher.flash()` calls probe-rs subprocess with correct args; graceful error if not in PATH
- [ ] `EsptoolFlasher.flash()` calls esptool subprocess; graceful error if not in PATH
- [ ] `ClangdManager.start()` launches clangd with `--query-driver` for arm-none-eabi
- [ ] `fw_flash`, `fw_debug_start`, `fw_debug_halt`, `fw_debug_read_mem` MCP tools registered
- [ ] `python -m pytest engine_oracle/firmware/test_hardware.py -q` passes (mock subprocess)

## AI hints

- `graphify query "subprocess PATH tool check graceful error"` — find existing pattern for optional-tool detection
- probe-rs CLI reference: `probe-rs --help`; chip names: `STM32F407VGTx`, `nRF52840_xxAA`, etc.
- clangd LSP JSON-RPC: initialize → textDocument/didOpen → textDocument/diagnostic; parse `method: "textDocument/publishDiagnostics"` notifications
- Relevant files: `engine_oracle/firmware/toolchains.py`, `engine_oracle/firmware/emulator.py`, `services/mcp/server.py`
