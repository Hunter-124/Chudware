---
id: T-FIN11
title: FPGA support pack - I/O bank validation and constraint export (XDC/LPF/CSV)
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN11-ecad-fpga-pack
depends_on: [T-FIN8]
lane: ecad-firmware
kind: new
priority: 3
---

## Context

Boards hosting FPGAs need bank-aware checks the generic flow can't do. Add an FPGA
part model (banks -> pins, VCCIO domains, clock-capable pins, diff-capable pairs) seeded
for 1-2 representative families (e.g. Lattice iCE40/ECP5 — open toolchain docs, OSS-first;
one Xilinx 7-series example). Checks: all pins of a bank on nets compatible with one VCCIO,
diff pairs on true pair pins, clocks on clock-capable pins, config/JTAG pins wired per
datasheet. Export: pin constraint files generated from the placed netlist (XDC for Xilinx,
LPF for Lattice, plus neutral CSV pinout) so the PCB is the source of truth for the HDL
flow.

## Files / areas touched

- engine ecad fpga.py — bank/pin model + checks + exporters
- component lib: seeded FPGA parts with bank maps
- services/mcp/ecad_tools.py — ecad_fpga_check + ecad_export(kind='constraints')
- tests: bank-violation fixture, golden XDC/LPF snapshots

## Definition of done

- Bank VCCIO conflict + non-clock-pin clock seeded fixtures flagged
- XDC/LPF/CSV exports match golden files for the demo board
- Model schema documented so more FPGA families are data additions, not code
- cad_guide('ecad') documents the FPGA flow
