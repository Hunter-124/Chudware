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
   FEA. Translate each selected condition into `run_fea_static` loads: preserve
   force vectors in N, divide pressure in Pa by 1,000,000 to obtain MPa, and select
   the physical bearing/crown face and appropriate sign (+ pressure acts inward,
   - outward). Provide material and restraints. The static tool has no
   `source_handoff` parameter. Execute the target study and inspect reactions,
   mesh/convergence and allowable stress/displacement; 1 kPa = 1000 Pa.
5. Close the loop: revised block/pipe/support geometry changes loads or restraint;
   update the site/piping model, recompute civil checks, rebuild the handoff and
   rerun FEA before attaching requirements evidence.
