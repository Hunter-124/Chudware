---
id: T-C3
title: Emulation loop via Renode with fw_sim_run MCP tool
workstream: C
owner: sonnet-dray
status: done
branch: task/T-C3-renode-emulation
day: 3
depends_on: [T-C2]
lane: ecad-firmware
kind: new
---

## Context

The emulation loop is the agent's end condition for firmware: generate → build → emulate → assert → iterate. Renode (MIT license, bundleable) boots the ELF on a platform description, captures UART output, and checks GPIO state via Robot Framework keywords. The `fw_sim_run` MCP tool runs this loop and returns pass/fail with logs. QEMU (generic Cortex-M) is the fallback for boards without a Renode platform file.

## Files / areas touched

- `engine_oracle/firmware/emulator.py` — new: Renode and QEMU subprocess wrappers
- `engine_oracle/firmware/platform_db.py` — new: MCU → Renode platform file lookup
- `engine_oracle/firmware/mcp_tools.py` — add fw_sim_run handler (extends T-C2)
- `services/mcp/server.py` — register fw_sim_run
- `engine_oracle/firmware/templates/renode_platforms/` — Renode .repl platform files + MIT LICENSE

## Steps

1. Run `graphify query "subprocess emulator firmware Renode QEMU"` to check if any prior emulation code exists.
2. Create `platform_db.py`: dict mapping `mcu_family` → Renode platform file path; include STM32F4 (`stm32f4_discovery.repl`), ESP32 stub, generic Cortex-M fallback.
3. Bundle minimal Renode platform files under `engine_oracle/firmware/templates/renode_platforms/` (from Renode's open repo, MIT).
4. Implement `emulator.py`:
   - `RenodeEmulator.run(elf_path, platform_file, assertions: list[str], timeout_s=30) -> SimResult`: launch Renode via subprocess; feed `.resc` script; capture UART via `--console` stdout; check each assertion (UART line match or GPIO state); return `{passed, uart_log, gpio_states, assertion_results}`.
   - `QEMUEmulator.run(elf_path, timeout_s=30) -> SimResult`: QEMU fallback (qemu-system-arm -M mps2-an385); capture semihosting output.
5. Add `fw_sim_run(project_id, assertions: list[str]) -> dict` handler to `mcp_tools.py`; register in `server.py`.
6. Test: mock Renode subprocess; assert SimResult.passed=True when UART output matches assertion; SimResult.passed=False when it doesn't.

## Definition of done

- [x] `RenodeEmulator.run()` launches Renode subprocess, captures UART, evaluates assertions
- [x] `QEMUEmulator.run()` fallback works for generic Cortex-M
- [x] `fw_sim_run` MCP tool registered; returns SimResult dict with `passed` bool
- [x] Renode platform files for STM32F4 and generic Cortex-M bundled under templates/
- [x] `python -m pytest engine_oracle/firmware/ -q` passes: **87 passed** (30 emulator + 7 fw_sim_run handler + 50 C1+C2 baseline)
- [~] Live Renode emulation: requires `renode` on PATH. Run `renode --disable-xwt --console <script.resc>`. UART capture works for platform files that define a UART peripheral (e.g. `stm32f4_discovery.repl`); `cortex_m_generic.repl` has no UART peripheral so stdout will be empty in live mode. See `emulator.py::_build_resc` docstring for details.

## Implementation notes (sonnet-dray finisher)

**Salvaged from sonnet-rill (process exit):** All 9 dirty/untracked files were sound and
kept. Salvage commit: 6b2dd40.

**Finisher additions (second commit):**
1. Added 7 `fw_sim_run` handler tests in `test_mcp_tools.py` covering:
   - missing `project_id` → error envelope
   - unknown project → error envelope
   - no ELF in build dir → graceful `SimResult(passed=False, uart_log="no_elf: ...")`
   - emulator absent → graceful `SimResult(passed=False, uart_log="emulator_not_found: ...")`
   - happy path (mocked `run_simulation`) → `SimResult(passed=True)` with full shape
   - failing assertions → `SimResult(passed=False)` with per-assertion detail
   - contract §4 field completeness check (all 5 keys present)
2. Moved top-level imports in `mcp_tools.py` (`Path`, `SimResult`, `run_simulation`) out
   of lazy-inline position for clean patchability.
3. Fixed `_build_resc` in `emulator.py`: removed the PTY UART routing that contradicted
   the `--console` stdout capture approach. Added honest live-path note about UART capture
   fidelity depending on platform file having a UART peripheral defined.

**Contract note (Q1):** `assertions?:[str]` is an additive extension beyond contract §5's
`{project_id, elf_path?, scenario?}`. Recorded in `work/questions/T-C3.md`. `scenario`
param is accepted but currently unused (reserved for future scripted test scenarios).

**Gate:** scripts/gate.sh — pre-existing reds only (check.py audit, pytest 0-collected gate-venv
gap per issue #6). Core/ctest/ui-abstract/ui-imgui stages pass.
