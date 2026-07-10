ECAD — schematic capture, SPICE, PCB layout, controlled impedance, 3D/physics handoff

WORKFLOW (schematic -> board -> fab):
  ecad_new_schematic('amp')          start a schematic (ecad_status() = THE context tool)
  ecad_find_part('opamp')            ranked real libids -> ecad_place_symbol(libid, x, y)
  ecad_wire('U1','1','R1','2')       pin-to-pin; ecad_add_power('GND')/ecad_label(...)
  ecad_run_erc() / ecad_simulate()   check + SPICE (op/dc/tran/ac, four/mc/temp/noise)
  ecad_make_board('board1')          ONE SHOT: sync -> route -> fill -> DRC; sets current board
  ecad_autoroute() / ecad_run_drc()  route + rule check (widths/clearances from NET CLASSES)
  ecad_export('gerber') / ecad_to_3d()   fab outputs / STEP + boardphys sidecar (below)
  BIAS: fix ERC errors before layout; fix DRC errors before export.

SIMULATION BACKENDS (ecad_simulate / ecad_tf, param backend=):
  'mna' (DEFAULT)  validated internal Python MNA solver — deterministic, always
                   available, supports every analysis; built-in behavioural models
                   only (R/C/L/V/I/D/Q/M, ideal OPAMP, NE555, ...).
  'ngspice'        same circuit through an installed ngspice binary (subprocess).
                   Unlocks REAL vendor .SUBCKT/.MODEL libraries (op-amps, regulators).
                   Covers op/dc/tran/ac/tf; advanced wrappers (four/mc/temp/noise)
                   always run on MNA.
  'auto'           mna unless the circuit carries vendor .lib/.subckt attachments
                   AND ngspice is installed.
  HONEST DEGRADE: no ngspice (or a failed run) falls back to MNA and says so in
  backend_note; result.backend always names the solver that actually ran.
  Install: `dnf install ngspice` or point AI_CAD_NGSPICE at a binary; detection via
  the installer's --check (services/installer/provision.py: ngspice_provisioning_status).

VENDOR MODEL IMPORT: attach a SPICE library to a component via its sim dict:
    {"sim": {"model": "SUBCKT", "subckt": "UA741",
             "lib": "/path/to/ua741.lib", "pin_order": ["1","2","3",...]}}
  netlist_to_spice_circuit records an X instance + .include; only the ngspice
  backend simulates it (MNA warns and ignores). Parity gated in
  engine/sim/test_ngspice_backend.py (RC + divider fixtures match across backends).

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
  Never invent power/thermal numbers — read them from ecad_status/boardphys, and add
  overrides with a source note.

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

HOST-BOARD RULE PACK (T-FIN9) — MCU/FPGA device-level checks above DRC/ERC:
  ecad_run_hostcheck()               run on the active board; advisory, DRC-style output
                                     with stable rule ids + per-finding fix_hints
  ecad_make_board(hostcheck=True)    opt-in: fold the pack into the one-shot board flow
  Rule families (suppression-stable ids):
    host_decoupling_missing/_far/_via   per power_in pin: a 10 nF–1 uF cap on the pin's
                                        net to GND, within 3 mm, on the same board side
    host_bulk_cap                       each supply rail needs a bulk cap >= 4.7 uF
    host_sequencing[/_unparsed/_unverifiable]
                                        eng.supply.sequencing ('VCORE before VIO', from
                                        the T-FIN8 metadata layer / boardphys.md — never
                                        hardcoded part knowledge) verified against the
                                        board's real enable/PGOOD chains: the later
                                        rail's regulator EN must be gated by the earlier
                                        rail or its PGOOD output
    host_xtal_loadcap/_loadcap_match/_loop/_keepout
                                        crystal load caps present + matched, within 5 mm
                                        (loop area), no other-net track within 1 mm
    host_reset_pull / host_reset_cap    reset pin needs a pull to a rail + filter cap
    host_bootstrap_float/_shared        BOOT/MODE/CFG pins strapped to a valid level
                                        (direct or one resistor) and not shared with an
                                        actively-driven signal
  NOTES:
    * Hosts are found by metadata (eng.supply.rails) or power_in pins; seed sequencing
      requirements per instance via sim_params.eng = {'supply': {'sequencing':
      ['VCORE before VIO']}} on the schematic symbol (carried to the board by sync).
      sequencing [] = known none required; null = unknown (honest silence).
    * Thresholds tune per board: board rules 'hostcheck' dict (decoupling_dist_mm,
      decoupling_min_f/max_f, bulk_min_f, xtal_loop_dist_mm, xtal_keepout_mm).

HIGH-SPEED / SIGNAL-INTEGRITY RULE PACK (formula + heuristic — T-FIN10):
  ecad_set_net_class(name, params, nets)
      THE declaration path for SI rules. Merges keys ADDITIVELY into the same
      board net_classes entries ecad_stackup_solve writes (track_width/diff_gap/
      via dims), extended with:
        match_tolerance_mm   the class IS a length-match group (e.g. a DDR byte
                             lane): every assigned net's routed length must sit
                             within this of the group median
        pair_skew_mm         intra-pair skew limit; diff pairs inferred from
                             net-name suffixes (_P/_N, +/-, P/N)
        termination          {"scheme": "series"|"parallel"|"thevenin"|"none",
                              "value_ohm": 33, "max_dist_mm": 10, "driver_ref": "U1"}
        rise_time_ps         signal edge rate — arms the via-stub check
  ecad_run_sicheck()
      Runs the pack: si_match_length (per-net deltas vs group median),
      si_pair_skew, si_termination (series R near driver / parallel R to GND-VTT
      / Thevenin pair — presence + topology from resistor footprints),
      si_via_stub (round-trip stub delay > tr/3 → warn, suggests back-drill),
      si_bga_via_in_pad / si_bga_escape / si_bga_congestion (BGA fanout).
      Advisory like ecad_run_drc; returns violations by rule + a match_report
      table of every group member's length and delta.

WORKED EXAMPLE — verify a DDR-ish byte lane + a series-terminated clock:
    ecad_set_net_class('ddr_byte0',
                       params={'match_tolerance_mm': 0.5, 'pair_skew_mm': 0.1,
                               'rise_time_ps': 100},
                       nets=['DQ0','DQ1','DQ2','DQ3','DQS_P','DQS_N'])
    ecad_set_net_class('clk50',
                       params={'termination': {'scheme': 'series', 'value_ohm': 33,
                                               'max_dist_mm': 10, 'driver_ref': 'U1'}},
                       nets=['CLK'])
    ecad_run_sicheck()
      -> si_match_length: "net 'DQ3' in match group 'ddr_byte0' is -2.050mm vs the
         group median 30.000mm (tolerance ±0.500mm)" + match_report per-net table
      -> si_termination: "net 'CLK' requires a SERIES termination resistor near the
         driver but no 2-pin R joins it to a companion net"
    # lengthen DQ3 (autoroute meanders), place R near U1, re-run until SI-clean.

SI HONESTY (stated in every ecad_run_sicheck response):
  * Closed-form IPC-2141A + topology heuristics ONLY — no 2-D/3-D field solver,
    no IBIS/behavioural models, no crosstalk/return-path/eye analysis. A clean
    result means "no classic layout mistakes", not multi-gigabit sign-off.
  * Termination checks are presence/topology (a ref-R* 2-pad footprint in the
    right place), not a waveform simulation.
  * Via-stub math ignores back-drilling — a flagged stub may be fine if drilled.
