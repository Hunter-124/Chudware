---
name: simulate
description: Run FEA, thermal, vibration, CFD, motion, and EM studies with actual setup units, backend checks, convergence, and analytic anchors.
inject: auto
triggers:
  keywords: [fea, cfd, simulate, simulation, stress, modal, thermal, vibration, wind tunnel, drag, motion, eigenmode, s-parameter, antenna, contact, load case, strong enough, will this hold, can this hold, factor of safety, deflection, buckling, von mises]
  tools: [run_fea_static, run_fea_adaptive, run_fea_modal, run_fea_fatigue, run_fea_frequency_response, run_fea_thermal, run_fea_buckling, run_fea_contact, run_fea_convergence, sim_evidence_packet, verify_packet, polymesh_cli, run_cfd_flow, run_cfd_accurate, simulate_motion, em_eigenmode, em_driven, em_timedomain, ecad_antenna_design, ecad_antenna_analyze]
  phases: [sim]
---

# Simulate

## Execute

1. State one criterion, case, pass rule, and units. Use each FEA tool's `material=`;
   `set_material` stores document identity/appearance but does not choose solver
   properties. Confirm faces with `query_geometry`. Every explicit FEA material
   dict needs `E_mpa` and `nu`; add `yield_mpa` for
   static/fatigue, density for modal/frequency/transient thermal,
   `ultimate_mpa`/`endurance_mpa` for fatigue, `conductivity_w_mk`/`k_w_mk` for
   steady thermal, and conductivity plus `cp_j_kgk` (or top-level
   `specific_heat_j_kgk`) for transient thermal. Registry material names resolve
   their stored properties.
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
   `run_fea_adaptive` with `eta_target_met`. `converged:false` or a peak that grows with
   refinement is a singularity, not strength. `vtu_dir` gives every leg its own `.vtu`
   (each row carries `vtu_path`) and the reply's `released_leg` names the finest solved
   leg's file — pass that reply to `sim_evidence_packet` and the released field travels
   with the numbers instead of needing a fourth solve.
2. **Check equilibrium AND connectivity.** Read `reactions.equilibrium_error_pct` before
   trusting any stress, and `mesh_quality` (aspect_max, jacobian_min, pct_poor,
   `connected_components` / `unfixed_components`, `thin_wall_ratio`) before trusting the
   mesh. Both ride beside `summary`, never inside it. A static solve now REFUSES a mesh
   whose components do not all reach a fixture — equilibrium cannot catch a mechanism,
   because a mechanism carries no load and its residual is tiny BECAUSE the model is
   broken (live: 9.6e6 mm of displacement at 0.00042% error). `thin_wall_ratio` under 1
   means one element spans the wall and cannot bend: re-mesh with h <= wall thickness.
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
  these are real-part FEA. **Buckling:** exactly one mount and one inward axial force
  in N; pressure/shear/tension cannot map to its CalculiX fixed-free box proxy.
- **Contact:** `run_fea_contact` is a CalculiX two-block load-transfer proxy, not the
  document's actual contact pair. Check base reaction against applied load.
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
- **Motion:** `simulate_motion` needs one dimensional mate, required `stop`, degrees
  or mm, and `steps>=2`. Refine samples near first contact. `dynamics={}` adds inverse
  reactions but no contact dynamics.
- **EM:** call `em_status` before `solver='openems'`. Builtin FDFD eigenmodes and
  analytic-uniform guide responses are not full-wave evidence. Track GHz versus Hz
  and mm; refine resolution/frequency samples and compare cutoff/resonance anchors.

## Verdicts

Static margin is `yield/SF - stress`. Exceeding that allowable means required margin
is not met; it does not itself mean yielding. Stress at/above yield supports yielding,
and fracture language requires stress at/above ultimate. Name convergence/anchor
evidence for PASS and the proxy/backend/linear-model limits for every verdict.

## Mechanism proof (kinematics)

A moving assembly is proven by the sweep, never by one pose. `simulate_motion` reports
`collisions:[{a,b,overlap_volume_mm3}]` on EVERY step (plus the unchanged
`first_collision_step`/`first_collision_value`), so quote the offending pairs and their
overlap volumes per step; "no interference" requires `check_interference=true` and an
empty `collisions` list at every sample, not just before the first hit.

- **Envelope:** `sweep_envelope(mate_id, start, stop, steps, components)` unions each
  component's 8 transformed bbox corners over the sweep — the clearance number. Its
  `fuse=True` mode builds the real swept solid (one `transform` copy per step fused into
  `<component>_sweep`) and returns its volume/bbox; use it when a bbox over-reports.
- **No dimensional mate to drive:** a steering assembly poses its uprights with
  concentric/coincident mates about measured kingpin axes and owns no angle/distance
  mate, so `simulate_motion` has nothing to sweep. Both kinematic tools take a `steer`
  spec instead — `steer={"components":["upright_FL","upright_FR"],
  "axis":{"point":[x,y,z],"dir":[x,y,z]}, "angle_deg":30}` (or `"mate_id":"<the
  concentric kingpin mate>"` to reuse the axis it already resolves, or
  `"angles_deg":[inner,outer]` for a true Ackermann pair). The named components are
  rotated rigidly about that axis on a DETACHED clone and the mate network is re-solved
  around them, so the live document keeps its revision and its mate-owned poses.
  `rear_steer` takes the same spec for 4WS; `steps` scales the angles 0 → full lock.
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
