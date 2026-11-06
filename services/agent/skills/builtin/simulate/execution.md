## Motion evidence and cost — before calling

For mechanism work, fetch `cad_skill(name="simulate", path="mechanism.md")`. `ok:true` means the sweep
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
