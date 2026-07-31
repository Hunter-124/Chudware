# ECAD schematic + PCB context pack
# Tools: ecad_new_schematic, ecad_new_pcb, ecad_place_symbol, ecad_wire,
#        ecad_add_power, ecad_autoroute, ecad_fill_zones, ecad_update_from_sch,
#        ecad_set_net_class, ecad_run_erc, ecad_run_drc, board_from_schematic, ecad_simulate

## Forced order
1. `ecad_new_schematic` → `ecad_find_part` + `ecad_place_symbol` → `ecad_wire` +
   `ecad_add_power` (nets required before SPICE).
2. `ecad_run_erc` — fix every error before board creation.
3. `board_from_schematic` → place footprints → `ecad_autoroute` or manual route →
   `ecad_fill_zones` → `ecad_run_drc`.
4. Impedance-critical nets: `ecad_set_net_class` + `check_impedance` before routing-density decisions.
5. Schematic+place alone is NOT done — board + DRC evidence required (or blocked handoff).

## Integrity
- Never invent pin maps; pull from part library.
- Power nets labeled; unconnected pins are ERC blockers until justified.
- After schematic edits, run `ecad_update_from_sch` before `ecad_run_drc`.
