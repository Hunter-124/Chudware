---
name: edit-sketch
description: 2D sketch workflow — close profiles, constrain to DOF 0, smart-dimension mating features first, then check_sketch before extrude/revolve. Uses ASCII readback for non-vision parity.
inject: auto
triggers:
  keywords: [sketch, profile, constrain, dimension, dof, rectangle, circle sketch, ascii]
  tools: [check_sketch, sketch_health, sketch_auto_constrain, render_sketch_ascii]
  phases: [sketch]
---

# Edit Sketch

## Sequence

1. Create/edit sketch geometry on the intended plane/face (via `add_features` sketch ops:
   `sketch_add_entity`, `sketch_add_constraint`, `sketch_set_dim`, fillet/chamfer/pattern/mirror…).
2. **READ the auto-readback** on every mutation response — compact ASCII (`ascii` / `ascii_block`)
   + NL summary (`sketch_summary`: entities with names/coords, dimensions with ids/values,
   constraints, open/closed status, endpoint-coincidence notes, health flags). Do not guess the
   next edit from memory; ground it on that block. Full render is still available via
   `render_sketch_ascii(sketch)`.
3. **Endpoints that touch are auto-coincident** when `sketch.auto_coincident` is on — profiles
   should close without a manual "not closed" fight. Near-coincident endpoints are called out
   in the readback findings.
4. **ASCII verify loop** until DOF 0:
   - edit (mutation) → **read ASCII + NL readback**
   - `sketch_health(id)` — narrated free variables + ordered relations-then-dimensions schedule
   - add scheduled relations, or `sketch_auto_constrain(id)` for near-touching endpoints
   - `check_sketch(id)` → require `dof == 0` before using the profile
   - fix → re-read → repeat
5. Dimension **mating features first** (hole diameters, center distances that define an interface).
   Envelope dims second. Dimension callouts appear by name/value in the readback.
6. **Before declaring a sketch done:** call `render_sketch_ascii(sketch)` + `sketch_health` /
   `check_sketch` and require dof 0 + closed profile. Text-only models rely on the NL entity /
   dim / constraint list in the readback — do not skip it.
7. Only then extrude/revolve/loft from the sketch via `add_features`.
8. After the 3D feature lands: `describe_part` / `measure_part` the result.

## Rules

- Never extrude an under-constrained sketch (DOF > 0) for production geometry.
- Mating diameters go through `fit_calc` when they seat on a shaft/pin.
- Verify-before-done: `check_sketch` green + post-feature describe/measure.
- Always **read the ASCII readback** after a sketch mutation before the next action.

## Deeper docs

- `cad_guide('gsl')` for text-only geometry read
- Explicit full ASCII: `render_sketch_ascii` / compact auto-readback on mutation tools
- Feature build: `skill://edit-feature`
