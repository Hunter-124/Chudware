---
name: hydraulic-design
description: Design, size, simulate, and bill hydraulic fluid-power circuits (ISO 1219 pumps, cylinders, motors, valves, accumulators, HPUs and their electric drives) with transient simulation evidence saved into the document.
inject: auto
triggers:
  keywords: [hydraulic, hydraulics, fluid power, HPU, power unit, hydraulic power unit, hydraulic cylinder, double-acting cylinder, single-acting cylinder, cylinder bore, hydraulic motor, pump, electric motor, pump motor, accumulator, relief valve, directional valve, spool valve, ISO 1219, hydraulic hose, hydraulic circuit]
  tools: [hydraulic_new, hydraulic_templates, hydraulic_add_component, hydraulic_add_hose, hydraulic_layout_3d, hydraulic_size_cylinder, hydraulic_size_pump, hydraulic_size_motor, pump_drive_size, hydraulic_simulate, hydraulic_bom, hydraulic_load, hydraulic_piping_handoff, hydraulic_verify_piping, hydraulic_fea_handoff, hydraulic_verify_fea, piping_pump_drive]
---

# Hydraulic design

Exact signatures, port names and the params the simulator reads: `cad_guide('hydraulic')`.
Units everywhere: bar **gauge**, L/min, mm, N, N·m, rpm, kW, s. Pump displacement is
cc/rev, accumulator volume L. Piping-side tools use kPa and m³/h. Convert at the
boundary and say so.

## 1. Fix the duty first

Before building anything, write down (ledger_note or the reply):

- every actuator: force or torque, stroke or speed, the motion time, which ones move
  together
- the available or wanted system pressure and the relief setting (usually 10–15 % above
  the highest working pressure)
- the prime mover: electric (voltage, phases, 50/60 Hz, IEC or NEMA) or engine speed
- duty cycle and fluid (default ISO VG32 mineral oil, 870 kg/m³, β = 15000 bar)

Missing inputs go to the user in **one** ask_user call with defaults. Do not invent loads.

## 2. Size before you draw

1. Cylinders: choose a bore from F = π/4·d²·p (working pressure, not relief), round up to
   a standard bore, then `hydraulic_size_cylinder(bore_mm, pressure_bar, stroke_mm,
   rod_mm, velocity_mm_s, component_id=..., doc_id=...)`. Check retract force on the
   annulus and rod buckling for long strokes (hydraulic_fea_handoff for the mounts).
2. Motors: `hydraulic_size_motor(torque_nm, speed_rpm, pressure_bar=<differential>,
   type=...)`. Use `standard_displacement_cc_rev`; recompute speed with the catalog
   displacement.
3. Pump: sum the flows of actuators that move at the same time. Call
   `hydraulic_size_pump(flow_lpm, pressure_bar, speed_rpm=<motor speed>)`.
4. Electric drive: `pump_drive_size(kind="hydraulic", flow_lpm, pressure_bar,
   speed_rpm, standard, voltage_v, phases, frequency_hz)`. Quote motor_rating_kw (or
   _hp), poles, frame and full_load_current_a. A piping network's centrifugal pump uses
   `piping_pump_drive` after `piping_solve`.

## 3. Build the circuit

1. `hydraulic_new(name, template=..., doc_id=<the CAD doc>)`. Native template ids:
   `blank` (default, custom empty circuit), `power_unit` (tank/pump/relief/return filter),
   `cylinder_circuit` (power unit + 4/3 valve + Ø50/28 × 300 mm cylinder), and
   `motor_circuit` (power unit + 4/3 valve + bidirectional 80 cc/rev motor). `hydraulic_templates()`
   lists their names, descriptions and component/hose counts. Non-blank templates are
   already wired and return a `simulation` setup; both circuit and setup are saved into
   the study. Template loads are examples, not the machine's duty: adapt them to §1.
   Their 4 s demos shift A at 0, B at 2 s, then at 2.5 s hold the partly retracted
   cylinder / resume steady forward motor rotation. Metered valves control assisting
   loads on the return; initial hose pressures balance the static load. A closed
   gravity-load cycle has zero net signed useful work, not zero gross mechanical work.
   Unknown ids refuse with `unknown_template` and a valid-id hint.
   After a reopen: `hydraulic_load(doc_id)` instead. Never rebuild a saved circuit from
   chat memory; its saved setup comes back as `simulation_setup`.
2. Keep a template's connected components and add `hydraulic_add_component` for any
   additional pump, relief valve, directional valve(s), actuators,
   reservoir, plus check/flow-control valves, filter, cooler and accumulator as the
   function needs. Use the sized numbers as params. Set the simulator's required params:
   pump displacement + speed, cylinder bore + stroke (+ rod), motor displacement, relief
   set pressure, accumulator volume + pre-charge. Put `max_pressure_bar` on rated parts.
3. `hydraulic_add_hose` per net, using the canonical port names (pump P/T, cylinder
   A/B, valve P/T/A/B, relief P/T, inline IN/OUT). Every port the simulator needs must be
   connected. Give hoses `max_pressure_bar`, `id_mm`, `length_mm`.
4. Pass `doc_id` on every call so the circuit lives in the document's
   `hydraulic_circuit` study.
5. Physical layout: `hydraulic_layout_3d(positions={"c1":[x_mm,y_mm,z_mm], ...},
   circuit=<saved name or inline circuit>, doc_id=...)` merges positions into the same
   `params.layout3d` the GUI's Hydraulics > 3D Layout edits. Omit circuit only when the
   document has one saved circuit or the active session circuit is unambiguous. Read
   `layout3d` and `saved_to_study` in the receipt; unknown IDs/non-finite triples refuse.
   Moving glyphs preserves schematic positions and simulation results; it does not
   alter hose length/loss parameters or build component CAD. `hydraulic_load` returns
   the layout after reopening. Humans select a component in Circuit > 3D layout,
   choose a working plane/offset/grid, then Place in 3D and click; Frame layout fits it.

## 4. Simulate the real sequence

1. `hydraulic_simulate(duration_s, valve_schedule=[{t_s, component_id, position}],
   loads={...}, doc_id=...)`. A new template's `simulation` (or a restored circuit's
   `simulation_setup`) uses these exact argument keys; pass `**setup` after replacing
   its example duty with the real sequence: extend, hold, retract, centre. Give loads
   with mass so acceleration and pressure spikes show. Template creation itself does
   not simulate. Constant positive force/torque resists extension/forward rotation
   and assists retraction/reverse rotation; model the actual load direction.
2. Read `summary` and compare with the hand calculation:
   - Cylinder steady velocity ≈ Q·η_v / A_bore (extend) or / A_annulus (retract).
   - Motor speed ≈ Q·η_v / D.
   - Working pressure ≈ F/A (or 2πT/(D·η_m)) + line and valve Δp.
   A gap over about 5 % with no physical explanation means a modelling error. Find it
   before going on.
3. Check `peak_pressure_bar` against the relief setting plus its override, and check
   `ratings.exceeded`. Any exceeded rating is a failed design. Fix it with a lower relief,
   a deceleration/flow control valve, an accumulator, or higher-rated parts. Then
   simulate again.
4. If `relief_open_time_s` > 0 during a move, flow is being dumped over relief and turned
   into heat (`heat_kj`). Resize the pump, or use a pressure-compensated pump
   (`compensator_bar`), and say so.
5. Refusals name the fix: `unconnected_port`, `unknown_port`, `missing_params`
   (`missing: ["c4.bore_mm"]`), `unknown_component`, `invalid_schedule`,
   `no_pressure_source`. Follow the hint. Never delete a component just to get a run
   through.

## 5. Lines, mounts, BOM

- Rigid tube / long hose runs: `hydraulic_piping_handoff(supply_pressure_bar, flow_lpm,
  load_pressure_bar)` gives real line losses and `margin.pump_margin_solved_bar`. A
  negative margin means a bigger bore or a higher pressure. Then call
  `hydraulic_verify_piping()`.
- Cylinder mounts: `hydraulic_fea_handoff(bore_mm, max_pressure_bar = relief setting)`,
  then `run_fea_static` per fea_args, then `hydraulic_verify_fea`.
- `hydraulic_bom(doc_id=...)`: every line without an MPN is open sourcing work. List
  them.

## 6. Evidence rules (done means)

- Every reported number cites the tool result it came from: forces, flows, displacements,
  motor kW and frame, peak pressure, stroke times, efficiency.
- A design is done when the simulation has been run on the final circuit and both hold:
  - no rating is exceeded
  - stroke and cycle times meet the requirement
  The `hydraulic_circuit` study must carry that simulate result. `save_part` writes it
  into the `.step+`.
- Series in the reply are already cut to ≤400 points. For full-resolution plots, use
  `series_path`. Never paste whole series into chat.
- Stamp nothing PASS from a refused, failed or stale run. An edited circuit drops its
  stored simulate result, so re-simulate after the last edit.
