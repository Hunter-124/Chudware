---
id: T-C3
title: Emulation loop via Renode with fw_sim_run MCP tool
workstream: C
owner: sonnet-rill
status: doing
branch: task/T-C3-renode-emulation
day: 3
depends_on: [T-C2]
---

## Context

The emulation loop is the agent's end condition for firmware: generate → build → emulate → assert → iterate. Renode (MIT license, bundleable) boots the ELF on a platform description, captures UART output, and checks GPIO state via Robot Framework keywords. The `fw_sim_run` MCP tool runs this loop and returns pass/fail with logs. QEMU (generic Cortex-M) is the fallback for boards without a Renode platform file.

## Files / areas touched

- `engine_oracle/firmware/emulator.py` — new: Renode and QEMU subprocess wrappers
- `engine_oracle/firmware/platform_db.py` — new: MCU → Renode platform file lookup
- `engine_oracle/firmware/mcp_tools.py` — add fw_sim_run handler (extends T-C2)
- `services/mcp/server.py` — register fw_sim_run
- `engine_oracle/firmware/templates/renode_platforms/` — Renode .resc templates for common MCUs

## Steps

1. Run `graphify query "subprocess emulator firmware Renode QEMU"` to check if any prior emulation code exists.
2. Create `platform_db.py`: dict mapping `mcu_family` → Renode platform file path; include STM32F4 (`stm32f4_discovery.repl`), ESP32 stub, generic Cortex-M fallback.
3. Bundle minimal Renode platform files under `engine_oracle/firmware/templates/renode_platforms/` (from Renode's open repo, MIT).
4. Implement `emulator.py`:
   - `RenodeEmulator.run(elf_path, platform_file, assertions: list[str], timeout_s=30) -> SimResult`: launch Renode via subprocess; feed `.resc` script; capture UART via telnet port; check each assertion (UART line match or GPIO state); return `{passed, uart_log, gpio_states, assertion_results}`.
   - `QEMUEmulator.run(elf_path, timeout_s=30) -> SimResult`: QEMU fallback (qemu-system-arm -M mps2-an385); capture semihosting output.
5. Add `fw_sim_run(project_id, assertions: list[str]) -> dict` handler to `mcp_tools.py`; register in `server.py`.
6. Test: mock Renode subprocess; assert SimResult.passed=True when UART output matches assertion; SimResult.passed=False when it doesn't.

## Definition of done

- [ ] `RenodeEmulator.run()` launches Renode subprocess, captures UART, evaluates assertions
- [ ] `QEMUEmulator.run()` fallback works for generic Cortex-M
- [ ] `fw_sim_run` MCP tool registered; returns SimResult dict with `passed` bool
- [ ] Renode platform files for STM32F4 and generic Cortex-M bundled under templates/
- [ ] `python -m pytest engine_oracle/firmware/test_emulator.py -q` passes (mock subprocess)

## AI hints

- `graphify query "subprocess timeout capture stdout"` — find subprocess utility patterns used elsewhere in repo
- Renode Robot keywords: `Execute Command`, `Wait For Line On Uart`, `Should Be Equal` — these can be scripted via Renode's telnet control port (port 1234 by default)
- Relevant files: `engine_oracle/firmware/toolchains.py`, `engine_oracle/firmware/mcp_tools.py`, `services/mcp/server.py`
- Renode subprocess: `renode --disable-xwt --port 1234 script.resc`; control via telnet `localhost:1234`; UART via separate telnet port specified in `.repl` platform file
- Bundle Renode as an optional dependency: check for `renode` in PATH; fall back to QEMU; fail gracefully with a clear error if neither is found
