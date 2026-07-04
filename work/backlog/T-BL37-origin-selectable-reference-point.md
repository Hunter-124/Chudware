---
id: T-BL37
title: Origin as a selectable, renderable reference point — FM selectable + viewport point render + engine ref-resolution
workstream: backlog
owner: sonnet-alba
status: review
priority: 3
lane: modeling-core
kind: new
depends_on: []
branch: task/T-BL37-origin-selectable-reference-point
---

## Context

Direction from Hunter (2026-07-03). The FM "Origin" node currently groups the Front/Top/
Right datum planes, which are selectable and usable as references. But the **origin itself
— the point at (0,0,0) — is not selectable and is not rendered**. Hunter wants the origin
to be a first-class reference: selectable in the FM tree, rendered in the viewport as a
point, and usable as a reference wherever datum planes/axes already are (sketch refs,
datums, surfacing, mates, measure, patterns-about-a-point, etc.).

This is cross-cutting (that's why it's its own task, not folded into the GUI restyle):
engine ref-resolution + viewport rendering + FM selectability all have to agree on an
"origin point" reference.

## Definition of done

- [ ] **Engine ref-resolution:** the origin resolves as a reference wherever datum
      planes/axes resolve (e.g. `plane.front`, `axis.z` precedent). Give it a stable ref
      identity (e.g. `point.origin` / `datum:origin` — match the existing datum-ref naming
      scheme, do NOT invent an inconsistent one). Accepted by the ops that take a point/
      datum ref (measure_between, datum creation, mate-to-point, pattern-about-point, and
      any surfacing/sketch op that takes a point). Where an op genuinely can't use a point,
      that's fine — don't force it — but it must not crash/reject with a confusing error.
- [ ] **Viewport render:** the origin renders as a point glyph in the 3D viewport, like
      datum planes/axes render (respecting show/hide, the eye toggle in the FM row). Sensible
      default visibility.
- [ ] **FM selectable:** the Origin row is selectable (click selects the origin point ref,
      not just expands/collapses the group) and can be picked as a reference in a form well
      that accepts a point/datum. Reconcile the expand/collapse affordance vs the new
      select affordance (e.g. chevron toggles expand, row body selects — or a clear rule).
- [ ] **Pick from viewport:** clicking the rendered origin point in the viewport selects it
      as a reference (the selector machinery — see resolve_edge_sel / make_selector, T-BL28
      heritage — needs a point-pick path if one doesn't exist).
- [ ] Round-trip: build a doc, select origin, use it as a reference in at least one real op
      (e.g. measure_between origin↔a vertex, or a datum/pattern about origin), verify it
      resolves and the op succeeds. Drive via MCP + confirm in GUI.
- [ ] Screenshots light+dark of the rendered origin + the selected state; eyeball them.
- [ ] flock-serialized build; scripts/gate.sh + `graphify update .`; refresh the surfacing/
      datum cheatsheet + cad_guide topic if origin-as-ref changes the agent-facing vocab
      (MCP freshness, WORKFLOW.md §6).

## Driver notes

- graphify-first: `graphify query "datum plane reference resolution"` and
  `graphify query "viewport datum rendering"` before raw reads.
- Precedents to mirror: how `plane.front`/`axis.z` are exposed in the FM, resolved in the
  engine (routeDatumPlane/routeDatumAxis in property_manager.cpp), and drawn in the
  viewport. Origin is the point analogue — reuse the same patterns, don't fork them.
- Selector/pick machinery: `resolve_edge_sel` / `make_selector` (T-BL28). A point-pick may
  need a new selector kind; keep it consistent with the existing datum selectors.
- **Tier: Opus-leaning** if the engine ref-resolution or the viewport point-pick turns out
  nontrivial. A Sonnet driver hitting depth in the engine ref layer should stop at a clear
  repro + a question file rather than guessing at datum-ref semantics.
- Related GUI: T-BL36 (animated selection border) — if it lands first, the new Origin row
  inherits the border treatment automatically via the shared selected-state; if this lands
  first, note it for T-BL36 so it covers the new row.

## Progress log
