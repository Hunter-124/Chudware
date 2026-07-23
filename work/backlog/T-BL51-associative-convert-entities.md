---
id: T-BL51
title: Associative Convert Entities — projected sketch references update when the source geometry changes
workstream: backlog
owner:
status: todo
priority: 3
lane: modeling-core
kind: new
depends_on: [T-BL43]
branch: task/T-BL51-associative-convert-entities
---

## Context

Follow-up filed from T-BL43 (2026-07-04). T-BL43 delivered CORRECT ONE-SHOT projection for
Convert Entities / external sketch references: the `sketch_convert` op + `sketch_project_entities`
RPC project external edges / face boundary loops / datum planes onto the active sketch plane via
OCCT `GeomProjLib` (oblique circles → ellipses, face loops complete, perpendicular edges → points),
and the projected entities are pinned with whole-entity `fix` constraints so new geometry can
constrain against them.

What is deliberately NOT delivered (stated honestly at review): **associativity**. No link from a
projected entity back to its source face/edge selector is stored; when the source model changes
(e.g. the referenced boss is resized), previously converted entities keep their old coordinates.

## Definition of done

- [ ] Store the source ref (selector + kind) on each projected entity (or on a per-convert group
      record) in the sketch feature params — survives save/load.
- [ ] On document rebuild, re-resolve each stored source ref and re-project; update the pinned
      entity coordinates in place (same entity ids, so downstream constraints/dimensions survive).
- [ ] Dangling-reference behaviour: source feature deleted/selector no longer resolves → mark the
      projected entity dangling (keep last-known geometry, surface a per-entity status the GUIs can
      tint yellow, SolidWorks-style) — do NOT silently drop it or fail the rebuild.
- [ ] Constraint solver interplay: re-projected coordinates re-enter through the same
      `fix`-pinned path so the solve after rebuild converges deterministically.
- [ ] Tests: resize the source body → converted entities follow; delete the source → dangling not
      crashing; undo/redo across a re-projection.
- [ ] Contract: extend the T-BL43 amendment in contracts/doc_ops.md (append-only) with the
      associative-update + dangling semantics.

## Driver notes

- Kernel pieces already exist from T-BL43: `chudware::doc::project_edges_to_plane` /
  `face_boundary_edges` (core/doc/recon.{h,cpp}) and the shared `project_external_refs` helper in
  core/doc/session.cpp — associativity is a doc-schema + rebuild-hook problem, not new projection
  math.
- The re-projection hook belongs where sketches are compiled (document.cpp sketch branch or a
  pre-solve pass in compile_document) — the stored ref must resolve against the geometry of
  features BEFORE the sketch in the tree (rollback semantics), not the finished body.
- Selector stability across rebuilds is the hard part (sid/centroid drift when the source is
  edited) — reuse the centroid-near re-resolution strategy `resolve_sketch_plane_doc`'s face
  branch uses (session.cpp).
- **Tier: OPUS** — rebuild-loop + reference-stability work; same rationale as T-BL43.

## Progress log
