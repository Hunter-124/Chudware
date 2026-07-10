ECAD — schematic capture, simulation, PCB layout, fab + 3D/physics handoff

Tools: ecad_new_schematic / ecad_new_pcb / ecad_find_part / ecad_place_symbol / ecad_wire /
       ecad_add_power / ecad_label / ecad_status / ecad_run_erc / ecad_simulate / ecad_sweep /
       ecad_tf / ecad_update_from_sch / ecad_make_board / ecad_autoroute / ecad_add_zone /
       ecad_fill_zones / ecad_run_drc / ecad_run_dfm / ecad_check_impedance /
       ecad_check_diff_impedance / ecad_export / ecad_to_3d / ecad_design / describe_board

WORKFLOW (schematic -> verified board -> physics handoff):
  1. ecad_new_schematic(name)          Start; ecad_find_part('resistor') gives real libids.
  2. ecad_place_symbol / ecad_wire / ecad_add_power / ecad_label
                                       Build the circuit. ecad_status() after every few ops —
                                       it names unconnected pins and the single best next op.
  3. ecad_run_erc / ecad_simulate('op'|'dc'|'tran'|'ac')
                                       Verify electrically before layout.
  4. ecad_make_board(...)              One-shot sync -> place -> route -> fill -> DRC.
  5. ecad_export('gerber'|'drill'|'bom'|'placement')   Fab outputs.
  6. ecad_to_3d()                      STEP of substrate + COMPONENT BODIES + the
                                       aicad.boardphys/1 sidecar (see below).

ENGINEERING METADATA (T-FIN8; contract: contracts/ecad/boardphys.md):
  The bundled library carries per-part engineering seeds — power_w (typ/max),
  theta_ja/theta_jc, Tj/Ta limits, package body dims, supply rails (voltage min/nom/max,
  current active/sleep), voltage domains, sequencing requirements — each with a datasheet
  source note.  Resolution: footprint eng < symbol eng < per-instance override.
  Per-instance override: place/update a symbol with sim_params={"eng": {...}} (partial dict,
  e.g. {"power_w": {"typ": 0.02}}) — it flows to the board on ecad_update_from_sch.
  HONESTY RULE: unknown metadata is null, never 0. sequencing [] = known none; null = unknown.

  ecad_status()/describe_board()       Per-component power_w + board "power" totals
                                       (n_missing_power tells you the coverage gap).
  ecad_to_3d()                         Writes <board>.step + <board>.step.boardphys.json:
                                       component -> solid label, body dims, power, theta,
                                       limits, rails + board totals. This sidecar is THE seam
                                       for thermal handoff, PDN checks and power budgets.

BIAS: fix ERC errors before layout; fix DRC errors before export; never invent power/thermal
  numbers — read them from ecad_status/boardphys, and add overrides with a source note.
