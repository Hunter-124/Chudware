// Fully Define Sketch — infer the geometric relations and driving dimensions a sketch is
// missing, the way a CAD user would add them, until it is fully defined (DOF 0).
//
// Pure function over the solver's sketch dict ({"entities":[...], "constraints":[...]}, the
// doc_schema §3 form solve_sketch takes). It never moves geometry: every relation it proposes
// already holds on the current solved coordinates (within tolerance), and every dimension
// takes the current measured value. Each candidate is accepted only if the sketch still
// converges with no conflicting/redundant constraint and the DOF drops, so the result is
// always consistent. Callers mint constraint ids (the op layer: sketch_fully_define).
//
// Inference is deterministic and limited to relations already present in the solved
// shape. Independent candidates are batch-verified; a rejected batch is reduced or
// split without changing existing constraints. Size dimensions precede orientation
// and location dimensions, so connected profiles do not acquire coordinate chains.
// Disabling origin anchoring uses an existing anchored point when available, otherwise
// a floating sketch point: global placement can intentionally remain free. Spline
// through-points receive ordinary location dimensions; any parameters still free
// are reported in notes, never fix-pinned.
#pragma once

#include <nlohmann/json.hpp>

#include <string>
#include <vector>

namespace chudware::sketch {

struct DefineOptions {
    bool relations = true;        // infer geometric relations (coincident, h/v, tangent, ...)
    bool dimensions = true;       // add driving dimensions for the DOF relations leave
    bool anchor_origin = true;    // tie the sketch to "origin" (coincident or dimensions)
    double angle_tol_deg = 0.5;   // h/v, parallel, perpendicular, tangent detection
    double length_tol = 1e-3;     // mm: coincident / equal / concentric / midpoint detection
    // Restrict inference to these entity ids (empty = whole sketch). Constraints may still
    // reference other entities (e.g. coincident to an unselected neighbour).
    std::vector<std::string> only_entities;
};

struct DefineResult {
    // New constraints in acceptance order, WITHOUT "id" (callers assign). Relations first,
    // then dimensions; dimensions carry "value":{"value":<current>} and a "placement" [u,v].
    nlohmann::json constraints = nlohmann::json::array();
    int dof_before = 0;
    int dof_after = 0;
    // Human-readable notes for what could not be defined (e.g. "spline e7 interior points
    // left free"), one string each.
    std::vector<std::string> notes;
};

DefineResult fully_define(const nlohmann::json& sketch, const nlohmann::json& env,
                          const DefineOptions& opt = {});

}  // namespace chudware::sketch
