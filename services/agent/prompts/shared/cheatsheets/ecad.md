# ECAD — schematic capture, ERC, simulation, PCB, fab

WORKFLOW: ecad_new_schematic → ecad_find_part / ecad_place_symbol → ecad_wire /
ecad_add_power / ecad_label → ecad_run_erc → ecad_simulate → ecad_make_board →
ecad_run_drc → ecad_export / ecad_to_3d.
ecad_status is THE context tool — components, nets, ERC summary, next_actions.

## Simulation (ecad_simulate / ecad_tf / ecad_sweep)

Analyses: 'op' (DC operating point), 'dc' (source sweep), 'tran' (transient),
'ac' (frequency response), 'four' (THD), 'mc' (Monte-Carlo tolerance),
'temp' (temperature sweep), 'noise' (output noise). ecad_tf gives DC gain/Rin/Rout.
Probes default to ALL node voltages + source currents when omitted.

### Backends (T-FIN14)

`ecad_simulate(..., backend=...)` and `ecad_tf(..., backend=...)` accept:

- `'mna'` (DEFAULT) — the validated internal Python MNA solver. Deterministic,
  always available, supports every analysis above. Behavioural built-in models
  only (R/C/L/V/I/D/Q/M, ideal OPAMP, NE555, ...).
- `'ngspice'` — runs the same circuit through an installed ngspice binary as a
  subprocess. Unlocks REAL vendor models: .SUBCKT/.MODEL libraries (op-amps,
  regulators) attached to a component. Covers op/dc/tran/ac/tf only; the
  advanced wrappers (four/mc/temp/noise) always run on the MNA solver.
- `'auto'` — mna unless the circuit carries vendor .lib/.subckt attachments AND
  ngspice is installed.

HONEST DEGRADE: if ngspice is not installed (or the run fails), the tool runs
the MNA solver instead and says so in `backend_note`; the result's `backend`
field always names the solver that actually ran. Install: `dnf install ngspice`
(or point `AI_CAD_NGSPICE` at a binary). Detection is reported by the installer's
`--check` (services/installer/provision.py: ngspice_provisioning_status).

### Vendor model import

Attach a vendor SPICE library to a component by carrying a sim dict on it:

    {"sim": {"model": "SUBCKT", "subckt": "UA741",
             "lib": "/path/to/ua741.lib", "pin_order": ["1","2","3",...]}}

netlist_to_spice_circuit records it as an X instance + `.include`; only the
ngspice backend simulates it (the MNA solver warns and ignores it). Parity is
gated in engine/sim/test_ngspice_backend.py (RC + divider fixtures must match
across backends).

## Boards

ecad_make_board is the one-shot sync→route→fill→DRC path. ecad_check_impedance /
ecad_check_diff_impedance read trace Z0 vs target. ecad_run_dfm for fab checks.
