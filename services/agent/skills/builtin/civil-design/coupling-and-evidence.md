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
