---
id: T-FIN4
title: Firmware toolchain/emulator provisioning doctor - fw_status probe with install guidance
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN4-fw-provisioning-doctor
depends_on: []
lane: ecad-firmware
kind: new
priority: 3
---

## Context

Toolchains and emulators (cmake, arm-none-eabi-gcc, renode, qemu-system-arm, probe-rs,
esptool, ninja) are assumed installed; failures surface as returncode=-1 strings. Add an
fw_status MCP probe mirroring em_status/reveng_status: detect each tool, report version +
path + which capabilities it unlocks (build/emulate/flash per family), and emit actionable
install guidance wired into the existing provisioning framework used by RE/sim (RE-06
pattern). Gate-friendly: never fails the suite, just reports.

## Files / areas touched

- engine_oracle/firmware/provision.py — detection + capability mapping
- services/mcp/firmware_tools.py — fw_status tool
- provisioning registry entries (follow RE-06 / T-V4 pattern)
- engine_oracle/firmware/test_mcp_tools.py — fw_status shape tests

## Definition of done

- fw_status reports per-tool found/version/missing with install hints on this machine
- Capability rollup: for each MCU family, can-build/can-emulate/can-flash booleans
- cad_guide('firmware') references fw_status as the first-call diagnostic
