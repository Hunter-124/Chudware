# 3D printing (FDM) — design, orient, slice, prove

## Workflow
1. While modelling each part: `dfm_check(printer="<make model>")`. Every fail/warn row names
   face sids, the feature that built them, a model-frame centroid and fixes. Fix fails in CAD,
   re-run. Compare `estimate` (material g, time h, support share) between revisions.
2. `optimize_print_orientation` when the part is loaded or has cosmetic/precision faces
   (`strength_dir=[x,y,z]` keeps the load in the layer plane; `preserve_faces=[sid…]` keeps
   supports and the bed off those faces). `slice_part(orientation="auto")` uses the same scorer.
3. `slice_part(doc_id=…, printer="ender 3", material="PLA", goal=…)` — vendor presets from the
   installed PrusaSlicer, orientation, supports and brim from the analysis, then
   `verify_gcode`. `copies=N` repeats one part on its plate.
   Explicit user preferences win: no brim means `brim=0`; a direct-drive conversion needs
   appropriate retraction overrides instead of blindly retaining Bowden retraction.
   Several parts in one job: `slice_part(parts=[{"doc_id": a, "copies": 2}, {"doc_id": b},
   {"path": "…stl"}], printer=…, name="small_parts")` — each part is oriented and analysed on
   its own (per-entry `orientation`/`build_dir`/`preserve_faces`/`strength_dir`), the
   plate is laid out and bed-checked deterministically, and the result is ONE G-code with
   `plate.objects` (position, height, print order).
   "Complete individual objects" (PrusaSlicer `complete_objects`): add
   `complete_individual_objects=True` (with `parts` or `copies`). Each object prints to
   completion before the next, so a failed object does not scrap the rest. Objects are spaced
   by the printer's `extruder_clearance_radius` (Ender-3: 55 mm) plus brims and printed in
   ascending height; at most ONE object may exceed `extruder_clearance_height` (Ender-3:
   25 mm), and it prints last. A plate that breaks this is refused naming the objects —
   split it, reorient lower, or print layer by layer. Never set `complete_objects` through
   `overrides`: stock PrusaSlicer's CLI checks none of these clearances, so the layout and
   `gcode_check.sequential_clearance` (hotend and gantry vs finished objects, from the
   toolpath) are the only guard.
4. Deliver only `print_ready: true`, or name the failing check. Put G-code on printer media
   only through the media tools below. When the user asked for media delivery and a job is
   not print_ready, ask them once (question tool) whether to put it on anyway, naming the
   failed check and its practical risk; if they say yes — or already said so — publish it
   with `user_override` (step 3). The user decides; never refuse or stall on it.
- No slicer installed → `slicer_status` returns the install command; give it to the user.
- NEVER download/extract a slicer, write slicer `.ini` keys by hand, or reuse a design-time
  build direction nobody measured. That combination produced collapsed support towers.

## Delivering to printer media (USB stick / SD card)
The printer lists file NAMES only, no dates: a stale revision beside the current one is
indistinguishable on its screen. The media must hold exactly the current set.
1. The moment a flashdrive/SD delivery is asked for:
   `print_media_inventory(root=<mount, e.g. /run/media/<user>/<LABEL>>)`. Read every page
   (`total`/`truncated`): printable rows, duplicate groups, managed status
   (current/superseded/modified), unmanaged printables. Compare them with the COMPLETE set of
   parts the project needs. Never decide which file is current by mtime or by "newest".
2. Prerequisite for publishing: the USER confirms the media is not in a printer that is
   printing or reading it (ejected/detached, or the printer idle). The tool cannot see the
   printer, so `media_idle=True` is that confirmation — never assume it.
3. `publish_print_media(root=…, project="<name>", jobs=[{"part_id": "upper_arm",
   "manifest_path": "<…>.print.json"}, …], expected_inventory=<inventory_token>,
   media_idle=True)`. `jobs` is the whole project set: a part left out is deleted. Every job
   must match the manifest's `gcode_sha256` (pass `gcode_path` only for a saved G-code that
   moved) and be `print_ready: true`, except a job the user explicitly told you to put on
   the media anyway: add `"user_override": "<the user's words>"` to that job. It publishes
   like any other, its failed checks recorded in the drive README/metadata and returned as a
   `published_not_print_ready` warning you report. The tool never reslices or relaxes a
   check on its own: without an override, a refusal means fix the part and run `slice_part`
   again. Same content set and layout = no new revision.
   Unmanaged/hand-copied printables are never touched unless the user reviewed them and you
   pass their relative paths in `retire_paths`.
   Deprecated G-code is permanently deleted after verified publication, NEVER archived
   on the drive or host. Temporary rollback staging is removed after commit. Obsolete
   managed archives are deleted too; unrelated files and other projects are untouched.
4. Layout defaults to raw `<part>.gcode` files at the drive ROOT. Use `layout="project"`
   only for one top-level `<PROJECT>/` folder; never create PRINT/, nested job folders,
   CAD/review packets or per-release delivery trees on printer media. Keep STL, STEP+,
   3MF and slicing evidence in the workspace, not on the drive.
5. Read back with `print_media_inventory` and report the returned folder and each exact
   filename with its part, the revision, what was deleted/replaced, every
   warning unchanged (slicer, printability, physical-fit caveats), and every unmanaged
   printable or duplicate still on the media. While any remain, the media is NOT clean.
- A refusal (inventory changed, idle not confirmed, hash mismatch, modified managed file,
  symlink/path escape) or partial failure: report it with the state the tool returned; never
  fall back to copying.
- Never `cp`/`rsync`/`fs_write`/shell-copy G-code onto media. Nothing can stop a shell copy;
  the tool is what makes the delivery hash-verified, revisioned and fully reported, and
  ad-hoc copies are how stale duplicates pile up. Humans have the same path:
  `python3 scripts/print_media.py inventory ROOT` and `python3 scripts/print_media.py publish
  ROOT --project PROJECT --jobs-file JOBS.json --expected-inventory TOKEN --media-idle
  [--layout project] [--retire-path RELPATH …]`.

## Geometry rules (0.4 mm nozzle, 0.2 mm layers; scale with nozzle)
- Walls: ≥ 1.2 mm (3 lines) structural, never < 0.8 mm; size in line-width multiples
  (0.45 mm: 0.9 / 1.35 / 1.8 / 2.25) — in-between values print as slow gap fill.
  Horizontal skins ≥ 0.8 mm (4 layers). A wall thinner than the nozzle disappears.
- Overhangs: ≤ 45° from vertical print unsupported. Bottom edges touching the bed: 45° chamfer,
  not a fillet (a fillet's lower arc is a 90° overhang). Ceilings: bridge ≤ ~5–10 mm between
  two anchored walls; one-sided shelves need support or a 45° gusset.
- Holes: vertical holes print ~0.1–0.2 mm undersize — model +0.2 mm or drill/ream; < 1 mm is
  not printable. Horizontal holes > ~6 mm: teardrop (45° roof) or diamond, or drill after.
  Precision bores: orient the axis vertical.
- Threads below M5: heat-set inserts (4.0–4.2 mm pilot for M3 inserts) or nuts in hex pockets,
  not printed threads.
- Internal geometry: no sealed cavities that need support (unremovable); pockets that need
  support must have an opening ≥ 3 mm for removal tools. Resin/SLS: ≥ 2 drain holes ≥ 3.5 mm.
- Bed: flat face down, contact ≥ 50 mm²; height/√contact > 4 tips over → brim or reorient.
- Strength: layers are the weak axis (~50–70 % of in-plane). Put tension/bending in the XY
  plane; pins and snap arms lie flat.

## Saving material and cycle time
- The biggest wastes are support and height: reorient first, then add chamfers/gussets so
  nothing needs support. A part that needs no support also prints cleaner.
- Infill does the bulk; walls/skins dominate small parts. 3 walls + 15 % (goal "balanced")
  suits most functional parts; goal "strength" (4+ walls, 35 % cubic) only for loaded parts;
  goal "material"/"speed" for covers, jigs, prototypes.
- Prefer one larger plate over many single jobs only when parts share a material and layer
  height; split parts that exceed the bed or would need large supports. Sequential plates
  (`complete_individual_objects=True`) hold far fewer objects (clearance spacing) but lose
  only one object to a failure; publish each plate as one job (`part_id` for the plate).
- Report slicer time/grams from `slice_part.stats` — never the deterministic estimate as truth.

## Reading verify_gcode
- `floating_extrusion` fail: plastic laid more than a line width away from anything below —
  spaghetti. Reorient, enable supports, or redesign; never ship it.
- `heat_before_extrude`, `temperatures`, `bounds`, `max_height`, `homing_before_motion` are hard
  stops. Warnings on retraction/fan are advice tied to the printer type.
- `sequential_clearance` fail (Complete individual objects): a later move runs the hotend
  inside the clearance radius of, or the gantry below the top of, a finished object;
  `regions` name it. Re-plan the plate; never print it.
