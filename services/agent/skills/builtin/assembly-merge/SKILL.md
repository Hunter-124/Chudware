---
name: assembly-merge
description: Merge independently-sourced CAD with measured placement, then verify assembly fit and native Motion studies before exporting; incomplete checks are not clearance evidence.
inject: auto
triggers:
  keywords: [merge cad, merge the wheel, merge two parts, mount onto, graft, retrofit, swap the wheel, replace the wheel, two step files, imported step, vendor part, third-party part, foreign geometry, off-frame, independently sourced, assembled mechanism, motion study, kinematic simulation, motion interference]
  tools: [fit_cylinder_axis, asm_add_component, asm_mate, solve_mates, simulate_motion]
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
- **Static fit is not swept clearance.** A moving mechanism also needs a native Motion study
  over its intended joint ranges. Missing/failed geometry, unresolved poses, skipped checks,
  or `interference.complete:false` mean **incomplete**, never "no unwanted interference".
  A movie alone does not verify interference, nor does a sampled check prove continuous
  clearance between samples. Identify deliberate joint contact separately; never silently
  discard known collision pairs to obtain a clear verdict.

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

## Assembled mechanism motion

1. **Complete the mechanism before animating it.** Roster each actual moving part and the
   fixed structure. Model pivots, bearings, shafts, engagement, running clearances and
   retention with native operations; record measured mount geometry and explicitly label
   proposed joint designs. A fixed pose is not a joint or evidence of a load path.
2. **Author the intended freedoms.** Add scalar angle (degrees) or distance (mm) mates to
   drive joints; concentric/coincident mates and coupling ratios are not scalar drivers.
   An angle-only joint needs its measured hinge line:
   `asm_mate("angle", ..., value={"value":0,"axis":[...],"axis_point":[...]})`.
   This input line is WORLD geometry, stored relative to the first reference component;
   wheel roll can therefore follow a steered carrier rather than spinning about an obsolete
   world axis. Keep a straight rear axle non-steering while allowing wheel roll when that is
   the design. `solve_mates()` must resolve the complete network before the sweep.
3. **Use the existing Motion tab for interactive studies.** Open the assembly, choose joints
   from the joint dropdown, add the needed driver rows, and set each start/stop range.
   Set shared sample steps and minimum clearance; keep interference checking enabled
   (the default). Run the study, then play or scrub its frames and use collision navigation
   to inspect offending poses and component pairs. The study runs asynchronously on the
   native engine without changing the saved assembly's poses or mates.
4. **Use the same engine from MCP for repeatable evidence.**
   `simulate_motion(drivers=[{"mate_id":steer_joint,"start":-30,"stop":30},
   {"mate_id":roll_joint,"start":0,"stop":360}], steps=49,
   check_interference=True, min_clearance_mm=clearance_mm, doc_id=assembly_id)`.
   Each ID must be a unique active angle/distance mate. Explicit `drivers` replaces legacy
   `mate_id/start/stop`; omitting it preserves one-driver calls. All rows sample the same
   normalized timeline, and every `frame.driver_values` records every requested joint value.
   Do not write another kinematic pose loop or hand-edit assembly source files.
5. **Classify evidence honestly.** Require `ok`, successful pose checks, and
   `interference.complete:true` with `frames_checked` equal to the requested sample count
   before claiming the sampled sweep is clear. Inspect every `trajectory` frame's
   `collision_detail`, not only `first_collision_step`; `component_a`/`component_b`
   identify components even when names repeat.
   `pair_failures` and `geometry_failures` must be resolved and the study rerun.
   Per-frame `interference:null` / `interference_complete:false` is unchecked or incomplete,
   not false. Known hits remain actionable even when other checks failed. Increase sampling
   around close approaches and report the tested ranges, steps and clearance threshold.
6. **Treat playback separately from verification.** `loop=True` reuses the original start
   frame, including all driver values and check outcomes. It adds no computed pose or
   interference check and does not increase `frames_checked` or the dynamics timeline.
   Keep model material colors, a stable full-mechanism camera, and visible collision
   information in animations. Preserve approved source models and movies when producing
   proposed alternatives; animation is not manufacturing or strength approval.

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
