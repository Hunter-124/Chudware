---
id: T-FIN1
title: Firmware emulation GPIO state extraction - real pin-level assertions from Renode/QEMU
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN1-fw-gpio-extraction
depends_on: []
lane: ecad-firmware
kind: fix
priority: 2
---

## Context

`fw_sim_run` accepts `gpio:<PIN>=<level>` assertions but `gpio_states` is always an
empty dict (engine_oracle/firmware/emulator.py ~L255 "future") — pin assertions can never
pass. Close the loop: drive Renode's GPIO monitoring (machine-readable output via the .resc
script / Renode's gpio peripheral hooks) into a parsed `gpio_states` timeline, and add a
semihosting-based shim for the QEMU fallback (firmware-side macro writes pin events to the
semihosting console in a parseable format). Emit a structured trace artifact
(time-stamped pin/uart events, JSON) alongside SimResult so later GUI/scenario work has a
stable seam.

## Files / areas touched

- engine_oracle/firmware/emulator.py — RenodeEmulator/QEMUEmulator .resc/.rep wiring, trace parser
- engine_oracle/firmware/mcp_tools.py + services/mcp/firmware_tools.py — surface trace artifact
- contracts/firmware_rpc.md — append trace schema (append-only contract)
- engine_oracle/firmware/test_emulator.py — pin-assertion tests with canned Renode output

## Definition of done

- `gpio:<PIN>=<level>` assertions pass/fail against real emulator output on stm32f4_discovery
- Final-state AND timeline forms available (`gpio:PA5=1@>10ms` style edge/time asserts optional stretch)
- Structured JSON trace artifact documented in contracts/firmware_rpc.md
- Tests cover Renode parse, QEMU semihosting parse, and assertion evaluation
