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
