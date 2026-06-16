// Solved-sketch -> OCCT planar-profile compiler (C++ port of the loop/region core
// of engine/doc/features/sketch.py). This is the missing link in the Wave 2
// pipeline: it turns the 2D output of aicad::sketch::solve_sketch into the planar
// TopoDS_Face(s) that geom::extrude_face / geom::revolve_face consume to make 3D
// solids — i.e. "solve a dimensioned sketch, get a face of exactly the right size".
//
// WHY a separate translation unit (not part of features.cpp):
//   This is the only place the C++ core couples the sketch solver (nlohmann::json
//   solved-entity dicts) to OCCT B-rep construction. Keeping it isolated means the
//   feature builders (extrude/revolve) stay pure OCCT-shape -> OCCT-shape and are
//   independently testable, exactly like the Python split (sketch.py builds the
//   regions; revolve.py/extrude consume faces).
//
// ALGORITHM (mirrors sketch.py):
//   - One OCCT edge per non-construction line/circle/arc, lifted onto the plane.
//   - _detect_loops: closed single edges (circles) are immediate one-edge loops;
//     open edges chain head-to-tail by endpoint proximity (EPS 1e-6) with reversal
//     tracking; non-closing open chains are discarded.
//   - even-odd region nesting (compute_regions): a loop contained in exactly one
//     other loop is that loop's hole; uncontained loops are outer regions. Each
//     region becomes one face = outer wire minus its (reversed) hole wires.
#pragma once

#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Face.hxx>
#include <TopoDS_Wire.hxx>

#include "geom/plane.h"

namespace aicad::geom {

// Build planar faces (in 3D world space, lifted onto `plane`) from solved sketch
// entities. `solved_entities` is SketchSolution::entities (id -> param dict).
// Construction geometry is skipped. Closed loops are detected by endpoint
// chaining (EPS 1e-6); inner loops nest as holes (even-odd containment).
// Returns one face per outer region (with its holes). Empty if no closed loop.
//
// Defensive: entities that fail to build an edge are skipped; on total failure
// (no valid region face) an empty vector is returned — never throws across the
// API boundary.
std::vector<TopoDS_Face> faces_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane);

// Build a single (open OR closed) wire from a sketch's raw non-construction edges,
// lifted onto `plane`, in entity order — the SPINE/PATH a sweep follows and the
// open trace a rib rides (where faces_from_solved_sketch's closed-loop assembly
// does not apply). Returns a null wire if no edge builds or they do not connect.
TopoDS_Wire wire_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane);

}  // namespace aicad::geom
