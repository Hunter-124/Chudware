# Guide: Simulation

```text
SIMULATION GUIDE — FEA + CFD on the live part.

Faces for fixtures and loads are named with the SAME plain-English geometry queries you
build with: a bare side word ("bottom", "+z") or a full query ({"of":"face","side":"bottom",
"pick":"largest"}). Use query_geometry(...) first if unsure what faces a part has.

run_fea_static(fixtures, loads, material) — linear static stress.
  fixtures : list of face queries that are held fixed (encastre). e.g. ["bottom"].
  loads    : list of dicts, each a face query plus a force or pressure:
               {"face": "top", "force": [0, 0, -500]}      # newtons, world XYZ
               {"face": "top", "pressure": 0.5}            # MPa, inward on the face
  material : "steel" | "aluminum" | "titanium" | "stainless" | "brass" | "abs" | "pla"
             (or an explicit dict with E_mpa / nu / density_g_cm3 / yield_mpa).
  Returns max von-Mises stress (MPa), max displacement (mm), and the min factor of safety
  (yield / max stress). FoS > 1 means it holds; design to a target margin.

run_fea_modal(fixtures, material, n_modes) — natural frequencies (Hz) of the held part.

run_fea_fatigue(fixtures, loads, material, load_type, theory) — high-cycle FATIGUE life.
  Reuses the static stress field, then applies the mean-stress theory (goodman/gerber/
  soderberg/asme/morrow) + Marin endurance correction + Shigley S-N line to report cycles-
  to-failure and a factor of safety. load_type "reversed"|"zero_based"|"ratio" sets the
  cyclic spectrum. regime="infinite" ⇒ runout (>1e6 cycles). Same FEA backend as run_fea_static.

run_fea_thermal(hot_faces, cold_faces, material) — steady heat conduction on the real
  part tet mesh using the engine. Temperature faces carry {face, temperature °C}.
  Flux/convection loads are supported; a fixed temperature or convection anchors the
  datum. Returns temperature extrema, flux_max_w_m2, mesh_quality/provenance; vtu_path
  writes the mesh with nodal temperature. No time axis and no external solver.

run_fea_thermal(..., transient={...}, loads=[...]) — TRANSIENT thermal on the REAL part mesh
  (C++ core, backward Euler). transient={"t_end_s":60, "dt_s":0.5,
  "t_init_c":20, "frame_times_s":[10,30,60]}. hot/cold faces are Dirichlet held for the run
  (may be empty — flux-only heat-up is fine). loads carry time-varying heat sources:
    {"face":"top", "flux_w_m2":5000, "schedule":{"duty":0.25, "period_s":1.0}}   duty cycle —
       the direct shape for an aicad.powerprofile/1 per-rail duty profile
    {"face":"top", "flux_w_m2":5000, "schedule":{"points":[[0,1],[2,0.2]], "period_s":4}}
       piecewise sample-and-hold; each load keeps its OWN independent schedule
    {"face":"left", "h_w_m2k":25, "t_amb_c":20}                                 convection film
  Result: summary.max_temp_c = the PEAK over the whole run (max-temp envelope) at
  summary.max_temp_time_s, plus per-frame max temps at the requested frame times. Use this to
  check duty-cycled electronics/heater loads that never reach the 100%-on steady state.

run_fea_buckling(fixtures, load, material, n_modes, size_mm=8, vtu_path=None,
  max_load_factor=10000) — engine
  linear eigenvalue BUCKLING on the real CAD tet10 mesh, with static face/load syntax.
  modes contain load_factor and participation; critical_load_factor<1 buckles below the
  reference load. Read critical_load_n, mesh_quality and provenance.solver="engine".
  No mode below max_load_factor is refused, not infinite strength. Refine size_mm
  for convergence; VTU carries mode shapes.

run_fea_contact(fixtures, load, material, max_gap_mm, max_iterations, mesh_size_mm,
  mesh_order) — FRICTIONLESS UNILATERAL CONTACT FEA on the document's own meshed bodies.
  Surface pairs facing across <=max_gap_mm carry compression only; an active-set loop opens
  and closes them until the set settles. For slip-fit pins, bolted/shimmed stacks, seated
  parts. Returns the run_fea_static envelope plus "contact"
  {candidate_pairs, active_pairs, contact_iterations, converged, held_by_contact_only,
  pairs:[{bodies, contact_force_n, force_vector_n, max_contact_pressure_mpa, ...}]}.
  No friction, no large sliding, no preload (apply the clamp force as a face load). A load
  that pulls the parts apart is refused as a mechanism. In-house engine, no ccx. Bonded
  (tension-carrying) joints: run_fea_static with study bond_gap_mm — mutually exclusive.

run_fea_frequency_response(fixtures, excitation_dir, sweep_hz, zeta, base_accel_g,
  psd_breakpoints, vibration_spec, ...) — harmonic + random vibration response.
  Answers "does this part resonate and fail under the vibe spec", not just "what are its
  natural frequencies". Builds on run_fea_modal's modes via modal superposition:
    Harmonic: SDOF-per-mode transmissibility sweep → response acceleration (g) vs frequency
              (peak_accel_g, peak_freq_hz). base_accel_g sets the input amplitude.
    Random:   Miles' equation per mode (Steinberg Eq. 6.6) → per-mode + total GRMS.
              REQUIRES psd_breakpoints=[[f_hz, g2_per_hz], ...] — with no PSD given the
              random field degrades to {ok:false, reason:"no_psd_provided"} (it NEVER
              assumes a default vibration profile).
  vibration_spec {"max_peak_accel_g": g, "max_grms": g} → pass/fail verdict (DAG-gateable).
  zeta damping default 0.02 (2%) — an ASSUMPTION, always echoed in the result.
  Response is acceleration amplification only: mode shapes are unit-normalized, so physical
  displacement/stress amplitudes are NOT recovered (see result.limitations).

HUMAN HYDRAULICS (Simulate -> Hydraulics):
  New Circuit -> Start: choose a power unit, cylinder circuit, motor circuit or blank.
  Nonblank templates include connected components and editable simulation setup. Drag components
  from the canvas toolbar onto the schematic; click two compatible ports or drag between
  them to connect immediately. Select a component or hose and press Delete; Ctrl+Z undoes.
  Circuit -> Design -> Simulate -> Results separates editing, sizing, setup and outcomes.
  Ribbon Simulate runs when ready and opens Results -> Outcome; otherwise it shows the
  setup blocker. Results also contains BOM and To Piping.
  3D Layout shows shaded component glyphs and pressure-coloured hoses in the native viewport.
  Its plane, offset, grid, snap and placement card appears only in 3D view; the selected
  component editor also retains 3D position fields and Place in 3D.
  Positions persist in params.layout3d; moving a component preserves simulation results.
  Agents share that layout through hydraulic_layout_3d. Glyphs are not manufactured CAD.

run_cfd_flow(velocity, aoa, fluid) — LBM wind tunnel over the whole part/assembly.
  velocity m/s, aoa degrees, fluid "air"|"water". Returns drag (N), lift (N), Cd, frontal
  area, and Reynolds number. Coarse grid by default — a directional estimate, raise `cells`
  for more resolution.
  CONVENTIONS (Z-up): freestream +X, lift +Z, span Y; +aoa pitches nose up. `reynolds`/`cd`
  use the part's LONGEST span as reference length (reported as `ref_length_mm`).
  TRUST THE DIAGNOSTICS: the result reports `voxel_mm` / `min_feature_voxels` (thin features
  under ~8 voxels are unresolved → lift reads ~0, a `warning` is set — raise `cells` or run the
  thin part alone) and `converged` / `residual` (a non-converged run is under-iterated, not
  physically zero). For heavy runs pass background=True and poll get_job(job_id).

run_cfd_accurate(velocity, aoa, fluid, preset) — HIGH-FIDELITY OpenFOAM RANS (k-omega SST + prism
  layers): STL export → snappyHexMesh body-fit → simpleFoam steady RANS, all via OpenFOAM
  subprocesses (GPL isolation — never linked). The TRUSTWORTHY absolute Cd/Cl path (validated:
  sphere/cube land in the documented ±10-15%-class experiment bands, see tools/foam_validation.py)
  vs run_cfd_flow's coarse LBM estimate. Requires OpenFOAM — detected automatically on Linux
  (sourced env / system / user-space conda env), native Windows blueCFD, or WSL; if absent the
  tool returns an honest {ok:false} with the provisioning command
  (`python -m tools.provision_openfoam --install` — user-space on Linux, no sudo). Runs for
  MINUTES, always background → poll get_job(job_id) (live meshing/solve progress, like the LBM
  path). preset coarse|medium|fine|xfine. Use run_cfd_flow first to scan trends fast, then
  run_cfd_accurate to certify the chosen design.
  Conjugate-heat-transfer variant (buoyant chtMultiRegionFoam) lives at the engine seam:
  engine.sim.openfoam.run_openfoam_tunnel(thermal={enabled, mode:"conjugate", inlet_temp_c,
  solids:[{name, stl_path, kappa_w_mk, power_w}, ...]}) — multi-solid regions with per-solid
  volumetric heat sources (board + components handoff).

Heavy solves: run_cfd_flow / run_fea_* accept background=True (run_cfd_accurate is always
background) → return a job_id immediately; poll get_job(job_id) for the result. This keeps a long
solve from tripping the transport timeout (which would otherwise drop the session). After a
restart, recover_parts() reloads auto-saved docs.

CROSS-SIM HANDOFF (aicad.simhandoff/1 — contracts/sim_handoff.md):
  When one physics domain feeds another (1D piping → 3D CFD, hydraulic → piping /
  hydraulic → FEA, motion → FEA and ECAD → thermal FEA today; thermal → CFD
  conjugate as it lands),
  the coupling follows one seam: a HANDOFF tool solves the source domain, derives geometry
  + boundary conditions with explicit units and provenance, and runs (or stages) the target
  study; a VERIFY tool then cross-checks a shared quantity between the two solvers at a
  tolerance and returns a PASS/FAIL verdict.
  Conforming tools embed a validated envelope under the reserved "simhandoff" key of their
  result: kind:"handoff" carries {source/target study refs, geometry[], conditions[] (each
  {name, kind, value, units, applied_to, provenance}), expectations[]}; kind:"verify"
  carries {checks[] ({quantity, units, source_value, target_value, deviation_pct,
  tolerance_pct, pass}), pass, verdict, advisories}. Study refs {domain, tool, doc_id?,
  study?, handoff_id?} let any sim result name exactly what produced it.
  Proven pair: piping_cfd_handoff (one-call 1D solve → bore STEP → run_cfd_flow with
  1D-derived BCs) then piping_verify_cfd (1D vs 3D pressure-drop verdict + velocity-limit
  advisories). Hydraulic pairs: hydraulic_piping_handoff/hydraulic_verify_piping (hose
  runs → piping 1D solve, real line losses back into the pump margin; doc-to-doc — the
  generated piping doc rides INLINE in the envelope, consumable via
  piping_solve(source_handoff=...)) and hydraulic_fea_handoff/hydraulic_verify_fea
  (cylinder mount loads at max pressure → case-labelled static-FEA load cases).
  motion → FEA (LIVE): motion_fea_handoff(component, fixtures, stop=…) runs the motion
  study WITH rigid-body dynamics (simulate_motion dynamics={} — mass/inertia from
  geometry x density, gravity/loads/damping, joint reactions via the mate constraint
  jacobian), picks the worst-case frame (or `at`=time s), applies the joint reaction
  forces on the selected component's mate faces and runs run_fea_static on it — "is this
  link strong enough at the worst instant" in one call. Conditions are `case`-labelled
  per frame; dry_run=True builds loads + envelope without solving.
  ECAD → thermal FEA (LIVE): ecad_thermal_handoff(environment='still_air'|
  'forced_air'|'cold_plate'|custom h/t_amb, include_trace_heating=…) takes the ACTIVE
  routed board to a solved board-level thermal study in one call — fused board+component
  FEA solid with the FR4/copper stackup HOMOGENIZED to one effective material (in-plane
  rule-of-mixtures k, every assumption listed in the result), one heat_source condition
  PER component from its boardphys power metadata (components without power_w get NO
  source and are named in coverage.unpowered_refs), optional PDN I2R trace heating from
  ecad_run_pdn as distributed flux, environment-preset convection, quasi-steady core
  transient solve, then per-component case temp + Tj (case + P*theta_jc) map-back.
  ecad_verify_thermal() gives the per-component Tj-vs-limit margin table and a theta_ja
  hand-calc cross-check at a GENEROUS tolerance (JEDEC theta_ja assumes a standard test
  board; minor power contributors get advisories, not pass/fail — neighbor heating is
  invisible to theta_ja). dry_run=True builds geometry + BCs + envelope without solving.
  ECAD → CHT airflow (LIVE, coupling ecad_to_cht): when the convection PRESETS
  aren't enough (enclosures, fans), ecad_airflow_handoff(inlet_mps=…, inlet_temp_c=…,
  enclosure={"clearance_mm":10,"wall_mm":2}?) exports the board slab + every POWERED
  component as separate watertight STL solids and runs OpenFOAM chtMultiRegionFoam via
  the multi-solid CHT seam (per-region volumetric heat sources; optional passive
  4-wall enclosure duct open along the flow axis), mapping per-region mean/peak °C back
  to component refs. ecad_verify_airflow() then answers "was the film-coefficient
  assumption sane?" — a per-component preset-h vs CHT rise deviation table with
  guidance + an effective-h re-run suggestion whenever the preset was OPTIMISTIC.
  Without OpenFOAM the handoff degrades honestly (skipped:true + full envelope) and
  offers the LBM wind tunnel as a qualitative-flow ADVISORY, clearly labeled
  NON-THERMAL. Budget: even the 'coarse' preset is minutes of meshing + solving.
  ECAD → structural FEA (LIVE): ecad_vibe_check(band_lo_hz=20, band_hi_hz=2000,
  psd_breakpoints=…, mount_holes=…) — vibration reliability screen of the mounted board:
  reuses the ecad_thermal_handoff fused board+components solid with a mass-corrected effective
  density (component masses from package dims x a mold-compound density assumption, or
  eng mass metadata), detects mounting holes (mounting-hole footprints / NPTH pads /
  large un-netted plated holes, or explicit mount_holes=[[x,y],…]) and fixes them, runs
  the core modal solve, FLAGS every natural frequency inside the excitation band
  (default 20-2000 Hz random-vibe screening), then estimates the 3-sigma board
  deflection at the worst in-band mode (Miles' response, Z=9.8*G/f^2) and screens each
  component against Steinberg's 0.00022*B/(C*h*r*sqrt(L)) deflection limit — a CITED
  HEURISTIC for solder-joint fatigue risk, not a life prediction. Default PSD is a
  flat 0.04 g^2/Hz screening level (echoed as an assumption); pass the qualification
  spectrum for a spec-true screen. dry_run=True builds mounts + mass model + envelope
  without solving.
  HEATSINK SELECTION: heatsink_select(component_ref, tj_target_c |
  t_case_target_c) closes the "pick a heatsink" loop on the LAST ecad_thermal_handoff
  study. Candidates = curated parts-library heatsinks (part_search domain "thermal",
  datasheet-class theta_sa + mass + cost) PLUS a parametric plain-finned aluminum
  generator sized to the component footprint (fin-efficiency theta_sa estimate;
  buildable via services.mcp.heatsink.sink_ops engine ops). Each candidate is
  "mounted" as bonded contact through a TIM bondline (default 0.1 mm @ 3.0 W/m-K,
  override tim_t_mm/tim_k_w_mk) and RE-SOLVED sequentially: because the core solver
  is single-material, the sink becomes an effective convection
  h_eff = 1/((R_tim + R_sa)*A_top) over the component top — board geometry is
  unchanged, so the baseline board MESH IS REUSED per candidate and board spreading +
  neighbor heating stay in the solve (sink-base overhang spreading is not modeled —
  conservative). Returns a ranked table {candidate, achieved_tj_c,
  delta_vs_target_c, mass_g, cost_usd}; winner = lightest candidate meeting the
  target, its study kept, result carries a verify envelope (coupling
  ecad_heatsink_to_thermal_fea). IMPOSSIBLE targets are honest: target_met=false with
  the best achievable candidate named; without a sim-enabled libchudware it degrades to
  a flagged network estimate (Tj = Ta + P*(theta_jc + R_tim + R_sa)).
  Helper builders/validator: services/mcp/simhandoff.py.

SYSTEM POWER BUDGET (aicad.powerbudget/1, tool: power_budget):
  Bookkeeping (NOT SPICE) rail-tree energy budget composing three seams: an
  aicad.boardphys/1 sidecar (ecad_to_3d — per-component supply-rail currents, sequencing,
  optional PDN block from ecad_run_pdn) supplies the LOADS; an aicad.powerprofile/1
  artifact (fw_sim_run / fw_power_profile — firmware emulation duty cycle) supplies the
  WEIGHTING; you supply the TREE: source {battery|supply, voltage_v, capacity_mah} →
  converters [{input, output, topology ldo|buck|boost, efficiency: fixed eta or
  [[load_frac, eta]] curve points, i_out_max_a}]. Verdict: battery life for the scenario,
  per-rail avg/peak vs converter rating with margins + worst-rail warning, PDN
  cross-check (copper drop / hotspots / stale-analysis), sequencing sanity note.
  No firmware profile → DEGRADED mode: datasheet-typ currents assumed always active,
  explicitly labelled; unknown currents stay null (sums become labelled floors), every
  number carries provenance. Also runs inside verify_battery as the 'power_budget'
  check (kwargs power_source / power_converters / board_path / power_profile_path).

GOLDEN DEMO (living E2E worked example):
  scripts/demo_sensor_node.py chains the full board → firmware → thermal → power surface
  on an STM32F4 sensor-node board: verification ladder (ERC/DRC/hostcheck/sicheck/FPGA/
  PDN) → firmware project + co-verify (fw_sim_run with fixture:i2c_temp_tmp103 when
  Renode is present) → ecad_thermal_handoff → heatsink_select(U1, tj_target_c=85) →
  power_budget → make_report. Run by scripts/gate.sh as the sensor-node demo stage
  (CHUDWARE_SKIP_SENSOR_DEMO=1 to opt out). Narrative + artifacts:
  docs/reference/SENSOR_NODE_DEMO.md. Prefer this script over
  inventing a new end-to-end recipe when demonstrating the content is finished.

TOPOLOGY OPTIMIZATION (study type "topology"; Simulate workspace → Topology ribbon tab, and
  the model tools topology_capabilities / run_topology_optimization). Both surfaces edit the
  SAME persisted study and run the same native sim_run_topology route. It searches for a
  material layout inside the part's current solid (the design domain) under a volume budget.
  The source part is only read (its exported STEP is hashed); it is never replaced.

  Capability matrix (read it live — the GUI Method form and topology_capabilities show the
  exact supported pairs, objectives, limits and parameters; never assume beyond them):
                     structural thermal modal buckling flow thermomechanical cht
    simp, ramp           yes      yes    yes    yes     yes        yes        yes
    beso, level_set      yes      yes    no     no      no         no         no
  BESO and level set are volume-budget only: compliance (structural) or thermal_compliance
  (thermal), no response limits, no mass objective. An unsupported pair shows its reason and
  cannot run. Formulations: SIMP rho^p / RAMP rho/(1+q(1-rho)) density interpolation with MMA
  updates and optional tanh projection; BESO = Huang-Xie soft-kill with binary removal AND
  reintroduction; level set = reaction-diffusion level set (phi in [-1,1], regularized
  Heaviside, linear ersatz material).

  Objectives (first = default; reported units) and optional limits (SIMP/RAMP only):
    structural        compliance N*mm | mass g        stress_limit_mpa, max_displacement_mm
                      (mass needs one of those limits)
    thermal           thermal_compliance W*K          max_temperature_c
    modal             frequency Hz (maximized)        min_frequency_hz
    buckling          buckling_factor (maximized)     min_buckling_factor
    flow              dissipation W                   max_pressure_drop_pa
    thermomechanical  compliance N*mm                 stress_limit_mpa, max_displacement_mm,
                                                      max_temperature_c
    cht               mean_temperature C              max_temperature_c, max_pressure_drop_pa
  Stress and max-temperature limits act on smooth (KS) aggregates of the relaxed design; the
  reanalysis of the reconstructed geometry (below) is what checks them on real geometry.

  Scalar settings (topology{}; defaults in parentheses; the pair's `parameters` list wins):
    volume_fraction (0,1] REQUIRED — budgets SOLID, except flow/cht where it budgets FLUID
    (rho = 1 is always solid). max_iterations 1..100000 (200); change_tolerance > 0 (0.01);
    filter_radius_mm >= 0 (absent: min_member_mm/2, else 2 x median element size);
    density_threshold (0,1) (0.5); min_density (0,1) property floor (1e-4). SIMP/RAMP:
    move_limit (0,1] (0.2), projection_beta >= 0 (0 = off). penalty >= 1 (3; SIMP, BESO);
    ramp_q >= 0 (5; RAMP); beso_evolution_rate (0,1) (0.02); level_set_regularization >= 0
    (5e-4). Clearing a GUI field erases the key, so the solver default applies.

  Units (converted once to SI inside the solver): geometry mm; force N; pressure and stress
    MPa; temperature C; heat W, W/m2, W/(m*K), W/(m2*K); flow m/s, Pa, Pa*s, kg/m3; material
    density g/cm3, alpha 1/K, cp J/(kg*K). Mesh is tet4 only (mesh_order 1; other orders are
    refused, never silently changed). Material keys required: structural/buckling E_mpa, nu;
    modal + density_g_cm3; thermal/cht k_w_mk; thermomechanical E_mpa, nu, k_w_mk,
    alpha_per_k; flow none (fluid properties live in flow{}).

  Setup by physics (sections a physics does not use stay saved but are not sent):
    structural / buckling / thermomechanical: fixtures [{faces, dofs?:[0,1,2]}] (no dofs =
      all) and loads {faces, type:"force", vec:[N,N,N]} | {faces, type:"pressure",
      magnitude: MPa inward}. Multi-load: load_cases [{name, weight > 0, fixtures?, loads}]
      add case fixtures to the shared ones; the objective is the weighted sum (buckling
      soft-mins the smallest positive load factor over cases, weights 1). Buckling needs a
      compressive pre-stress. Modal uses fixtures only (saved loads are kept, not sent).
    thermal / thermomechanical / cht: thermal{reference_temperature_c, fixtures [{faces,
      temperature_c}], loads {type:"flux", magnitude_w_m2} | {type:"power", power_w} |
      {type:"convection", h_w_m2k, ambient_c}}. Thermomechanical requires
      reference_temperature_c (the strain-free temperature).
    flow / cht: flow{viscosity_pa_s, density_kg_m3, inlets [{faces, velocity_m_s:[3],
      temperature_c (cht only, required there)}], outlets [{faces, pressure_pa}], walls?,
      brinkman_max (1e5), reference_length_mm (default: equivalent inlet diameter)}; cht adds
      fluid_k_w_mk and fluid_cp_j_kgk, and its heat input goes in thermal.loads. Every
      exterior face outside a port is a no-slip wall; walls overlapping a port refuse.

  Passive regions and ties:
    preserve (fixed solid) / exclude (fixed void): {faces, thickness_mm?} = elements touching
      the faces plus those within the thickness (default topology.preserve_thickness_mm /
      exclude_thickness_mm, 0), or {box_min_mm, box_max_mm} by element centroid. An element
      in both refuses (passive_conflict).
    Kept solid automatically: structural fixture and load patches; thermal fixture and load
      patches for thermal and thermomechanical (not cht). Kept fluid: flow inlets/outlets.
      A boundary inside an exclude refuses (boundary_in_keep_out), a port inside a preserve
      refuses (port_in_preserve), and fixed regions already above the budget refuse
      (volume_fraction_infeasible).
    symmetry_axes (+ symmetry_origin_mm, default bounding-box centre) and extrusion_axis tie
      design variables. A tied group touching a passive region becomes passive; one spanning
      fixed solid and fixed void refuses (tie_passive_conflict); a mesh without mirror images
      within the local element size refuses (symmetry_domain_mismatch).

  Manufacturing: intent vs enforcement vs measurement (result.manufacturing):
    intent only — min_member_mm sets the filter radius; member thickness is NOT measured.
    enforced — symmetry/extrusion ties; no_enclosed_voids (BESO and level set reject updates
      that enclose a void; SIMP/RAMP treat it as a feasibility check).
    heuristic — build_direction + overhang_angle_deg + overhang_penalty penalize unsupported
      material; this never guarantees a support-free part.
    measured on the reconstructed geometry — closed manifold surface, single body, passive
      regions kept, enclosed cavities, unsupported-overhang fraction, reanalysis response
      limits; each check row reads pass | fail | not_measured.

  Reading a result:
    ok = the computation finished, not a certified design. converged = a stationary design
    that also satisfies every constraint. feasible = the final re-evaluated constraints (and
    the enclosure rule) hold. An iteration limit, stall or violated limit is a finished but
    unconverged or infeasible run. termination: converged | max_iterations | stalled |
    line_search_failed | infeasible | stuck_at_bounds | cancelled | failed. Objective and
    fields are re-evaluated at the FINAL design (summary.fields_at); summary carries baseline
    vs final objective, volume fraction and mass, max_constraint_violation,
    convergence_criterion and iterations. Report units: mm, N, MPa, C, W, Pa, Hz, g.
    Thresholded actual-domain reanalysis: a completed design is cut at density_threshold
    into a closed surface plus conforming clipped tets, the study's ORIGINAL selectors are
    resolved again on that new mesh, and the physics is solved once on it (solid physics:
    the clipped solid; flow: pure Stokes in the clipped fluid channel; cht: the two-phase
    solid + fluid mesh, solid still a Brinkman resistance). See reconstruction{},
    summary.reanalysis_objective / reanalysis_feasible / geometry_valid and the
    reconstruction_response check. A boundary that does not survive reconstruction is a
    named failure, never repaired. Unfinished runs are not reconstructed.

  Artifacts are created exclusively, SHA-256 hashed and never overwritten: run.json,
    study.json, normalized_spec.json, history.jsonl / history.json, checkpoint.json,
    density.vtu, reanalysis.vtu, overlay.aicm (viewport), surface.stl (only when the geometry
    is valid), result.json. Model-tool runs live under
    <project>/simulation/topology/<study>/<run_id>/ (or a project-relative
    topology.result_dir) with source.step, inputs/setup/provenance JSON. checkpoint.json holds
    retained optimizer state, but sim_run_topology takes no resume input: a cancelled,
    failed or killed run starts over from the beginning; its checkpoint iteration is only reported.
    reanalysis.vtu has a point field von_mises[@case] (MPa) and the cell field
    element_von_mises[@case]. To animate it, use render_animation(kind='fea', vtu_path=...).

  Human workflow (Topology tab): New Study (needs an open part with solid geometry) → Method
    (physics, method, objective, volume budget, limits; only supported pairs) → Material →
    Fixture / Force / Pressure / Heat / Flow (pick faces in the viewport; load cases in the
    boundary panel) → Preserve / Exclude → Mfg. Rules → Mesh → Run (progress, Cancel) →
    Results: convergence history, constraints, manufacturing checks, artifacts. "Show in
    viewport" overlays density/fields; Export copies a run file to a path you choose
    (surfaces only when geometry is valid); Compare sets a baseline run (metrics compare only
    for the same physics, objective and units); "Use these settings" restores a run's scalar
    settings. Every run is retained in the study, and the setup stays editable (locked only
    while a run is active). Reopening or renaming unchanged geometry keeps retained runs
    current; runtime document IDs and study history are excluded from the source digest.
    Runs made on other geometry are marked stale; model-tool runs are flagged unverified
    against the editor's geometry digest.

  Return to CAD: Results → Result geometry and export → Continue in CAD. A valid,
    available, non-stale surface opens as a separate solid in Model / Feature Manager;
    the source part and retained study remain unchanged. Add or cut features, then save
    the new part as .step+. The imported base is faceted geometry, not recovered sketches
    or smooth parametric history. Coplanar triangles share planar faces for selection.
    Solver convergence and feasibility do NOT establish valid reconstructed geometry;
    disconnected/invalid results cannot continue in CAD. DFAM checks remain separate.

  Model workflow: topology_capabilities → run_topology_optimization(study=…, background=True)
    (saved into the document as type "topology" BEFORE the solve, so the GUI shows it) →
    wait_job (standalone tool call, not a long-running eval cell) / get_job →
    sim_cancel(job_id) to stop → read ok / converged / feasible separately,
    result.constraints, the reanalysis, manufacturing checks and artifacts.
    run_topology_optimization(study_id=…) re-runs a GUI-authored study exactly as stored.

  Model limits:
    flow / cht: steady incompressible Stokes-Brinkman on MINI elements. Refused
      (flow_regime_invalid) when the prescribed-inlet Reynolds number exceeds 1 or when the
      solved velocity bound does. That covers pressure-driven flow the inlet check cannot see.
      No Navier-Stokes, no turbulence. Solid is a Brinkman resistance with finite leakage,
      reported (cht: thermal.heat_leakage). More than 1.5 M flow unknowns refuses
      (problem_too_large).
    cht: steady advection-diffusion with a constant fluid heat capacity rho_f*cp_f on the
      superficial velocity (energy-conservative) and interpolated conductivity; one-way
      flow → heat coupling.
    thermomechanical: one-way steady coupling (temperature drives thermal strain), linear
      elastic.
    All physics are linear and small-strain: no contact, plasticity, geometric nonlinearity,
      post-buckling or design-dependent convection.
    The output is a mesh surface plus fields, NOT a reconstructed parametric CAD feature
      history.
    print_ready stays false. A DFAM check of the surface (model-tool runs) is evidence, not
      a print gate, and nothing is sliced automatically. Slice the exported surface.stl with
      slice_part; only its passing G-code check makes a part print-ready.

SIMULATE WORKSPACE (host GUI): every live simulation family has guided setup plus
  exact numeric fields and an expandable native-parameter editor. Gestures are
  additive conveniences, not substitutes for solver parameters:
    Structural / modal / buckling / contact / fatigue / frequency-response /
      thermal FEA: drag a model face into a boundary well, or drag a compatible
      fixture/load/thermal tool onto a face. Boundary chips identify targets;
      exact vectors, magnitudes, materials, mesh and solver controls remain editable.
    Topology: target wells cover all supported physics, preserve/exclude regions
      and manufacturing controls; the complete native definition stays editable.
    External flow / thermal CFD: fluid and surface palettes, direction/axis
      controls and drag-adjustable operating values retain exact custom inputs.
    Motion: click or drag a mate into a driver, reorder drivers, and edit exact
      sweep/dynamics controls. Driver identity follows the mate, not its row index.
    EM: click/drop waveguide presets or an STL, then edit custom geometry,
      excitation, spectrum, ports, boundaries and supported solver controls.
    Electronics: device/source/probe palettes complement exact operating-point,
      DC, AC and transient settings; PCB thermal and heatsink handoffs retain
      their native mesh, schedule, source and thermal-interface parameters.
    Firmware: the simulation panel and Firmware workspace share editable native
      scenarios, event tracks and assertions; JSON files can be loaded/dropped.
    Hydraulics: choose a Start template (power unit, cylinder circuit, motor circuit)
      or blank; drag components from the canvas toolbar and click two ports to connect.
      Delete removes a selection; Ctrl+Z undoes. Ribbon Simulate runs when ready and
      opens Results. The 3D Layout placement card appears only in 3D view; selected
      component editors retain 3D position fields. Components, hoses, fluid, simulation
      settings and handoff parameters remain fully editable.
    Piping: drop model faces into route endpoint wells; click/drop named ports
      into network/internal-flow boundaries; vessel and duct presets retain
      custom geometry, operating conditions and native solver definitions.
  Apply validates and commits a native draft atomically; Revert reloads the
    current definition. Invalid/stale drafts are retained for correction, not
    silently substituted. Unknown metadata and inactive settings survive edits.
    Native run requests use the committed supported parameters; document/source
    identity and resource-governor limits are not user-overridable solver fields.
  Background capability/project discovery does not interrupt a drag or Apply.
    Active document requests and solves still block conflicting setup changes.
  Leaving solver results or changing their setup restores the editable model
    and face picking. Temporary result meshes never replace the part's model cache.
  Drag payloads are fenced to their originating document/study or panel
    generation. Repeating the same face in the same boundary role does not
    duplicate its load; dropping it into a different role is an independent BC.
  Face unions use {facesets:[...]} with nonempty terminal children, preserving
    each child's points/tolerance/normal/surface rather than flattening disjoint
    samples into one envelope. One authored force stays one force over the union.
  Setup remains editable when an optional backend is unavailable; Run reports
    that capability limit rather than changing physics. Thermal CFD needs
    OpenFOAM; full-wave openEMS modes need openEMS. Contact/proxy, qualitative
    flow and firmware electrical-model limitations remain explicit in results.
  Multiphysics handoffs use aicad.simhandoff/1. Worker seam:
    services/sim/multiphysics_rpc.py (sim_caps / sim_run_contact /
    sim_freq_response / ecad_thermal_handoff / heatsink_select / piping_verify_cfd).
```

