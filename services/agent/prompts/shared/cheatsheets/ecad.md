ECAD — schematic capture, SPICE, PCB layout, controlled impedance

WORKFLOW (schematic -> board -> fab):
  ecad_new_schematic('amp')          start a schematic (ecad_status() = THE context tool)
  ecad_find_part('opamp')            ranked real libids -> ecad_place_symbol(libid, x, y)
  ecad_wire('U1','1','R1','2')       pin-to-pin; ecad_add_power('GND')/ecad_label(...)
  ecad_run_erc() / ecad_simulate()   check + SPICE (op/dc/tran/ac, four/mc/temp/noise)
  ecad_make_board('board1')          ONE SHOT: sync -> route -> fill -> DRC; sets current board
  ecad_autoroute() / ecad_run_drc()  route + rule check (widths/clearances from NET CLASSES)
  ecad_export('gerber') / ecad_to_3d()   fab outputs / STEP bridge

CONTROLLED IMPEDANCE (stackup advisor + checkers):
  ecad_stackup_solve(copper_layers, targets, profile, net_map)
      Inverts the validated IPC-2141A models (microstrip outer / stripline inner) over a
      canned FAB PROFILE (real dielectric menus: 'jlc_4layer_7628' — validated against
      JLCPCB's published JLC04161H-7628 stackup — 'jlc_6layer_3313', 'generic_2layer',
      or 'auto'). Returns layer order + prepreg/core heights + reference planes AND the
      trace width / diff-pair gap per target; with an active board it REPLACES the board
      stackup and writes each target as a net class (track_width, diff_gap, via dims) —
      exactly what ecad_autoroute and ecad_run_drc read. Infeasible targets fail honestly
      naming the binding constraint (min_trace / max_trace / min_space).
  ecad_check_impedance(net, target)          read back a routed trace's Z0 vs target
  ecad_check_diff_impedance(net_p, net_n)    differential pair Z_diff + intra-pair skew

WORKED EXAMPLE — 4-layer USB board with 50R single-ended + 90R differential:
    ecad_new_pcb('usbboard')
    ecad_stackup_solve(copper_layers=4,
                       targets={'sig50': 50,
                                'usb90': {'type': 'diff', 'z0': 90}},
                       profile='jlc_4layer_7628',
                       net_map={'usb90': ['USB_DP', 'USB_DN']})
      -> stackup: F.Cu 0.035 / 7628 prepreg 0.2104 (er 4.4) / GND plane / core 1.065 /
         PWR plane / 7628 prepreg / B.Cu   (JLC04161H-7628, 1.6 mm)
      -> sig50: w=0.350 mm microstrip on F.Cu;  usb90: w=0.260 mm, gap=0.150 mm
      -> net classes 'sig50'/'usb90' written; USB_DP/USB_DN assigned to 'usb90'
    ecad_autoroute()          # routes those nets at the solved width/gap
    ecad_check_impedance('USB_DP', target=45)   # per-line odd-mode sanity read
    ecad_run_drc()

NOTES:
  * Net classes are the seam: autoroute reads track_width/diff_gap/via_* and DRC reads
    clearance from board net_classes — anything that writes a class steers both.
  * The impedance closed forms are quasi-static IPC-2141A (+/-5% typical FR-4); fab
    field solvers including solder mask can differ a few percent — order controlled
    impedance from the fab's coupon service for critical interfaces.
  * No board open? ecad_stackup_solve(apply=False) still returns the full solve
    (design-ahead mode) so you can plan the stackup before layout starts.
