# Guide: Piping

```text
PIPING — route, solve, code-check, bill and verify pipe systems

Tools: piping_route / piping_solve / piping_report / piping_spec_check /
       piping_bom / piping_load / run_pipe_flow / piping_operating_point /
       piping_pump_drive / piping_fluid / piping_internal_volume /
       piping_cfd_handoff / piping_verify_cfd

UNITS: lengths mm (run paths, clearances), pressures kPa GAUGE, temperatures
  degC (piping_fluid alone takes T in KELVIN and P in Pa), flows m3/h in
  boundary demands, m3/s in run_pipe_flow, velocities m/s.

THE NETWORK: an aicad.piping/1 JSON doc (contracts/piping_schema.md):
  runs[] = {id:'r1', path:[[x,y,z],...] (>=2 pts, mm), nps, schedule, material,
            end_type}; fittings[] = {id:'ft1', type:'elbow'|'tee'|'reducer'|
            'flange', origin:[x,y,z], nps, params}. piping_route writes runs and
  their elbows for you; every tool below takes that doc inline OR the name of
  a network already in the session (omitted = the last one).

WORKFLOW:
  0. piping_load(doc_id, network=None)  Resume: restore a network persisted in a
                                        doc (after save/reopen) into the session.
                                        Several networks in one doc -> it refuses
                                        and lists their names; pass network=.
  1. piping_route(start={'point':[x,y,z],'dir':[dx,dy,dz]},
                  end={'point':[x,y,z],'dir':[...]}, nps, schedule='40',
                  material=None, obstacles=[{'min':[..],'max':[..]}],
                  clearance_mm=50, grid_mm=None, bend_penalty=None, piping=None,
                  name=None, doc_id=None, ignore_bodies=None, network=None,
                  fresh=False)
     Orthogonal obstacle-avoiding A* (bends penalised). doc_id makes every solid
     body of that doc an obstacle — list the equipment you connect TO in
     ignore_bodies — and persists the network there. `dir` (optional, snapped to
     the nearest axis) is the direction of TRAVEL: start.dir = the way the pipe
     leaves start (= that nozzle's outward normal); end.dir = the way it arrives
     at end (= MINUS the end nozzle's outward normal). A bare [x,y,z] works too.
     The run is APPENDED to piping= / the session network / the doc's saved
     network (fresh=True starts a new one); name= names the run.
     Returns piping (new run + 90-deg LR elbow at every bend), run_id, path,
     length_mm, bends, min_clearance_mm. Refusals: no_route (blocked: widen
     the corridor, lower clearance_mm, change grid_mm), start_inside_obstacle /
     end_inside_obstacle (move the point to the nozzle face or ignore that body).
  2. piping_fluid(name, T=<K>, P=<Pa>)  Resolve fluid properties (sourced values).
  3. piping_solve(doc, boundary, fluid=None, pumps=None, doc_id=None, network=None)
     1-D steady solve: flows + pressure map. boundary entries name a node AND
     carry one condition, e.g.
       {'run':'r1','end':'start','pressure':400}   (kPa gauge — fixes it)
       {'run':'r1','end':'end','demand':20}        (m3/h, + leaves)
     Node ids ('n0','n1') are DERIVED by snapping run endpoints, so they only
     exist AFTER a solve (result.node_menu lists them). Address the first
     boundary by {'run','end'}; a failed reference returns
     addressable_endpoints. At least one node must fix pressure or head.
  4. piping_report()                     Per-segment K-factor + dP table (no args
                                         = report on the last solve).
  5. piping_spec_check(design_pressure_kpa, piping=None, design_temp_c=40,
                       corrosion_allowance_mm=1.5, mill_tolerance_frac=0.125,
                       joint_factor=1.0, fluid_density_kgm3=1000,
                       insulation_kgm=0, doc_id=None, network=None)
     ASME B31.3 eq. 3a wall t = PD/(2(SEW+PY)), Y=0.4, allowable stress vs
     temperature (A106-B, A53-B, A312-TP304/316), B16.5 flange P-T rating and
     support span (2.5 mm sag / bending limit, pipe+fluid+insulation weight).
     Per run: t_required_mm vs t_nominal_mm, mawp_kpa, utilization,
     support_span_m, pass. all_pass:false -> raise the schedule, change
     material or flange class and re-check; never ship a failing line.
  6. piping_bom(piping, max_spool_length_mm=12000, doc_id=None, network=None)
     Line list, pipe/elbow/tee/reducer/flange quantities with masses, shop
     spools broken at flanges and at max spool length, shop/field weld counts.
  7. run_pipe_flow(...)                  3-D internal-flow check of a CRITICAL
     fitting or manifold (native D3Q19 LBM over the bore). Geometry: either
     piping=<doc or network> + run_id (bore and inlet/outlet planes derived from
     the run ends) or stl_path / doc_id(+body) of a fluid-volume solid with
     inlet={'point','normal'} / outlet={...} (normals point INTO the domain).
     fluid='water' (or density_kgm3 + viscosity_pas), mean_velocity_ms or
     flow_m3s; resolution cells_across=20 (voxels across the hydraulic
     diameter; raise it to refine) or an explicit voxel_mm; max_steps;
     background=True -> poll get_job(job_id).
     Returns dp_pa, reynolds, regime, converged, pressure_profile and
     darcy_estimate_pa; quote dp only when converged and read `caveats` (an
     unresolvable physical Re is stated, never hidden). doc_id+name persist a
     pipe_flow study.
  8. piping_operating_point(pump_curve=<CSV text/path>,
                           system={'static_head_m':h0,'k':k})
                                        Pump duty point + NPSH margin.
  9. piping_pump_drive(network=<name>, run_id=None, speed_rpm=1500,
                       standard='IEC'|'NEMA', doc_id=...)
     Electric motor for the pump from the last solve's pump run (flow_m3h +
     pump_head_m, solve fluid density) via pump_drive_size; flow_m3h + head_m
     override it with a piping_operating_point duty point. Saved as
     results.pump_drive.<run_id> in the piping_network study.

1-D vs 3-D: piping_solve is the sizing truth for straight runs and catalogue
  fittings (fast, deterministic). Use run_pipe_flow only where the K-factor
  model is doubtful — close-coupled fittings, manifolds, tees with strong
  branch flow, custom transitions — and compare its dp_pa with the 1-D link
  dP. piping_cfd_handoff(doc, boundary) + piping_verify_cfd(solve_result,
  cfd_result) remain the one-call bore -> run_cfd_flow chain with a 1-D vs
  3-D verdict; piping_internal_volume(doc) gives the bore STEP + volume_mm3.

FITTINGS + MINOR LOSSES: piping_edit draws runs (tees/elbows appear at
  junctions/bends) and adds inline valve/orifice/custom/reducer/flange fittings.
  A fixed loss is params.k_model={'kind':'constant','K':k} (also two_k,
  three_k, cv, table). A tee's loss is per path: update_fitting(id=<tee>,
  params={'k_model_run':{'kind':'constant','K':kr},
          'k_model_branch':{'kind':'constant','K':kb}}) -- straight-through K on
  run_ids[1], branch K on branch_run, each on that pipe's velocity. Never add a
  custom fitting as a stand-in for a tee or bend loss: every fitting is a
  piping_bom item. Unknown params keys refuse unknown_param and list the accepted
  keys. piping_fitting_k previews K (tees return paths.run / paths.branch).

GEOMETRY: piping_route paths are centrelines. To model a line as CAD, feed its
  path to sketch3d_path_from_points(name, points) then sketch3d_pipe(id,
  diameter_mm=<OD>) (cad_guide('sketch3d')).

PERSISTENCE: pass doc_id (+ network, default 'piping') on route / solve /
  spec_check / bom and the network plus its solve, spec_check and bom results
  land in that doc's `piping_network` study; save_part() writes it into the
  .step+ and piping_load restores it. Cite study results, not chat memory.

EXTERNALLY-SUPPLIED NETWORKS: piping_solve(source_handoff=<aicad.simhandoff/1
  handoff envelope>) consumes a network generated by another domain — e.g.
  hydraulic_piping_handoff hands its hose runs over as an inline piping doc with
  boundary + fluid in the envelope meta; runs may carry an explicit bore_mm to
  bypass the NPS schedule catalog (hydraulic hose IDs aren't catalog pipes).

Vessels, ducts and pipe-rack/skid frames: cad_guide('industrial'). Decision
  procedure: cad_skill('piping-design').
```

