# Agent engineering checks

These families combine native geometry, explicit inputs, reference tables and engineering
heuristics. A result's basis, assumptions, scope, revision and completeness matter as
much as its status. `not_evaluated`, absent measurements and incomplete checks are not
passes. A screened or sampled result is not certification; rerun after relevant edits.

## Discovery and registration

`services/mcp/server.py` registers DFM through `services/mcp/intake_tools.py` in the
`intake` policy family, stress screening and assemblability in `cad`, FEA and probing
in `sim`, and harness tools in `ecad`. Policy gating applies independently of the
agent's category packs. `services/agent/tool_categories.py` groups related tools into
`intake`, `stress`, `sim`, `assembly`, `harness` and other task-oriented packs;
`enable_tools` can expose the applicable category or tools. Online sessions reach
registered host tools; native RPCs are engine operations rather than extra MCP tools.
`cad_guide` topics include `printing`, `evaluate`, `stress_risers` (also
`stress_concentration`) and `harness`. Advisor matches and context packs are defined
in `services/agent/advisor/triggers.py`, `triggers.yaml` and
`services/agent/advisor/context_packs/`; relevant triggers include
`stress_concentration` and `harness_design`, as well as DFM and assembly pre-operation
advice. Triggering guidance is not execution of a check.

## DFM

`dfm_check` in `services/mcp/intake_tools.py` combines the process profile and general
checks with geometry-derived rows dispatched by `services/mcp/dfm_rows.py`.
`dfm_common.py` supplies the context and row conventions. Producers are
`dfm_rows_additive.py` (FDM/SLA/SLS/MJF), `dfm_rows_machining.py` (mill/lathe),
`dfm_rows_sheetmetal.py`, and `dfm_rows_molding.py` (molding/casting). Depending on
the row, evidence is sampled geometry, process-table limits or a labelled heuristic;
read the row basis instead of interpreting every row as an exact measurement. For
printed walls, the DFM inscribed-sphere mesh measurement is distinct from the
`wall_thickness`/`eval_thickness` visualization; inspect sample spacing and unmeasured
regions. Missing rule, measurement or producer emits `not_evaluated`, not a pass;
`dfm_pass: null` means incomplete.
For native part queries, face and edge `owner_feature` values come from the compiled
owner map using the body ordinal and body-local topology index. Sheet-metal bend
screening uses those attributions alongside the recorded bend frame; missing or
ambiguous surviving geometry remains `not_evaluated`.

`dfm_reinforce` in `services/mcp/dfm_fix_tools.py` previews FDM wall edits on detached
clones, with before/after thickness and side effects; it does not apply edits.
`services/mcp/dfm_fix_verify.py` provides the shared shadow-clone build and measurement
verifier. Row proposals are retained only when verification clears that same finding;
this does not establish fit, assembly clearance or strength. `dfm_check` defaults to
`verify_fixes=True`; `False` preserves findings but skips clone verification and omits
proposed operations, including reinforcement. Review side effects before applying any
candidate and repeat the checks afterward.

To add a row, put its stable name in the applicable producer's `CHECKS` and process in
`PROCESSES`, implement `rows(ctx, names)` using `dfm_common` row conventions and
explicit measurement/rule basis, and use the shared verifier for proposed operations.
Register a new producer in `dfm_rows.py` if needed; ensure missing evidence produces
`not_evaluated`. Update applicable process rules, guidance and tests along with the row.

## Stress and FEA

`services/mcp/stress_tools.py` implements `find_stress_risers` over native
`eval_edge_analysis` and other geometry evidence. It locates concave edges, blends,
ligaments and thin regions; severity and hypothetical chart comparisons are screens,
not actual load-specific strength. Where geometry, nominal stress or load is missing,
Kt is `not_evaluated`. Candidate fixes may be dry-run built, but a successful build
is not evidence of improved strength. Local analysis and verified fix pages are bounded;
follow pagination and local windows to cover the inventory.

Static FEA collection in `services/mcp/cad_sim_tools.py` reports unmasked nodal hot
spots and low-factor-of-safety regions. `services/mcp/fea_probe.py` attributes spots to
nearest display triangles approximately and implements `fea_probe` for actual VTU
field sampling at points or regions. Attribution distance and unavailable geometry
matter; a peak may be singular. `run_fea_convergence` compares mesh legs using a
specified summary or local von Mises monitor, including point, face or hot-spot
location. Failed or incomparable legs are not convergence evidence; a growing local
peak can indicate a possible singularity, not prove one. Monitor a physically meaningful
non-singular location, and inspect fixtures and mesh sensitivity.

`services/research/calcs/stress_concentration.py`, reached via `research_eng_calc`,
provides cited idealized-geometry elastic Kt and, in its stated steel range, estimated
fatigue Kf = 1 + q(Kt−1). Respect the geometry, load, nominal-stress definition and
validity range. Kf is a fatigue strength-reduction estimate, not a static yield
multiplier. A resolved FE notch peak already includes Kt: do not apply Kt or Kf again
to that peak as though it were nominal stress.

To add a stress screen or calculation, define its actual geometry and load domain,
source and basis, retain unknowns and bounded pagination, and distinguish a geometry
proposal's build verification from a load-case verification. Wire the tool/calculation
through the existing registration and discovery surfaces and add tests for unsupported
inputs, incomplete evidence and the cited validity limits.

## Assemblability

`services/mcp/assembly_check_tools.py` and `assembly_fastener_checks.py` implement
read-only assembly checks. They use native world-placed geometry and metadata; unknown
hardware, uncertain intent, missing geometry and incomplete queries remain
`not_evaluated` rather than assumed safe.

| MCP tool | Evidence and engine RPCs |
|---|---|
| `assembly_dof` | `eval_assembly_dof`: instantaneous solved-pose mate-Jacobian freedoms and disconnected groups, not a finite-motion proof; component projections are not additive. |
| `fastener_check` | `doc_tree`, `asm_world_bodies`, `doc_query` and `doc_get_meta`: screen recognized screws against coaxial placed geometry and supplied requirements; unrecognized hardware is unknown. |
| `tool_access_check` | Same placed screw/geometry discovery plus `eval_envelope_clearance`: finite illustrative tool envelopes, not proof for every real tool or operating motion. |
| `disassembly_check` | `asm_world_bodies` and `eval_removal_clearance`: straight removals along sampled world/mate axes and a blocker-derived order; not arbitrary-path planning. |
| `mate_fit_check` | `doc_tree`, `asm_world_bodies`, `doc_query`: measure referenced concentric bore/shaft geometry and compare to tabulated ISO fit candidates; dimensions alone do not establish a tolerance or functional fit. |
| `bom_consistency_check` | `asm_bom_components`, `asm_world_bodies`, `doc_query` and metadata: compare native quantities/source identity and screen bore patterns against finite screw shanks; unknown hole intent is not a missing-fastener finding. |

Add a check beside these implementations using actual component ownership and
world-space evidence, bound queries and report incomplete/ambiguous cases explicitly.
Register it in the `cad` family, add the assembly category and appropriate guide or
advisor guidance, and cover measured, ambiguous and missing-evidence cases in tests.

## Harness

[The harness contract](../../contracts/harness.md) defines the document study, routing
state, complete tool inventory, sources and limitations. `services/harness/model.py`
models and persists connectivity and the routing forest; `services/mcp/harness_tools.py`
uses engine `harness_route` and `harness_path_check` for centerlines, bends and
clearance. Clearance is against display tessellation, not exact B-rep. Bundle OD and
bend defaults are sourced estimates, not measured cable properties. Motion checking
uses conservative transformed AABBs at sampled poses, not continuous motion.

`services/harness/electrical.py`, `bus_electrical.py`, `cable_models.py` and
`wire_data.py` implement electrical, bus, cable and cited property screens exposed by
`services/mcp/harness_electrical_tools.py`. Actual loads, return paths, ratings,
construction and bus topology govern what can be evaluated; unknown evidence is not
a pass. Formboard flattening lives in `services/harness/formboard.py`; the tool emits
SVG/DXF/PDF and reports overlaps and length verification, but does not integrate with
drawing sheets. Segment-length labels are checked against the actual connector symbol
polygons rather than circular approximations. `cad_guide('harness')`, the `harness` category,
`services/agent/tool_guidance/harness.py`, and the `harness_design` advisor context pack
make the workflow discoverable.

To add a harness rule, place it with the relevant model, routing, electrical or
formboard analysis, retain cited input provenance, units and explicit unknowns, and
expose bounded results through the existing tool family where applicable. Update the
versioned contract for an interface change, category/guidance and tests for both
supported and missing evidence. Do not treat a calculated default as a cable-specific
measurement.
