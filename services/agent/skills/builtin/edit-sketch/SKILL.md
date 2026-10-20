---
name: edit-sketch
description: 2D sketch workflow — true curves, geometric relations and functional driving dimensions, residual fully-definition, then check_sketch DOF 0 and not pinned_only before a solid feature.
inject: auto
triggers:
  keywords: [sketch, profile, constrain, dimension, dof, rectangle, circle sketch, ascii, draw a, draw me a]
  tools: [check_sketch, sketch_health, sketch_auto_constrain, render_sketch_ascii, apply_ascii_edits]
  phases: [sketch]
---

# Edit Sketch

## Sequence

1. Create/edit sketch geometry on the intended plane/face (via `add_features` sketch ops:
   `sketch_add_entity`, `sketch_add_constraint`, `sketch_set_dim`, fillet/chamfer/pattern/mirror…).
2. **READ the auto-readback** on every mutation response — `sketch_readbacks[i]` (or
   `ascii_readback` on single-sketch tools) carries `ascii_ruled` (the ONE ruled ASCII
   picture, col/row numbered so you can address cells), `summary` (entities with
   names/coords, dimensions with ids/values, constraints, open/closed status,
   endpoint-coincidence notes, closure + dof + ordered schedule), `grid` + `index` (the
   cell<->world contract) and `findings`. Do not guess the next edit from memory; ground it
   on that block. Full render is still available via `render_sketch_ascii(sketch)`.
3. Draw roughly, then add geometric relations before independent functional driving dimensions.
   origin, axis.h and axis.v are valid refs. For example:
   `{"op":"sketch_add_constraint","sketch":"f1","constraint":{"id":"c1","type":"coincident","refs":["e1.center","origin"]}}`
   then `{"op":"sketch_add_constraint","sketch":"f1","constraint":{"id":"c2","type":"radius","refs":["e1"],"value":{"value":4,"expr":null}}}`.
   `sketch_set_dim` retunes an existing dimension: `{"op":"sketch_set_dim","sketch":"f1","id":"c2","value":5}`.
   Read existing relations before adding coincidents; near-touching points may still need joining.
4. Dimension mating features first (fit_calc for fit-driven sizes), envelope second. Use symmetry
   about axis.v or a construction centerline and one half-size/spacing, not two dimensioned halves.
   Use true arcs and tangent lines for slots/rounded profiles, not faceted polylines/splines.
5. Convert surrounding geometry with sketch_convert, construction:true, associative:true:
   face/edge/vertex refs use selectors; edge mode:"pierce" creates a plane-crossing point;
   datum:"axis.x"|"axis.y"|"axis.z" and kind:"sketch_entity" are also supported.
   Relate authored geometry to returned entities. Fixed external refs are legitimate.
6. Append `{"op":"sketch_fully_define","sketch":"f1","relations":true,"dimensions":true,"anchor":"origin"}`.
   add_features defaults auto_define:true after explicit intent. For staged edits use call
   auto_define:false, or auto_define:false on feature/params/authoring op, then define before use.
7. Read sketch_health and check_sketch: require closed material profile, dof == 0,
   pinned_only == false, no conflicts/skipped constraints. Never blanket-fix the profile
   to pass. Resolve free variables with relations/dimensions; re-read and repeat.
8. Only then extrude/revolve/loft; a sketch alone is not a solid.
9. After the 3D feature lands: `describe_part` / `measure_part` the result.

## Rules

- Never consume an under-constrained or pinned-only authored profile for production geometry.
- Mating diameters go through `fit_calc` when they seat on a shaft/pin.
- Verify-before-done: check_sketch dof 0 and not pinned_only + post-feature describe/measure.
- Always **read the ASCII readback** after a sketch mutation before the next action.

## Deeper docs

- `cad_guide('gsl')` for text-only geometry read
- Explicit full ASCII: `render_sketch_ascii` / compact auto-readback on mutation tools
- Feature build: `cad_skill("edit-feature")`
- Targeted cell edits: `apply_ascii_edits(kind='sketch', sketch_id=..., edits_json='<JSON list>', grid_json='<JSON grid>', index_json='<JSON index>')` using the `grid`/`index` from the last ASCII render.
