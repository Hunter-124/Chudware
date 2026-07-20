# ECAD schematic + PCB context pack
# Tools: ecad_new_schematic, ecad_new_pcb, place_symbol/ecad_place_symbol, wire/ecad_wire,
#        ecad_add_power, autoroute/ecad_autoroute, fill_zones, update_from_sch, set_net_class,
#        run_erc/ecad_run_erc, run_drc/ecad_run_drc, board_from_schematic, ecad_simulate

## Forced order
1. ecad_new_schematic → place symbols (ecad_find_part + place) → wire + add_power (nets REQUIRED before SPICE).
2. run_erc — fix every error before board.
3. board_from_schematic → place footprints → autoroute/manual → fill_zones → run_drc.
4. Impedance-critical nets: set_net_class + check_impedance before routing density decisions.
5. Schematic+place alone is NOT done — board + DRC evidence required (or blocked handoff).

## Integrity
- Never invent pin maps; pull from part library.
- Power nets labeled; unconnected pins are ERC blockers until justified.
- After schematic edits: update_from_sch before DRC.
