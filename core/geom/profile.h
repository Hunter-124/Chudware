// Solved-sketch -> OCCT planar-profile compiler (C++ port of the loop/region core
// of engine/doc/features/sketch.py). This is the missing link in the Wave 2
// pipeline: it turns the 2D output of chudware::sketch::solve_sketch into the planar
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

namespace chudware::geom {

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

// Like faces_from_solved_sketch, but returns ONE face per detected loop — both
// material (even depth) and void (odd depth) regions — each with its direct child
// loops as holes. Callers that need the doc_sketch_regions GUI index must re-sort
// by face (u,v) centroid themselves. Used so a region pick of the "inner disc" of
// concentric circles extrudes a solid cylinder rather than the washer.
std::vector<TopoDS_Face> all_region_faces_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane);

// Group the material (depth-0) region faces into CONNECTED MATERIAL REGIONS: the
// sets of top-level loops whose 2D footprints OVERLAP or TOUCH. Extruding such a
// set fuses their prisms into ONE solid, so it is ONE material region however many
// closed loops drew it — which is what "material region" means to a parametric
// modeller (SolidWorks extrudes the unioned contour) and therefore what an agent
// readback must count and what a `hint_index` can address.
//
// Motivating live run run-42508173312214 ("lexus_f_keychain"): three OVERLAPPING
// rectangles in one sketch were narrated as "3 material regions" while the extruder
// correctly fused them into one 264 mm^2 union, so the model asked for regions 1
// and 2 — indices that addressed nothing and failed as "boss added no material".
//
// Each returned group holds the same faces faces_from_solved_sketch would emit for
// its member loops (outer wire minus its direct hole wires), in loop order; the
// groups themselves are ordered by their lowest member loop index. Concatenating
// every group therefore reproduces faces_from_solved_sketch's whole-sketch profile.
std::vector<std::vector<TopoDS_Face>> connected_material_regions(
    const nlohmann::json& solved_entities, const SketchPlane& plane);

// Build a single (open OR closed) wire from a sketch's raw non-construction edges,
// lifted onto `plane`, in entity order — the SPINE/PATH a sweep follows and the
// open trace a rib rides (where faces_from_solved_sketch's closed-loop assembly
// does not apply). Returns a null wire if no edge builds or they do not connect.
TopoDS_Wire wire_from_solved_sketch(
    const nlohmann::json& solved_entities, const SketchPlane& plane);

}  // namespace chudware::geom
