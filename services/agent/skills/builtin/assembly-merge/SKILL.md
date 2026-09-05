---
name: assembly-merge
description: Merge independently-sourced CAD (a vendor wheel onto someone else's frame) with measured, verified placement — roster and exclude bodies at import, fit axes, mate, then prove the fit before exporting.
inject: auto
triggers:
  keywords: [merge cad, merge the wheel, merge two parts, mount onto, graft, retrofit, swap the wheel, replace the wheel, two step files, imported step, vendor part, third-party part, foreign geometry, off-frame, independently sourced]
  tools: [import_part, fit_cylinder_axis, asm_add_component, asm_mate, solve_mates, check_interference, measure_between, list_world_bodies, describe_part, describe_assembly]
  phases: [assembly]
---

# Assembly Merge

Two files from two sources become one machine. Neither file knows the other's frame, so
EVERY placement number must be measured from the geometry — never guessed, never typed into
a source file.

## Hard rules

- **NEVER hand-edit STEP/JSONL/`.step+` text.** Rewriting entities or transforms in a source
  file is not a CAD operation: it silently destroys the assembly's provenance and is how a
  merged wheel ends up off-frame. Geometry moves only via `asm_add_component` / `asm_mate` /
  the `transform` feature.
- **Never place a foreign part on a guessed pose.** `at`/`rotate` are a starting pose or a
  MEASURED pose. A pose you did not derive from `fit_cylinder_axis` / `measure_between` /
  `query_geometry` is a guess.
- **Placement is not done until it is verified.** `check_interference` + `measure_between`
  (or `list_world_bodies` against the expected envelope) after the last mutation. Until then
  `save_part`/`export_part` reply with
  `warnings:["placement_unverified: N component(s) placed by transform with no
  check_interference since last mutation"]`.

## Procedure

1. **Import each source separately.** `import_part(path)` per file — one document per source
   (`into=` only when you deliberately want one multi-body part). Note each `doc_id` and
   `body_count`.
2. **Roster the bodies.** `describe_part(doc_id)` prints an indexed roster of every
   multi-body import feature (`N. <name>`, addressable as `<fid>#k`). A 760-body frame is a
   roster, not a blob.
3. **Drop legacy bodies at import time.** Re-import with
   `import_part(path, exclude_bodies=[...])` (or `keep_bodies=[...]`) using the ordinals or
   names from the roster — e.g. drop the frame's original wheels before adding a new one.
   Both accept 1-based ordinals and/or `embedded_body_names` strings. NEVER delete geometry
   by editing the file.
4. **Measure the mating references on BOTH parts.** Tessellated imports have no analytic
   cylinder to query, so use `fit_cylinder_axis(near=[x,y,z], search_radius_mm=...)`:
   - hub bore of the wheel → `axis_point`, `axis_dir`, `radius_mm`
   - axle/spindle of the frame → the same three numbers
   - check `rms_residual_mm` against `radius_mm` (a residual that is a meaningful fraction of
     the radius means you fitted a chamfer or two surfaces — re-aim, do not proceed)
   - `measure_between` for every seat/shoulder offset, `measure` for a colour-coded readout.
5. **Place on those numbers.** Save each source as `.step+`, then `new_assembly` →
   `asm_add_component(path, fixed=True)` for the frame → `asm_add_component(path, at=...,
   rotate=..., angle=...)` for the wheel with the pose computed from the two fitted axes
   (rotate `axis_dir`→`axis_dir` first, then translate `axis_point`→`axis_point`).
   Prefer `asm_mate("concentric", ...)` + `asm_mate("coincident", ...)` and let the solver own
   the pose — a mated component is stamped `placement_source: "mate"`, a raw pose
   `"transform"`.
6. **Solve.** `solve_mates()`.
7. **Verify — all three, after the last mutation:**
   - `check_interference()` — no interference; the seat clearance is what the fit calls for.
   - `measure_between` — the mating features really are coaxial/seated (bore axis vs axle
     axis, seat face gap ≈ 0).
   - `list_world_bodies()` / `describe_assembly()` — every body's world bbox inside the
     expected envelope, and every `placement_source` accounted for.
8. **Only then export.** `save_part()` for the editable deliverable, `export_part(fmt=...)`
   for interchange. A `placement_unverified` warning in the reply means step 7 has not run
   since the last change — go back, do not ship.

## Failure modes this exists to prevent

- Placing a component by rewriting STEP coordinates → wheels land off-frame, nothing warns.
- Assuming `transform` moves a whole import: **one `transform` feature moves ONE body**
  (`body:"f5#3"`, pivot defaults to the origin, rotate then translate). Moving a multi-body
  import means one call per body — that is the signal to use assembly components instead.
- Leaving the source's legacy bodies in because "the import is one feature" — that is what
  `exclude_bodies` is for.
- Trusting a `fit_cylinder_axis` result with a large `rms_residual_mm`.

## Deeper docs

- `cad_skill("edit-assembly")` (interface-first mating), `cad_skill("source-parts")`
  (catalog hardware)
- `cad_guide('assembly')`, `cad_guide('verify')`
