---
name: edit-sketch
description: 2D sketch workflow — close profiles, constrain to DOF 0, smart-dimension mating features first, then check_sketch before extrude/revolve.
inject: auto
triggers:
  keywords: [sketch, profile, constrain, dimension, dof, rectangle, circle sketch]
  tools: [check_sketch, sketch_health, sketch_auto_constrain]
  phases: [sketch]
---

# Edit Sketch

## Sequence

1. Create/edit sketch geometry on the intended plane/face.
2. **Endpoints that touch are auto-coincident** when `sketch.auto_coincident` is on — profiles should close without a manual "not closed" fight.
3. Health loop until DOF 0:
   - `sketch_health(id)` — narrated free variables + ordered relations-then-dimensions schedule
   - add scheduled relations, or `sketch_auto_constrain(id)` for near-touching endpoints
   - `check_sketch(id)` → require `dof == 0` before using the profile
4. Dimension **mating features first** (hole diameters, center distances that define an interface). Envelope dims second.
5. Only then extrude/revolve/loft from the sketch via `add_features`.
6. After the 3D feature lands: `describe_part` / `measure_part` the result.

## Rules

- Never extrude an under-constrained sketch (DOF > 0) for production geometry.
- Mating diameters go through `fit_calc` when they seat on a shaft/pin.
- Verify-before-done: `check_sketch` green + post-feature describe/measure.

## Deeper docs

- `cad_guide('gsl')` for text-only geometry read
- Feature build: `skill://edit-feature`
