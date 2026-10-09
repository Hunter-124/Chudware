---
name: piping-design
description: Route, size, code-check, bill, and verify piping systems, and size pressure vessels and HVAC/process ducts, using B31.3 and VIII-1 checks, 1-D solves, and pipe-bore CFD that saves its studies into the document.
inject: auto
triggers:
  keywords: [piping, pipe route, pipe routing, route a pipe, pipe run, pipe sizing, line sizing, P&ID, pressure drop, pump, pumps, vessel, pressure vessel, tank, tanks, duct, ducts, ductwork, hvac, process skid, pipe rack, b31.3, asme viii]
  tools: [piping_route, piping_edit, piping_fitting_k, piping_fitting_k_cfd, piping_solve, piping_report, piping_spec_check, piping_bom, piping_load, run_pipe_flow, vessel_size, duct_size, piping_operating_point, piping_cfd_handoff]
---

# Piping design

Depth and exact signatures: `cad_guide('piping')`, `cad_guide('industrial')`.
Units everywhere: mm geometry, kPa **gauge** pressure, °C temperature, m³/h boundary
demands, m³/s for `run_pipe_flow`/`duct_size` airflow, m/s velocity. `piping_fluid` alone
takes T in K and P in Pa.

## 1. Fix the design basis first

Before routing, write down (ledger_note or the reply): fluid and its temperature,
design pressure and temperature, flow rate, pipe material (A106-B, A53-B, A312-TP304,
A312-TP316), corrosion allowance, and the equipment nozzles (point and outward
direction). Missing inputs go to the user in **one** ask_user call with defaults. Do not
invent them. Resolve fluid properties with `piping_fluid` instead of using remembered
values.

## 2. Route

1. Equipment is in a CAD doc: call `piping_route(start, end, nps, schedule, material,
   doc_id=<that doc>, ignore_bodies=[<the equipment you connect to>], network=<name>)`.
   Every other solid becomes an obstacle, and the network is persisted in that doc.
   `dir` is the direction of travel. For start, that is the start nozzle's outward
   normal. For end, it is MINUS the end nozzle's outward normal.
2. No CAD yet: pass `obstacles=[{min,max}]` boxes in mm.
3. For each additional line or branch, pass the returned `piping` back as `piping=`.
4. Pick NPS from a velocity target before routing. Liquids: about 1–3 m/s. Gases and
   steam: about 15–30 m/s. Pump suction: lower than the discharge velocity.
5. Refusals:
   - `no_route`: widen the corridor, lower `clearance_mm`, change `grid_mm`, or move
     equipment.
   - `start_inside_obstacle` / `end_inside_obstacle`: put the point on the nozzle face,
     or list that body in `ignore_bodies`.

   Never hand-draw a run through solids to get past a refusal.

### Draw, edit, and fit losses

- Use `piping_edit(ops=[{"op":"add_run","path":[[0,0,0],[500,0,0],[500,500,0]],
  "nps":2,"schedule":"40"}], network="process", doc_id=...)` for known clear
  waypoints (the human equivalent is Draw Pipe on a viewport plane). Bends become
  elbows, and a branch endpoint on a run splits it and creates a tee. Never bypass
  obstacle-route refusals by drawing through solids.
- Read returned `created`, `removed`, and `changed` ids. Modify geometry with
  `move_vertex`, `split_run`, or `set_run`; batches are atomic. Add an inline valve
  with `add_fitting`, `type:"valve"`, `at:[x,y,z]`, `params:{"valve_type":"ball"}`.
  Orifices use `beta` or `bore_mm`; custom fittings require `params.k_model`.
- Preview with `piping_fitting_k(fitting={type,params,nps}, reynolds=...)`.
  Report its `method`. K overrides are `constant`, Hooper `two_k`, Darby `three_k`,
  US `cv`, or a log-Re interpolated `table`. Set/remove overrides through
  `update_fitting` (`params:{"k_model":null}` restores the correlation).
- Calibrate a doubtful fitting with
  `piping_fitting_k_cfd(fitting_id=..., network="process", doc_id=...,
  quality="quick", background=True, apply=True)`. Poll this same tool with
  `job_id=...`; `cancel=True` stops the actual solver child. Quick uses two Reynolds
  points / 12 cells across; standard uses three / 16. Both return table K(Re),
  2-K least-squares fit, convergence, resolution, steps, timestamps and comparison
  to deterministic K. Never treat coarse-resolution CFD as a certification.
- Elbows of arbitrary angle/radius use the existing bore sweep and native pipe-flow
  solver, subtracting straight friction before calculating K. Reducer extraction
  does not yet include its taper; reducers, tees, valves, orifices and custom
  fittings therefore require real geometry: a closed **fluid-volume** `stl_path`
  or `doc_id` (+ optional `body`), inlet/outlet planes and `inner_dia_mm`.
  Supply straight developed inlet/outlet legs; these custom geometries cannot be
  automatically extended. Without geometry use the deterministic correlation.
- `apply=True` mutates the selected fitting through native `piping_edit` and saves
  the updated `piping_network` study with `doc_id`. Re-solve and regenerate the
  BOM/spec checks after an edit; save the part to retain the workflow.

## 3. Solve (1-D first)

1. Run `piping_solve(doc, boundary, fluid, doc_id, network)`. At least one node must fix
   pressure or head. Address the first boundary as `{run, end}`, then read
   `node_menu`.
2. `piping_report()` gives the per-segment K-factor and dP table.
3. For a pump, run `piping_operating_point(pump_curve, system)`. Report the duty point
   and the NPSH margin. A negative margin is a failed design.
4. If a velocity or dP is out of range, change the NPS or routing and solve again.
   Don't argue the number away.

## 4. Code check and BOM

1. Run `piping_spec_check(piping, design_pressure_kpa, design_temp_c, ...,
   doc_id, network)` (ASME B31.3 eq. 3a, B16.5 flange rating, support span).
2. `all_pass:false` blocks delivery. Raise the schedule, change the material, or raise
   the flange class, then re-check.
3. Cite per run: `t_required_mm`, `t_nominal_mm`, `mawp_kpa`, `utilization`,
   `support_span_m`.
4. Run `piping_bom(piping, max_spool_length_mm, doc_id, network)` for the line list,
   quantities, masses, spools, and weld counts.

## 5. When to run 3-D CFD

The 1-D solve is the sizing basis. Use `run_pipe_flow` (native LBM through the bore)
only where K-factors are doubtful:

- close-coupled fittings
- manifolds or headers with uneven split
- tees with strong branch flow
- custom transitions or nozzles
- a fitting whose dP is a large share of the budget

How to run it:

1. Pass `piping` + `run_id`, or a fluid-volume STL/body with inlet/outlet normals
   pointing **into** the domain.
2. For large grids, use `background=True` and poll `get_job`.
3. Quote `dp_pa` only when `converged:true`.
4. Compare it with `darcy_estimate_pa` and with the 1-D link dP.
5. Repeat any `caveats` (unresolved physical Re, coarse voxels) in the reply. A
   transitional or under-resolved run is a trend, not a number.

## 6. Vessels and ducts

- **No CAD document yet** (a vessel or duct sizing on its own): `new_part(name=<system
  name>)` first and pass its `doc_id`, so the sizing is a study the user can open, then
  `save_part` (a study-only document saves without geometry).
- **Vessel:** run `vessel_size(...)` (ASME VIII-1 UG-27/UG-32). Use the returned
  `t_nominal_mm` for shell and heads, `mawp_kpa`, `hydrotest_kpa`, and masses. Then model
  it with a revolve at those thicknesses, add nozzles, and route to the nozzle faces.
- **Ducts:** run `duct_size(segments=[...parent tree...])` with equal friction. Size the
  fan from `fan_static_pa` and the `critical_path`. No lengths given: assume a
  representative layout (segment lengths, K for elbows/take-offs), state it, and quote
  the resulting `fan_static_pa` — never a formula in place of the number. A velocity
  warning means a lower friction rate (bigger duct) or a split branch.
- **Skid, rack, or supports:** build the frame with `weld_member` (cad_skill
  `weldment-frame`) and check it with `struct_check`. Space supports at or inside the
  spec check's `support_span_m`.

## 7. Evidence rules (done means)

- Pass `doc_id` on route, solve, spec_check, bom, vessel_size, duct_size, and
  run_pipe_flow so every result is a **study in the document**. Then `save_part` writes
  them into the `.step+`.
- After a reopen, call `piping_load(doc_id, network)` before re-solving. Never rebuild
  the network from chat memory.
- Every reported pressure, thickness, or size cites the tool result and its code clause
  (B31.3 eq. 3a, B16.5 class, VIII-1 UG-27/UG-32/UG-99).
- Stamp nothing PASS from a failed, unconverged, or refused result.
- Refusals (`ok:false` + `reason` + `hint`) are inputs, not obstacles. Follow the
  hint, change the design, and retry. If it still refuses, tell the user which
  constraint blocks the design.
