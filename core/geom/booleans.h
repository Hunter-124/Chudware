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

#include <TopoDS_Shape.hxx>

namespace geom {

// Union: a ∪ b (BRepAlgoAPI_Fuse). Welds two solids into one; overlapping volume is
// counted once. Empty shape on kernel failure.
TopoDS_Shape fuse(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Difference: a − b (BRepAlgoAPI_Cut). Removes b's material from a — the bore/pocket
// op (a block minus a cylinder = a drilled hole). Empty shape on kernel failure.
TopoDS_Shape cut(const TopoDS_Shape& a, const TopoDS_Shape& b);

// Intersection: a ∩ b (BRepAlgoAPI_Common). The volume shared by both solids. Empty
// shape on kernel failure OR when the two solids do not overlap.
TopoDS_Shape common(const TopoDS_Shape& a, const TopoDS_Shape& b);

}  // namespace geom
