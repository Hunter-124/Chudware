# T-BL43 — questions for the advisor

## Q1 (2026-07-04, opus-brae): contract amendment for `sketch_convert` — ratify?

**Context.** `contracts/doc_ops.md` (C53) contracted a `sketch_convert` op, but the C++ engine
never implemented it — both GUIs (host `sketch_mode.cpp:applyConvertRef` and ui/imgui
`sketch_overlay.cpp`) have been emitting an op that always failed with `unsupported`. That is
the "Convert Entities is broken" in this task's DoD.

**What I did.** Implemented `op_sketch_convert` in `core/doc/session.cpp` conforming to the
contracted shape (refs list, real-geometry default, whole-entity `fix` pin, `$idx`/`$idx#k`
id_map, `invalid_op`/`convert_failed`/`unsupported_ref` errors). BUT the projection math is
UPGRADED beyond the contracted bullet ("same math as `sketch_project_ref`": faces →
intersection line, circles only when axis ∥ normal): now true orthographic projection via OCCT
`GeomProjLib` — faces project their full boundary wires, oblique circles land as ellipses,
perpendicular edges collapse to points, splines sample. This is exactly Hunter's 2026-07-03
direction in the task file ("fix Convert Entities so external edges/face loops project
correctly"), so I treated the task as the authority and APPENDED a dated amendment block to
doc_ops.md (append-only respected — no existing lines altered) rather than blocking.

**Question.** Ratify the appended amendment (or reword it)? Alternative was to implement the op
bug-for-bug to the stale contracted math and file the upgrade separately — rejected because the
task's Definition of done explicitly requires face-loop projection and correct curve
classification, and shipping the stale math would leave Convert Entities wrong.

**Recommended answer.** Ratify as appended; the contracted op *shape* is untouched, only the
projection fidelity improved, and the old behaviour was never shipped (op didn't exist in the
engine), so nothing can regress.

## Note: associativity (honesty statement)

Delivered ONE-SHOT projection. No associative link from projected entities back to the source
face/edge is stored; converted geometry does not update when the source model changes. The `fix`
pin makes them stable anchors, not live references. Follow-up task filed for associative update
(see task file Progress log).
