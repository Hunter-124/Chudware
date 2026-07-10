---
id: T-FIN6
title: Firmware<->board co-verification - pinmap round-trip and emulated pin activity vs netlist
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN6-fw-board-coverify
depends_on: [T-FIN1, T-FIN8]
lane: ecad-firmware
kind: new
priority: 3
---

## Context

pinmap.py already generates PIN_<NET> headers from the ECAD netlist (one-way). Close
the loop so the board and the firmware are checked against each other before fab: (a)
static checks — every firmware-referenced pin exists on the MCU footprint and net, pin
direction/type conflicts (driving an input-only pin, two drivers on a net), boot-strap /
reset pins not repurposed, required decoupling present per power pin (uses T-FIN8
metadata); (b) dynamic check — run the firmware in emulation and diff observed pin activity
(T-FIN1 trace) against netlist expectations (e.g. firmware toggles a pin wired to an input
peripheral). Expose as fw_verify_board returning a findings list in the standard
violations shape used by ERC/DRC.

## Files / areas touched

- engine_oracle/firmware/coverify.py — static + dynamic checks
- engine_oracle/firmware/pinmap.py — richer pin model (direction, mux, strap flags)
- services/mcp/firmware_tools.py — fw_verify_board tool
- tests with a seeded conflict board fixture

## Definition of done

- Seeded-violation fixture board yields the expected findings (strap conflict, direction clash, missing decouple)
- Clean demo board passes; findings shape matches ERC/DRC violation conventions
- Dynamic path runs a real emulation and cross-references the trace
- Documented in cad_guide('firmware') + contracts/firmware_rpc.md
