# Thin wall context pack

- While modelling printed parts, run `wall_thickness()` AND `dfm_check`.
- Inspect the process-minimum RED heat map. Fix every red region at its named face/feature
  or explicitly justify it before export/slicing; re-run after geometry changes.
- Let `min_wall_mm` default to the bound process or FDM profile (currently 1.0 mm).
  Read the profile, not a memorized process table; structural loads may require more.
- `resolution` is optional sample spacing in mm; `image=True` is the default,
  `image=False` returns JSON. Read `measurement_basis.spacing_mm` and `budget_limited`.
- Thickness uses inscribed spheres. Null/grey means unmeasured/no opposing wall found,
  not a safe wall. Use `thin_regions` and face/feature references for targeted edits.
- Treat the advisor as screening, not manufacturing sign-off; also inspect DFM results.
