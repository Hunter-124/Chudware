ECAD — schematic capture, SPICE, PCB layout, controlled impedance, 3D/physics handoff

ROLE HANDOFF:
  ecad_engineer owns schematic capture, ERC/SPICE, netlist, BOM, and vector plot.
  It ledger_note's the verified sch_doc_id + artifacts for pcb_engineer. PCB layout,
  zones, DRC, Gerbers, and board antenna analysis belong to pcb_engineer.

WORKFLOW (schematic -> explicit handoff -> board -> fab):
  ecad_new_schematic('amp')          start a schematic (ecad_status() = THE context tool)
  ecad_find_part('opamp')            ranked real libids -> ecad_place_symbol(libid, x, y)
  ecad_wire('U1','1','R1','2')       pin-to-pin; ecad_add_power('GND')/ecad_label(...)
  ecad_run_erc(); enable_tools(categories=['ecad']); ecad_simulate()  ERC + SPICE
  board_from_schematic(sch_doc_id)   pcb_engineer consumes the verified handoff
  ecad_autoroute() / ecad_fill_zones() / ecad_run_drc()  route, pour, rule check
  ecad_export('gerber') / ecad_to_3d()   fab outputs / STEP + boardphys sidecar (below)
  BIAS: fix ERC errors before handoff; fix DRC errors before export.

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
  Install: `dnf install ngspice` or point CHUDWARE_NGSPICE at a binary; detection via
  the installer's --check (services/installer/provision.py: ngspice_provisioning_status).

VENDOR MODEL IMPORT: attach a SPICE library to a component via its sim dict:
    {"sim": {"model": "SUBCKT", "subckt": "UA741",
             "lib": "/path/to/ua741.lib", "pin_order": ["1","2","3",...]}}
  netlist_to_spice_circuit records an X instance + .include; only the ngspice
  backend simulates it (MNA warns and ignores).

ENGINEERING METADATA (contract: contracts/ecad/boardphys.md):
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

HOST-BOARD RULE PACK — MCU/FPGA device-level checks above DRC/ERC:
  ecad_run_hostcheck()               run on the active board; advisory, DRC-style output
                                     with stable rule ids + per-finding fix_hints
  ecad_make_board(hostcheck=True)    opt-in: fold the pack into the one-shot board flow
  Rule families (suppression-stable ids):
    host_decoupling_missing/_far/_via   per power_in pin: a 10 nF–1 uF cap on the pin's
                                        net to GND, within 3 mm, on the same board side
    host_bulk_cap                       each supply rail needs a bulk cap >= 4.7 uF
    host_sequencing[/_unparsed/_unverifiable]
                                        eng.supply.sequencing ('VCORE before VIO', from
                                        the engineering metadata / boardphys.md — never
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

HIGH-SPEED / SIGNAL-INTEGRITY RULE PACK (formula + heuristic):
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

PDN / IR-DROP (DC resistive solve of power-net copper):
  ecad_run_pdn()                     analyze every net with a detectable source
  ecad_run_pdn(net='+5V', source_refs='U3')   one net, explicit regulator/connector ref
  ecad_run_pdn(net='+5V', sink_overrides='{"U2": 0.35}', vsrc=5.0, resolution_mm=0.5)
  Meshes the net's pours/tracks/pads into a 2.5D resistor grid (sheet R from stackup
  copper thickness; vias as annular barrel resistors) and sparse-solves DC voltages.
  Sources = pads held at vsrc: auto-detected from power_out symbol pins, or source_refs.
  Sinks = per-component supply currents from the engineering rail metadata (supply.rails
  current_a_active); components WITHOUT current data are reported in
  unknown_current_refs — never silently modelled as 0 A (honesty rule).
  REPORTS: per-component drop vs the rail's voltage_v_min (within_tolerance),
  current-density hotspots vs j_limit_a_mm2 (default 30 A/mm2), open PDN paths.
  I2R HEAT MAP: the per-element dissipation field is written as the `pdn` block of the
  aicad.boardphys/1 sidecar (merged into board.step.boardphys.json when ecad_to_3d ran,
  else pdn.boardphys.json) — the trace-heating input for the thermal handoff.
  BIAS: run after routing + zone fill (unrouted copper = "not connected" violations);
  fix drops by widening copper / adding a pour / stitching vias, then re-run.

FPGA SUPPORT PACK (model schema aicad.fpga_device/1):
  Data-driven FPGA part model in services/ecad/fpga_devices/*.json — banks -> pins, VCCIO
  domains, clock-capable pins, TRUE differential-pair pins, config/JTAG pins. Adding a
  new family/package is a DATA addition (one JSON), not code. Shipped seeds:
    iCE40UP5K-SG48       FULL (icestorm + iCEBreaker sources); placeable starter symbol
                         'FPGA:iCE40UP5K-SG48' + QFN-48-1EP footprint, pins generated
                         FROM the JSON so symbol and checker can never drift.
    LFE5U-25F-CABGA256   partial (prjtrellis banks + true pairs; no clock/config data yet)
    XC7A35T-CPG236       partial (Cmod A7 XDC sites; MRCC/SRCC clock flags + pairs)
  Bind any other symbol to a device with fields={"fpga_device": "<device>"}.

  ecad_fpga_check([ref], [io])     Bank-aware checks on the schematic netlist:
      1. bank VCCIO — all IO of a bank compatible with ONE VCCIO (bank voltage inferred
         from the net on its VCCIO pins; per-net need from IOSTANDARD or a voltage-tagged
         net name like SENSOR_3V3); illegal VCCIO voltages flagged too.
      2. diff pairs — nets X_P/X_N (or X+/X-) must land on both halves of ONE true pair,
         correct polarity (P on 'p' pin).
      3. clocks — *CLK* nets must enter on clock-capable pins (GBIN/MRCC/SRCC).
      4. config/JTAG — required pins (e.g. iCE40 CRESET_B) actually wired.
      io={"NET": {"iostandard": "LVCMOS18"}} declares IO standards. HONESTY: partial
      seeds SKIP checks they lack data for (warning), never guess.
  ecad_export('constraints')       Pin-constraint files from the design netlist (the
      board is the source of truth for the HDL flow): native dialect per vendor — XDC
      (Xilinx) or LPF (Lattice) — plus a neutral CSV pinout (pin,net,port,site,bank,
      clock_capable,diff_pair,polarity,iostandard). One <ref>_pins.* set per FPGA,
      byte-deterministic.
  Typical flow: place FPGA symbol -> wire IO/power/config -> ecad_run_erc() ->
  ecad_fpga_check() -> fix -> ecad_export('constraints') -> feed XDC/LPF to Vivado/nextpnr.

THERMAL FEA HANDOFF (aicad.simhandoff/1 coupling ecad_to_thermal_fea):
  ecad_thermal_handoff([environment], [include_trace_heating], [transient], [dry_run])
      One call from the ACTIVE routed board to a solved thermal FEA study — zero
      manual BC setup. Pipeline: fused board+component FEA solid (substrate slab +
      flattened package/proxy bodies, drills omitted) -> stackup HOMOGENIZED to one
      effective material (in-plane rule-of-mixtures k ~10-25 W/mK; through-plane
      ~0.3 W/mK reported but not separately modeled; assumptions[] lists every
      simplification) -> one heat source PER component from boardphys power_w
      (typ else max) as top-face flux -> convection from the environment preset ->
      quasi-steady core transient solve -> per-component case temp + Tj map-back
      (Tj = Tcase + P*theta_jc when jc is known, honest null otherwise).
  ENVIRONMENTS: 'still_air' (h=10 W/m2K), 'forced_air' (h=50), 'cold_plate' (board
      bottom held at t_amb + still air on top), or custom h_w_m2k/t_amb_c. Default
      ambient 25 C.
  HONESTY: components WITHOUT power metadata get NO heat source and are named in
      coverage.unpowered_refs (never invented watts) — seed power_w via library eng
      blocks or sim_params.eng overrides. The steady answer is the final frame of a
      5-time-constant transient (the steady kernel aggregates flux magnitudes;
      per-component powers need the transient path).
  TRACE HEATING: include_trace_heating=True pulls the ecad_run_pdn I2R map in as
      distributed flux cells (hottest cells first, omitted remainder reported) —
      run ecad_run_pdn on the supply nets first.
  DUTY CYCLES: transient={"t_end_s":..., "schedules": {"U1": {"duty":0.25,
      "period_s":1.0}}} runs a true transient with per-component duty schedules.
  ecad_verify_thermal([handoff_result], [tolerance_pct=75])
      Per-component Tj-vs-tj_max margin table (components without limits listed
      UNCHECKED, never assumed OK) + theta_ja hand-calc cross-check on the
      temperature RISE (Tj-Ta vs P*theta_ja). Tolerance is GENEROUS by design:
      JEDEC theta_ja assumes a standard 2s2p board; the check catches setup
      blunders, not layout accuracy. Minor power contributors (<25% of board
      power) get advisories instead of pass/fail — their temperature is neighbor
      heating, invisible to theta_ja. Result carries the verify envelope
      (checks[], computed pass, verdict).
  Typical flow: ecad_make_board -> ecad_run_pdn('+5V') -> ecad_thermal_handoff(
      include_trace_heating=True) -> ecad_verify_thermal() -> widen copper / add
      vias / forced_air and re-run until margins clear.

VIBRATION RELIABILITY SCREEN (aicad.simhandoff/1 coupling ecad_to_structural_fea):
  ecad_vibe_check([band_lo_hz=20], [band_hi_hz=2000], [psd_breakpoints],
                  [mount_holes], [zeta=0.05], [dry_run])
      One call from the ACTIVE routed board to a mounted-board vibration screen —
      zero manual BC setup. Pipeline: the thermal handoff's fused board+components FEA solid
      (reused) with a MASS-CORRECTED effective density (component masses from
      package dims x a mold-compound density assumption, eng mass metadata wins;
      mass_model lists every number) -> mount holes detected (mounting-hole
      footprints, NPTH pads >= 1.5 mm, un-netted plated holes >= 2 mm; or pass
      mount_holes=[[x,y],[x,y,drill_d],...]) and FIXED (screw-land patches on both
      faces) -> core modal solve -> every natural frequency inside the excitation
      band FLAGGED (default 20-2000 Hz random-vibe screening band) -> Miles'
      random response at the worst in-band mode -> 3-sigma board deflection
      (Z = 9.8*G/f^2) screened per component against Steinberg's
      0.00022*B/(C*h*r*sqrt(L)) deflection limit.
  HEURISTIC + CITED: the deflection screen is Steinberg (Vibration Analysis for
      Electronic Equipment, 3e, Wiley 2000) — a solder-joint fatigue RISK screen
      for ~20e6 random-vibe cycles, NOT a life prediction; the citation rides in
      deflection_screen.citation. Components without body dims are UNCHECKED,
      never assumed OK.
  DEFAULT PSD: flat 0.04 g^2/Hz across the band (NAVMAT P-9492-class screening
      level, simplified flat — echoed as a warning). Pass
      psd_breakpoints=[[f_hz, g2_per_hz], ...] for the real qualification
      spectrum before sign-off. zeta damping default 0.05 (assumed, echoed).
  READ THE RESULT: modes[] (freq + in_band flags), band.verdict (FLAG/CLEAR),
      response.worst_mode + total_grms, deflection_screen.table (per-component
      Z_allow vs Z_est margins) + verdict, mass_model, simhandoff envelope with
      one fixed_support condition PER mount hole + the base_excitation PSD.
  Typical flow: ecad_make_board -> ecad_vibe_check() -> if FLAG/FAIL: add a mount
      / thicken laminate / move heavy parts toward mounts -> re-run -> re-screen
      with the real spec PSD.

HEATSINK SELECTION LOOP — heatsink_select on the last thermal study:
  heatsink_select(component_ref, tj_target_c | t_case_target_c,
                  [tim_t_mm=0.1], [tim_k_w_mk=3.0], [max_candidates=8])
      Run ecad_thermal_handoff() FIRST — its geometry/loads/mesh are the cached
      context this tool re-solves. Candidates come from the parts library
      (part_search domain "thermal": curated plain-finned aluminum heatsinks with
      datasheet-class theta_sa + mass + cost) PLUS a parametric plain-finned sink
      generator sized to the component footprint (fin-efficiency theta_sa; geometry
      buildable via services.mcp.heatsink.sink_ops engine ops).
  MOUNT MODEL: bonded contact through a TIM bondline (R_tim = t/(k*A_contact),
      default 0.1 mm of 3.0 W/m-K grease-class TIM). The core solver is
      single-material, so a mounted sink is an EFFECTIVE CONVECTION
      h_eff = 1/((R_tim + R_sa)*A_top) over the component top: board geometry is
      unchanged -> the baseline BOARD MESH IS REUSED per candidate, and board
      spreading + neighbor heating stay fully in the FEA re-solve. Not modeled
      (conservative): sink-base overhang spreading beyond the footprint.
  RESULT: ranked table rows {candidate, source library|parametric, achieved_tj_c,
      achieved_t_case_c, delta_vs_target_c, theta_sa_c_per_w, mass_g, cost_usd,
      meets_target}; winner = LIGHTEST candidate meeting the target (cost tiebreak),
      its solved study kept, and an aicad.simhandoff/1 VERIFY envelope (coupling
      ecad_heatsink_to_thermal_fea) under result.simhandoff.
  HONESTY: an impossible target returns ok:true with target_met:false and the best
      achievable candidate NAMED (never a fake win); a target at/below ambient is
      rejected outright; without a sim-enabled libchudware the tool runs a FLAGGED
      mode="network_estimate" (Tj = Ta + P*(theta_jc + R_tim + R_sa) — board
      coupling ignored). Library theta_sa values are curated datasheet-class
      nominals: verify the chosen part's datasheet before release.
  Typical flow: ecad_thermal_handoff() -> ecad_verify_thermal() shows U1 over its
      limit -> heatsink_select('U1', tj_target_c=85) -> source the winning library
      part (part_source) or model the parametric winner via sink_ops.
