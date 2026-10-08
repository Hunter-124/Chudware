---
name: civil-design
description: Design terrain, site utilities, drainage and plant tie-ins with SI civil models, profile-driven checks, coupled piping/FEA evidence and persisted deliverables.
inject: auto
triggers:
  keywords: [civil engineering, site design, site grading, terrain, earthwork, water distribution, fire flow, sewer, stormwater, catchment, detention, culvert, buried pipe, thrust restraint, utility connection, LandXML, GeoPackage]
  tools: [civil_site_new, civil_load, civil_site_edit, civil_import, civil_surface_build, civil_grade_pad, civil_alignment, civil_water_solve, civil_fire_flow, civil_gravity_analyze, civil_gravity_design, civil_hydrology, civil_detention_route, civil_culvert, civil_buried_pipe_check, civil_piping_couple]
---

# Civil design

Use this procedure for site/utility design and for validating a plant's utility
connections. Tool signatures and limitations are in the civil guide topic;
`contracts/civil_schema.md` defines `aicad.civil/1` and its persistence/couplings.
Civil lengths/elevations/diameters are **m**, flow **m³/s**, head **m**, pressure
**kPa**, time **s**. Rainfall alone uses mm and mm/h. Project x/y are easting/
northing; z uses the project vertical datum. Piping geometry is **mm**, boundary
flow m³/h and pressure kPa gauge. Coupling converts; never rescale twice.

## 1. Establish the design basis and resume the actual site

1. Identify the working document, project limits, survey source/accuracy, projected
   metre CRS, vertical datum and local-grid parameters. Gather jurisdictional
   criteria, required operating/fire/storm cases, available supply heads, demands,
   pipe/soil properties, groundwater, outfalls and receiving-water tailwater.
   Resolve missing inputs with the user through the available question interface;
   never invent IDF data, geotechnical values, pump curves or market prices.
2. Reopened project: `civil_load(doc_id, site=<saved name>)` restores model **and
   results**. New project: `civil_site_new(name, crs, criteria, doc_id)`.
   Select `site` explicitly when multiple sites are open: omitted site can select
   the active session site instead of the document you intended.
3. Criteria are editable profile values, not a universal acceptance code.
   `ten_states_2014` is guidance; confirm adoption, local amendments and overrides.
   Retain references and distinguish published recommendations from practice
   starting values. Apply approved changes with `civil_site_edit` set_criteria.
4. Import through `civil_import` with format `landxml`, `geojson`, `geopackage`,
   `epanet`, `swmm`, `dem_ascii` or `dem_xyz`. Review unit/CRS provenance,
   assumptions, dropped/unmapped fields and supported feature types. Foreign
   feet/gpm/cfs are adapter conversions, not metres already. DEM inputs must be
   metre-based. LandXML text coordinates are north/east, not x/y.
5. Use `civil_crs_transform` for horizontal/local-grid conversion and optional
   scale factors; it does not imply a vertical datum conversion or edit the model.
   Set the site CRS deliberately with `civil_site_edit`. Never label an unknown
   grid with a guessed EPSG code just to make GIS export succeed.
6. Read `civil_site_summary` counts, CRS, validation errors and stored result keys.
   Repair incomplete imports with upsert/remove operations through
   `civil_site_edit`. Imported drawings may lack demands, roughness or elevations;
   an imported network is not yet a solved design.

## 2. Ground, grading, alignment and constructible utility elevations

1. Build existing ground with `civil_surface_build`: survey points, constrained
   breaklines and boundary, or DEM source/stride. Inspect resolution, gaps and the
   modeled footprint before computing quantities or pipe cover.
2. Use `civil_surface_analyze` contours/slope/profile to inspect drainage and
   terrain; use volume/volume_to_elevation to quantify earthwork and stage_storage
   for pond volume. Record which surface is top/bottom and the datum: for
   surface-surface volume, positive top-minus-bottom means fill and net=fill-cut.
3. Grade with `civil_grade_pad`, giving pad elevation, polygon and **H:V** cut/fill
   slopes. Check daylight reach and surface coverage. Do not deliver grading that
   depends on terrain extrapolation. The graded surface is a model edit: recompute
   cover, drainage and quantities on the final proposed surface.
4. Create `civil_alignment` from PIs/radii/spirals or explicit horizontal elements,
   plus the vertical PVI profile. Use `civil_alignment_analyze` profile and
   design_vs_existing for utility/road levels; sections and earthwork for corridor
   quantities; sight_distance with the actual design speed for curve screening.
5. Inspect continuity, profile grades, section spacing, shrink/swell assumptions
   and returned curve checks. Stationing is horizontal distance; station equations
   and superelevation are not modeled. No smooth-looking view replaces numbers.

## 3. Pressurized utilities and fire-flow adequacy

1. Check network source heads, junction elevations/demands, hydraulic diameters,
   statuses, pump curves, tank geometry and operating levels. Hazen-Williams uses
   C; Darcy-Weisbach roughness is **m**. Confirm source head versus elevation.
2. Run `civil_water_solve` for required normal/peak and extended-period cases.
   Inspect convergence and continuity/energy residuals before reading design
   verdicts. Review pressure/velocity checks, delivered versus requested demands,
   source balance, pumps/valves and tank levels at each relevant time.
3. Use `civil_fire_flow` with prescribed flow to test the requirement; omit flow
   to find available withdrawal at the specified residual pressure. Cite flow,
   minimum pressure, `limiting_node_id`, the system-wide residual-pressure map,
   convergence and the profile/explicit residual limit.
4. Choose and disclose DDA versus PDA. DDA can deliver requested demand at
   unrealistic low pressure; PDA reduces **base** demand with pressure. Neither
   makes a low-pressure design pass. Fire withdrawal remains fixed in both.
   Fire-flow analysis is at the initial operating/tank state, not endurance;
   separately establish required duration and storage/supply-volume adequacy.
5. Treat unsupported hydraulic controls/emitters/quality or pump/tank forms as
   explicit limitations, not ignored records. Change the real design/modeling
   basis or report the missing capability; never delete essential behavior.

## 4. Storm/sewer flows, conveyance, detention and culverts

1. Define catchments with area/polygon, receiving gravity node, C or CN and Tc
   segments. `civil_watershed` provides DEM-based D8 delineation; inspect the
   outlet position, nodata and catchment boundary against the survey.
2. `civil_hydrology` tc assembles travel times; idf interpolates **user-supplied**
   tables/CSV for the chosen duration and return period. Rational gives peak flow,
   not storage volume. Use scs_runoff/design_storm/unit_hydrograph/
   runoff_hydrograph for event volume and time histories; document applicability.
   Storm `depth_mm` contains interval depths at `t_s` boundaries; hydrograph
   `q_m3s` contains instantaneous ordinates, not interval-average rainfall.
3. Run `civil_gravity_analyze` with the design inflows and proposed ground surface.
   `catchment_peaks=True` sums simultaneous rational peaks at outlets, with no
   network attenuation; do not also provide those same flows as direct inflow.
   Check slope, capacity/depth, velocity, cover, manhole spacing and crown drops;
   inspect node HGL versus rim/crown, surcharge, flooding and tailwater basis.
4. Adverse slopes are not fixed by larger diameters. Correct inverts/direction or
   establish an explicitly supported hydraulic basis. Use `civil_gravity_design`
   first with `apply=False` and actual available diameters; apply only a reviewed
   proposal. `apply=True` changes the site: re-analyze and recheck cover afterward.
5. **HGL/EGL is preliminary steady Manning/K screening**, not SWMM dynamic-wave
   routing or FHWA's access-hole energy-loss procedure. HEC-22 4th ed.
   §9.1.6.6 excludes Table 9.4 K from final EGL design. Passing profile rows is not
   final drainage/code acceptance; name the additional analysis/approval required.
6. Use `civil_open_channel` for normal/critical depth, capacity, standard-step GVF
   or rectangular hydraulic jump checks. These are stateless results: retain the
   inputs and method separately, not as a claimed persisted network solve.
7. `civil_detention_route` needs an inflow hydrograph, monotone stage-storage,
   outlets and initial stage. Derive storage from a terrain/polygon/stage range
   where appropriate. Inspect continuity/mass balance, peak stage/outflow,
   storage, freeboard and timestep sensitivity. Explicitly transfer routed flows
   to downstream conveyance; gravity screening does not route pond storage.
8. `civil_culvert` analyzes a flow or a supplied performance-curve flow list.
   Check entrance geometry, inlet/outlet control, tailwater and road overtopping.
   Disclose its HDS-5 transition approximation and honor low-flow backwater
   refusals; a refused point cannot be omitted to make a curve appear adequate.

## 5. Burial, thrust, supports and real structural evidence

1. `civil_buried_pipe_check` screens soil/live loads, Iowa deflection, ring
   buckling and flotation. Supply verified OD/wall, long-term stiffness,
   embedment/compaction, wheel-load distribution, groundwater and limit/load
   basis. Do not confuse ring EI per unit axial length with beam EI.
2. `civil_thrust_restraint` needs fitting geometry, applicable pressure and
   verified soil bearing/joint restraint data. A stored water pressure is a
   scenario input, not automatically design/surge pressure. Cite force, bearing
   dimensions/restraint length and every applicable returned check.
3. `civil_pipe_supports` operates on the process piping model and converts its mm
   paths. Supply temperature-appropriate properties/allowables, fluid/insulation
   and fitting weights. Cite governing span and per-support reactions/rack loads.
   Coincident supports on separate runs carry separate loads: do not drop them.
   Euler-Bernoulli/tributary-load screening is not ASME sustained/expansion
   acceptance, exact continuous-beam reactions or riser-anchor stability.
4. `civil_fea_handoff(source='thrust'|'buried', target_doc_id=...,
   face_selector=..., site=...)` produces a `civil_to_fea` envelope, not solved
   FEA. Select physical bearing/crown faces, explicit thrust direction or buried
   pressure selection, material and restraints. Execute the target static study
   through the FEA workflow; inspect reactions, mesh/convergence and allowable
   stress/displacement. N/Pa load conversion is explicit; 1 kPa = 1000 Pa.
5. Close the loop: revised block/pipe/support geometry changes loads or restraint;
   update the site/piping model, recompute civil checks, rebuild the handoff and
   rerun FEA before attaching requirements evidence.

## 6. Validate the plant utility connections by coupling, not assumption

1. Confirm physical endpoint and elevations on both sides. Create site `tie_ins`
   with civil network/node, piping document and run/end endpoint, direction and
   elevation offset. Supply endpoint z is civil elevation plus offset, not source
   hydraulic head. Retain ordinary site demands separately from plant exchange.
2. Run `civil_piping_couple` with the actual plant piping model, boundaries/fluid
   and selected tie IDs. Its fixed-point loop exchanges civil supply pressure and
   process flow, preserving site base demands. This requires steady DDA,
   unpatterned/unscaled tie-in demands and incompressible liquid piping.
   Do not silently swap a PDA or extended-period basis to satisfy the tool.
3. Inspect `converged`, iteration history, each tie's head/flow agreement,
   direction/reverse-flow warnings, civil result and piping result. Envelopes
   `civil_to_piping` carry kPa-gauge pressure; `piping_to_civil` carry m³/s demand.
   Meeting iteration-change tolerance alone is insufficient: actual exchange
   agreement must also meet the declared `tol_rel` (default 0.01).
4. Check coupled site pressure/velocity criteria and plant operating/design
   pressure, line losses and stress/support acceptance through the piping design
   workflow. Recompute buried/thrust/support checks and FEA for changed loads.
   Re-couple after editing **either** network. A once-through assumed pressure is
   not proof that the site can supply the plant.
5. This water/piping loop is not gravity-discharge routing. For plant effluent to
   sewer/storm, obtain the real outflow history, apply it explicitly to the
   gravity inflow/detention basis and recheck downstream capacity/tailwater.

## 7. Quantities, estimate, schedule and deliverables

1. After final edits/checks, run `civil_quantities` with final ground and trench
   inputs. Trench requires `outer_diameter_m` or material ID-to-OD data, side
   clearance/width, bedding and sample step. Hydraulic ID/nominal diameter is
   **not** physical OD. Reconcile pipe lengths/materials, structures, excavation,
   bedding/haunch/backfill and stored earthwork; state excluded waste/compaction.
2. `civil_cost_estimate` consumes current qto: `no_qto` means compute it first.
   Users supply unit prices, units, currency, source/date and escalation.
   Report quantity basis, missing-price refusals, contingency and exclusions;
   never substitute remembered regional rates or invent a price book.
3. `civil_schedule` uses user/project task durations, dependencies and element
   assignments for CPM and 4D. Cite critical path/float and inspect assignments;
   optional `day` returns planned state, not measured construction progress.
4. Use `civil_requirement_evidence` to map **current stored check rows** to
   requirement IDs. Review the payload and attach it through the requirements
   workflow; generating a payload does not approve or close a requirement.
5. `civil_export` supports LandXML, GeoJSON/GeoPackage, IFC, EPANET/SWMM,
   qto_csv/cost_csv and msproject. EPANET/SWMM requires an explicit `network_id`.
   Review exchange warnings/skipped/dropped
   elements and scope. IFC 4.3 ADD2 pipe solids are coordination envelopes,
   not wall design, and do not export every site collection. Unknown hydraulic
   head must never become pipe physical elevation. GIS needs a known CRS.

## Evidence rules: what may be claimed

- Pass `doc_id` on calls supporting persistence; verify `saved_to_study`, then
  save the document normally. The `civil_site` study stores `params.model` and
  latest results by analysis key; earthwork has kind/ID subkeys. This is not a
  complete scenario archive.
- Model changes through `civil_site_edit`, imports, grading, alignment or applied
  sizing clear stored results. Finish edits first, then recompute dependent checks,
  coupling, quantities/cost and evidence; never cite a pre-edit result as current.
- For each numerical claim cite the site/network/element, scenario, tool/result
  key, value/units, criterion/reference and method. Include warnings, assumptions,
  omitted checks and screening limitations. Keep FEA and plant-side evidence too.
- `ok:true` is not PASS. Require convergence where applicable, acceptable residuals,
  all applicable check rows passing, no unaddressed capacity/flooding/rating
  failures and evidence for checks the tool omits. Separate profile screening
  from authority-approved/code acceptance and any required independent analysis.
- `converged:false` and reason/hint refusals are engineering evidence, not errors
  to hide. Correct real inputs/design and rerun; if an unsupported method blocks
  acceptance, state that exact limitation rather than claiming completion.
