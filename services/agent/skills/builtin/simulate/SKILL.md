---
name: simulate
description: Run FEA, thermal, vibration, CFD, motion, EM, and topology optimization studies with actual setup units, backend checks, convergence, and analytic anchors.
inject: auto
triggers:
  keywords: [fea, cfd, simulate, simulation, stress, modal, thermal, vibration, wind tunnel, drag, motion, eigenmode, s-parameter, antenna, contact, load case, strong enough, will this hold, can this hold, factor of safety, deflection, buckling, von mises, topology, thermomechanical, conjugate heat transfer]
  tools: [run_fea_static, run_fea_adaptive, run_fea_modal, run_fea_fatigue, run_fea_frequency_response, run_fea_thermal, run_fea_buckling, run_fea_contact, run_fea_convergence, sim_evidence_packet, verify_packet, polymesh_cli, run_cfd_flow, run_cfd_accurate, simulate_motion, sweep_envelope, steering_turning_radius, motion_fea_handoff_sweep, em_eigenmode, em_driven, em_timedomain, ecad_antenna_design, ecad_antenna_analyze, topology_capabilities, run_topology_optimization]
  phases: [sim]
---

# Simulate

## Motion evidence and cost — before calling

For mechanism work, read **Mechanism proof** below. `ok:true` means the sweep
completed, not that it is collision-free. Guard `ok:false` before accessing
`trajectory`; refusals/timeouts need not contain it. Never fabricate missing frames.
Print the existing result before rerunning slow work. A timed-out call marked
`status:"still_running"` / `in_flight:true` is not cancelled: the identical call in
the same run joins the original work rather than restarting it.

Start with overlap checking (`min_clearance_mm=0`). Positive `min_clearance_mm`
requests expensive exact pair distances, not a cheap tolerance adjustment; budget
it deliberately after a short sweep. Baseline overlaps are findings, not automatically
permitted joints. Any accepted overlap needs a separately justified interface.
Read every computed frame: top-level `interference.detail` is **FIRST HIT ONLY**,
never the union of all swept pairs. Finite sampled poses cannot prove continuous
path clearance, and overlap-only checks cannot prove a positive minimum clearance.

## Execute

1. State one criterion, case, pass rule, and units. Use each FEA tool's `material=`;
   `set_material` stores document identity/appearance but does not choose solver
   properties. Confirm faces with `query_geometry`. Every explicit FEA material
   dict for structural analysis needs `E_mpa` and `nu`; add `yield_mpa` for
   static/fatigue, `density_g_cm3` for modal/frequency response,
   and `ultimate_mpa`/`endurance_mpa` for fatigue. Thermal material dicts instead
   need `conductivity_w_mk`/`k_w_mk`; transient thermal also needs
   `density_g_cm3` and `cp_j_kgk` (or top-level `specific_heat_j_kgk`).
   Registry material names resolve their stored properties.
2. Record an independent closed-form anchor before solving, or explicitly record
   `no anchor`. Never convert pressure to force without loaded area.
   Useful anchors: justified beam stress/mode order, Fourier heat flow, vibration
   resonance at modal Hz, CFD dynamic pressure/V², motion closure/clearance, and
   EM TE10 cutoff/antenna resonance. Analytic-uniform S21 proves no discontinuity.
3. Run a cheap discretization, then a finer one with identical setup. Report both
   discretizations and require the governing value to settle (normally <10% change)
   or remain consistent with the independent anchor. One favorable mesh is not PASS.
   Polyhedral-FEA is the sole volume mesher. When a peak stress drives the decision or
   a coarse result looks mesh-sensitive, use `run_fea_adaptive`; it costs several solves.
   Its error indicator is dimensionless and relative (smaller is better), not a
   percentage.
4. Read the envelope literally: `ok:false`, `skipped:true`, failed/cancelled,
   `converged:false`, `random.ok:false`, or `verdict:null` proves nothing.

## Release evidence

A number leaves this role as an evidence packet, not a chat message. For any result that
closes a requirement:

1. **Converge.** `run_fea_convergence(sizes_mm=[coarse,med,fine], vtu_dir=<absolute dir>)`
   — one setup, three stated sizes, `monitor` on a NON-singular value; or
   `run_fea_adaptive` with `eta_target_met`. `converged:false` means the target was
   not met; a peak that grows with refinement needs singularity assessment, not a
   strength claim. `vtu_dir` gives every leg its own `.vtu`
   (each row carries `vtu_path`) and the reply's `released_leg` names the finest solved
   leg's file — pass that reply to `sim_evidence_packet` and the released field travels
   with the numbers instead of needing a fourth solve.
2. **Check equilibrium AND connectivity.** Read `reactions.equilibrium_error_pct` before
   trusting any stress, and `mesh_quality` (aspect_max, jacobian_min, pct_poor,
   `connected_components` / `unfixed_components`, `thin_wall_ratio`) before trusting the
   mesh. A static solve refuses a mesh whose components do not all reach a fixture:
   low equilibrium error alone cannot prove connectivity or exclude a mechanism.
   `thin_wall_ratio` under 1 warns that the wall is under-resolved; refine to
   h <= wall thickness before relying on a bending result.
   `plausibility.flag == "implausible"` means the peak displacement exceeded the part's
   own bounding-box diagonal, so the small-strain assumption is gone, `warnings` says so,
   and a convergence leg in that state is marked `implausible` and cannot converge.
3. **Keep the field.** `vtu_dir` on the ladder, or `vtu_path=<absolute .vtu>` on a single
   solve, so displacement/von Mises are inspectable outside the tool; `provenance`
   records engine build, polymesh commit, solver, mesher and timestamp. To re-solve ONE
   released leg on its own mesh, use `run_fea_static(mesh_size_mm=<that row's size_mm>,
   vtu_path=...)` — `mesh_size_mm` is the exact element size and is mutually exclusive
   with `mesh_fineness`, so the field can never be attributed to a mesh nobody solved.
4. **Package.** `sim_evidence_packet(case_id, requirement_id, out_dir, study_result=<the
   full result>, geometry_path, material_source, load_rationale, notes)` writes
   inputs/result/provenance/convergence/checklist plus geometry+VTU sha256. Its
   `unsatisfied_checklist` is the release gate: locations, singularity assessment,
   allowable and hand calculation are yours to close. A packet folder is NEVER
   overwritten: re-running a case moves the previous packet into
   `superseded_<date>_<hash>/` and the reply's `archived_to` says where (the new
   provenance.json's `superseded_from` names what it displaced). `overwrite=True` is the
   only way to replace in place, and it destroys the earlier evidence. An existing `.vtu`
   is archived the same way before a solve writes over it.
5. **Verify before you cite.** `verify_packet(folder=<packet_dir>)` checks the folder's
   files describe ONE run: same geometry hash in inputs and provenance, same engine build
   id, result.json still hashing to what provenance.json and convergence.md cite,
   reduction fields present, recorded `.vtu` still intact. `ok:false` with
   `reason:"packet_inconsistent"` means do not quote a number out of it.
6. **Cross-check the critical case.** `polymesh_cli(action="solve"|"diag", ...)` re-solves
   it through the standalone Polyhedral-FEA binary with frozen boxes (METRES, pascals,
   newtons). Explain any difference; never average the two. `polymesh_cli_unavailable`
   means the cross-check was NOT performed — report it that way. Its artifacts follow the
   same rule: a re-run archives the previous `_mesh.vtu` / `_result.vtu` / `_diag.json`
   plus that action's log and reports `archived_to`.

## Current surfaces

- **Static/fatigue:** mounted face plus non-zero force in N or pressure in MPa;
  these are real-part FEA. Use `interfaces` for bonded multi-body load paths.
  Static supports explicit small-strain J2 `plasticity`; state whether it was enabled.
  **Buckling:** `run_fea_buckling` uses the real CAD mesh and static force/pressure
  dialect. It reports positive load factors under `max_load_factor`; a refusal for
  no mode below that ceiling is not proof against buckling or nonlinear collapse.
- **Contact:** `run_fea_contact` meshes the document's real bodies with frictionless
  unilateral contact and optional bonded pairs. Measure the clearance before setting
  `max_gap_mm`; use `interface_size_mm` for local contact resolution. Read equilibrium,
  `contact.tangential_force_fraction` and `resolution.mode`; `closed_fit` idealizes
  sub-mesh clearance, it does not resolve it. No large sliding/deflection or true bolt
  preload; an unsettled active set is a refusal, not a result.
- **Modal/vibration:** modal takes no load; `fixtures=[]` is legitimate free-free and
  should show six near-zero rigid modes. Frequency response uses `excitation_dir`,
  `base_accel_g`, damping `zeta`, and optional PSD in g²/Hz; it returns acceleration
  amplification, not stress or displacement.
- **Thermal:** steady and transient `run_fea_thermal` use the real part mesh,
  with `hot_faces`/`cold_faces` temperature entries in °C and `flux_w_m2` or
  `h_w_m2k`/`t_amb_c` loads. Steady needs a fixed-temperature or convection
  datum. Schedules require `transient={t_end_s,...}`; flux-only heat-up is valid.
  State k W/(m·K), cp J/(kg·K), and time seconds.
- **CFD:** `run_cfd_flow` is directional LBM; require `converged=true` and
  `min_feature_voxels>=~8`, then refine `cells`. Absolute Cd/Cl needs
  `run_cfd_accurate`; unavailable OpenFOAM returns SKIP and started runs use `get_job`.
- **Motion:** `simulate_motion` drives angle/distance mates with a required `stop`
  (one driver or explicit synchronized `drivers`), in degrees or mm, and `steps>=2`.
  Refine samples near first contact. `dynamics={}` adds inverse reactions
  (`steps>=3`) but no contact dynamics.
- **EM:** call `em_status` before `solver='openems'`. Builtin FDFD eigenmodes and
  analytic-uniform guide responses are not full-wave evidence. Track GHz versus Hz
  and mm; refine resolution/frequency samples and compare cutoff/resonance anchors.
- **Figures:** show fixtures, loads and the analyzed region in surrounding assembly
  context. Static vehicle stress views keep the rest of the vehicle in frame.

## Topology optimization

One persisted study (`type:"topology"`) is shared with the GUI Topology tab, and both run
the native `sim_run_topology`. The tools are in the `sim` group
(`enable_tools(categories=["sim"])` if they are not advertised). `run_topology_optimization`
writes to the document, so Ask/Plan cannot run it.

1. **Discover.** Call `topology_capabilities()` first. Only a `supported_pairs` entry runs,
   and `topology{}` may hold only that pair's `parameters` and `limits`; any other key is
   refused, not dropped. SIMP/RAMP support all seven physics (structural, thermal, modal,
   buckling, flow, thermomechanical, cht) with their response limits. BESO and level_set
   support structural and thermal only, volume budget only: compliance /
   thermal_compliance, no limits, no mass objective. `ok:false`
   `capabilities_unavailable` means no solver; never guess the matrix.
2. **Set up.** Units are mm, N, MPa, C, W, W/m2, W/(m*K), W/(m2*K), m/s, Pa, Pa*s, kg/m3,
   g/cm3, and the mesh is tet4 only (`mesh_order:1`). Use `boundaries` and
   `material_required` from the reply. `volume_fraction` budgets FLUID for flow/cht and
   SOLID otherwise (rho=1 is always solid). Structural fixtures take optional `dofs`;
   loads are a force `vec` in N or an inward `pressure` magnitude in MPa. `load_cases`
   take weights > 0. Mass needs `stress_limit_mpa` or `max_displacement_mm`. Buckling
   needs a compressive pre-stress. Thermomechanical needs
   `thermal.reference_temperature_c`. Every cht inlet needs `temperature_c`, and cht heat
   input goes in `thermal.loads`. Preserve/exclude regions, fixture and load patches
   (plus thermal patches for thermal/thermomechanical) are kept solid, and flow ports are
   kept fluid. Conflicts refuse by name (`passive_conflict`, `boundary_in_keep_out`,
   `port_in_preserve`, `tie_passive_conflict`, `symmetry_domain_mismatch`,
   `volume_fraction_infeasible`). Fix the setup; do not loosen it.
   For printed parts choose a `print_process` id from discovery (e.g. `"fdm_0.4"`), set
   `build_plate:{"faces":["bottom"]}` to the actual bed face, and use a real print material
   (e.g. `"pla"`). The preset fills only absent, offered topology keys; explicit values win.
   The planar, lowest bed face supplies the build direction; alternatively set
   `topology.build_direction`. Overhang control defaults to 45 degrees when only a penalty
   is set. `min_member_mm` is a lower bound, not a guarantee of printable feature size.
3. **Run.** `run_topology_optimization(study=..., background=True)` saves the study before
   solving and returns `job_id`, `study_id`, `run_id` and `artifact_dir`. Re-run a study the
   user built with `study_id=` (given keys overlay the stored study). The same name updates
   that study instead of creating another one.
4. **Follow.** Use `wait_job(job_id)` (max 900 s per call; call again while
   `still_running`) or `get_job`. Stop with `sim_cancel(job_id)`; there is no
   `cancel_job`. A failed or cancelled run is still retained with whatever the solver
   wrote. `checkpoint.json` is NOT resumable (the native run takes no resume input), so a
   stopped run starts over.
5. **Inspect.** Report `ok` (the computation finished), `converged` and `feasible` as
   three separate facts, plus `termination`. Read `result.constraints` (g<=0, violation),
   `summary` baseline vs final objective/volume/mass and `convergence_criterion`. Then
   read the thresholded actual-domain reanalysis: `reconstruction`,
   `summary.reanalysis_objective`/`reanalysis_feasible`/`geometry_valid`, and the
   `reconstruction_response` check. Treat `result.manufacturing.checks` rows by status:
   `min_member` is intent only (`not_measured`), and overhang is a heuristic, never
   support-free. Use `density.vtu`, `reanalysis.vtu` and `surface.stl` (only when the
   geometry is valid) from `artifact_dir`. `reanalysis.vtu` has a point field
   `von_mises[@case]` (MPa). To animate it, use
   `render_animation(kind='fea', vtu_path=..., field='von_mises@<case>')`.
6. **Print workflow.** Read `result.manufacturing.dfam` for the chosen process, walls and
   overhang findings; `print_ready` stays false until slicing. Use the preferred validated
   print surface in `print_handoff`, and call
   `slice_part(printer=<the user's printer>, **print_handoff["slice_part"])`, preserving its
   reported design direction (an `"assumed"` source is not measured). Quote the slicer's
   `gcode_check`/`print_ready`. Continue in CAD via
   `import_part(**print_handoff["import_part"])`; the original study/history remains editable.

Limits to state: flow/cht are steady Stokes-Brinkman (MINI). They refuse
`flow_regime_invalid` when the prescribed-inlet Re exceeds 1 or the solved velocity bound
does, and model no inertia or turbulence. The solid is a leaky Brinkman resistance
(leakage reported; cht `thermal.heat_leakage`). Cht uses a constant fluid heat capacity
on the superficial velocity (conservative). Thermomechanical is one-way and steady.
Everything is linear: no contact, plasticity or geometric nonlinearity. The result is a
mesh surface plus fields, not reconstructed parametric CAD.

## Verdicts

Static margin is `yield/SF - stress`. Exceeding that allowable means required margin
is not met; it does not itself mean yielding. Stress at/above yield supports yielding,
and fracture language requires stress at/above ultimate. Name convergence/anchor
evidence for PASS and the proxy/backend/linear-model limits for every verdict.

## Mechanism proof (kinematics)

Check the actual requested assembly/revision and each joint's range. For every
computed `trajectory` frame inspect `converged`, `max_residual`, `worst_mate`,
`tolerance`, `dof`, `residual_dof`, `pose_check` and `bodies` (component/name/world
transform). Inspect `collision_detail` and `collisions`: stable `component_a` /
`component_b`, `overlap_volume_mm3` for overlaps, or `distance` (mm) and warning for
below-minimum-clearance pairs. `collisions` also includes clearance hits, with
zero overlap volume; zero volume does not mean clear.

Frame `interference` is true for known hits, false only for a complete clear check,
and null when unchecked or incomplete without a known hit. Read
`interference_complete`, `pairs_checked`, `pairs_reused`, `pair_failures` and
`geometry_failures` (check fields exist before using them on unchecked frames).
Reused pairs retain exact results for unchanged relative poses; they are not skipped
evidence. Known hits survive incomplete checks. A sampled-clear claim requires
`check_interference=True`, top-level `interference.complete:true`,
`frames_checked == steps`, all computed frames converged/pose-valid and no hits.
Failed poses, missing geometry or pair failures are incomplete, never clear.
`loop=True` appends a playback copy, not another computed check. Report ranges,
sample count, clearance threshold and all findings; do not report a measured global
minimum distance from a sweep that only checked overlaps or threshold violations.

- **Envelope:** `sweep_envelope(mate_id, start, stop, steps, components)` bounds each
  component's 8 transformed bbox corners at sampled poses, not a measured clearance.
  `fuse=True` unions real placed solids (one `transform` copy per step fused into
  `<component>_sweep`) and returns volume/bbox when a bbox over-reports. Both are
  sampled envelopes, not a continuous swept-volume proof.
- **No dimensional mate to drive:** a steering assembly poses its uprights with
  concentric/coincident mates about measured kingpin axes and owns no angle/distance
  mate, so `simulate_motion` has nothing to sweep. `sweep_envelope` and
  `steering_turning_radius` take a `steer` spec instead —
  `steer={"components":["upright_FL","upright_FR"],
  "axis":{"point":[x,y,z],"dir":[x,y,z]}, "angle_deg":30}` (or `"mate_id":"<the
  concentric kingpin mate>"` to reuse the axis it already resolves, or
  `"angles_deg":[inner,outer]` for a true Ackermann pair). The named components are
  rotated rigidly about that axis on a DETACHED clone and the mate network is re-solved
  around them, so the live document keeps its revision and its mate-owned poses.
  Keep a straight rear axle non-steering for the vehicle workflow; `rear_steer`
  supports explicitly requested 4WS studies, not an assumed vehicle configuration.
- **Turning radius:** `steering_turning_radius(steer=… | steer_mate_id, …)` intersects
  the POSED wheel axle lines — a rolling wheel turns about a point on its own axle line —
  and reports `turning_radius_m`/`turning_radius_ft` plus per-wheel `steer_angle_deg`
  and radius, inner/outer roles, and `turn_center_station_mm` (0 on the rear axle for
  2WS, mid-wheelbase for equal-and-opposite 4WS). `axle_line_residual_mm` is the
  Ackermann inconsistency: ~0 for a correct linkage, `(track/2)·sin(steer)` for parallel
  steer. Read its `assumptions` and `derived_from`: an unstated wheelbase/track is
  DERIVED from the solved axle centres, so pass `wheelbase_mm`/`track_mm` when the
  geometry is not a two-axle vehicle. Quote the turning radius with
  `outer_swept_radius_mm` — a corridor claim is about the swept corner, not the
  centreline — and `check_interference=True` for the steering lock the geometry imposes
  (`first_interference_deg`).
- **Load case selection:** the worst joint reaction is not always the worst stress, so
  `motion_fea_handoff_sweep(component, fixtures, positions|start/stop/steps, ...)`
  batches the single-frame handoff across positions and names the governing one. Each
  row is a real static solve; a row with `ok:false` proves nothing about that position.
