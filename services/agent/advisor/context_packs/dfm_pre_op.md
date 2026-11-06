# Design for manufacturability pre-op (aicad.trigger/1)

**When this fires:** manufacturing intent is explicit or a modeling operation completes.
After an edit, apply the additive-specific advice only if the bound process is additive.

**Do this:**
- While modelling printed parts, run `wall_thickness()` for the process-minimum RED
  heat map AND `dfm_check`. Fix every red region at its named face/feature or explicitly
  justify it before export/slicing; fix DFM failures and re-run both.
- At FDM milestones call `dfm_reinforce`: inspect sampled before/after thickness,
  candidate ops and volume/other-region side effects. It does not apply edits. If no
  fix verifies, inspect the driving sketch or add an appropriate constrained rib.
- Favor wall thicknesses in whole extrusion widths, smooth internal fillets and
  load-path gussets; orient principal loads along layers. For load-bearing parts,
  consider topology optimization and check fit and loading separately.
- Read the bound ProcessProfile thresholds; default FDM `min_wall_mm` is currently 1.0 mm.
  Resolve the process if it changes the decision; do not apply irrelevant process rules.
- For printed-part minimum walls, `dfm_check` uses DFAM's `mesh_wall_thickness`
  inscribed-sphere measurement, not `eval_thickness`. Read its face/feature regions,
  limits and fixes. `dfm_pass:null`/`not_evaluated` means incomplete, not a pass.
- `wall_thickness(min_wall_mm=…, resolution=…, image=True)` uses `eval_thickness`;
  minimum defaults to the bound process/FDM profile and resolution is optional mm spacing.
  Use `image=False` for JSON. Read basis/spacing; null/grey is unmeasured, not safe.
- Keep the advisor's wall screening separate from the DFM process verdict. Increase the
  named wall or revise its feature; add draft only where the selected process requires it.
