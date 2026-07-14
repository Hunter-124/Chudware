---
name: ecad-board
description: Schematic + PCB workflow — symbols from library first, net integrity, board outline from mechanical, then ECAD→3D and power/DFM checks before done.
inject: auto
triggers:
  keywords: [schematic, pcb, ecad, board outline, footprint, netlist, kicad]
  tools: [ecad_new_schematic, ecad_to_3d]
  phases: [feature, deliver]
---

# ECAD Board

## Sequence

1. **Mechanical envelope first** when the board lives in a housing — measure keep-outs and connector heights from the CAD part (`measure_part` / mates). Do not invent board outline vs enclosure in parallel.
2. Schematic: start with library symbols/parts (`part_search` in ECAD domain / project libraries). Prefer standard passives and connectors.
3. Assign footprints; run ERC/net checks available on the ECAD tool surface before layout.
4. Board layout: outline from mechanical; place connectors at mating interfaces first (interface-first).
5. `ecad_to_3d` (or project equivalent) to merge into the mechanical assembly.
6. Validate: board-in-enclosure interference, connector mate alignment (`interface_check`), power budget if rails exist.
7. Done: `verify_battery` including any ECAD/power checks wired for the doc.

## Rules

- Verify-before-done — no "schematic complete" without ERC + footprint assignment check.
- Interface-first: connector and mounting-hole positions come from the mechanical mating part.
- Search phrasing for electronics: `USB-C receptacle 16pin`, `M3 standoff 6mm`, not "power jack thing".

## Deeper docs

- `cad_guide('ecad')` when available; cheatsheet under agent prompts ECAD
- Mechanical mate workflow: `skill://edit-assembly`
