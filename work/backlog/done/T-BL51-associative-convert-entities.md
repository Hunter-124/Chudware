---
id: T-BL51
title: Associative Convert Entities — projected sketch references update when the source geometry changes
workstream: backlog
owner: hunter
status: done
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

- [x] Store the source ref (selector + kind) on each projected entity (or on a per-convert group
      record) in the sketch feature params — survives save/load.
- [x] On document rebuild, re-resolve each stored source ref and re-project; update the pinned
      entity coordinates in place (same entity ids, so downstream constraints/dimensions survive).
- [x] Dangling-reference behaviour: source feature deleted/selector no longer resolves → mark the
      projected entity dangling (keep last-known geometry, surface a per-entity status the GUIs can
      tint yellow, SolidWorks-style) — do NOT silently drop it or fail the rebuild.
- [x] Constraint solver interplay: re-projected coordinates re-enter through the same
      `fix`-pinned path so the solve after rebuild converges deterministically.
- [x] Tests: resize the source body → converted entities follow; delete the source → dangling not
      crashing; undo/redo across a re-projection.
- [x] Contract: extend the T-BL43 amendment in contracts/doc_ops.md (append-only) with the
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


**2026-08-03 — closed.** The substance of this card shipped under **T-BL49** (2026-07-07):
`params.external_refs[]` (`x<N>` records owning their projected entity ids),
`refresh_sketch_external_refs` running on every mutating `doc_apply_ops` batch and on
`doc_rebuild`/`doc_activate` *before* the compile, `reconcile_external_ref_entities` updating
entities in place under stable ids (re-adding the `fix` pin, cascading constraints only when a
re-projection changes an entity's TYPE), `stale:true` + last-good geometry on an unresolvable
source, a feature-status warning in `document.cpp`, `sketch_health.external_refs[]`, and the
`sketch_break_external_ref` op. The T-BL49 amendment in `contracts/doc_ops.md` covers it.

Gap closed today: the DoD's third test case (undo/redo across a re-projection) did not exist.
Added `assoc: undo/redo across a re-projection restores both states` to
`core/tests/test_sketch_convert.cpp` — it pins the ordering that makes undo correct (the refresh
runs on the batch's working tree, so the undo snapshot pushed from the pre-batch `s.doc` carries
the projection that matches its source): widen the source 40→60, undo → converted loop and the
downstream boss volume snap back to 40, redo → both return to 60, entity ids stable across all
four states. Also appended the T-BL51 undo/redo amendment to `contracts/doc_ops.md`.
Verified: `ctest -R "sketch|convert"` 8/8, `test_sketch_convert` 8 cases / 103 assertions.