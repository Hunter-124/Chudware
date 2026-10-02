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
   `verify_gcode`. One call per part; `copies=N` for repeats on one plate.
4. Deliver only `print_ready: true`, or name the failing check. Copy files to removable media
   with the file tools and compare sha256 from the `.print.json` manifest.
- No slicer installed → `slicer_status` returns the install command; give it to the user.
- NEVER download/extract a slicer, write slicer `.ini` keys by hand, or reuse a design-time
  build direction nobody measured. That combination produced collapsed support towers.

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
  height; split parts that exceed the bed or would need large supports.
- Report slicer time/grams from `slice_part.stats` — never the deterministic estimate as truth.

## Reading verify_gcode
- `floating_extrusion` fail: plastic laid more than a line width away from anything below —
  spaghetti. Reorient, enable supports, or redesign; never ship it.
- `heat_before_extrude`, `temperatures`, `bounds`, `max_height`, `homing_before_motion` are hard
  stops. Warnings on retraction/fan are advice tied to the printer type.
