---
name: edit-sketch
description: 2D sketch discipline — geometric relations first, only critical dimensions, probe before dimensioning, let auto_define finish, then check_sketch DOF 0 before a solid feature.
inject: auto
triggers:
  keywords: [sketch, profile, constrain, dimension, dof, rectangle, circle sketch, ascii, draw a, draw me a]
  tools: [new_part, check_sketch, sketch_health, sketch_probe, sketch_auto_constrain, render_sketch_ascii, apply_ascii_edits]
  ops: ["sketch_*", "add_feature:sketch"]
  phases: [sketch]
---

# Edit Sketch

Goal: a well-constrained, human-editable sketch — few critical dimensions, many relations.

## Sequence

1. Author in ONE `add_features` batch: geometry on the intended plane/face, then relations,
   then only the critical dimensions. origin, axis.h, axis.v are valid refs.
2. **Relations first**: coincident, horizontal/vertical, parallel/perpendicular, tangent, equal,
   concentric, midpoint, point_on, symmetric. Use construction geometry (a centerline or
   construction circle) and symmetry about axis.v: one half-size, not two dimensioned halves.
   True arcs + tangent lines for slots/rounded profiles, never faceted polylines.
3. **Critical dimensions only**: mating/functional sizes (fit_calc for shaft/pin fits), the
   envelope, partner spacings. Never dimension what a relation already implies (equal,
   symmetric, tangent, collinear), and never pin geometry with `fix` — `fix` is only for
   external references from sketch_convert.
4. **Probe before a driving dimension**: `sketch_probe(sketch, add_constraint=… | set_dim=… |
   delete_constraint=…)` dry-runs one change on a copy. Add it only if `would_over_define`
   is false and `conflicts_with` is empty; `dof_delta` shows what it removes.
5. `auto_define` (default on) fills the remaining freedom after your intent and tags what it
   adds `auto`. Read the reply's `sketch_definitions[]` (notes, dof_before/after, rounded) and
   in readbacks compare `authored_dimensions` with `auto_dimensions`; findings
   `all_dimensions_auto` (you authored no intent) and `dimension_heavy` (too many authored
   dims) mean redo with relations. Opt out with `auto_define=false` only for staged edits.
6. READ `sketch_readbacks[i]` after every mutation (`ascii_ruled`, `summary`, `findings`);
   ground the next edit on it. `hints` are suggestions, not a plan.
7. `sketch_convert` projects model geometry as construction reference (default); relate
   authored geometry to the returned entities.
8. Gate: `check_sketch` — closed profile when a region is needed (open paths for sweeps and
   rib profiles are valid), dof 0, not pinned_only, no conflicts or skipped constraints.
9. Only then create the solid feature; afterwards `describe_part` / `measure_part`.

## Deeper docs

- `cad_guide('sketch')` ops table, face-sketch frame; `cad_skill("edit-feature")` for solids.
- Cell edits: `apply_ascii_edits(kind='sketch', sketch_id=…, edits_json=…, grid_json=…, index_json=…)`
  with the `grid`/`index` of the last render.
