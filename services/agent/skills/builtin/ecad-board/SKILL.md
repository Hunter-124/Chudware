---
name: ecad-board
description: Schematic + PCB workflow — symbols from library first, net integrity, board outline from mechanical, ASCII place/route verify loop, then ECAD→3D and power/DFM checks before done.
inject: auto
triggers:
  keywords: [schematic, pcb, ecad, board outline, footprint, netlist, kicad, ascii board, route the board, autoroute, autoplace, ground plane, copper pour, silkscreen, solder mask]
  tools: [ecad_new_schematic, ecad_to_3d, render_board_ascii, render_schematic_ascii, render_sim_ascii, apply_ascii_edits, ecad_autoroute, ecad_autoplace]
  phases: [feature, deliver]
---

# ECAD Board

## Sequence

1. **Mechanical envelope first** when the board lives in a housing — measure keep-outs and
   connector heights from the CAD part (`measure_part` / mates). Do not invent board outline vs
   enclosure in parallel.
2. Schematic: start with library symbols/parts (`part_search` in ECAD domain / project libraries).
   Prefer standard passives and connectors.
3. Assign footprints; run ERC/net checks available on the ECAD tool surface before layout.
4. Board layout: outline from mechanical; place connectors at mating interfaces first
   (interface-first). Mutation tools (`ecad_update_from_sch`, `ecad_autoplace`, `ecad_autoroute`,
   `ecad_optimize_traces`, `board_from_schematic`, `ecad_add_zone`) **auto-append** a compact
   per-layer ASCII excerpt + NL `net_delta` + findings.
5. **ASCII verify loop** (place / route / via):
   - mutate (place / route / zone) → **READ the ascii_readback** (layer grid + net connectivity
     counts + crossings / courtyard overlaps / unconnected multi-pad nets)
   - fix placement or re-route → re-read
   - before declaring the board done: `render_board_ascii` (full) + `ecad_run_drc` / DFM
6. `ecad_to_3d` (or project equivalent) to merge into the mechanical assembly.
7. Validate: board-in-enclosure interference, connector mate alignment (`interface_check`),
   power budget if rails exist.
8. Done: `verify_battery` including any ECAD/power checks wired for the doc.

## Rules

- Verify-before-done — no "schematic complete" without ERC + footprint assignment check.
- Interface-first: connector and mounting-hole positions come from the mechanical mating part.
- Search phrasing for electronics: `USB-C receptacle 16pin`, `M3 standoff 6mm`, not "power jack thing".
- Always **read the ASCII readback** after place/route/via mutations before the next action.
- Text-only models: `net_delta` + findings carry enough NL (net pad lists, track/via counts,
  crossing/overlap messages) to layout without vision.

## Deeper docs

- `cad_guide('ecad')` when available; cheatsheet under agent prompts ECAD
- Explicit full ASCII: `render_board_ascii` / compact auto-readback on board mutation tools
- Mechanical mate workflow: `skill://edit-assembly`

- Schematic sheet ASCII: `render_schematic_ascii` (grid + cell index).
- SPICE/sim plaintext: `render_sim_ascii`.
- Targeted layout/schematic edits from the grid: `apply_ascii_edits(kind='pcb'|'schematic', edits=[...])` with `grid_json`/`index_json` from the last render (move/swap/delete/replace_section/place).
