"""cad_guide topic texts + the cad_guide_text(topic) dispatcher (T-RF8 split of cad_tools.py).
Consumed by the cad_guide tool (cad_doc_tools), tools/manual_gen.py, and re-exported
through the cad_tools facade."""

from pathlib import Path
from .cad_common import cb  # noqa: F401
import os


# --------------------------------------------------------------------- the guide text

_GUIDE_PROMPTS = ["overview", "review", "simulation", "motion"]


def _prompts_dir() -> Path:
    d = os.environ.get("CHUDWARE_PROMPTS_DIR")
    if d and Path(d).is_dir():
        return Path(d)
    # Fall backs: the staged dist prompts, then the repo source prompts.
    staged = cb.dist_dir() / "app" / "services" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return cb._repo_root() / "services" / "agent" / "prompts"


def _read_prompt(rel: str) -> str:
    p = _prompts_dir() / rel
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


_SIM_GUIDE = """\
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
  power_budget → make_report. Wired into scripts/gate.sh as stage 4.7
  (CHUDWARE_SKIP_FIN25=1 to opt out). Narrative + artifacts:
  docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md. Prefer this script over
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
"""

_MOTION_GUIDE = """\
MOTION / KINEMATICS GUIDE — make an assembly MOVE, and check it clears as it goes.

Load cad_skill(name="simulate") before mechanism work. simulate_motion drives one or more
synchronized angle/distance mates and re-solves the graph at each computed sample. It returns
body world poses plus per-frame overlap/clearance findings, not continuous-path proof.

EVIDENCE / COST FIRST
  * Guard ok:false before indexing trajectory: refusals/timeouts need not contain it. Never
    fabricate a trajectory. ok:true means completed, NOT collision-free.
  * Read EVERY computed frame's collision_detail/collisions. Top-level interference.detail
    is FIRST HIT ONLY, never a union; baseline overlaps are not automatically permitted
    joints. Justify accepted pairs separately, rather than hiding them as known contacts.
  * Sampled-clear requires check_interference=true, interference.complete=true,
    frames_checked==steps, all frames converged/pose-valid and no hits. Missing geometry,
    failed pairs, null unchecked evidence or failed poses are incomplete, not clear.
  * min_clearance_mm=0 checks overlaps only. A positive threshold requests expensive exact
    distances for candidate pairs below it, not a cheap tolerance adjustment or a global
    minimum-distance table. Start with a short overlap sweep; budget clearance deliberately.
  * Print the existing result before rerunning slow work. status="still_running" /
    in_flight=true after timeout means not cancelled: an identical call in the same run
    joins the original work, never restarts it. Do not change args to duplicate live work.

SCOPE: kinematics by default — position (and, by sampling, velocity). Pass dynamics={...} (even
{}) to ALSO run RIGID-BODY INVERSE DYNAMICS on the same sweep: mass/inertia from each component's
geometry x density (densities/masses overridable per component), gravity, applied forces/torques,
simple viscous damping, and per-mate JOINT REACTION forces/moments solved through the mate
constraint jacobian (per-frame min-norm least squares; V&V'd against analytic pendulum +
slider-crank reactions). The result gains a `dynamics` block with per-frame reactions,
per_joint_worst and worst_case. Contact dynamics is still out of scope.

MOTION → FEA: motion_fea_handoff(component, fixtures, stop=…, at="worst"|t_s) chains it —
worst-instant joint reactions become force loads on that component's mate faces and
run_fea_static answers "is this link strong enough at the worst instant" in one call,
conforming to aicad.simhandoff/1 (see cad_guide('simulation'), CROSS-SIM HANDOFF).

HOW TO USE
  1. Build an ASSEMBLY with asm_add_component + asm_mate, ground the base, then
     solve_mates. Every moving joint needs a mate: a driven revolute angle uses
     value={"value":0,"axis":[ax,ay,az],"axis_point":[px,py,pz]}
     from the measured WORLD-space hinge line. Concentric+coincident seat the
     pivot; an angle/distance mate defines the scalar you drive. Fixed
     interfaces use coincident/concentric/lock mates.
     `at` is only a starting pose, not a persistent joint relation.
  2. simulate_motion(mate_id="m3", start=0, stop=180, steps=48) sweeps that
     joint's range with interference at each sample; repeat for each joint.
     Use drivers=[{"mate_id":"m3","start":0,"stop":180}, ...] for a synchronized
     multi-joint sweep. Do not hand-roll forward kinematics in Python or build
     throwaway pose assemblies.
  3. Read trajectory[i]: bodies[j].transform is row-major world 4x4; converged,
     max_residual/worst_mate/tolerance, dof/residual_dof and pose_check describe that pose.
     collision_detail has stable component_a/component_b, overlap_volume_mm3 for overlaps,
     or distance (mm) and warning="below min clearance". collisions contains the same
     hits with overlap_volume_mm3 defaulted to zero: zero volume can still be unsafe.
     interference is true for known hits, false only for complete clear checks, or null
     for unchecked/incomplete checks without known hits; interference_complete,
     pairs_checked/pairs_reused, pair_failures and geometry_failures expose coverage.
     Counts/geometry_failures may be absent on unchecked frames. Reused pair results
     (reused=true) at unchanged relative poses count toward pairs_checked, not missing work.
     Known hits survive incomplete checks. The loop playback copy is not another check.
     interference.first_collision_step / first_collision_value and detail describe ONLY
     the first colliding sample. Live poses/mates/history never change: it solves a snapshot.
  4. The user WATCHES it as the document's Motion study: every successful sweep is saved into
     the open assembly (result `motion_study.view`; Features > Assembly > Motion Study opens it).
     A rendered .mp4/.gif is a file, not a study — a film of another assembly file is not a
     study of the open one. Asked to show/animate the OPEN assembly, run simulate_motion on it
     (re-sweeping the same joints replaces its study, never duplicates it); do not only point
     at old films.

TIPS
  * More `steps` = smoother playback and safer against a re-solve flipping a tight mechanism to a
    mirror pose (positions carry forward step-to-step, so a fine sweep stays on one branch).
  * A single angle/distance mate under-constrains a free body (it removes one DOF of six); pin the
    unwanted DOF with extra mates (e.g. a `parallel` to keep a planar linkage in-plane) so the
    driven pose is unique.
  * More samples improve resolution but never prove continuous clearance. Report the
    actual assembly/revision, ranges, computed sample count, threshold and all findings;
    an overlap-only or threshold sweep does not measure a global minimum clearance.
"""

_ASSEMBLY_GUIDE = """\
ASSEMBLY GUIDE — put SEPARATE PARTS together so they actually fit (mates + solve + checks).

TWO WAYS TO ASSEMBLE
  A) TRUE ASSEMBLY (mates — SolidWorks/Onshape model; use for anything that must fit or move):
     components are SAVED PART FILES placed by a mate solver. Gives per-component identity
     (BOM), mates, kinematics (simulate_motion), exploded views.
  B) MULTI-BODY PART (quick static stack-up): import_part(into=doc) / make_standard_part then
     `transform` features with explicit coordinates. No mates, no motion — fine for a weldment
     or a one-off clearance check, wrong for a real mechanism.

TRUE-ASSEMBLY WORKFLOW
  0. For a multi-part mechanism, write a short design/layout.md in the project
     with fs_write (read/amend it if already present): joint axes, link lengths,
     motor/horn interfaces, clearances, fasteners. Choose stated fit tolerances
     or shims and start building within a few minutes, not after extended thought.
  1. Build/obtain ONE part at a time: new_part + add_features,
     make_standard_part for catalog hardware, or import_part for acquired CAD.
     describe_part/measure_part, then save_part(path) to editable .step+ before
     starting the next.
  2. new_assembly("motor_mount") once a saved base exists; add each component
     and its mates as the part becomes available, not after all parts are done.
  3. asm_add_component(path, fixed=True) grounds the base (first defaults fixed);
     add other saved parts as they arrive. `at` gives only a starting pose for
     the solver, never a substitute for mates on moving/fixed interfaces.
  4. asm_mate(mate_type, a, b, ...)                 constrain pairs of faces/edges:
       a/b = {"component": "k1", "query": {"of":"face","side":"top"}}
       The query is the SAME plain-word geometry query as query_geometry, resolved against
       that component's SOURCE PART, and must match exactly ONE face/edge (narrow with
       pick/near/count; the error lists candidates when it matches several).
       A face/edge sid tag read off describe_part, query_geometry or a render tile
       ("F2bb10f") also works: "query": "F2bb10f", "query": {"sid": "F2bb10f"}, or
       {"component": "k1", "sid": "F2bb10f"} — part-frame or assembly-frame sids alike.
       mate types: coincident | parallel | perpendicular | tangent | concentric |
                   distance | angle | lock   (distance mm / angle deg take value=)
       align="aligned"|"anti" flips which way normals/axes point.
     SEATING PATTERNS (what a person clicks in SolidWorks):
       bolt in hole      concentric (shank ↔ bore) + coincident (head seat ↔ plate face)
       motor on bracket  concentric (pilot boss ↔ pilot bore) + coincident (flange ↔ plate)
                         + parallel or a second concentric (bolt ↔ hole) to stop spin
       pin joint         concentric (pin ↔ bore) + coincident/distance for axial position
     MATE EVERY JOINT: driven revolute = angle mate with
       value={"value":0,"axis":[ax,ay,az],"axis_point":[px,py,pz]}
       (measured WORLD hinge line);
       concentric+coincident seats a pin and its axial position; fixed servo
       pockets and horn-to-link interfaces use coincident/concentric/lock.
  5. asm_mate solves after each add (solve=True default) — or call solve_mates() explicitly.
  6. VALIDATE: describe_assembly() (world bboxes + solve status), check_interference()
     (clashes + tightest clearances; min_clearance_mm flags too-tight fits), interface_check
     (do hole patterns/axes actually line up), measure_between (spot dimensions).
  7. Drive EACH joint over its range with simulate_motion(mate_id, start, stop)
     and swept interference, not hand-rolled kinematics or throwaway pose
     assemblies. save_part(path) the mated assembly .step+; simulate_motion
     restores the starting pose after the study.
  Iterate on the saved feature trees when fits change; parametric geometry is
  cheap to edit. Never spend more than a few minutes deliberating without a
  file or CAD change.

BEST PRACTICE (structure)
  * Build DEEP products as SUBASSEMBLIES — each its own .step+, mated into the parent —
    never one monolithic file. It keeps mates, interference checks, and the BOM legible.
  * Before a large assembly build call assembly_plan_review (extract-subassembly / group /
    split-multibody advice) and use cad_graph to TRAVERSE an existing assembly's structure
    and dimensional links.

GOTCHAS
  * asm_* ops REQUIRE an assembly doc (new_assembly) — they are rejected on a part doc.
  * Components reference their source FILES: keep the .step+ files where the assembly can
    find them (same directory is safest) and re-save parts before re-solving the assembly.
  * An unconstrained component has 6 DOF: a single concentric leaves it free to slide+spin —
    add the seating coincident (and an anti-spin mate) before trusting positions.
  * Set the FIRST component fixed (default) so the solve has a ground.
"""

_OVERVIEW_HEADER = """\
Chudware MODELING GUIDE (C++ parametric engine — same as the desktop app).

WORKFLOW
  1. new_part(name)                         start an editable part document.
  2. add_features([ ...ops... ])            build with standard features (vocabulary below).
                                            Returns per-feature status + body count + volume
                                            so you can see a dropped cut or a stray body.
                                            A `sketch` feature should carry geometric RELATIONS
                                            (coincident/horizontal/parallel/tangent/...) + DRIVING
                                            DIMENSIONS, not loose geometry; check_sketch("s1")
                                            reports dof + conflicts so you can reach fully-defined.
     preflight_ops(ops)                     OPTIONAL but recommended before big edits: dry-runs
                                            the planned ops on a shadow clone — checks dangling
                                            refs, impossible fillets, hole-through-nothing, and
                                            downstream feature breaks — WITHOUT touching the live
                                            doc. Returns aicad.preflight/1 with advisory_items.
                                            A fail verdict is advisory (never blocks); add_features
                                            runs preflight automatically on non-trivial batches.
                                            A suppressed run (trivial batch, CHUDWARE_PREFLIGHT=0,
                                            or a preflight error) returns verdict "skipped" with
                                            the reason in skipped/skipped_detail.
                                            An invalid fillet/chamfer solid is rejected even if
                                            OCCT reports construction success. Atomic failure keeps
                                            the prior live tree/revision; regen_diagnosis uses the
                                            rejected candidate and native feature error. Provider
                                            unavailability does not erase that native evidence.
  3. render_part()                          SEE it — ONE call returns a 2x2 grid of the four
                                            most useful complementary shots (the planner
                                            measures ~200 cameras in the engine and picks the
                                            set that shows the most), faces SID-tagged, and a
                                            caption: what each tile shows, faces NO tile sees
                                            (by sid), tag legend. render_part(target="f2")
                                            frames the grid on that feature (painted, rest
                                            dimmed; a SECTION tile with ghosted outline when it
                                            is internal). render_part(view="right" |
                                            {"azimuth":30,"elevation":20} | "auto") = one
                                            single shot instead. render_views() = the planner's
                                            7-view set (views=[...] for a fixed drawing set);
                                            contact_sheet() = the 4-view grid or named views.
                                            inspect_feature(target): planner close-ups +
                                            whole-part context with dims and a describe_part
                                            excerpt; inspect_part(target) one framed close-up;
                                            look_around(target) N planner angles. Numbers
                                            first — do not render after every mutation.
  4. measure_part() / query_geometry(q)     check mass/bbox, or explore faces/edges.
     set_material("aluminum" | {color,...})  store a named engineering material + matching
                                            appearance (explicit colour dicts are visual-only).
  5. run_fea_static(...) / run_cfd_flow(...) prove it (see cad_guide("simulation")).
  6. save_part()                            save the NATIVE .step+ — the editable feature tree
                                            PLUS its simulation studies + any drawings, in one
                                            file the desktop app reopens fully editable. This is
                                            Chudware's native format; save to it to hand the user a
                                            part they can keep working on (not a flattened mesh).
  7. export_part(fmt) / make_drawing()      hand off a NON-native interchange/mesh file
                                            (STEP/IGES/BREP exact, STL/OBJ/3MF mesh) or a
                                            dimensioned 2D drawing.

Address geometry in PLAIN WORDS (no coordinates, no face numbers): "the top face", "all
edges", "the vertical edges", "the largest round face". The engine resolves the words to
topology. cad_guide("review") explains how to read the annotated render. The full op
vocabulary, with worked examples, follows.

DOC OWNERSHIP & SAVE DISCIPLINE
  Docs you mint (new_part/new_assembly/import) are AI-OWNED. The user sees them live in a
  mirror tab while you work; when you finish (detach/close/disconnect) the app AUTO-CLOSES
  that live view unless the user is actively viewing or has adopted it by editing. So the
  responsibility to preserve work is YOURS, not the user's:
    * save_part(path=...) every DELIVERABLE before you finish — this is the hand-off.
    * close_part(discard=True) for SCRATCH docs you don't want kept.
  An unsaved AI doc that's just abandoned is retired to a crash-recovery tier — a safety
  net, NOT a delivery mechanism, and never surfaced to the user as a warning. If the user
  edits your doc they ADOPT it (it becomes theirs); you keep co-editing the same live doc.
  Explicit save_part/export_part relative paths (e.g. "parts/redesign/base.step+") use
  the selected workspace: CHUDWARE_PROJECT_DIR, then CHUDWARE_FS_ROOT, independent of
  worker cwd or continuation. A selected nested workspace is not widened to a parent
  project/repository. Without a valid project root, pass an absolute path. Absolute
  and "~" destinations may be outside the workspace, but credential/key locations
  are refused after resolving symlinks. Pathless saves keep an existing native file
  in place, otherwise use CHUDWARE_PROJECT_SAVE_DIR (temp only with no project).
  Native STL export refuses collapsed facets or a source-solid mesh that is not closed
  and consistently oriented in the serialized output. It does not heal geometry or
  weld nearby points. Keep the native error and inspect the source/export conditions
  rather than deleting triangles or claiming an unchecked mesh is print-ready.

PRIMITIVE PLACEMENT & ORIENTATION (box / cylinder / sphere / cone / torus)
  A primitive is built CENTRED ON THE ORIGIN and then placed. These are the placement keys,
  and they live at FEATURE level — beside "type"/"name"/"params", NOT inside params:
    at         [x,y,z] absolute, OR a geometry query to sit on existing geometry:
               {"of":"face","side":"top"} centres the primitive on that face.
    at_offset  [dx,dy,dz] world-axis nudge applied ON TOP of `at` (boss raised 10 mm above
               the face it sits on: at_offset:[0,0,10]).
    axis       [x,y,z] or "+x"/"-x"/"+y"/"-y"/"+z"/"-z" — orients the primitive's local +Z.
               Default [0,0,1] (straight up) = the historical behaviour. This is how you get
               a SIDEWAYS cylinder/cone/box without falling back to sketch+extrude.
    centered   false = the natural base/corner sits at `at` (box corner, cylinder/cone base
               centre) instead of the centroid. Default true.
    mode       "boss" fuse into the running body (default) | "cut" subtract it.
    merge      false = start a NEW body instead of fusing into the current one.
  Transform order: build centred -> rotate local +Z onto `axis` about the origin -> apply the
  `centered:false` base offset ALONG THE ROTATED AXIS -> translate by `at` -> add `at_offset`.

  WRONG  {"type":"cylinder","name":"DrainSpout","params":{...},
          "placement":{"origin":[200,0,40],"direction":[1,0,0]}, "boolean":"union"}
  RIGHT  {"type":"cylinder","name":"DrainSpout","params":{...},
          "at":[200,0,40], "axis":[1,0,0], "centered":false, "mode":"boss"}
  A handful of dialect spellings (placement.origin/position/location/point, placement.
  direction/axis/normal/dir, feature-level position/location/origin/direction/dir/normal/
  orientation, boolean/operation "union"|"cut"|"new") are rewritten into the canonical keys
  above for you — but write the canonical keys. ANY OTHER unrecognised feature-level key is a
  HARD ERROR that names the offending key and lists the accepted ones. It is never silently
  dropped, so a placement typo now fails loudly instead of quietly building the primitive at
  the origin, buried inside the part ("boss added no material").

  ── WORKED EXAMPLE: bucket body + SIDEWAYS drain spout ────────────────────────────────

    new_part('mud_saver_bucket')
    add_features([
      {"op":"add_feature","feature":{"type":"cylinder","name":"Body",
         "params":{"radius":{"value":165,"expr":null},"height":{"value":300,"expr":null}}}},
      {"op":"add_feature","feature":{"type":"cylinder","name":"DrainSpout",
         "params":{"radius":{"value":25,"expr":null},"height":{"value":60,"expr":null}},
         "at":[150,0,-100], "axis":[1,0,0], "centered":false, "mode":"boss"}}])
    render_part()                 # see the spout stick out the side

  The spout's `at` must START INSIDE the wall (x=150 < the body's r=165) so the boss actually
  intersects the body — a boss that adds no material is REJECTED, not silently ignored.

GEOMETRY QUERY GRAMMAR (query_geometry, refs.*, at:{...}, FEA fixtures/loads, asm_mate)
  A query is a STRUCTURED OBJECT (or a bare side word like "top"/"+z"), never a sentence:
    of             "face" | "edge".  OPTIONAL — defaults to "face".
    side           top | bottom | left | right | front | back, or "+z"/"-x"/... — which way
                   it faces. With pick "largest"/"smallest": the largest face facing that way.
    surf           faces "flat" | "round" (any curved), or cylindrical/conical/spherical/
                   toroidal; edges "straight" | "round" (or exact line/circle).
    parallel       "z" | "x" | "y" | [x,y,z] — the direction the geometry runs along.
    perpendicular  same value forms — keep only geometry perpendicular to that direction.
                   NOTE a face's direction is its NORMAL, an edge's is the way it RUNS. So a
                   VERTICAL WALL FACE is perpendicular:"z" (its normal is horizontal) while a
                   VERTICAL EDGE is parallel:"z". "Vertical"/"horizontal" are not query words.
    pick           largest | smallest | longest | shortest | inner | outer — narrow a
                   multi-match to the one you mean (also "index":[i]).
    near           [x,y,z] (+ "count") — the face/edge nearest a point ON it (or at its
                   centroid), measured to the actual geometry: a point on a flange rim picks
                   the rim, not a bolt hole whose centre is closer. On a shared edge of two
                   faces the face whose centroid is nearer wins.
  Examples:
    {"of":"face","side":"top"}                                the top face
    {"of":"face","surf":"round","pick":"outer"}               the outer cylindrical wall
    {"of":"edge","surf":"line","parallel":"z"}                the vertical straight edges
    {"of":"face","surf":"flat","perpendicular":"z"}           the upright flat wall faces
  A FREE-TEXT SENTENCE IS NOT A QUERY. "the outer cylindrical wall face at bottom near
  x=200,y=0" is not parseable geometry — EXPLORE first with describe_part() /
  query_geometry({"of":"face"}) / hover(sid) to see what the part actually has, then SELECT
  with a structured query built from the keys above.

================================================================================
"""


_GSL_GUIDE = """\
GEOMETRY-SEMANTICS LAYER — read a part WITHOUT drowning in a flat face dump, and get a
usable read on geometry EVEN WITHOUT VISION (no image, just text).

On a complex/imported part, do NOT page through query_geometry's whole face list. Instead:
  describe_part()        a compact NAMED outline: bbox, the feature groups WITH a placement
                         narrative (which face each feature sits on / touches), any detected
                         symmetry/instanced PATTERN (e.g. "6-fold rotational about +Z" with the
                         6 blade faces collapsed to one line), the bores/holes and flat faces by
                         side, and a stable `sid` for every addressable face/edge. Token cost
                         scales with the number of DISTINCT roles, not the raw face count.
  describe_sketch(id)    a sketch's plane placement, entity roster (line/circle/arc + dims),
                         constraint roster with driving-dimension values, closed-region count/
                         area, and the DOF (fully/under/over-defined) verdict — the sketch
                         equivalent of describe_part, for reviewing layout before you extrude it.
  sketch_health(id)      the narrated constraint read: WHICH free variables remain (dof in plain
                         words), open chain endpoints, per-entity under/full/over, redundant/
                         conflicting with removal hints, and an ORDERED schedule (relations first,
                         then driving dims). Draw roughly -> relations -> critical dimensions ->
                         sketch_fully_define -> check_sketch: dof 0 and not pinned_only.
  sketch_auto_constrain(id)  stitch near-touching endpoints coincident (apply=True, default) or
                         just propose them (apply=False); each add is rollback-safe (skips any that
                         would conflict) and it reports which chains closed. Runs automatically after
                         edits when doc meta sketch.auto_coincident is on (default).
  sketch_fully_define(id) infer residual editable relations/dimensions (never blanket fix).
                         relations=True, dimensions=True, anchor="origin"|"none", entities=[ids]
                         optionally limit it. add_features runs this in the same revision after
                         NEW sketches / sketch_add_entity batches; auto_define:false opts out.
  describe_scene(views)  the TEXT equivalent of render_part/contact_sheet for a model with no
                         vision: per requested view (default front/right/top/iso), which faces
                         face the camera (by sid/surf/side/dims) plus the part bbox — computed
                         from B-rep normals with NO occlusion modeling (says so inline). This is
                         the non-vision self-review step; do it instead of skipping the review.
  inspect_part(target)   CLOSE-UP of a face/edge/feature (plain-word query or feature id fN):
                         the ENGINE frames the target's own bbox, so a 5 mm hole in a 120 mm
                         plate is RENDERED at 5 mm scale, not cropped out of a whole-part shot.
                         view="auto" is the shot planner's single best MEASURED camera (every
                         candidate is measured in the engine); for an internal target that is
                         a SECTION through it, cut face hatched and the removed half ghosted.
  inspect_feature(target) planner close-ups (hero + complementary alternates, a section for
                         anything internal) + whole-part CONTEXT with the target highlighted,
                         plus describe_part excerpt + dims. Use when the grid is ambiguous.
  render_views(views?)   the planner's 7 complementary views (or a custom view list, e.g. the
                         6-ortho+iso drawing set); optional target= frames every tile on the
                         feature, and the caption names what each view shows.
  look_around(target)    N close-ups of one target tiled: the planner's complementary set by
                         default (ranked=False for an even orbit), each labelled with how much
                         of the target it shows.
  Every emphasis render paints the target's EXACT visible faces (shading kept so curvature
                         still reads), desaturates the rest, and reports measured coverage —
                         a camera that cannot see the target SAYS so instead of showing a blank.
  hover(sid)             expand ONE face/edge by the sid describe_part / query_geometry gave —
                         its full card (surf, side, signed normal, concavity bore/boss, radius).
  query_adjacency()      the face-adjacency graph: which faces touch, and whether each shared
                         edge is a CONVEX outside-corner or a CONCAVE inside/pocket edge. This is
                         how you reason about "the edge between the top and front face".

Every query_geometry/describe_part match now also carries: `side` (top/bottom/left/right/front/
back), `normal` (SIGNED outward dir — unlike the unsigned `axis`), `concavity` (bore vs boss),
`radius`, `axis_point` (cylinder/cone: a point ON the axis — `centroid` is off-axis on a fillet),
torus faces (sweep bends) `major_radius`/`minor_radius`/`torus_axis`/`torus_center` (bend
radius, tube radius, bend axis, bend centre — their `radius`/`axis` stay null), and a stable
`sid`. add_features returns a `changes` write-diff ("added 1 cylinder; 1
new bore; volume -3927; part bbox 60x40x20 mm") so you can confirm an op did what you meant
instead of guessing: `changes.bbox_mm` is the whole part's size (check it against what you were
asked for rather than spending a measure_part call), a volume move too small to be real material
says so instead of printing a delta, and anything the kernel objected to arrives as an
"ENGINE WARNING:" prefix on the summary. Best-effort `overlap_warnings` also travels when the
batch introduced a boss/cut overlap (see check_feature_overlap for the dedicated tool).
"""

_INTAKE_GUIDE = """\
INTAKE / FIRST-TRY-FIT — get real-world parts to fit on the first print.

NORTH STAR: never silently guess a dimension that decides whether parts FIT. Spend questions only
on the dims that gate a mating interface; default everything else and flag it.

ISO 286 FIT CALCULATOR — call fit_calc BEFORE setting any bore or shaft diameter:
  fit_calc(nominal, fit)          ISO 286 tolerance bands + fit character. Always call this
                                  BEFORE hole_sizing or setting a bore dimension. Example:
                                  fit_calc(20.0, "H7/g6") → hole 20.000–20.021,
                                  shaft 19.980–19.993, clearance 0.007–0.041 mm.
  recommend_fit(function)         not sure which fit class? "sliding", "press fit", "bearing",
                                  "dowel pin" → recommended ISO 286 classes + rationale.
                                  Call recommend_fit FIRST, then fit_calc with the class it gives.
  suggest_dimension_scheme(doc, sketch, process)
                                  ordered dimension plan for a sketch: which dims to drive,
                                  default IT-grade tolerances by process, bolt-pattern datum rules,
                                  and needs_user_confirm flags for function-critical dims.
                                  CNC/lathe → IT7, FDM → IT12, sheet/laser → IT10.

WHEN the design must incorporate a real part you have NO model/datasheet for:
  1. LOOK IT UP FIRST — list_standard_parts / make_standard_part. Only a real miss triggers photos.
  2. resolve_process(request) — pick the manufacturing process (FDM 3D-printing is the default).
     If it returns ambiguous, ASK the user the one `clarification` question (e.g. sheet-metal vs
     CNC for a flat metal plate); then set_process(process). The process sets the fit clearances.
  3. request_part_photos(label, critical_features) — ask for photos WITH a scale reference (a
     printable scale card / calipers-in-frame / ruler) in the SAME PLANE as the feature, plus the
     ortho views + close-ups it lists. The user replies with photos next turn.
  4. submit_part_photos(claims) — report the dims YOU read from the photos as structured claims
     (with scale_source / in_plane / agrees_across_views / edge_quality). It scores each and
     returns `open_demands`: the fit-critical dims whose confidence is too low for their fit.
  5. confirm_dimensions([{key,value_mm}]) — get the user to TYPE a caliper number for each demand
     (a press/slip fit won't work off a photo guess). When the gate clears, build the geometry.
  6. Build the RECEPTACLE to fit, not a copy — the process clearance is already in the numbers.
  7. fit_report() to review modeled-vs-measured + clearances before export; dfm_check() for wall/
     draft vs the process minimums; make_fit_coupon(key) to print a tiny test of one interface
     first. All of this persists in the .step+ (reopens in-app intact).
"""

_CAM_GUIDE = """\
CAM / MACHINING GUIDE — generate real CNC toolpaths + G-code from the part.

The CAM suite turns a part into a machining JOB: a tree of SETUPS (each a stock + work
coordinate system + machine/platform) holding OPERATIONS (toolpaths), driven by a tool
library and a material-aware feeds-&-speeds engine, posted to controller G-code.

TYPICAL LOOP
  1. cam_new_job("Bracket", post="grbl")            create the job (pick a post dialect).
  2. cam_add_tool(number=1, type="endmill_flat", diameter=6, flutes=3)   build a tool library.
  3. cam_extract(kind="all")  OR  cam_extract(kind="stock"|"holes"|"section"|"mesh")
     pull stock bounds, a mesh, planar regions, and drill points FROM THE CURRENT PART.
  4. cam_add_setup(name="Op1", machine="mill3", stock=<from extract>, material="aluminum").
  5. cam_add_operation(setup_id, type=..., tool_number=..., geometry=<from extract>, ...).
        2.5D mill: face, contour2d, pocket2d (islands), slot, chamfer2d, bore2d, engrave, adaptive2d
        holes:     drill (cycle=drill|peck|chipbreak|tap|ream|bore|spot), thread_mill
        3D mill:   parallel3d, contour3d (waterline), scallop3d, pencil3d, pocket3d, adaptive_clear3d, radial3d, spiral_finish3d
        turning:   turn_face, turn_rough, turn_finish, turn_groove, turn_thread, turn_part, turn_drill
        cutting:   cut2d (laser/plasma/waterjet — kerf, pierce, lead in/out); probe
     Feeds/speeds resolve automatically from the material + tool; override any of rpm/feed/
     plunge/stepover/stepdown on the op to pin it.
  6. cam_generate()    build the toolpaths (per setup / op).
  7. cam_simulate()    material-removal sim: removed/remaining volume, gouge + rapid-collision
                       checks, estimated cycle time.
  8. cam_post("fanuc") emit the NC program; cam_setup_sheet() for the operator sheet.
  9. Audit: cam_audit — modal parse, envelope, feeds, tool and round-trip checks; ship only ok:true.

FEEDS & SPEEDS ADVISOR (table-driven, aicad.calc/1 contract)
  Before cam_add_operation, call feeds_speeds() to get a recommended envelope:
    feeds_speeds("alu_6061", "endmill_flat", 10.0, "roughing")
    → {schema:"aicad.calc/1", answer:{rpm:{min,mid,max}, feed:{min,mid,max},
       stepdown:{min,mid,max}, stepover:{min,mid,max}, chipload, coolant, provenance}}
  After adding the op, call cam_check_feeds_envelope() to flag risky values:
    cam_check_feeds_envelope("alu_6061", "endmill_flat", 10.0, "roughing", rpm=50000)
    → {schema:"aicad.advisory/1", items:[{severity:"hint", candidate_fixes:[clamped_values]}]}
  For tap operations, get the drill size first:
    tap_drill("M6x1.0")  → {answer:{tap_drill_mm:5.0, pct_thread:75}}
    tap_drill("1/4-20")  → {answer:{tap_drill_mm:5.1, ...}}
  Supported tool_types: endmill_flat, endmill_ball, drill, tap, face_mill.
  Supported operations: roughing, finishing, slotting, drilling.
  Materials: alu_6061, steel_1018, steel_304_stainless, ti_6al4v, abs, pla,
             nylon_pa12, cast_iron_gray, inconel_718 (+ aliases from cam_materials()).

PLATFORMS: 3-axis mill, router, 4/5-axis (3+2), lathe / mill-turn, laser, plasma, waterjet.
POSTS: grbl, linuxcnc, fanuc, haas, mach3, marlin (laser), siemens (840D), centroid/tormach —
  fully data-driven (cam_post_dialects() lists them); per-controller canned cycles, arc IJK/R,
  work offsets, tool changes, coolant, laser power gating.
REFERENCE: cam_post_dialects(), cam_materials() (cutting-data DB), cam_tool_types(), cam_op_types(),
           feeds_speeds(), cam_check_feeds_envelope(), tap_drill().
Address geometry the same plain-English way as modeling — extract regions/holes/sections from the
part, don't hand-type coordinates unless you must.
"""

_DRAWINGS_GUIDE = """\
2D ENGINEERING DRAWINGS — professional sheets from the live part or assembly

WHAT A NEW DRAWING CONTAINS (Make Drawing in the app, make_drawing for agents)
  - Paper: ISO A4-A0 or ANSI A-E, landscape by default; "A4 portrait" / "A4P" for portrait.
  - ISO 5457 frame: 20 mm filing margin, 10 mm elsewhere, zone letters/numbers in the margin,
    centring marks.
  - ISO 7200 title block (180 mm wide, lower right): TITLE, DRAWING / PART NO., REV, SHEET
    (auto "n OF m"), MATERIAL (from the part), FINISH, MASS, SCALE, SIZE, DRAWN, DATE (today),
    CHECKED, APPROVED, COMPANY, the ISO 5456-2 projection symbol, UNITS (the drawing's unit
    system — change with drawing units, never by typing), GENERAL TOLERANCES and the standard.
    Values shrink to fit their cell and never overflow it.
  - Views: front / top / right + an isometric, on the projection lattice — third angle (ASME,
    default: top above front, right view to its right) or first angle (ISO E: mirrored). Views
    share projection lines, are drawn at ONE standard ISO 5455 scale chosen to fit the free
    area (frame minus title block and tables), and never cover the title block.
  - Line types per ISO 128: visible outlines 0.5 mm continuous, hidden edges 0.25 mm dashed,
    centre lines 0.25 mm long-dash-dot, dimensions/hatching 0.25 mm. The isometric carries no
    hidden lines.
  - Auto-dimensions per ISO 129-1: each overall extent stated once on the sheet (no redundant
    dims), round outlines by their diameter, one "N× ⌀d" callout per distinct hole size, centre
    marks on every hole, centre lines on hole/boss axes seen side-on, and a chain pitch circle
    with its diameter for equally spaced hole patterns. Filled 3 mm arrowheads, 3.5 mm text,
    extension lines with a 1 mm gap and 2 mm overshoot, arrows flip outside on tight spans.
  - Assemblies: overall envelope, a parts list (BOM) sitting on the title block, and one balloon
    per BOM item.

EDITING
  - Sections: chain cutting plane with thick ends, viewing arrows and letters on the parent,
    hatched cut faces, "SECTION A-A" caption. Details: boundary circle + letter on the parent,
    "DETAIL B  SCALE 2:1" caption.
  - arrange_drawing(doc_id, projection?, scale?, size?) re-lays a sheet (switch first/third
    angle, force a standard scale, change paper) and moves dimensions and cuts with their views.
  - Dimensions, tolerances (symmetric / deviation / limit / basic), GD&T frames, datums, surface
    finish, weld symbols, notes, balloons, tables: see the agent cheatsheet below.

DELIVERABLES (export_drawing)
  - PDF: vector, true paper size, ISO line weights, filled arrows, searchable Helvetica text.
  - DXF R12: layers VISIBLE, HIDDEN (DASHED), CENTER, DIMENSIONS, HATCH, BORDER, TABLES; opens in
    any CAD package.
  - DWG (AutoCAD R2000, written natively): the same layers/colors/text, DASHED/CENTER
    linetypes and aligned text.
  - SVG: editable paths and real text. PNG review raster via review_drawing.
  - Drawings save inside the part's .step+ and reopen associatively; a standalone .draw+ embeds
    its source.
"""


def cad_guide_text(topic: str = "overview") -> str:
    topic = (topic or "overview").strip().lower()
    if topic in ("overview", "features", "ops", "geometry", "vocabulary", "vocab"):
        return _OVERVIEW_HEADER + _read_prompt("shared/ops_cheatsheet.md")
    if topic in ("review", "render", "vision"):
        return _read_prompt("reviewer.md") or "(reviewer guide unavailable)"
    if topic in ("simulation", "sim", "fea", "cfd"):
        return _SIM_GUIDE
    if topic in ("motion", "kinematics", "kinematic", "mechanism", "simulate_motion",
                 "linkage", "animate", "playback"):
        return _MOTION_GUIDE
    if topic in ("assembly", "assemblies", "asm", "mate", "mates", "component",
                 "components"):
        return _ASSEMBLY_GUIDE
    if topic in ("intake", "fit", "fits", "iso286", "tolerance",
                 "manufacturing", "dfm", "photos"):
        return _INTAKE_GUIDE
    if topic in ("gsl", "describe", "outline", "digest", "adjacency"):
        return _GSL_GUIDE
    if topic in ("cam", "machining", "toolpath", "toolpaths", "gcode", "g-code", "mill", "nc"):
        return _CAM_GUIDE
    if topic in ("narration", "narrate", "describe_toolpaths", "describe_results",
                 "describe_assembly", "describe_board", "describe_project",
                 "results", "fea_results", "cfd_results", "board", "project"):
        return (
            "NARRATION TOOLS — compact text summaries of every domain state:\n\n"
            "  describe_toolpaths([job_id])\n"
            "    Narrates an active CAM job: per-op strategy, tool, stepdown/stepover/stock,\n"
            "    cycle time + gouge/rapid counts (from cam_simulate), and a G-code digest\n"
            "    (bounds, feed range, tool changes) from cam_post.\n"
            "    Use INSTEAD of dumping cam_job_tree JSON.\n"
            "    Call cam_generate + cam_simulate first for full output.\n\n"
            "  describe_results([doc_id], [study_name])\n"
            "    Narrates the latest (or named) FEA/CFD study on a part: max von Mises stress,\n"
            "    max displacement, factor of safety, margin vs yield, the fixed/loaded faces\n"
            "    named by sid, modal frequencies, and mesh convergence caveat.\n"
            "    Use INSTEAD of dumping run_fea_static / run_fea_modal JSON.\n"
            "    Returns a graceful 'no results attached' message when no study exists.\n\n"
            "  describe_assembly([doc_id])\n"
            "    Queries pre-solve world-frame bounding boxes and origins, runs a live mate\n"
            "    solve, then eval_interference with clearance_mode='threshold',\n"
            "    min_clearance_mm=0 and include_coincident=False. Exact clash volumes remain;\n"
            "    clearance bounds are not measured gaps. No minimum measured gap is reported.\n"
            "    Returns raw fit completeness, checked/total pairs and failures; incomplete\n"
            "    or no-pair reports cannot qualify collision-free. Use INSTEAD of raw JSON.\n"
            "    Call solve_mates first if placement rows must show the solved pose. To validate\n"
            "    that mating interfaces LINE UP (hole patterns, coaxial bores, flush contact faces,\n"
            "    fastener pass-through) — not just that nothing clashes — run interface_check.\n\n"
            "  describe_board([doc_id])\n"
            "    Narrates an active schematic or PCB: component roster (ref/value/footprint),\n"
            "    net list with members, ERC/DRC violation counts, and unconnected pins.\n"
            "    Promotes ecad_status into the GSL narration layer.\n"
            "    Open a design first with ecad_new_schematic or ecad_new_pcb.\n\n"
            "  describe_project([run])\n"
            "    Narrates a DAG project run: nodes done/doing/blocked/pending, budget usage,\n"
            "    and open questions. Pass run=dag_status()['run'] or run_id=<id>.\n"
            "    See contracts/dag_schema.md for the run dict shape.\n\n"
            "All tools narrate EXISTING numbers only — they never recompute values.\n"
            "See contracts/narration.md for the full style guide (rounding, attribution,\n"
            "graceful-degradation phrases, section order)."
        )
    if topic in ("deliverables", "deliverable", "report", "presentation", "deck", "workbook",
                 "spreadsheet", "artifacts", "artifact"):
        # Imported on demand: only this topic needs the deliverables package.
        from services.deliverables import DELIVERABLES_GUIDE  # noqa: PLC0415
        return DELIVERABLES_GUIDE
    if topic in ("parts", "standard_parts", "standard-parts", "library", "catalog",
                 "part_search", "part_get", "part_place", "hardware", "bom_parts",
                 "parts_taxonomy", "taxonomy", "facets", "faceted_search"):
        return (
            "STANDARD-PARTS LIBRARY — search → get → place (aicad.parts/1)\n\n"
            "BIAS: always prefer standard parts over custom geometry for hardware.\n"
            "Standard parts are editable, sourced, and BOM-ready out of the box.\n\n"
            "REFERENCE ACQUISITION ORDER:\n"
            "  1. part_search / make_standard_part for catalog and standard parts\n"
            "     (servos, steppers, bearings, fasteners, extrusions); part_get\n"
            "     confirms dimensions and part_place places a catalog record.\n"
            "  2. match_model / part_source for a discrete product's CAD.\n"
            "  3. fetch_cad_model(url) for a specific model page or STEP/IGES file\n"
            "     found by web search; import_artifact if not auto-imported.\n"
            "  4. Web search only for specs/datasheets absent from the library.\n"
            "     Then COMMIT to cited dimensions with clearance and build; never\n"
            "     loop on research. For an MG90S six-axis arm, search MG90S first,\n"
            "     use its reference envelope/mount dimensions, and build brackets.\n\n"
            "WORKFLOW:\n"
            "  1. part_search(query, domain?, limit?)\n"
            "       Search the federated catalog.  Returns ranked PartRecord list.\n"
            "       Examples:\n"
            "         part_search('M8 socket head')         → fastener records\n"
            "         part_search('6205')                   → ISO 6205 bearing\n"
            "         part_search('W8x31')                  → AISC W8×31 section\n"
            "         part_search('resistor', domain='ecad')→ KiCad resistor symbols\n"
            "         part_search('elbow NPS1')             → piping fitting\n"
            "         part_search('pump', domain='hydraulic')→ ISO 1219 pump\n"
            "       Domains: fastener | bearing | mech_family | section | fitting |\n"
            "                hydraulic | ecad\n\n"
            "  2. part_get(part_id)\n"
            "       Fetch the full PartRecord for a stable part_id:\n"
            "         part_get('fastener:ISO4762:M8')       → M8 SHCS record\n"
            "         part_get('bearing:ISO 15:6205')       → 6205 bearing record\n"
            "       Inspect params + geometry_ref to confirm dimensions before placing.\n\n"
            "  3. part_place(part_id, doc?, params?)\n"
            "       Insert the part — always editable, never an opaque solid.\n\n"
            "       Mechanical (fastener / bearing / mech_family):\n"
            "         part_place('fastener:ISO4762:M8')\n"
            "         part_place('fastener:ISO4762:M8', params='{\"length_mm\": 25}')\n"
            "         → Creates a new editable part doc_id.  Use make_standard_part\n"
            "           equivalently if you already know family/size.\n\n"
            "       ECAD (schematic symbol):\n"
            "         part_place('ecad:Device:R',\n"
            "                    doc=json.dumps(sch_doc),\n"
            "                    params='{\"pos\":{\"x\":10,\"y\":20},\"value\":\"1k\"}')\n"
            "         → Appends sch_add_symbol op; symbol appears in doc['symbols'].\n\n"
            "       Structural section:\n"
            "         part_place('section:AISC:W8x31',\n"
            "                    doc=json.dumps(struct_doc),\n"
            "                    params='{\"kind\":\"beam\",\"length_mm\":3000}')\n"
            "         → Appends StructuralElement to doc['elements'].\n\n"
            "       Piping fitting:\n"
            "         part_place('fitting:elbow:NPS1',\n"
            "                    doc=json.dumps(piping_doc),\n"
            "                    params='{\"type\":\"elbow\",\"nps\":1.0}')\n"
            "         → Appends fitting record to doc['fittings'].\n\n"
            "       Hydraulic component:\n"
            "         part_place('hydraulic:pump:gear-1cc',\n"
            "                    doc=json.dumps(hyd_doc),\n"
            "                    params='{\"type\":\"pump\",\"displacement_cc_rev\":1.0}')\n"
            "         → Appends component record to doc['components'].\n\n"
            "DEDUPLICATION NOTE:\n"
            "  Threaded-hardware queries may surface both fastener: and mech_family: records\n"
            "  for the same designation.  The search ranks fastener > mech_family for\n"
            "  standard-hardware queries, and same-designation results carry a related_ids\n"
            "  field pointing to the sibling.  Prefer the top-ranked result; both are\n"
            "  placeable and editable.\n\n"
            "EDITABILITY GUARANTEE:\n"
            "  part_place always returns {'editable': true}.  A placed part that isn't\n"
            "  editable is a bug — report it and use part_get to inspect geometry_ref.\n\n"
            "TAXONOMY BROWSE + FACETED SEARCH (Parts Library V2):\n"
            "  parts_taxonomy()                       → root categories (fasteners,\n"
            "      bearings_motion, structural_stock, drivetrain, tools, electronics,\n"
            "      fluid_power, hardware_misc, vendors, ...)\n"
            "  parts_taxonomy('bearings_motion.ball_bearings.deep_groove')\n"
            "      → children + breadcrumbs + EFFECTIVE facets (the facet keys this\n"
            "        category accepts: bore_mm, od_mm, width_mm, seals, ...)\n"
            "  part_search(query, category_id=..., facets='{...}')\n"
            "      → taxonomy-ROUTED search (right domains + the category's vendor\n"
            "        sites) with facet post-filtering.  Examples:\n"
            "    part_search('tube', category_id='structural_stock.metal_tube',\n"
            "                facets='{\"od_mm\": 25.4, \"alloy\": \"4130\"}')\n"
            "    part_search('bearing', category_id='bearings_motion.ball_bearings',\n"
            "                facets='{\"bore_mm\": {\"min\": 10, \"max\": 14}}')\n"
            "    part_search('end mill', category_id='tools.cutting_tools.end_mills',\n"
            "                facets='{\"flutes\": \"4\", \"coating\": \"AlTiN\"}',\n"
            "                price_max=40, vendors='mcmaster', in_stock=True)\n"
            "  The response's facet_summary shows the top param values in THIS result\n"
            "  set — read it to see what to narrow by next.\n\n"
            "NEED-DRIVEN SOURCING: part_source extracts taxonomy facets and ranks\n"
            "  candidates with artifact plans; see cad_guide('sourcing').\n\n"
            "SOURCING:\n"
            "  Each PartRecord carries a sourcing[] list of VendorPart dicts (aicad.sourcing/1).\n"
            "  Use bom_rollup after placing multiple parts to get a costed BOM.\n"
        )
    if topic in ("reverse", "re", "recognize", "recon"):
        # Lazy import: recon_tools imports the cad_tools facade, which re-exports this module.
        from services.mcp.recon_tools import _RE_GUIDE  # noqa: PLC0415
        return _RE_GUIDE
    if topic in ("requirements", "req", "req_create", "req_update", "req_status", "req_report",
                 "brief", "design_brief", "layout_plan", "layout", "critical_dim"):
        return (
            "REQUIREMENTS (aicad.req/1) — design-brief CRUD + markdown projection\n\n"
            "Schema: contracts/requirements.md  |  Engine: services/requirements/\n\n"
            "WORKFLOW:\n"
            "  req_create(title, body)              Create a new requirements doc; returns req_id.\n"
            "  req_update(req_id, needs?, reqs?)    Add/edit individual need or requirement rows.\n"
            "  req_status(req_id)                   Current fulfilment table (met/partial/open).\n"
            "  req_report(req_id)                   Full markdown projection for sharing —\n"
            "                                       battery-populated evidence is listed\n"
            "                                       separately from hand-entered evidence.\n"
            "  req_verify_by_id(requirement_id,     AUTO-VERIFY one requirement: runs its\n"
            "      doc_id?, req_path?)              verify_by verifiers via the battery and\n"
            "                                       writes status flip + evidence back into\n"
            "                                       the doc (no manual req_update needed).\n\n"
            "AUTOMATIC PROOF LOOP: a DAG verify node with inputs\n"
            "  {check_type: 'requirement', req_id: 'R1', req_path|req_doc_id, doc_id}\n"
            "runs the requirement's named verify_by verifier and auto-flips its status\n"
            "with a source='battery' evidence ref — closing idea → requirement → proof.\n\n"
            "BIAS: capture requirements BEFORE modeling — req_create at project start, then\n"
            "reference req_id in every verify_battery call so the battery knows what to check.\n"
            "A requirement without a verifier is aspirational; wire them up.\n\n"
            "LAYOUT-FIRST — MANDATORY before any modeling:\n"
            "  layout_plan(doc_id,\n"
            "              envelope_x_mm, envelope_y_mm, envelope_z_mm,\n"
            "              critical_dims=[{param, value, unit, tol_plus, tol_minus, why, verify_by},...],\n"
            "              interfaces=[{id, desc, verify_by},...],\n"
            "              note='intent summary')\n\n"
            "  Call layout_plan() FIRST — before add_features — to record:\n"
            "    - Overall bounding envelope (x/y/z mm)\n"
            "    - Critical dimensions: value + tolerance + why-critical (each becomes a req)\n"
            "    - Component interfaces: what mates to what, via which features\n\n"
            "  Each entry becomes an aicad.req/1 requirement (kind=dimensional or interface)\n"
            "  visible in req_report and checked by the coverage gate at sign-off.\n\n"
            "  layout_plan_report(doc_id)  — human-readable coverage state per dim/interface.\n\n"
            "COVERAGE GATE — mandatory before declaring done:\n"
            "  design_review(doc_id, checks=['layout_coverage', ...])\n"
            "    WARN/FAIL if any critical dim or interface has no verification evidence.\n"
            "    Passes when every critical dim has status=verified+evidence, user_confirmed,\n"
            "    or waived. An agent finishing a part with an unverified critical dim gets\n"
            "    WARN/FAIL — not silence.\n\n"
            "What counts as evidence per dim type:\n"
            "  dimensional  → measure_between, measure_part, query_geometry, confirm_dimensions,\n"
            "                  run_fea_static/modal, fit_report\n"
            "  interface    → check_interference, solve_mates, measure_between, fit_report,\n"
            "                  interface_check\n\n"
            "FULL LOOP:\n"
            "  1. layout_plan(doc_id, ...)           capture before modeling\n"
            "  2. add_features(...)                  model to the plan\n"
            "  3. measure_between(...) / req_status(requirement_id, 'verified', evidence=[...])\n"
            "                                        record evidence as each dim is confirmed\n"
            "  4. design_review(doc_id, ['layout_coverage'])  gate before done\n"
            "  5. verify_battery(doc_id)             final battery\n"
        )
    if topic in ("dag", "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort",
                 "dag_replan", "orchestrate", "mission", "project_run", "workflow"):
        return (
            "DAG ORCHESTRATOR — multi-step missions with gates, budgets, and human approvals\n\n"
            "Tools: dag_plan / dag_run / dag_status / dag_approve / dag_abort / dag_replan\n"
            "Schema: contracts/dag_schema.md\n\n"
            "WORKFLOW:\n"
            "  dag_plan(goal, nodes)     Draft a DAG of tool-call nodes with retry budgets\n"
            "                            and end_conditions.  Returns a plan dict.\n"
            "  dag_run(plan)             Execute the plan; checkpoints each node; returns run_id.\n"
            "  dag_status(run_id)        Poll: nodes done/doing/blocked/pending, budget usage.\n"
            "  dag_approve(run_id, node) Ungate a human-approval node so the run continues.\n"
            "  dag_abort(run_id)         Cancel a running mission cleanly.\n"
            "  dag_replan(run_id, nodes) Splice new nodes into a RUNNING checkpointed run\n"
            "                            (validates end_condition + retry_budget on every node).\n\n"
            "NARRATION: describe_project(run=dag_status(...)['run']) gives a human-readable\n"
            "  summary (nodes done/doing/blocked, budget, open questions) instead of raw JSON.\n\n"
            "BIAS: use DAG for any mission with 3+ sequential tool calls that could fail\n"
            "  independently — it checkpoints progress and resumes after transient errors."
        )
    if topic in ("verify", "verify_battery", "battery", "check", "done", "sign-off"):
        return (
            "VERIFY BATTERY — deterministic done-check before calling a design finished\n\n"
            "Tools: verify_battery(doc_id, checks?) / check_metadata()\n\n"
            "MANDATORY before declaring done:\n"
            "  verify_battery(doc_id)   Runs the full battery: feature overlap, interference,\n"
            "                           wall thickness, draft angles, DFM, and any req_id-linked\n"
            "                           requirement checks.  Returns a verdict card per\n"
            "                           contracts/narration.md — PASS/WARN/FAIL per check.\n"
            "  check_metadata()         Confirms doc has title, material, and process set.\n\n"
            "ALSO: check_feature_overlap() and check_interference() run individual checks.\n\n"
            "REQUIREMENT AUTO-VERIFICATION (aicad.req/1): req_verify_by_id(requirement_id,\n"
            "  doc_id?, req_path?) runs a requirement's named verify_by verifiers through\n"
            "  the battery and writes the verified/failed status + evidence ref back into\n"
            "  the requirements doc automatically.  DAG verify nodes do the same with\n"
            "  inputs {check_type: 'requirement', req_id, req_path|req_doc_id, doc_id}.\n\n"
            "BIAS: never report 'done' to a user until verify_battery returns no FAILs.\n"
            "  A WARN is a judgment call — narrate it; don't silently ignore it.\n"
            "  Wire req_id into the battery call so requirement fulfilment is checked too."
        )
    if topic in ("interaction", "elicitation", "ask_user", "clarify", "questions"):
        return (
            "INTERACTION / ELICITATION — the single ask_user seam (contracts/interaction.md)\n\n"
            "Tool: ask_user(questions, scope?)\n\n"
            "  DO NOT ask by default. Prefer a typed suggested default + required:false, or\n"
            "  skip the tool and record an assumption. Ask only when the answer changes the\n"
            "  build (fit-critical dim, process branch, design option, which feature to edit).\n\n"
            "  ask_user([\n"
            "    # Design decision — single-select choice (recommended option in suggested):\n"
            "    {'id': 'material', 'kind': 'choice', 'prompt': 'What material?',\n"
            "     'options': [{'value':'aluminum','label':'6061-T6'},\n"
            "                 {'value':'steel','label':'1018 steel'}],\n"
            "     'suggested': 'aluminum', 'required': false,\n"
            "     'why': 'Drives stock + feeds.'},\n"
            "    # Multi-select design options (pick any that apply):\n"
            "    {'id': 'keep_feats', 'kind': 'choice', 'multi': true,\n"
            "     'prompt': 'Which features should stay?',\n"
            "     'options': [{'value':'rib'},{'value':'fillet'},{'value':'boss'}],\n"
            "     'suggested': ['rib','fillet']},\n"
            "    # Geometry pick — user clicks the model (do NOT invent sids):\n"
            "    {'id': 'edit_face', 'kind': 'geometry_select',\n"
            "     'prompt': 'Which face should get the pocket?',\n"
            "     'targets': ['face'], 'suggested': null, 'required': true},\n"
            "    # Dimension / text / approval also supported.\n"
            "  ])\n\n"
            "  kinds: choice (multi?:bool), dimension, text (multiline?:bool),\n"
            "         file_request, approval, geometry_select (targets, multi?).\n"
            "  Returns {id: {value, canonical?, source, at}}. Answers persist + replay:\n"
            "  same scope+id returns the stored answer without re-prompting.\n\n"
            "BIAS: batch unknowns into ONE ask_user call (≤4). Use suggested everywhere.\n"
            "  Design decisions → choice options. Can't see the model → geometry_select.\n"
            "  Scope='run' (default) per run; scope='doc' persists in the .step+."
        )
    if topic in ("sourcing", "bom", "bom_rollup", "bom_price", "vendor", "purchase",
                 "part_source", "vendors_health", "cam_tool_import", "procure",
                 "scrape", "scrape_enqueue", "import_artifact"):
        return (
            "SOURCING — need → candidates → geometry → BOM → quote (Parts Library V2)\n\n"
            "Use cad_guide('parts') for the reference acquisition order.\n"
            "PART SOURCE DETAILS:\n"
            "  parts_taxonomy(node_id?) shows categories and effective facet keys.\n"
            "  part_source(need, constraints?, limit?) ranks a plain-language need:\n"
            "    part_source('4130 chromoly tube OD 25.4 wall 1.25')\n"
            "  It extracts facets and returns {part_id, title, vendor, price,\n"
            "  specs, score, rationale, artifact_plan}; see contracts/sourcing.md.\n"
            "  Narrow via part_search(query, category_id, facets, price_max,\n"
            "  vendors, in_stock), inspect facet_summary, then part_get(part_id).\n"
            "  Follow artifact_plan.how for real geometry: part_place for local\n"
            "  geometry_ref; scrape_import via scrape_enqueue(site, query),\n"
            "  scrape_queue_status(), import_artifact(artifact_path) for vendor\n"
            "  catalogs; make_standard_part for standard families; find_model via\n"
            "  match_model(part_id) for a branded product. Only confidence 'exact'/\n"
            "  attach:true can auto-place; confirm strong/weak matches with the human.\n"
            "  A specific CAD URL goes to fetch_cad_model(url); match_model's\n"
            "  reverse_engineer fallback requires human confirmation before starting\n"
            "  reverse_engineer_product(part_id|url).\n"
            "CUTTING TOOLS: an end mill / drill from the library goes\n"
            "       into the CAM job via cam_tool_import(part_id, number=0) — it maps\n"
            "       the vendor record (tool_diameter_mm→diameter, flutes, coating) into\n"
            "       the tool library with provenance and suggests conservative\n"
            "       stepover/stepdown.  Feeds/speeds ALWAYS come from the CuttingDB\n"
            "       (feeds_speeds / cam_check_feeds_envelope), never the vendor row.\n"
            "BOM + COST: bom_rollup(doc_path) rolls the tree into a flat BOM\n"
            "       (PartRecord sourcing[] rows merge in automatically); bom_price\n"
            "       resolves live prices via approved providers (Nexar, DigiKey,\n"
            "       Mouser); request_quote(doc_path, vendor, qty) for PO decisions;\n"
            "       cam_cost_estimate for made-part cost seeds.\n\n"
            "VENDOR LOGIN ETIQUETTE:\n"
            "  vendors_health(sites?) runs a canary search per adapter and reports\n"
            "  {site, ok, latency_ms, logged_in, hit_count, error, needs_human_login}.\n"
            "  needs_human_login=true means a HUMAN must run vendor_login(site) — a\n"
            "  headed browser window only a person can complete.  Record a question\n"
            "  for the human and continue with the healthy sites; NEVER fabricate\n"
            "  results for a logged-out site, and never retry a login-required site\n"
            "  in a loop.  vendor_status() lists adapter login state cheaply.\n\n"
            "BIAS: prefer the catalog/standard family before custom geometry.\n"
            "  Never invent prices or specs: every number in a recommendation\n"
            "  comes from a tool result."
        )
    if topic in ("cost", "cost_estimate", "cam_cost", "quote", "request_quote", "pricing"):
        return (
            "COST-OUT — made-part cost model + approved quote-out APIs\n\n"
            "Tools: cam_cost_estimate(doc_path, material?, process?) / request_quote(doc_path, vendor, qty?)\n\n"
            "  cam_cost_estimate(doc_path)   Material + machining cost seed from the CAM job.\n"
            "                                Returns cost_usd_low/mid/high, cycle_time_min.\n"
            "                                Call AFTER cam_generate so toolpaths exist.\n"
            "  request_quote(doc_path, vendor, quantity)\n"
            "                                Submit a quote request to an approved provider.\n"
            "                                Returns quote_id; provider contacts out-of-band.\n\n"
            "BIAS: cam_cost_estimate first for internal budget; request_quote for PO decisions.\n"
            "  Never invent prices — use these tools; the provenance rule applies here too."
        )
    if topic in ("spec", "spec_sheet", "spec_sheet_match", "match_part_number",
                 "datasheet", "mpn", "part_number"):
        return (
            "SPEC-SHEET MATCHING — deterministic part-number → standard-part model\n\n"
            "Tools: match_part_number(mpn) / spec_sheet_match(text_or_url, mpn, template) / list_spec_templates()\n\n"
            "  match_part_number(mpn)              Resolve an MPN to a PartRecord (BOLTS/KiCad).\n"
            "                                      Returns part_id usable in part_place.\n"
            "  spec_sheet_match(text_or_url, mpn, template)\n"
            "                                      Extract structured parameters from a datasheet\n"
            "                                      PDF/URL using a named template.\n"
            "  list_spec_templates()               Show available extraction templates.\n\n"
            "WORKFLOW: paste/URL a datasheet → spec_sheet_match → part_place with extracted params.\n"
            "BIAS: use match_part_number before custom geometry — if the MPN resolves, you get\n"
            "  an editable, pre-sourced part in one call."
        )
    if topic in ("civil", "civil_engineering", "site", "site_design", "terrain", "grading",
                 "earthwork", "alignment", "water_distribution", "fire_flow", "sewer",
                 "stormwater", "storm", "drainage", "hydrology", "detention", "culvert",
                 "buried_pipe", "thrust_block", "landxml", "gis", "infrastructure"):
        from services.mcp.civil_guide import GUIDE_TEXT  # noqa: PLC0415
        return GUIDE_TEXT
    if topic in ("piping", "pipe", "pipe_network", "fluid", "pressure_drop",
                 "piping_solve", "piping_report", "piping_route", "piping_spec_check",
                 "piping_bom", "piping_load", "run_pipe_flow", "pipe_routing", "b31.3"):
        return (
            "PIPING — route, solve, code-check, bill and verify pipe systems\n\n"
            "Tools: piping_route / piping_solve / piping_report / piping_spec_check /\n"
            "       piping_bom / piping_load / run_pipe_flow / piping_operating_point /\n"
            "       piping_pump_drive / piping_fluid / piping_internal_volume /\n"
            "       piping_cfd_handoff / piping_verify_cfd\n\n"
            "UNITS: lengths mm (run paths, clearances), pressures kPa GAUGE, temperatures\n"
            "  degC (piping_fluid alone takes T in KELVIN and P in Pa), flows m3/h in\n"
            "  boundary demands, m3/s in run_pipe_flow, velocities m/s.\n\n"
            "THE NETWORK: an aicad.piping/1 JSON doc (contracts/piping_schema.md):\n"
            "  runs[] = {id:'r1', path:[[x,y,z],...] (>=2 pts, mm), nps, schedule, material,\n"
            "            end_type}; fittings[] = {id:'ft1', type:'elbow'|'tee'|'reducer'|\n"
            "            'flange', origin:[x,y,z], nps, params}. piping_route writes runs and\n"
            "  their elbows for you; every tool below takes that doc inline OR the name of\n"
            "  a network already in the session (omitted = the last one).\n\n"
            "WORKFLOW:\n"
            "  0. piping_load(doc_id, network=None)  Resume: restore a network persisted in a\n"
            "                                        doc (after save/reopen) into the session.\n"
            "                                        Several networks in one doc -> it refuses\n"
            "                                        and lists their names; pass network=.\n"
            "  1. piping_route(start={'point':[x,y,z],'dir':[dx,dy,dz]},\n"
            "                  end={'point':[x,y,z],'dir':[...]}, nps, schedule='40',\n"
            "                  material=None, obstacles=[{'min':[..],'max':[..]}],\n"
            "                  clearance_mm=50, grid_mm=None, bend_penalty=None, piping=None,\n"
            "                  name=None, doc_id=None, ignore_bodies=None, network=None,\n"
            "                  fresh=False)\n"
            "     Orthogonal obstacle-avoiding A* (bends penalised). doc_id makes every solid\n"
            "     body of that doc an obstacle — list the equipment you connect TO in\n"
            "     ignore_bodies — and persists the network there. `dir` (optional, snapped to\n"
            "     the nearest axis) is the direction of TRAVEL: start.dir = the way the pipe\n"
            "     leaves start (= that nozzle's outward normal); end.dir = the way it arrives\n"
            "     at end (= MINUS the end nozzle's outward normal). A bare [x,y,z] works too.\n"
            "     The run is APPENDED to piping= / the session network / the doc's saved\n"
            "     network (fresh=True starts a new one); name= names the run.\n"
            "     Returns piping (new run + 90-deg LR elbow at every bend), run_id, path,\n"
            "     length_mm, bends, min_clearance_mm. Refusals: no_route (blocked: widen\n"
            "     the corridor, lower clearance_mm, change grid_mm), start_inside_obstacle /\n"
            "     end_inside_obstacle (move the point to the nozzle face or ignore that body).\n"
            "  2. piping_fluid(name, T=<K>, P=<Pa>)  Resolve fluid properties (sourced values).\n"
            "  3. piping_solve(doc, boundary, fluid=None, pumps=None, doc_id=None, network=None)\n"
            "     1-D steady solve: flows + pressure map. boundary entries name a node AND\n"
            "     carry one condition, e.g.\n"
            "       {'run':'r1','end':'start','pressure':400}   (kPa gauge — fixes it)\n"
            "       {'run':'r1','end':'end','demand':20}        (m3/h, + leaves)\n"
            "     Node ids ('n0','n1') are DERIVED by snapping run endpoints, so they only\n"
            "     exist AFTER a solve (result.node_menu lists them). Address the first\n"
            "     boundary by {'run','end'}; a failed reference returns\n"
            "     addressable_endpoints. At least one node must fix pressure or head.\n"
            "  4. piping_report()                     Per-segment K-factor + dP table (no args\n"
            "                                         = report on the last solve).\n"
            "  5. piping_spec_check(design_pressure_kpa, piping=None, design_temp_c=40,\n"
            "                       corrosion_allowance_mm=1.5, mill_tolerance_frac=0.125,\n"
            "                       joint_factor=1.0, fluid_density_kgm3=1000,\n"
            "                       insulation_kgm=0, doc_id=None, network=None)\n"
            "     ASME B31.3 eq. 3a wall t = PD/(2(SEW+PY)), Y=0.4, allowable stress vs\n"
            "     temperature (A106-B, A53-B, A312-TP304/316), B16.5 flange P-T rating and\n"
            "     support span (2.5 mm sag / bending limit, pipe+fluid+insulation weight).\n"
            "     Per run: t_required_mm vs t_nominal_mm, mawp_kpa, utilization,\n"
            "     support_span_m, pass. all_pass:false -> raise the schedule, change\n"
            "     material or flange class and re-check; never ship a failing line.\n"
            "  6. piping_bom(piping, max_spool_length_mm=12000, doc_id=None, network=None)\n"
            "     Line list, pipe/elbow/tee/reducer/flange quantities with masses, shop\n"
            "     spools broken at flanges and at max spool length, shop/field weld counts.\n"
            "  7. run_pipe_flow(...)                  3-D internal-flow check of a CRITICAL\n"
            "     fitting or manifold (native D3Q19 LBM over the bore). Geometry: either\n"
            "     piping=<doc or network> + run_id (bore and inlet/outlet planes derived from\n"
            "     the run ends) or stl_path / doc_id(+body) of a fluid-volume solid with\n"
            "     inlet={'point','normal'} / outlet={...} (normals point INTO the domain).\n"
            "     fluid='water' (or density_kgm3 + viscosity_pas), mean_velocity_ms or\n"
            "     flow_m3s; resolution cells_across=20 (voxels across the hydraulic\n"
            "     diameter; raise it to refine) or an explicit voxel_mm; max_steps;\n"
            "     background=True -> poll get_job(job_id).\n"
            "     Returns dp_pa, reynolds, regime, converged, pressure_profile and\n"
            "     darcy_estimate_pa; quote dp only when converged and read `caveats` (an\n"
            "     unresolvable physical Re is stated, never hidden). doc_id+name persist a\n"
            "     pipe_flow study.\n"
            "  8. piping_operating_point(pump_curve=<CSV text/path>,\n"
            "                           system={'static_head_m':h0,'k':k})\n"
            "                                        Pump duty point + NPSH margin.\n"
            "  9. piping_pump_drive(network=<name>, run_id=None, speed_rpm=1500,\n"
            "                       standard='IEC'|'NEMA', doc_id=...)\n"
            "     Electric motor for the pump from the last solve's pump run (flow_m3h +\n"
            "     pump_head_m, solve fluid density) via pump_drive_size; flow_m3h + head_m\n"
            "     override it with a piping_operating_point duty point. Saved as\n"
            "     results.pump_drive.<run_id> in the piping_network study.\n\n"
            "1-D vs 3-D: piping_solve is the sizing truth for straight runs and catalogue\n"
            "  fittings (fast, deterministic). Use run_pipe_flow only where the K-factor\n"
            "  model is doubtful — close-coupled fittings, manifolds, tees with strong\n"
            "  branch flow, custom transitions — and compare its dp_pa with the 1-D link\n"
            "  dP. piping_cfd_handoff(doc, boundary) + piping_verify_cfd(solve_result,\n"
            "  cfd_result) remain the one-call bore -> run_cfd_flow chain with a 1-D vs\n"
            "  3-D verdict; piping_internal_volume(doc) gives the bore STEP + volume_mm3.\n\n"
            "FITTINGS + MINOR LOSSES: piping_edit draws runs (tees/elbows appear at\n"
            "  junctions/bends) and adds inline valve/orifice/custom/reducer/flange fittings.\n"
            "  A fixed loss is params.k_model={'kind':'constant','K':k} (also two_k,\n"
            "  three_k, cv, table). A tee's loss is per path: update_fitting(id=<tee>,\n"
            "  params={'k_model_run':{'kind':'constant','K':kr},\n"
            "          'k_model_branch':{'kind':'constant','K':kb}}) -- straight-through K on\n"
            "  run_ids[1], branch K on branch_run, each on that pipe's velocity. Never add a\n"
            "  custom fitting as a stand-in for a tee or bend loss: every fitting is a\n"
            "  piping_bom item. Unknown params keys refuse unknown_param and list the accepted\n"
            "  keys. piping_fitting_k previews K (tees return paths.run / paths.branch).\n\n"
            "GEOMETRY: piping_route paths are centrelines. To model a line as CAD, feed its\n"
            "  path to sketch3d_path_from_points(name, points) then sketch3d_pipe(id,\n"
            "  diameter_mm=<OD>) (cad_guide('sketch3d')).\n\n"
            "PERSISTENCE: pass doc_id (+ network, default 'piping') on route / solve /\n"
            "  spec_check / bom and the network plus its solve, spec_check and bom results\n"
            "  land in that doc's `piping_network` study; save_part() writes it into the\n"
            "  .step+ and piping_load restores it. Cite study results, not chat memory.\n\n"
            "EXTERNALLY-SUPPLIED NETWORKS: piping_solve(source_handoff=<aicad.simhandoff/1\n"
            "  handoff envelope>) consumes a network generated by another domain — e.g.\n"
            "  hydraulic_piping_handoff hands its hose runs over as an inline piping doc with\n"
            "  boundary + fluid in the envelope meta; runs may carry an explicit bore_mm to\n"
            "  bypass the NPS schedule catalog (hydraulic hose IDs aren't catalog pipes).\n\n"
            "Vessels, ducts and pipe-rack/skid frames: cad_guide('industrial'). Decision\n"
            "  procedure: cad_skill('piping-design')."
        )
    if topic in ("industrial", "industrial_design", "plant", "vessel", "pressure_vessel",
                 "tank", "vessel_size", "duct", "ducting", "hvac", "duct_size", "skid",
                 "pipe_rack"):
        return (
            "INDUSTRIAL — pressure vessels, duct systems, skids and pipe racks\n\n"
            "Tools: vessel_size / duct_size / piping_route ... (cad_guide('piping')) /\n"
            "       struct_analyze / struct_check / struct_estimate / add_features\n"
            "       (weld_member frames) / run_pipe_flow\n\n"
            "UNITS: mm for geometry, kPa GAUGE for pressure, degC, m3/s for air/fluid flow,\n"
            "  m for duct segment lengths, Pa for duct pressure drops.\n\n"
            "PRESSURE VESSELS — vessel_size(inner_diameter_mm, design_pressure_kpa,\n"
            "  tan_tan_length_mm=None | volume_m3=None, design_temp_c=40, material='SA-516-70'\n"
            "  (also SA-240-304, SA-240-316, SA-285-C), corrosion_allowance_mm=3,\n"
            "  joint_efficiency=0.85, head_type='ellipsoidal_2_1'|'hemispherical'|\n"
            "  'torispherical', fluid_density_kgm3=1000, fill_fraction=0.8,\n"
            "  orientation='vertical'|'horizontal', doc_id=None, name=None)\n"
            "  ASME VIII-1: shell UG-27(c)(1) t = PR/(SE-0.6P), heads UG-32; nominal\n"
            "  thickness rounded up to standard plate. Returns shell/heads t_required_mm,\n"
            "  t_nominal_mm, mawp_kpa, head depth_mm, hydrotest_kpa (UG-99(b)), volume_m3,\n"
            "  tan_tan_length_mm and empty/operating/full-of-water masses.\n"
            "  Then MODEL it: revolve the shell+head profile at the returned thicknesses\n"
            "  (cad_guide('overview')), add nozzles, and route piping to the nozzle faces\n"
            "  with piping_route(doc_id=..., ignore_bodies=[<vessel body>]).\n\n"
            "DUCTS — duct_size(segments=[{id, parent=None, airflow_m3s, length_m,\n"
            "  k_fittings=0, shape='round'|'rect', aspect_ratio=2}, ...],\n"
            "  method='equal_friction', friction_pa_per_m=1.0, max_velocity_ms=10,\n"
            "  air_temp_c=20, roughness_mm=0.09, doc_id=None, name=None)\n"
            "  Segments form a tree through `parent` (the fan end has none). Standard round\n"
            "  sizes, Huebscher equivalent diameter for rectangular ducts, Colebrook/\n"
            "  Clamond friction. Returns per-segment size, velocity, Reynolds, friction and\n"
            "  fitting dP, the critical_path and fan_static_pa (size the fan from it).\n\n"
            "SKIDS, PIPE RACKS, SUPPORTS — structural frames:\n"
            "  geometry: sketch member centrelines, then add_features with a weld_member op\n"
            "  (real stock profile, miter/butt/cope; cad_skill('weldment-frame')).\n"
            "  analysis: build an aicad.structure/1 doc (part_place(domain='section') for\n"
            "  AISC sections) carrying the pipe/vessel operating loads, then\n"
            "  struct_check(doc_path) for AISC LRFD D/C ratios and struct_analyze for raw\n"
            "  member forces; struct_estimate for a rate-table budget. Pipe support spacing\n"
            "  comes from piping_spec_check support_span_m — place supports at or inside it.\n\n"
            "PERSISTENCE: doc_id + name on vessel_size / duct_size persist a `vessel` /\n"
            "  `duct_system` study in that doc (saved into the .step+ by save_part).\n"
            "  Decision procedure: cad_skill('piping-design')."
        )
    if topic in ("hydraulic", "hydraulics", "iso1219", "hydraulic_circuit",
                 "hydraulic_new", "cylinder", "pump", "fluid_power", "hpu", "power_unit",
                 "hydraulic_simulate", "hydraulic_motor", "pump_drive"):
        return (
            "HYDRAULIC — ISO 1219 fluid power: circuit, sizing, electric drive, transient\n"
            "simulation, rigid lines, BOM\n\n"
            "Units: bar (gauge), L/min, mm, N, N·m, rpm, kW, s. Decision procedure:\n"
            "  cad_skill('hydraulic-design').\n\n"
            "WORKFLOW (signatures match the live tools):\n"
            "  0. hydraulic_load(doc_id, name=None)   After a reopen: restore the saved\n"
            "     circuit (+ sizing / bom / simulate + valve schedule) as the ACTIVE circuit.\n"
            "     Never rebuild a saved circuit from chat memory.\n"
            "  1. hydraulic_new(name, doc_id=None)    Create the circuit; it becomes the\n"
            "     ACTIVE circuit, so later calls may omit `doc`. Pass the CAD doc_id on this\n"
            "     and EVERY later call: the circuit and each analysis merge into that doc's\n"
            "     `hydraulic_circuit` study (saved into the .step+ by save_part).\n"
            "  2. hydraulic_add_component(type, name='', x=0, y=0, params={}, sourcing={},\n"
            "                             doc=None, doc_id=None)\n"
            "     `type` is a frozen ISO 1219 member: pump, cylinder_linear, cylinder_rotary,\n"
            "     valve_directional, valve_check, valve_relief, valve_flow_control,\n"
            "     accumulator, reservoir, motor_hydraulic, filter, heat_exchanger.\n"
            "     Common spellings map on (cylinder->cylinder_linear, motor->motor_hydraulic,\n"
            "     check_valve->valve_check, tank->reservoir), but a bare 'valve' is REFUSED —\n"
            "     four valve types exist. Returns component_id ('c1', 'c2', ...).\n"
            "     Params the simulator needs (defaults in parens):\n"
            "       pump displacement_cc_rev + speed_rpm (or flow_lpm), vol_efficiency (0.95),\n"
            "         compensator_bar for a pressure-compensated pump, max_pressure_bar rating\n"
            "       cylinder_linear bore_mm, stroke_mm, rod_mm (0), actuator_type\n"
            "       motor_hydraulic displacement_cc_rev, vol/mech_efficiency (0.95/0.9)\n"
            "       valve_relief set_pressure_bar (override_bar 10 % of set)\n"
            "       valve_directional center_condition closed|tandem|open|float, capacity\n"
            "         Kv | Cv | orifice_area_mm2 | rated_flow_lpm + rated_dp_bar\n"
            "       valve_flow_control set_flow_lpm | Kv | Cv; valve_check cracking_pressure_bar\n"
            "       accumulator volume_l + pre_charge_bar; reservoir pressure_bar (0)\n"
            "  3. hydraulic_add_hose(net_name, connections, doc=None, doc_id=None)\n"
            "     connections is a LIST of ports, not from/to:\n"
            "       [{'component_id':'c1','port':'P'}, {'component_id':'c2','port':'P'}]\n"
            "     Port names (case-insensitive): pump P/T/L; cylinder_linear A (cap)/B (rod);\n"
            "     motor_hydraulic A/B/L; valve_directional P/T/A/B; valve_relief P/T;\n"
            "     check/flow-control/filter/heat_exchanger IN/OUT; reservoir/accumulator any.\n"
            "     Rated hoses carry max_pressure_bar (+ id_mm/length_mm for the piping leg).\n"
            "  4. SIZE (no circuit needed; component_id + doc_id file the result under\n"
            "     results.sizing.<kind>.<component_id>):\n"
            "     hydraulic_size_cylinder(bore_mm, pressure_bar, stroke_mm, rod_mm=0,\n"
            "                             velocity_mm_s=50, efficiency=0.95)\n"
            "       FORWARD sizing (bore -> force): extend/retract_force_kn, flow_extend_lpm,\n"
            "       cycle_time_s. For a required force pick d from F = pi/4 d^2 P, confirm.\n"
            "     hydraulic_size_motor(torque_nm, speed_rpm, pressure_bar, mech_efficiency=0.9,\n"
            "                          vol_efficiency=0.95, type=gear|vane|piston|orbital)\n"
            "       pressure_bar is the DIFFERENTIAL across the motor. Returns exact\n"
            "       displacement_cc_rev + standard_displacement_cc_rev (catalog step), flow_lpm.\n"
            "     hydraulic_size_pump(flow_lpm, pressure_bar, efficiency=0.85, speed_rpm=1500)\n"
            "       Sum the actuator flows that run together -> displacement, shaft kW, torque.\n"
            "  5. pump_drive_size(kind='hydraulic', flow_lpm, pressure_bar, speed_rpm=1500,\n"
            "                     standard='IEC'|'NEMA', service_factor=1.15, voltage_v=400,\n"
            "                     phases=3, frequency_hz=50, efficiency_class='IE3')\n"
            "     The electric motor: required_motor_kw, motor_rating_kw (IEC) or\n"
            "     motor_rating_hp (NEMA), poles, sync_speed_rpm, frame, full_load_current_a.\n"
            "     kind='centrifugal' takes flow_m3h + head_m; for a SOLVED piping network use\n"
            "     piping_pump_drive (cad_guide('piping')).\n"
            "  6. hydraulic_simulate(duration_s=5, valve_schedule=[...], loads={...},\n"
            "                        fluid={density_kgm3, bulk_modulus_bar, viscosity_cst},\n"
            "                        initial={...}, output_dt_s=0.01, doc_id=None)\n"
            "     Lumped-parameter transient: hose nets are capacitive nodes, orifice valve\n"
            "     flow, relief override, cylinder/motor dynamics with end stops, polytropic\n"
            "     accumulators, energy bookkeeping.\n"
            "       valve_schedule [{'t_s':0.1,'component_id':'c3','position':'a'}]\n"
            "         (a | b | center | 0..1 proportional)\n"
            "       loads {'c4':{'force_n':10000,'mass_kg':200}} / {'c6':{'torque_nm':80}}\n"
            "     Read summary: peak_pressure_bar, relief_open_time_s, cylinders.<id>\n"
            "     stroke_time_s / max_velocity_mms / end_reached, motors.<id> steady_speed_rpm,\n"
            "     pumps.<id> mean_power_kw, efficiency, heat_kj. Series are cut to <=400 points;\n"
            "     series_path is the full-resolution JSON. Refusals name the fix:\n"
            "     unconnected_port, unknown_port, missing_params (missing:['c4.bore_mm']),\n"
            "     unknown_component, invalid_schedule, no_pressure_source.\n"
            "  7. CHECK: peak_pressure_bar vs the relief set pressure (+ override) and every\n"
            "     rating — `ratings.exceeded` lists components/hoses whose peak node pressure\n"
            "     passes their max_pressure_bar. Stroke time vs requirement; relief open\n"
            "     during a move = wasted power (heat_kj). Fix the circuit, re-simulate.\n"
            "  8. hydraulic_piping_handoff(supply_pressure_bar, flow_lpm, load_pressure_bar)\n"
            "     Rigid tube / hose runs -> the piping 1-D solver for REAL per-line pressure\n"
            "     drops (each hose keeps its bore); losses feed the pump margin\n"
            "     (margin.pump_margin_solved_bar). Verify: hydraulic_verify_piping(). The\n"
            "     generated network re-solves with piping_solve(source_handoff=...).\n"
            "  9. hydraulic_bom(doc=None, doc_id=None)   BOM lines + missing_mpn summary.\n\n"
            "MOUNT LOADS: hydraulic_fea_handoff(bore_mm, max_pressure_bar, rod_mm=0, axis=...)\n"
            "  -> run_fea_static-ready fea_args per case (extend/retract at max pressure;\n"
            "  axis takes [x,y,z] or 'z'/'-x'/'up'), then hydraulic_verify_fea(handoff,\n"
            "  fea_result) — cad_guide('simulation') CROSS-SIM HANDOFF.\n\n"
            "PERSISTENCE: with doc_id each tool merges into the doc's `hydraulic_circuit`\n"
            "  study (one per circuit name). Editing the circuit drops its stored simulate /\n"
            "  bom (they described the old circuit); sizing is kept. The Hydraulics panel\n"
            "  opens the same study."
        )
    if topic in ("firmware", "fw", "embedded", "mcu", "fw_build", "fw_new_project",
                 "flash", "debug", "fw_sim_run", "fw_verify_board", "coverify",
                 "fw_power_profile", "powerprofile", "power_profile"):
        return (
            "FIRMWARE — embedded compile loop: build, sim, flash, debug\n\n"
            "Tools: fw_status / fw_new_project / fw_build / fw_sources / fw_sim_run /\n"
            "       fw_verify_board / fw_power_profile / fw_flash / fw_debug_start /\n"
            "       fw_debug_halt / fw_debug_read_mem\n\n"
            "WORKFLOW:\n"
            "  fw_status()                          FIRST CALL — provisioning doctor. Reports\n"
            "                                       each toolchain/emulator/flasher (found,\n"
            "                                       version, path, install hint) plus per-MCU-\n"
            "                                       family can_build/can_emulate/can_flash.\n"
            "                                       Never fails; use it to explain any\n"
            "                                       toolchain_not_found before retrying.\n"
            "  fw_new_project(name, mcu_family)     Scaffold a CMake firmware project.\n"
            "                                       Auto-selects toolchain; generates pinmap.h\n"
            "                                       from linked ECAD board netlist.\n"
            "  fw_sources(project_id, add)          Add source files to the project.\n"
            "  fw_build(project_id)                 Compile; returns ELF path + errors.\n"
            "  fw_sim_run(project_id, scenario)     Run ELF through Renode/QEMU with assertions:\n"
            "                                       uart:<text>, gpio:PA5=1 (real pin states),\n"
            "                                       gpio:PA5=1@>10ms (timeline). Emits a JSON\n"
            "                                       trace artifact (trace_path) of pin+UART\n"
            "                                       events; QEMU pin events need the project's\n"
            "                                       chudware_trace.h CHUDWARE_GPIO_TRACE macro.\n"
            "  fw_verify_board(board_doc_ref, ...)  Firmware<->board co-verification\n"
            "                                       BEFORE fab: static checks (every PIN_<NET>\n"
            "                                       exists on the MCU/net, strap/reset pins\n"
            "                                       BOOT0/NRST/GPIO0 not repurposed, direction\n"
            "                                       clashes vs the schematic netlist, decoupling\n"
            "                                       per MCU supply rail) + dynamic diff of\n"
            "                                       emulated pin activity vs the netlist (pass\n"
            "                                       trace_path=<elf>.trace.json or emulate=true).\n"
            "                                       Findings use the ERC/DRC violation shape\n"
            "                                       {rule, severity, net, pins, message, fix}.\n"
            "                                       Pass sch_doc_ref for direction checks.\n"
            "  fw_power_profile(trace_path, ...)    Power/duty-cycle profile\n"
            "                                       (aicad.powerprofile/1, contract §11) from\n"
            "                                       an existing trace: CPU run/sleep residency\n"
            "                                       (CHUDWARE_CPU_TRACE markers, else assumed\n"
            "                                       active), per-pin GPIO duty, and per-rail\n"
            "                                       i_avg/i_peak from the board's\n"
            "                                       eng.supply.rails currents weighted by duty.\n"
            "                                       BOOKKEEPING not SPICE; nulls stay null.\n"
            "                                       rails[*].schedule = {duty, period_s} — feed\n"
            "                                       it straight to run_fea_thermal transient\n"
            "                                       heat-load schedules (duty-cycle heating).\n"
            "                                       fw_sim_run auto-emits <elf>.power.json\n"
            "                                       (power_profile_path) for board-linked\n"
            "                                       projects.\n"
            "  fw_flash(elf_path, chip, port)       Flash ELF to target hardware.\n"
            "  fw_debug_start/halt/read_mem(chip)   GDB-over-SWD debug session controls.\n\n"
            "SUPPORTED TARGETS (Renode platform pack — what the bundled platform\n"
            "actually models; fw_sim_run refuses honestly when an assertion needs a\n"
            "peripheral the platform lacks):\n"
            "  family    emulate  uart gpio-observe i2c  spi  timers adc  notes\n"
            "  stm32f4   renode   yes  yes          yes  yes  yes    no   USART1/2, GPIO A-E, I2C1-3, SPI1-3, TIM1-5\n"
            "  stm32f1   renode   yes  yes          yes  no   yes    no   upstream stm32f103.repl (USART1-5, GPIO A-G)\n"
            "  nrf52     renode   yes  no           yes  yes  yes    no   upstream nrf52840.repl; gpio: asserts unsupported\n"
            "  rp2040    minimal  yes  no           no   no   no     no   bare-metal PL011 UART only (no stock Renode model)\n"
            "  esp32     NONE     -    -            -    -    -      -    build (idf.py) + flash (esptool) ONLY, no emulation\n"
            "  cortex-m  renode   no   no           no   no   no     no   generic fallback (CPU+memory); QEMU semihosting path\n"
            "  (no family has an ADC peripheral model upstream — but see adc_waveform below:\n"
            "   a Python-peripheral stub feeds programmed samples on stm32f4/stm32f1)\n\n"
            "SENSOR STIMULUS SCENARIOS (aicad.fwscenario/1 — contract §9):\n"
            "  fw_sim_run's scenario param drives simulated sensors/GPIO into the emulated\n"
            "  firmware (Renode path only). Pass a fixture name or a JSON object:\n"
            "    fixtures: fixture:i2c_temp_tmp103   I2C TMP103 temp sensor @0x70, 25.5 C\n"
            "              fixture:spi_imu_stub      SPI IMU connectivity stub on spi1\n"
            "              fixture:quadrature_encoder PA0/PA1 encoder, 24 steps CW @5ms\n"
            "              fixture:adc_thermistor    NTC heat-up curve via ADC register stub\n"
            "              fixture:user_button       3x 80ms presses on PA0\n"
            "  track kinds: gpio_edges | button | quadrature | i2c_sensor | i2c_registers |\n"
            "               spi_stub | uart_rx | adc_waveform (sine/ramp/samples; stm32 only)\n"
            "  WORKED EXAMPLE — firmware reads an I2C temp sensor + button, prints readings:\n"
            "    fw_sim_run(project_id, scenario='{\n"
            "      \"schema\": \"aicad.fwscenario/1\",\n"
            "      \"tracks\": [\n"
            "        {\"kind\": \"i2c_sensor\", \"model\": \"tmp103\", \"bus\": \"i2c1\",\n"
            "         \"address\": 72, \"temperature_c\": 31.0},\n"
            "        {\"kind\": \"button\", \"pin\": \"PA0\",\n"
            "         \"presses\": [{\"t_ms\": 200, \"hold_ms\": 80}]}\n"
            "      ]}',\n"
            "      assertions=['uart:temp=31', 'uart_within:temp=31@2000ms',\n"
            "                  'toggles:PA5>=2', 'period:PA5=100ms+-20ms'])\n"
            "  Assertion forms: toggles:<PIN>>=<N> (toggle count),\n"
            "  period:<PIN>=<P>ms+-<TOL>ms (mean rising-edge period),\n"
            "  uart_within:<text>@<T>ms (needs scenario time marks). Invalid/unsupported\n"
            "  scenarios refuse honestly (invalid_scenario / scenario_error) — e.g.\n"
            "  adc_waveform on nrf52 explains that no ADC stub window is registered.\n\n"
            "BIAS: always fw_sim_run before fw_flash — catches assertion failures without\n"
            "  hardware.  Link the ECAD board doc so pinmap.h is auto-generated.\n\n"
            "GOLDEN DEMO: scripts/demo_sensor_node.py builds an STM32F4 sensor-\n"
            "  node board, runs fw_new_project + fw_verify_board, and fw_sim_run with\n"
            "  fixture:i2c_temp_tmp103 when Renode is provisioned (honest SKIP markers\n"
            "  otherwise). See docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md +\n"
            "  cad_guide('simulation')."
        )
    if topic in ("structural", "structure", "struct", "struct_analyze", "struct_check",
                 "struct_estimate", "beam", "frame", "truss", "steel_design"):
        return (
            "STRUCTURAL — member forces & deflections for aicad.structure/1 docs (PyNite)\n\n"
            "Tools: struct_analyze / struct_check / struct_estimate\n\n"
            "WORKFLOW:\n"
            "  Build structure doc via part_place(domain='section') for beams/columns.\n"
            "  struct_analyze(doc_path, load_combo)  Member forces, deflections, reactions.\n"
            "                                        load_combo: 'DEAD_LIVE' (default),\n"
            "                                        'DEAD_LIVE_WIND', 'DEAD_LIVE_SEISMIC'.\n"
            "  struct_check(doc_path, load_combo)    Code check (AISC 360 / NDS) — PASS/FAIL\n"
            "                                        per member with UC ratios.\n"
            "  struct_estimate(doc_path)             Quick material + fabrication cost estimate.\n\n"
            "BIAS: struct_check after struct_analyze — the check references the analysis results.\n"
            "  Use part_search('W8x31') + part_place for AISC sections; standard-before-custom."
        )
    if topic in ("regen", "regen_diagnose", "rebuild", "repair", "fix_ops"):
        return (
            "REGEN DIAGNOSIS — on a rebuild failure, diagnose + propose editable fix-ops\n\n"
            "Tool: regen_diagnose(document_json, reference_json?)\n\n"
            "  regen_diagnose(document_json)         Diagnose a doc that failed to rebuild.\n"
            "                                        Returns a verdict card: root cause,\n"
            "                                        proposed fix-ops as an accept/edit/reject\n"
            "                                        card, and a correction trace.\n\n"
            "WHEN TO USE: call after add_features returns any feature with ok=false, or when\n"
            "  close_part / export_part / run_fea_* fail with a regen error.\n"
            "WORKFLOW: add_features → check feature ok flags → if any false, regen_diagnose\n"
            "  → apply the proposed fix-ops → re-run add_features → verify_battery."
        )
    if topic in ("code_compliance", "code_check", "building_code", "compliance",
                 "ibc", "asce", "nfpa"):
        return (
            "CODE COMPLIANCE — deterministic rule engine + agentic RAG for building-code checks\n\n"
            "Tool: code_check(doc_path, codes?, jurisdiction?)\n\n"
            "  code_check(doc_path)                  Run compliance checks against applicable\n"
            "                                        building codes (IBC, ASCE 7, NFPA, etc.).\n"
            "                                        Returns PASS/WARN/FAIL per clause with\n"
            "                                        code citation and remediation hint.\n"
            "  code_check(doc_path, codes=['IBC2021'], jurisdiction='CA')\n"
            "                                        Scope to specific code edition + jurisdiction.\n\n"
            "BIAS: run code_check after struct_check for anything that will be permitted.\n"
            "  RAG citations are sourced from the research tools (research_fetch_page) —\n"
            "  the model never recalls code text from training."
        )
    if topic in ("serving", "serving_status", "adapter", "lora", "canary",
                 "serving_promote", "serving_rollback", "serving_evaluate"):
        return (
            "ADAPTER SERVING — per-insertion-point LoRA routing: status, canary, promote, rollback\n\n"
            "Tools: serving_status / serving_set_canary / serving_rollback /\n"
            "       serving_evaluate / serving_promote\n\n"
            "  serving_status()                      Show which adapter version is live at each\n"
            "                                        insertion point + current canary %, eval\n"
            "                                        scores, and promotion eligibility.\n"
            "  serving_set_canary(insertion_point, pct)\n"
            "                                        Dial canary traffic: 0=off, 100=full.\n"
            "  serving_evaluate(insertion_point, shadow_log)\n"
            "                                        Score a shadow log against the baseline.\n"
            "  serving_promote(insertion_point, adapter_version, evidence)\n"
            "                                        Promote after evidence + human sign-off.\n"
            "                                        Auto-promote is disabled; always gated.\n"
            "  serving_rollback(insertion_point, to_version?)\n"
            "                                        Instantly revert to last stable version.\n\n"
            "BIAS: evaluate before promote; rollback is always safe and instant."
        )
    if topic in ("tolerance_stackup", "stackup", "stackup_analyze", "tolerance_stack"):
        return (
            "TOLERANCE STACK-UP ANALYSIS — 1D worst-case + RSS over a dimension chain\n\n"
            "Tool: stackup_analyze(dims, mode='both')\n\n"
            "  stackup_analyze(dims)              Compute worst-case AND RSS for a 1D\n"
            "                                     dimension chain.  Returns nominal_gap,\n"
            "                                     total_tol, gap_max/gap_min for each\n"
            "                                     mode, and contributors[] sorted by\n"
            "                                     worst-case sensitivity (highest first).\n\n"
            "  dims format (list of dicts):\n"
            "    { name, nominal, tol, direction (+1/-1), ref?, assumed? }\n"
            "    tol = half-tolerance (± value, >= 0).\n"
            "    direction = sign in the 1D chain (+1 adds to gap, -1 subtracts).\n"
            "    assumed = true if tol was defaulted (honest-degrade: reported separately).\n\n"
            "  mode: 'worst_case' | 'rss' | 'both' (default).\n\n"
            "HONEST DEGRADE: if the CAD doc lacks tolerance data on a dimension, pass\n"
            "  assumed=true and a reasonable tol.  The tool lists all assumed dim names\n"
            "  in assumed_dims[] so the caller can flag them in the report.\n\n"
            "SENSITIVITY RANKING: contributors[] is sorted descending by contrib_wc\n"
            "  (= |direction| * tol).  The first row is the dimension that dominates\n"
            "  the stack -- the highest-leverage target for tolerance tightening.\n\n"
            "DISCLAIMER: result carries 'Preliminary stack-up -- not a certified tolerance\n"
            "  analysis. Verify with a qualified dimensional-metrology engineer before\n"
            "  release.' Always surface this to the user.\n\n"
            "REFERENCE: Fischer, 'Mechanical Tolerance Stackup and Analysis,' 2nd ed.,\n"
            "  CRC Press 2011, Ch. 3 (equal-bilateral 1D chain formulas).\n\n"
            "WORKFLOW: review critical dims -> dfm_check() for process capability ->\n"
            "  stackup_analyze() for assembly gap variation -> fit_report() for\n"
            "  modeled-vs-measured clearance check."
        )
    if topic in ("design_review", "review_battery", "review_design", "full_review"):
        return (
            "DESIGN REVIEW BATTERY — one-call battery: overlap + DFM + verify narrated verdict\n\n"
            "Tool: design_review(doc_id, checks?)\n\n"
            "  design_review(doc_id)                 Dispatch the full review battery:\n"
            "                                        feature_overlap, dfm_check, wall_thickness,\n"
            "                                        draft_analysis, interference + interface_check\n"
            "                                        (assemblies/multi-body).\n"
            "                                        Returns a narrated verdict card per\n"
            "                                        contracts/narration.md — never raw JSON.\n"
            "  design_review(doc_id, checks=['dfm', 'overlap'])\n"
            "                                        Scope to specific checks.\n"
            "  design_review(doc_id, req_ids=['R1'], req_path?)\n"
            "                                        ALSO auto-verify aicad.req/1 requirements:\n"
            "                                        each runs its verify_by verifiers and the\n"
            "                                        status flip + evidence ref is written back\n"
            "                                        into the requirements doc automatically.\n\n"
            "INTERFACE ALIGNMENT (assemblies/multi-body): interface_check(doc_id) is part of the\n"
            "  battery and also stands alone. It catches the layout errors that PASS interference\n"
            "  (nothing overlaps) but are still wrong — bolt holes that don't register, mating\n"
            "  bores that aren't coaxial, contact faces that aren't flush, a fastener that can't\n"
            "  pass the stack. Run it while laying out a multi-component assembly so the fit is\n"
            "  right the first time; pass fastener='M6' to also check pass-through. Every finding\n"
            "  names the components, the feature, the numeric error, and a fix direction.\n\n"
            "RELATIONSHIP TO verify_battery: design_review is geometry-focused (what's wrong\n"
            "  with the shape); verify_battery is requirement-focused (does it meet the spec).\n"
            "  Run both before declaring done."
        )
    if topic in ("doc_diff", "diff", "compare", "revision", "doc_diff_tool"):
        return (
            "DOC DIFF — semantic feature-level diff between two document revisions\n\n"
            "Tool: doc_diff_tool(rev_a, rev_b)\n\n"
            "  doc_diff_tool(rev_a, rev_b)           Semantic diff of two part/assembly docs.\n"
            "                                        Returns: added/removed/changed features\n"
            "                                        with parameter deltas, body count change,\n"
            "                                        and a human-readable summary string.\n\n"
            "WORKFLOW: snapshot doc = close_part(doc_id, return_doc=True) → modify → diff.\n"
            "  Or compare two versions from PLM history (see cad_guide('plm')).\n"
            "BIAS: diff after every significant edit session so the change record is explicit."
        )
    if topic in ("plm", "versioning", "snapshot", "history", "project_vault"):
        return (
            "PLM / PROJECT VAULT — snapshot-based project history + CAD graph\n\n"
            "A project is a folder + hidden .aicadvault (history) + derived .plm/.\n"
            "Open is idempotent: plm_open/plm_init on an existing vault reuses it,\n"
            "never wipes history. Pillar 19 nouns only: Project / History / Checkpoint.\n\n"
            "PROJECT HISTORY (plm_* — services/mcp/plm_tools.py):\n"
            "  plm_open(path) / plm_init(path)   open or create vault (idempotent)\n"
            "  plm_status(path)                 items, dirty, head, ai_mode, remotes\n"
            "  plm_save(path, kind=auto|checkpoint)\n"
            "  plm_checkpoint(path, message='', item_id='')\n"
            "  plm_history(path)                timeline entries (newest first)\n"
            "  plm_restore(path, commit, item_id='')\n"
            "  plm_release(path, item_id, message='', confirm=False)\n"
            "  plm_compare(path, commit_a, commit_b, item_id, visual=False)\n"
            "  plm_set_ai_mode(path, mode)      manual | assisted | managed\n"
            "                                   managed → AI cuts vault checkpoints\n"
            "                                   at milestones (author=ai, discardable)\n"
            "  plm_item_set / plm_where_used    item identity + consumers\n"
            "  plm_timeline / plm_rollback      AI-VC per-doc silent checkpoints\n"
            "  plm_add_remote / plm_sync / plm_lock / plm_unlock\n\n"
            "CAD GRAPH (cad_graph_* — services/mcp/graph_tools.py):\n"
            "  cad_graph(query_kind, node_id='', src='', dst='', project_dir='')\n"
            "    query_kind: get | blast | path | tree\n"
            "    Traverses dimensional links + assembly structure for the project.\n"
            "  cad_graph_link(src, dst, kind, note='', project_dir='')\n"
            "    Author an AI link (origin='ai') between two nodes; src/dst may be\n"
            "    cross-project qualified '<project_key>/<node_id>'.\n"
            "  assembly_plan_review(project_dir='', doc_id='')\n"
            "    Best-practice structure review before a large build.\n\n"
            "GRAPH WEB (plm_graph_* — keywords, files, office docs, cross-project):\n"
            "  The graph covers more than CAD: `file` nodes (office docs, PDFs,\n"
            "  images, exchange files) and `keyword` nodes (kw:<kw>, TAGGED edges).\n"
            "  plm_graph_keyword(path, target, keyword, op=add|remove|list)\n"
            "    Tag any node or project file with a keyword of interest.\n"
            "  plm_graph_search(project_dir, q='', kinds='', keyword='', linked=False)\n"
            "  plm_graph_neighbors(project_dir, node_id, depth=1, linked=False)\n"
            "    Navigate the web: find nodes, expand around one.\n"
            "  plm_graph_brief(project_dir, node_id)\n"
            "    One-node dossier: keywords, degree, neighbors, links, watched.\n"
            "  plm_graph_links(project_dir, op=list|remove, link_id='')\n"
            "  plm_graph_suggest(project_dir, node_id='', limit=10)\n"
            "    Ranked link suggestions (name/content/folder/keyword heuristics).\n"
            "  plm_graph_watch(path, op=add|remove|list, keyword='', node_id='')\n"
            "    Register keywords of interest; plm_graph_changes(project_dir)\n"
            "    diffs the graph since the last snapshot and flags watched nodes.\n"
            "  plm_graph_configure(path, op=get|set, office_content=…,\n"
            "    add_linked_project='…', remove_linked_project='…', project_key='…')\n"
            "    office_content=true → office text enters the graph (text_preview +\n"
            "    part-number mention edges). linked_projects → other project roots\n"
            "    merge into one graph (linked=True on search/neighbors/get); same\n"
            "    keyword in two projects is ONE shared node — the web spans projects.\n"
            "  Example: plm_graph_keyword(path, 'Specs/motor.pdf', 'motor-driver') →\n"
            "    plm_graph_link('doc:Bracket.step+', 'file:Specs/motor.pdf',\n"
            "    'REFERENCES') → plm_graph_search(project_dir, keyword='motor-driver').\n\n"
            "AI ENABLEMENT (bridge precedence):\n"
            "  1. manifest ai_mode='managed'  → agent milestones auto-checkpoint\n"
            "  2. CHUDWARE_PLM_BRIDGE=1          → real vault client (still needs managed\n"
            "                                   for AI-authored cuts)\n"
            "  3. default off                 → MockVault / no project History writes\n"
            "AI-VC (plm_timeline) is always-on for agent docs and is separate from\n"
            "project History checkpoints.\n\n"
            "See also: cad_guide('doc_diff') for semantic feature diffs."
        )
    if topic in ("mechanical", "gear_check", "bearing_life", "gear", "bearing",
                 "agma", "iso281", "transmission", "power_transmission"):
        return (
            "MECHANICAL CHECKS — AGMA gear tooth stress + ISO 281 bearing L10 life\n\n"
            "Tools: gear_check / bearing_life\n\n"
            "  gear_check(Wt, module, face_width, pitch_diameter, J, I, ...)\n"
            "    AGMA 2001 bending + contact (pitting) stress for spur gears.\n"
            "    Returns sigma_bending_MPa, sigma_contact_MPa, SF_bending, SF_contact.\n"
            "    J (bending geometry factor) and I (contact geometry factor) are required inputs\n"
            "    from AGMA 908 charts or textbook tables (not bundled — cite your source).\n"
            "    Default Cp=191 MPa^0.5 (steel/steel, Shigley's Table 14-8).\n"
            "    Supply allowable_bending_stress (S_t) and allowable_contact_stress (S_c)\n"
            "    to get safety factors; omit for stress-only output.\n\n"
            "  bearing_life(C, Fr, Fa, n_rpm, bearing_type, ...)\n"
            "    ISO 281 L10 life: L10h = 10^6/(60n) * (C/P)^p.\n"
            "    C = dynamic load rating (N) — supply from manufacturer datasheet.\n"
            "    The parts catalog (bearing:ISO 15:*) carries bore/od/width only — no C.\n"
            "    Never fabricate C; if unknown, ask the user or mark the field TBD.\n"
            "    bearing_type: 'ball' (p=3) or 'roller' (p=10/3).\n\n"
            "Both tools return a disclaimer — preliminary estimates, not certified analysis.\n"
            "Reference: Shigley's MED 10th ed., Ch. 11 (bearing) + Ch. 14 (gear)."
        )
    if topic in ("sketch3d", "3d_sketch", "3dsketch", "sketch_3d",
                 "sketch3d_tools", "sketch_preview", "guide_curve", "sweep_guide"):
        return (
            "3D SKETCH (ENGINE FEATURE) + SKETCH PREVIEW TOOLS\n\n"
            "A `sketch3d` is a REAL doc feature (doc_schema \u00a73B): free-space entities in\n"
            "world mm (point / line / 3-POINT arc / spline), a constraint solver, and compiled\n"
            "3D wire chains a downstream sweep consumes as path (spine) or guides[] entry.\n"
            "It persists with the document (save/undo/redo).\n"
            "Full contract: contracts/sketch3d_gui_contract.md.\n\n"
            "\u2500\u2500 AUTHOR A 3D PATH \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_path_from_points(name, points, fit='polyline'|'spline', close=False)\n"
            "      ONE CALL: feature through the given world points. 'polyline' = crisp bends\n"
            "      (a line per pair); 'spline' = one smooth cubic through all. Returns the\n"
            "      feature id (e.g. 'f3') + wires + health.  PREFER THIS for routing paths.\n"
            "  sketch3d_new(name)                  Empty sketch3d feature; returns {id}.\n"
            "  sketch3d_add_curves(id, curves)     Append entities. Curve specs {kind, pts}:\n"
            "      'line' 2 pts | 'polyline' 2+ | 'spline' 2+ through-points |\n"
            "      'arc' 3 pts [start, mid, end] (circumcircle) | 'point' 1 pt (anchor)\n"
            "      \u2014 or raw \u00a73B entities {type, p1/p2/pm/points}. Touching endpoints\n"
            "      (1e-4 mm) chain into wires automatically.\n"
            "      'eq_curve3d' kind (T-3DEQ, 0 DOF \u2014 equation-fixed): xt/yt/zt expression\n"
            "      strings over t\u2208[t1,t2] (doc equations \u00a74, degrees trig). Helix example:\n"
            "      {kind:'eq_curve3d', xt:'R*cos(t)', yt:'R*sin(t)', zt:'P*t/360', t1:0, t2:720}\n"
            "      Endpoints (p1@t1/p2@t2) chain via coincident like any fixed geometry.\n"
            "  sketch3d_ref_vertex(id, topo_id=N)   ground a constraint to a MODEL VERTEX\n"
            "      (T-3DXREF \u2014 \"a spline that touches a specific corner on my part\"). Pass\n"
            "      the viewport pick's topo_id (kind='vertex'); returns ref_id 'x1' \u2014 use it\n"
            "      as a point handle: {type:'coincident', refs:['e3.p2','x1']}. Re-resolves on\n"
            "      every rebuild; stays at last-good position + surfaces a warning if the\n"
            "      source vertex stops resolving (never breaks the sketch).\n\n"
            "\u2500\u2500 CONSTRAIN + CHECK (mirror of the 2D sketch loop) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_add_constraints(id, constraints)   append + narrated solve results\n"
            "      v1 types: coincident, fix, distance (pt-pt / pt-line), length, parallel,\n"
            "      perpendicular, angle, along_axis (axis:'x'|'y'|'z' or dir:[x,y,z]),\n"
            "      point_on (line/arc). Handles: 'e3.p1'/'e3.p2' ends (also eq_curve3d\n"
            "      endpoints), 'e3.pm' arc mid, 'e3.p0'..'e3.pN' spline pts, 'x1' external\n"
            "      ref point. value: number or {value, expr}; driven:true = reference dim.\n"
            "      (tangent/symmetric/equal/midpoint are v2 \u2014 rejected.)\n"
            "  check_sketch3d(id)     narrated health: status full/under/over, dof,\n"
            "      conflicting/redundant ids, free variables in plain words\n"
            "      ('e2: p2 x/y/z loose'), wires [{closed, n_edges}]. Aim for dof 0.\n"
            "  sketch3d_preview(id)   no-vision TEXT describe: entities w/ solved coords +\n"
            "      per-entity status, wire refs, length, bbox, health verdict.\n"
            "  sketch3d_get(id) / sketch3d_list() / sketch3d_clear(id)  read / roster / empty.\n\n"
            "\u2500\u2500 SWEEP ALONG IT \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_pipe(id, diameter_mm=12)   ONE CALL: 'run a 12mm tube along this\n"
            "      path'. Auto-places the profile plane at the path start \u22a5 the tangent,\n"
            "      circles it, sweeps, returns feature ids + volume. Options:\n"
            "      profile_sketch='s1' (your own 2D profile instead of a circle),\n"
            "      wire=k (multi-chain sketch), guide={feature,wire}, mode='boss'|'cut'.\n"
            "      Profile plane is EXACTLY \u22a5 the tangent (datum plane point_normal method,\n"
            "      T-3DDATUM) \u2014 works for any start direction, no axis-alignment needed.\n"
            "  Manual sweep op refs (add_features):\n"
            "      path: 'fN' (single chain) or {feature:'fN', wire:k} (from check_sketch3d\n"
            "      wires); guides: [{feature:'fM', wire:0}] \u2014 ONE guide, must differ from\n"
            "      the path; it drives profile orientation + SCALING (SW Guide Curves).\n"
            "      The 2D profile must sit at the path START, \u22a5 its tangent \u2014\n"
            "      sketch3d_sample_at(id, 0) gives {point, tangent}.\n"
            "  sketch3d_as_sweep_guide(id, role='spine'|'guide')  the exact ref syntax +\n"
            "      wires for this sketch.\n\n"
            "\u2500\u2500 PATH SAMPLING + SECTION \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  sketch3d_sample_at(id, t)      point + unit tangent at arc-length fraction\n"
            "      t \u2208 [0,1] of the SOLVED path (arcs tessellated, splines approximated\n"
            "      by their through-points).\n"
            "  sketch3d_section_at(id, t)     origin-plane section ORIENTED from the path\n"
            "      tangent at t (X\u2192'right', Y\u2192'front', Z\u2192'top'). ENGINE LIMIT: the cut\n"
            "      is always AT the world origin; use add_section_view(offset_mm) to move it.\n\n"
            "\u2500\u2500 WORKED EXAMPLE: routed 12 mm coolant tube \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "    new_part('manifold')\n"
            "    r = sketch3d_path_from_points('route',\n"
            "          [[0,0,0],[0,0,40],[30,0,40]], fit='polyline')   # -> id 'f1'\n"
            "    sketch3d_add_constraints('f1', [\n"
            "        {type:'fix', refs:['e1.p1']},\n"
            "        {type:'along_axis', refs:['e1'], axis:'z'},\n"
            "        {type:'length', refs:['e1'], value:40},\n"
            "        {type:'coincident', refs:['e1.p2','e2.p1']},\n"
            "        {type:'along_axis', refs:['e2'], axis:'x'},\n"
            "        {type:'length', refs:['e2'], value:30}])\n"
            "    check_sketch3d('f1')          # -> status 'full', dof 0\n"
            "    sketch3d_pipe('f1', diameter_mm=12)   # profile plane + circle + sweep\n"
            "    render_part()                 # see the tube\n\n"
            "  (Editing later: change a length constraint via edit_feature \u2014 the path\n"
            "   re-solves and the sweep follows parametrically.)\n\n"
            "\u2500\u2500 2D SKETCH PREVIEW \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  render_sketch(sketch)            multi-view PNG + health caption (2D sketches;\n"
            "                                   falls back to a wireframe pre-extrude).\n"
            "  sketch_preview_text(sketch)      no-vision text: plane/entities/constraints/\n"
            "                                   DOF/regions + health schedule (2D).\n"
            "  doc_sketch_contact_sheet()       whole-doc render + health table covering\n"
            "                                   BOTH 2D sketches and sketch3d features.\n\n"
            "\u2500\u2500 NOTES \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
            "  \u2022 A sketch3d never produces a body/face \u2014 path/guide material only (no 3D\n"
            "    'profile' extrude in v1). Loft guides do not consume sketch3d yet.\n"
            "  \u2022 Closed 3D wires are valid sweep paths (close=True on path_from_points).\n"
        )
    # RE-40: Reverse Engineering GUI workspace walkthrough (cad_guide('reveng')).
    # Distinct from cad_guide('reverse'), which is the engine-level STEP/mesh import + recognizer
    # path. This branch owns the ONE thing no cheatsheet covers — the tab's actual layout — and
    # then appends shared/cheatsheets/reveng.md (the role/handoff view) so that file is reachable.
    if topic in ("reveng", "reverse_eng", "re_workflow", "scan_to_cad"):
        walkthrough = (
            "REVERSE ENGINEERING WORKSPACE (GUI) — contracts/reveng.md\n"
            "Pipeline: Source → Scale → Reconstruct → Model → Review → Deliver\n"
            "Doc lanes (Datasheet / Web / From Library) run Source → Model → Review → Deliver.\n\n"
            "START: 'Reverse Eng' title-bar tab. New Session from the ribbon, or pick one in the\n"
            "session rail.\n\n"
            "LAYOUT (three columns; both side columns collapse to a header strip + chevron)\n"
            "  LEFT   Session rail — one rich card per session: thumbnail of the first photo, a\n"
            "         status chip (new/capturing/reconstructing/modeling/done/error), image count,\n"
            "         an honesty tag, and a stage progress bar reading 'n/6 stages'. A filter box\n"
            "         at the top matches name, session id or status. Refresh re-lists.\n"
            "  CENTRE Source-lane SegmentedControl, then THE STAGE STEPPER pinned at the TOP (a\n"
            "         clickable kit::StepTrack — click a step to navigate). Each step is coloured\n"
            "         from the session's real stage badges; a step whose prerequisite is unmet is\n"
            "         dimmed, un-clickable, and its tooltip/toast names what is missing. The\n"
            "         dependency graph is NOT list order: Reconstruct depends on Source, not on\n"
            "         Scale, because the ArUco solver needs a sparse model to triangulate against\n"
            "         (reveng_set_scale answers reason:'scale_needs_sparse' otherwise). Below the\n"
            "         stepper is the active stage body.\n"
            "  RIGHT  Properties column with three tabs:\n"
            "         • Details      selected session: ids, counts, scale of record, hero image.\n"
            "         • Capabilities live reveng_status() probe (async, cached, Refresh to re-run)\n"
            "                        showing the six capability flags — sparse, dense, splat,\n"
            "                        single_photo, webcam, gpu — plus per-backend provisioning\n"
            "                        (colmap, openmvs, brush, triposr, vggt, docling) and the\n"
            "                        fix-it banners naming the env var that repairs each miss\n"
            "                        (COLMAP_BIN, OPENMVS_BIN, ...).\n"
            "         • Provenance   the session's provenance records, newest first.\n\n"
            "SOURCE LANE (Source stage) — pick one:\n"
            "  Scan        photos / video / webcam → Scale → Reconstruct → Model\n"
            "  Datasheet   local PDF/spec → Model  (skips Scale + Reconstruct)\n"
            "  Web/Part#   part number or URL → research → Model  (skips Scale + Reconstruct)\n"
            "  From Library seed from the Parts Library; auto-routed to Scan or Datasheet\n"
            "  Live webcam preview with device + resolution/format mode selection ('Auto' asks the\n"
            "  backend for its highest-resolution mode — photogrammetry wants pixels, not fps),\n"
            "  per-control sliders (focus, exposure, white balance, gain, ...) and the primary\n"
            "  'Lock focus + exposure for capture' action, which freezes intrinsics for the whole\n"
            "  set. A synthetic 'there is no camera' frame is labelled as synthetic, never passed\n"
            "  off as a real capture.\n"
            "  Captured frames land in a contact sheet: responsive tile grid, hover magnifier with\n"
            "  per-frame metadata, double-click full-size viewer with next/prev, honest badges\n"
            "  (an unrated frame gets NO badge), filter + count summary, per-frame cull/reveal.\n\n"
            "SCALE (Scan lane only): ArUco Mat / Two Points / Known Dim.\n"
            "  The mat card previews the generated ArUco sheet inline and warns to print at 100 %\n"
            "  actual size. Two-point picks two pixels on a chosen photo. 'Scale of record' shows\n"
            "  the one number every downstream dimension is multiplied by. Scale must be CONFIRMED\n"
            "  before any dimension is trustworthy; ArUco needs a sparse model first, so running\n"
            "  Reconstruct before Scale is the normal order in the Scan lane.\n\n"
            "RECONSTRUCT (Scan lane only): Sparse → Dense → Mesh (COLMAP + OpenMVS), shown as a\n"
            "  live sub-step track driven by the worker's progress events.\n"
            "  CAPABILITY PRE-FLIGHT gates the Run button and always names its reason: no worker,\n"
            "  no sparse backend (COLMAP/VGGT both absent → hard block with the COLMAP_BIN fix),\n"
            "  no session selected, no images, or a run already in flight. Non-blocking warnings\n"
            "  cover a missing OpenMVS (sparse cloud only, no dense mesh) and an unprobed backend\n"
            "  set. The result renders inline in the 3D preview slot (splat when available,\n"
            "  otherwise the sparse cloud or the mesh).\n"
            "  MCP tool: reveng_reconstruct(session_id, quality)\n\n"
            "MODEL: Fit Primitives → parametric build → Mesh Body fallback.\n"
            "  Fitted primitives render as per-primitive cards (kind, role, confidence) beside an\n"
            "  inline 3D view of the mesh they were fitted to; the build result gets its own 3D\n"
            "  preview plus its strategy, match score and honesty tier.\n"
            "  Honesty tier: approximate sessions are tagged [approx] in the rail.\n"
            "  Engine-level RE (STEP/mesh → editable feature tree): cad_guide('reverse').\n\n"
            "REVIEW: dimension-confirm ledger + deviation report.\n"
            "  The ledger lists every dimension with its source (datasheet / measurement / fitted\n"
            "  primitive), its confidence and its ledger key; fit-critical rows demand a TYPED\n"
            "  caliper value. The deviation card paints the mesh-vs-B-rep error field with max/rms\n"
            "  chips (under 0.1 mm tight, under 0.5 mm loose, above that the rebuild does not\n"
            "  represent the part) and turns an instructive refusal into an inline blocked state.\n"
            "  MCP tools: list_reveng_dims(session_id), confirm_dimensions([{key, value_mm}]),\n"
            "             get_reveng_deviation(session_id), describe_reveng_result(session_id)\n\n"
            "DELIVER: hero summary of the delivered part (3D preview of the built document, or the\n"
            "  mesh when nothing was built), Attach to Parts Hub, Export bundle, Provenance.\n\n"
            "SHOT MODES for headless verification:\n"
            "  CHUDWARE_SHOT=reveng          full workspace shell\n"
            "  CHUDWARE_SHOT=reveng_capture  Source stage: capture suite + webcam + gallery\n"
            "  CHUDWARE_SHOT=reveng_scale    Scale stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_recon    Reconstruct stage + mid-run progress state\n"
            "  CHUDWARE_SHOT=reveng_model    Model stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_review   Review stage with a canned dim + deviation payload\n"
            "  CHUDWARE_SHOT=reveng_deliver  Deliver stage with a canned session\n"
            "  CHUDWARE_SHOT=reveng_splat    standalone SplatRenderer draw into its own FBO\n\n"
            "See also: cad_guide('reverse') for the engine import path + tool parameters, "
            "cad_guide('parts') for the Parts Hub integration.\n"
        )
        # The role/handoff view lives on disk as a cheatsheet; the early return above would
        # otherwise make it unreachable under every alias. Append it, degrade to the walkthrough
        # alone when the prompts tree is not staged.
        sheet = _read_prompt("shared/cheatsheets/reveng.md")
        if sheet:
            return walkthrough + "\n" + sheet
        return walkthrough
    if topic in ("evaluate", "measure", "measurement", "interference", "fitment",
                 "eval", "check_interference"):
        return (
            "EVALUATE / MEASURE — colour-coded measurement + interference fitment\n\n"
            "measure(targets=[...], doc_id?, view='auto', annotate=True)\n"
            "  Resolve each plain-English selector (same vocabulary as inspect_part /\n"
            "  measure_between), call eval_measure on all refs at once, paint each\n"
            "  element a DISTINCT colour from the shared 10-colour emphasis palette\n"
            "  (per_face mode), and return [image, caption]. Caption lists colour +\n"
            "  reading (length/area/diameter/distance) and embeds measure_json with\n"
            "  structured {kind,value,unit,color} per element; 2+ refs also report\n"
            "  the between distance/angle. Prefer this when you want the same\n"
            "  colour-coded highlight view the Evaluate ribbon shows.\n\n"
            "INTERFERENCE / FITMENT LOOP\n"
            "  1. check_interference(doc_id?)  — pairwise clash/clearance over bodies\n"
            "     (assemblies: world-placed components). Response includes\n"
            "     ok (the check completed), clear (the verdict: no interference),\n"
            "     interferences[{a,b,volume,overlap_volume_mm3,overlap_bbox,overlap_mesh}],\n"
            "     clearances[{a,b,distance}], contacts, parts[{name,bbox,volume}].\n"
            "     A clash returns ok=true, clear=false; ok=false means incomplete.\n"
            "  2. Subset: engine eval_interference accepts optional members:[name,...]\n"
            "     to pairwise-check only selected components (absent/empty = all).\n"
            "  3. Read overlap_volume_mm3 + overlap_bbox (world mm) → fix fitment\n"
            "     (edit mates / geometry / offsets) → re-check until clear=true.\n"
            "  4. Intra-part feature collisions (boss swallowed by cut, etc.):\n"
            "     check_feature_overlap — not the body-level interference tool.\n\n"
            "Related: measure_between (text-only single/pair probe), measure_part\n"
            "(mass props), inspect_feature (emphasis close-up + dims), verify_battery.\n"
        )

    if topic in ("drawings", "drawing_standards", "2d", "engineering_drawing",
                 "engineering_drawings"):
        sheet = _read_prompt("shared/cheatsheets/drawing.md")
        return _DRAWINGS_GUIDE + ("\n" + sheet if sheet else "")

    # Otherwise treat the topic as a per-feature cheatsheet name (hole, fillet, revolve, ...).
    sheet = _read_prompt(f"shared/cheatsheets/{topic}.md")
    if sheet:
        return sheet
    avail = sorted(p.stem for p in (_prompts_dir() / "shared" / "cheatsheets").glob("*.md")) \
        if (_prompts_dir() / "shared" / "cheatsheets").is_dir() else []
    return (f"No guide for {topic!r}. Topics: overview, simulation, motion, review, intake, gsl, "
            f"cam, narration, parts, reverse, reveng, deliverables, requirements, dag, verify, "
            f"interaction, sourcing, cost, spec, piping, industrial, hydraulic, firmware, structural, "
            f"drawings, "
            f"mechanical, "
            f"regen, code_compliance, serving, design_review, doc_diff, plm, tolerance_stackup, "
            f"evaluate, measure, sketch3d; or a feature cheatsheet: {avail}")
