"""Agent-facing civil topic; registration belongs to cad_guide.py."""

GUIDE_TEXT: str = """CIVIL — site, terrain, utilities, plant interfaces and delivery evidence

Tools: civil_site_new / civil_load / civil_site_edit / civil_site_summary /
       civil_import / civil_export / civil_crs_transform / civil_surface_build /
       civil_surface_analyze / civil_grade_pad / civil_watershed / civil_alignment /
       civil_alignment_analyze / civil_water_solve / civil_fire_flow /
       civil_gravity_analyze / civil_gravity_design / civil_open_channel /
       civil_hydrology / civil_detention_route / civil_culvert /
       civil_buried_pipe_check / civil_thrust_restraint / civil_pipe_supports /
       civil_piping_couple / civil_fea_handoff / civil_requirement_evidence /
       civil_quantities / civil_cost_estimate / civil_schedule

UNITS: SI m coordinates/elevations/diameters; m2, m3, m3/s, head m, kPa, time s.
  Rainfall only: mm depth, mm/h intensity. Radians unless *_deg; slopes m/m or H:V.
  x=easting, y=northing, z=project vertical datum. Piping uses mm geometry,
  m3/h demands and kPa gauge; tie-in coupling converts explicitly.

THE SITE: aicad.civil/1 (contracts/civil_schema.md): crs, criteria, surfaces[],
  alignments[], water_networks[], gravity_networks[], catchments[], structures[],
  tie_ins[], provenance; stable collection IDs. site= is model, JSON/path or name.
  Omitted: active session site, else single doc study. Name multi-site work explicitly.

PERSISTENCE: doc_id on supported edits/analyses writes a civil_site study:
  params.model plus results keyed by analysis. Check saved_to_study and save the
  document normally. Results are latest; earthwork is keyed by kind/IDs.
  civil_load(doc_id, site=<saved name>) restores model AND results after reopen.
  Changed models clear analyses; recompute after the FINAL edit.

WORKFLOWS:
  1. SITE SETUP, CRS AND IMPORT
     civil_site_new(name, crs, criteria, doc_id) or civil_load for an existing site.
     Establish a projected metre CRS, vertical datum and any local-grid transform
     before merging survey/utility data. civil_crs_transform(points, src, dst,
     scale_factors=True) reports transforms/scale; it does not change the site or
     silently transform vertical datums. Set CRS explicitly with civil_site_edit.
     civil_import(source, format, site, merge=True, options, doc_id) accepts
     landxml / geojson / geopackage / epanet / swmm / dem_ascii / dem_xyz.
     LandXML has north/east coordinate order; adapters convert foreign units.
     Read provenance, dropped/unmapped data and warnings. EPANET map units may
     be assumed, not known. DEM elevations/spacing must already be in metres.
     GIS external fields require mappings; multipart/holes are unsupported.
     civil_site_summary gives validation errors, counts, CRS and result keys.
     Repair missing hydraulic inputs with civil_site_edit, not guessed defaults.
     Ops: upsert/remove a collection element, set_crs, set_criteria, rename.

  2. TERRAIN, GRADING AND EARTHWORK
     civil_surface_build(surface_id, points, breaklines, boundary, doc_id=...)
     builds a constrained TIN; dem_path is an alternative source. Preserve survey
     breaklines/boundaries and choose DEM stride without losing critical features.
     civil_surface_analyze(kind, surface_id, ...) supports contours, slope,
     profile, volume, volume_to_elevation and stage_storage.
     civil_grade_pad(surface_id, pad_polygon, pad_elevation, cut_slope, fill_slope,
     graded_surface_id=..., doc_id=...) uses H:V slopes and daylight to ground.
     Inspect daylight coverage and cut/fill signs: surface-surface positive
     top-minus-bottom is fill; net=fill-cut. No extrapolated terrain is evidence.
     Grading adds a surface and invalidates old results: calculate quantities and
     any terrain-dependent utility checks again after grading.

  3. ALIGNMENTS, PROFILES AND SECTIONS
     civil_alignment(alignment_id, pis/radii/spiral_lengths OR horizontal,
     vertical=..., start_station=..., doc_id=...) builds line/arc/Euler-spiral
     plan geometry and symmetric parabolic vertical curves.
     civil_alignment_analyze(kind, alignment_id, surface_id, ...) supports
     profile, design_vs_existing, sight_distance, sections and earthwork.
     Give design_speed_kmh for sight-distance checks, a template for sections,
     and suitable stations/step. Earthwork uses average_end_area or prismoidal;
     declare shrink/swell. Stationing is horizontal distance, with no station
     equations or superelevation. Inspect continuity, terrain coverage, grades,
     curve checks and section intervals; cite the returned method, not a sketch.

  4. WATER DISTRIBUTION AND FIRE FLOW
     Upsert/import a water network with elevations, source heads, demands,
     pipe dimensions/roughness and real pump curves. H-W roughness is C;
     D-W roughness is metres. civil_water_solve(network_id, doc_id=...) uses
     Todini-Pilati GGA, steady or extended-period operation from network options.
     Check converged, residuals, pressures, delivered demands, velocities,
     pump/valve states and tank levels throughout every required scenario.
     civil_fire_flow(network_id, node_id, flow_m3s, residual_min_m, doc_id=...)
     checks fixed fire withdrawal; omit flow_m3s to find available flow. Residual
     pressure is checked at ALL junctions: cite the limiting node and flow.
     PDA scales base demand with pressure; DDA enforces demand even at low head.
     State which was used. Fire withdrawal itself stays fixed; the scenario uses
     initial tank levels and is NOT a duration/storage-endurance test.
     Quality, rule controls, emitters and unsupported pump/tank cases refuse.

  5. SEWER/STORM: CATCHMENTS -> HYDROLOGY -> GRAVITY -> DETENTION -> CULVERTS
     Define catchment area, outlet, C/CN and Tc basis; civil_watershed(dem_path,
     outlet_xy, ...) delineates a D8 catchment. Inspect DEM nodata/outlet location.
     civil_hydrology(kind, catchment_id, ...) supports tc, idf, rational,
     scs_runoff, design_storm, unit_hydrograph and runoff_hydrograph.
     Supply local IDF tables, storm depth/distribution and return period; never
     invent rainfall. Rational peaks are not runoff volumes. Storm depth_mm is
     interval depth; hydrograph q_m3s is an instantaneous ordinate, time in s.
     civil_gravity_analyze(network_id, flows=..., surface_id=..., doc_id=...)
     accumulates inflows and checks slope, capacity, depth, velocity and cover.
     catchment_peaks=True adds rational peaks at catchment outlets: simultaneous
     peaks, not network routing. Do not count the same inflow twice.
     civil_gravity_design(..., available_diameters=..., apply=False) proposes
     sizes; apply=True edits the model. Re-analyze applied sizes and cover.
     civil_open_channel(kind, shape, flow_m3s, ...) is stateless: normal_depth,
     critical_depth, standard_step, hydraulic_jump or capacity for channel work.
     civil_detention_route(...) uses Modified Puls, an inflow hydrograph,
     monotone stage-storage and real outlet geometry; terrain can supply storage.
     Check mass balance, peak stage/outflow and physical freeboard. Explicitly
     transfer routed downstream flows; gravity screening does not route storage.
     civil_culvert(...) checks inlet/outlet control, tailwater and overtopping;
     supplied flows produce a performance curve. Respect low-flow backwater
     refusals and the disclosed HDS-5 transition approximation.

  6. BURIED PIPE, THRUST, SUPPORTS AND FEA
     civil_buried_pipe_check(inputs, conduit_id/network_id, surface_id, ...) uses
     prism/Marston, explicit wheel loads, Iowa deflection, ring buckling and
     flotation. Supply OD/wall, embedment, groundwater and load/allowable basis;
     ring EI per axial length is NOT beam EI. Cite every returned check row.
     civil_thrust_restraint(pressure_kpa, fitting, ...) checks hydrostatic force,
     soil bearing and restrained length; use the applicable operating/design
     pressure and verified soil/joint data. Live water pressure is not surge.
     civil_pipe_supports(piping=..., ...) converts piping mm geometry and returns
     beam-based spacing, support reactions and rack loads with user allowables.
     civil_fea_handoff(source='thrust'|'buried', target_doc_id, face_selector, site)
     makes a civil_to_fea envelope, NOT an executed FEA study. Bind bearing/crown
     faces, real material/restraints and explicit load direction/selection, then
     execute and review static FEA. Loads are N/Pa; 1 kPa = 1000 Pa. Beam support
     screening is not ASME sustained/expansion acceptance or a full rack analysis.

  7. PLANT TIE-IN COUPLING WITH PROCESS PIPING
     Define tie_ins: civil_network/node, piping_doc_id and run_id/end endpoint,
     supply/discharge direction and elevation_offset_m. Supply endpoint z is
     civil elevation + offset, NOT hydraulic head. Provide plant boundary/fluid.
     civil_piping_couple(network_id, piping, boundary, fluid, tie_in_ids,
     tol_rel=0.01, max_iter=30, doc_id=...) iterates site head <-> plant demand.
     It requires steady DDA, unpatterned/unscaled tie-in demands and liquid flow;
     do not silently replace a PDA/EPS design basis to bypass a refusal.
     Cite converged, history and per-tie head/flow agreement. simhandoff carries
     civil_to_piping pressure (kPa gauge) and piping_to_civil demand (m3/s).
     A pressure-only solve is not coupled evidence. Re-run after either network
     changes; recheck site criteria, plant pressure/stress limits and support/FEA
     loads. Gravity plant discharge needs explicit inflow routing, not this loop.

  8. QTO, COST, SCHEDULE AND 4D
     civil_quantities(trench=..., surface_id=..., doc_id=...) gives pipe,
     structures, trench/bedding/backfill and stored earthwork quantities. Trench
     needs real outer_diameter_m or a material ID-to-OD table, side clearance,
     bedding depth and sample step; hydraulic diameter is NOT OD.
     civil_cost_estimate(price_book, contingency_pct, escalation, doc_id=...)
     consumes stored qto; no_qto means run quantities first. Users supply prices,
     units, currency, source/date and escalation; no invented market rates.
     civil_schedule(tasks, start_date, day, doc_id=...) gives CPM, element-task
     4D links and optional planned state at day. Supply durations/dependencies;
     cite critical path/float and unmapped elements, not an invented build plan.

  9. REQUIREMENTS EVIDENCE AND DELIVERABLES
     civil_requirement_evidence(requirement_ids_by_check, source_key, site)
     maps stored check rows to requirements evidence payloads; review and attach
     through the requirements workflow. It does not independently certify PASS.
     civil_export(format, path, ...) needs network_id for epanet/swmm; formats:
     landxml, geojson, geopackage, ifc, epanet, swmm, qto_csv, cost_csv, msproject.
     LandXML carries survey/TIN/alignment/networks; IFC 4.3 ADD2 is coordination
     geometry, not pipe wall design. Review skipped/dropped objects and missing
     elevations/CRS. GIS exports refuse crs_unknown. Do not promise lossless IFC
     delivery of terrain, structures or catchments. Cite current study evidence.

HONESTY: ten_states_2014 is editable guidance, NOT adopted jurisdictional code.
  Limits are profile values; confirm overrides/references with the authority.
  Gravity HGL/EGL is preliminary steady Manning/K screening, not SWMM dynamic
  wave or the FHWA access-hole energy-loss method. HEC-22 4th ed. §9.1.6.6
  excludes Table 9.4 K from final EGL design. Passing screening is not acceptance.
  Report warnings/assumptions/method, omitted checks and non-convergence. ok:true
  means a calculation returned, not that it passed or converged. Refusals carry
  reason/hint: correct the actual inputs/design; never suppress the limitation.
"""
