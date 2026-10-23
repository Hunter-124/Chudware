// OCCT multi-body boolean operators (C++ port of engine/doc/features/boolean.py's
// op->class core). These are the modeling ops that turn two solids into one: union
// (fuse), difference (cut), and intersection (common). They are what lets a base
// extrude become a real multi-feature part — a block with a bored hole, two welded
// bosses, the shared volume of two stock blanks.
//
// WHY a separate translation unit (not part of features.cpp):
//   features.cpp builds PRIMITIVES (box / prism / revol) from a single profile;
//   booleans.cpp COMBINES finished solids. Keeping them apart mirrors the Python
//   split (extrude.py / revolve.py make solids, boolean.py composes them) and keeps
//   each builder a clean shape->shape (or shape*shape->shape) function that is
//   independently testable.
//
// OP -> OCCT-CLASS MAPPING (must match boolean.py exactly):
//   "combine"   -> BRepAlgoAPI_Fuse   (a ∪ b)
//   "subtract"  -> BRepAlgoAPI_Cut    (a − b, material of a minus b)
//   "intersect" -> BRepAlgoAPI_Common (a ∩ b, shared volume)
//
// ERROR CONTAINMENT: every op checks IsDone() and returns an empty TopoDS_Shape on
// failure rather than throwing — the same discipline as boolean.py (which wraps the
// kernel call so a failure becomes a contained error, never a crash). The caller /
// tessellator handles an empty shape gracefully.
#pragma once

#include <vector>

#include <TopoDS_Shape.hxx>

namespace geom {

// Union: a ∪ b (BRepAlgoAPI_Fuse). Welds two solids into one; overlapping volume is
// counted once. Empty shape on kernel failure.
TopoDS_Shape fuse(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Difference: a − b (BRepAlgoAPI_Cut). Removes b's material from a — the bore/pocket
// op (a block minus a cylinder = a drilled hole). Empty shape on kernel failure.
TopoDS_Shape cut(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Multi-tool union: base ∪ tools[0] ∪ tools[1] ∪ … computed in ONE BOPDS pass
// (BRepAlgoAPI_Fuse with SetArguments/SetTools). MUCH faster than folding the pairwise
// fuse() over a growing accumulator — that re-traverses the whole running result on every
// step (~O(N²) for N tools, the pattern-replication hot path); the multi-argument boolean
// intersects all arguments once. Null tools are skipped; returns `base` if no live tools
// remain, empty shape on kernel failure.
TopoDS_Shape fuse_many(const TopoDS_Shape& base, const std::vector<TopoDS_Shape>& tools);

// Multi-tool difference: base − (tools[0] ∪ tools[1] ∪ …) in ONE pass (BRepAlgoAPI_Cut
// with all tools as Tools). Same O(N²)→O(N)-pass win as fuse_many for a patterned cut.
// Null tools are skipped; returns `base` if no live tools remain, empty on failure.
TopoDS_Shape cut_many(const TopoDS_Shape& base, const std::vector<TopoDS_Shape>& tools);

// Intersection: a ∩ b (BRepAlgoAPI_Common). The volume shared by both solids. Empty
// shape on kernel failure OR when the two solids do not overlap.
TopoDS_Shape common(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Merge co-planar/co-cylindrical adjacent faces sharing a domain into single faces
// (ShapeUpgrade_UnifySameDomain). build123d runs this in its `.clean()` after every
// boolean, so a combine/mirror that welds two flush blocks reports ONE merged face
// instead of two with a seam edge. Returns `s` unchanged on failure (best-effort).
TopoDS_Shape unify_same_domain(const TopoDS_Shape& s);

// Fuse then UnifySameDomain, skipping the unify pass when the BOP reports no
// section edges. Disjoint / non-touching operands produce an empty
// section-edge list — UnifySameDomain is pure overhead there. Intersecting or
// flush-touching fuses keep the clean() face-merge. Empty on kernel failure.
TopoDS_Shape fuse_unify(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Cut then UnifySameDomain: the feature-level material-removal counterpart of
// fuse_unify, so a flush cut leaves no coplanar seam faces. Empty on kernel failure.
TopoDS_Shape cut_unify(const TopoDS_Shape& a, const TopoDS_Shape& b);

}  // namespace geom
