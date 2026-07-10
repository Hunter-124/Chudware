---
id: T-FIN9
title: MCU host-board rule pack - decoupling, power sequencing, crystal, boot-strap checks
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN9-ecad-host-board-rules
depends_on: [T-FIN8]
lane: ecad-firmware
kind: new
priority: 2
---

## Context

First MCU/FPGA-specific verification layer on top of generic ERC/DRC. Rules:
per-power-pin decoupling coverage (cap of suitable value within distance, via quality),
bulk capacitance per rail, power-supply sequencing graph validated against component
sequencing requirements (enable/PGOOD chains), crystal/oscillator layout (load caps
present+matched, loop area / keepout heuristic), reset line pull + cap, boot-strap pins at
valid levels and not shared with active signals. Implement in the Python engine first with
C++ parity where the existing DRC parity pattern demands it; findings use the standard
violation shape with rule ids + fix hints.

## Files / areas touched

- engine ecad rules module (new hostboard_rules.py) + optional core/ecad parity
- services/mcp/ecad_tools.py — ecad_run_hostcheck (or fold into ecad_run_drc rule groups)
- seeded-violation test fixtures per rule family
- cad_guide('ecad') — rule-pack documentation

## Definition of done

- Each rule family has a seeded-violation fixture that trips it and a clean fixture that passes
- Sequencing check consumes T-FIN8 metadata (no hardcoded part knowledge)
- Findings carry actionable fix hints; rule ids stable for suppression
- Runs inside ecad_make_board verdict pipeline as an opt-in group
