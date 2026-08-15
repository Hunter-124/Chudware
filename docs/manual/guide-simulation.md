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

run_fea_thermal(hot_faces, cold_faces, material) — steady-state THERMAL (heat conduction) via
  CalculiX (ccx). hot_faces/cold_faces each carry a {face + temperature °C}. Solves a prismatic
  box proxy sized to the part bbox (through-conduction estimate). ccx is a GPL-2.0 subprocess
  (PATH-discovered); if absent it returns {skipped:true} — never a fabricated result.

run_fea_thermal(..., transient={...}, loads=[...]) — TRANSIENT thermal on the REAL part mesh
  (C++ core, backward Euler; no ccx, no box proxy). transient={"t_end_s":60, "dt_s":0.5,
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

run_fea_buckling(fixtures, load, material, n_modes) — linear (eigenvalue) BUCKLING via CalculiX.
  Returns the critical load factors λ (applied load × λ = Pcr); λ<1 buckles below the load.
  Box-proxy Euler-column estimate for slender members. ccx subprocess; absent ⇒ {skipped:true}.

run_fea_contact(pressure_mpa, material) — nonlinear large-deflection CONTACT FEA via CalculiX
  (*STATIC,NLGEOM + *CONTACT PAIR). Two-block proxy demonstrating load transfer across a touching
  interface (press/snap-fits). ccx subprocess; absent ⇒ {skipped:true}.

run_fea_frequency_response(fixtures, excitation_dir, sweep_hz, zeta, base_accel_g,
  psd_breakpoints, vibration_spec, ...) — harmonic + random vibration response (T-BL17).
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
  ECAD → thermal FEA (LIVE, T-FIN16): ecad_thermal_handoff(environment='still_air'|
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
  ECAD → CHT airflow (LIVE, T-FIN19, coupling ecad_to_cht): when the convection PRESETS
  aren't enough (enclosures, fans), ecad_airflow_handoff(inlet_mps=…, inlet_temp_c=…,
  enclosure={"clearance_mm":10,"wall_mm":2}?) exports the board slab + every POWERED
  component as separate watertight STL solids and runs OpenFOAM chtMultiRegionFoam via
  the T-SIM5 multi-solid seam (per-region volumetric heat sources; optional passive
  4-wall enclosure duct open along the flow axis), mapping per-region mean/peak °C back
  to component refs. ecad_verify_airflow() then answers "was the film-coefficient
  assumption sane?" — a per-component preset-h vs CHT rise deviation table with
  guidance + an effective-h re-run suggestion whenever the preset was OPTIMISTIC.
  Without OpenFOAM the handoff degrades honestly (skipped:true + full envelope) and
  offers the LBM wind tunnel as a qualitative-flow ADVISORY, clearly labeled
  NON-THERMAL. Budget: even the 'coarse' preset is minutes of meshing + solving.
  ECAD → structural FEA (LIVE, T-FIN21): ecad_vibe_check(band_lo_hz=20, band_hi_hz=2000,
  psd_breakpoints=…, mount_holes=…) — vibration reliability screen of the mounted board:
  reuses the T-FIN16 fused board+components solid with a mass-corrected effective
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
  HEATSINK SELECTION (T-FIN17): heatsink_select(component_ref, tj_target_c |
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

SYSTEM POWER BUDGET (aicad.powerbudget/1 — T-FIN23, tool: power_budget):
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

GOLDEN DEMO (T-FIN25 — living E2E worked example):
  scripts/demo_sensor_node.py chains the full FIN surface on an STM32F4 sensor-node
  board: verification ladder (ERC/DRC/hostcheck/sicheck/FPGA/PDN) → firmware project +
  co-verify (fw_sim_run with fixture:i2c_temp_tmp103 when Renode is present) →
  ecad_thermal_handoff → heatsink_select(U1, tj_target_c=85) → power_budget →
  make_report. Wired into scripts/gate.sh as stage 4.7 (CHUDWARE_SKIP_FIN25=1 to opt out).
  Narrative + artifacts: `docs/reference/FIN25_SENSOR_NODE_GOLDEN_DEMO.md`. Prefer this script over
  inventing a new end-to-end recipe when demonstrating the content is finished.

SIMULATE TAB (T-FIN24, host GUI): one multiphysics Simulate workspace hosts every live
  study kind with progressive disclosure by open doc type, plus handoff actions
  (Send board to thermal FEA, Pick heatsink, …) driven by aicad.simhandoff/1. Worker
  seam: `services/sim/multiphysics_rpc.py` (sim_caps / sim_run_buckling /
  sim_run_contact / sim_freq_response / ecad_thermal_handoff / heatsink_select /
  piping_verify_cfd). EM forms wire the live EM backend; deeper EM solvers stay on
  T-SIM11. Supersedes the deferred T-SIM10 host-path brief.
```

