---
id: T-BL43
title: Sketch external references — project external faces/edges/planes into a sketch, fix Convert Entities, select external features while in a sketch
workstream: backlog
owner: unassigned
status: todo
priority: 2
lane: modeling-core
kind: new
depends_on: []
branch: task/T-BL43-sketch-external-references
---

## Context

Direction from Hunter (2026-07-03). Three tightly-related sketch-reference capabilities:
(a) select geometry OUTSIDE the current sketch while editing it, (b) project external
faces/edges/planes into the sketch as reference geometry, (c) make the **Convert Entities**
tool work properly. Together these let a sketch reference the rest of the model.

## Definition of done

- [ ] **Select external features while in a sketch:** the selection system lets the user pick
      faces, edges, planes, body edges, and other sketches' geometry while a sketch is open
      for editing (today the in-sketch selection filter likely blocks external picks — allow
      them for reference/projection).
- [ ] **Convert Entities works:** select external edge(s) / a face loop → project onto the
      active sketch plane as sketch entities. Fix whatever is currently broken so it produces
      correct projected curves on the plane.
- [ ] **Projected geometry as references:** add external faces/edges/planes projected into the
      current sketch as reference geometry that can constrain/drive new sketch entities.
- [ ] **Associativity (ideal):** projected/converted geometry updates when the source
      geometry changes. If full associativity is too large for one task, deliver correct
      one-shot projection now and file a follow-up for associative update — do NOT silently
      ship non-associative as if it were associative; state which you delivered.
- [ ] Real-flow verify: make a body → start a sketch on a face → Convert Entities on an
      external edge → constrain new geometry to the projected edge → extrude. Also project a
      plane/edge from another body. Drive via MCP + confirm in GUI.
- [ ] Screenshots light+dark of the projected geometry in-sketch; eyeball.
- [ ] MCP freshness (WORKFLOW.md §6) if the sketch/convert vocab changes; scripts/gate.sh +
      `graphify update .` before review.

## Driver notes

- graphify-first: `graphify query "sketch convert entities projection"`,
  `graphify query "sketch selection filter external"`, `graphify query "project edge onto plane"`.
- Engine-heavy: projecting OCCT edges/faces onto a sketch plane (BRepProj / geometric
  projection) + representing the result as sketch reference entities + the associativity link
  back to the source. This is real geometry-kernel work.
- Selection: find where sketch-edit mode filters selectable entities; it must allow external
  picks for projection without letting the user accidentally add non-coplanar geometry AS
  sketch geometry — projected copies are the mechanism.
- **Tier: OPUS** — OCCT projection + associativity + the selection-boundary change are gnarly
  geometry/kernel work. A Sonnet driver who claims it and hits the projection kernel should
  stop at a repro + question file, not guess.
- Related: T-BL42 (absorbed sketches reusable as refs), T-BL41 (dimension/relation display).

## Progress log
