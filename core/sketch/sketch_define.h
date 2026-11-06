// Fully Define Sketch — infer the geometric relations and driving dimensions a sketch is
// missing, the way a CAD user would add them, until it is fully defined (DOF 0).
//
// Pure function over the solver's sketch dict ({"entities":[...], "constraints":[...]}, the
// doc_schema §3 form solve_sketch takes). It never moves geometry beyond the rounding
// described below: every relation it proposes already holds on the current solved
// coordinates (within tolerance), and every dimension takes the current measured value.
// Each candidate is accepted only if the sketch still converges with no conflicting
// constraint, no NEW redundant constraint and the DOF drops, so the result is always
// consistent. Callers mint constraint ids (the op layer: sketch_fully_define).
//
// Existing redundant GEOMETRIC relations (e.g. a duplicated coincident) do not block the
// pass: they are tolerated and reported in notes. Only a non-converged solve, conflicting
// constraints, or a redundant DRIVING dimension refuse the pass, and the refusal is always
// explained in notes.
//
// Inference is deterministic and limited to relations already present in the solved
// shape. Independent candidates are batch-verified; a rejected batch is reduced or
// split without changing existing constraints. Axis symmetry precedes orientation,
// and aligned centers and points share relations rather than coordinate dimensions.
// Size dimensions precede location dimensions; symmetric partners use full spacing
// dimensions rather than half-spacing offsets from the origin.
// Disabling origin anchoring also disables axis relations and uses an existing
// anchored point when available, otherwise a floating sketch point: global placement
// can intentionally remain free. Spline through-points receive ordinary location
// dimensions; any parameters still free are reported in notes, never fix-pinned.
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
    // Snap each measured driving value to a clean increment when it is already within
    // noise of one (so a rough 39.98 becomes 40, not a frozen 39.98): the nearest multiple
    // of S, S = 1 (|v|>=100), 0.5 (10<=|v|<100), 0.1 (1<=|v|<10), 0.05 (|v|<1) mm, 1 deg for
    // angles, when |snapped - v| <= min(0.1*S, 0.005*|v|). Other values stay as measured,
    // cleaned to 1e-4. Off keeps every measured value exact (geometry never moves).
    bool round_dimensions = false;
};

struct DefineResult {
    // New constraints in acceptance order, WITHOUT "id" (callers assign). Relations first,
    // then dimensions; dimensions carry "value":{"value":<current>} and a "placement" [u,v].
    nlohmann::json constraints = nlohmann::json::array();
    int dof_before = 0;
    int dof_after = 0;
    // Driving values snapped by round_dimensions, and the largest change (mm or deg).
    int rounded = 0;
    double rounded_max_delta = 0.0;
    // Human-readable notes, one string each: why the pass was refused (conflicting or
    // non-converged system, redundant driving dimension), tolerated redundant relations,
    // skipped constraints, what is left free ("spline e7 interior points left free").
    std::vector<std::string> notes;
};

DefineResult fully_define(const nlohmann::json& sketch, const nlohmann::json& env,
                          const DefineOptions& opt = {});

struct SettleAutoResult {
    // Existing constraints retain their ids; newly inferred auto constraints have no id.
    // Geometry is reseeded to the settled solution so refilled dimensions hold that shape.
    nlohmann::json sketch;
    std::vector<std::string> superseded;
    // Why auto constraints were left as they were (inconsistent explicit system) plus the
    // refill pass's own notes.
    std::vector<std::string> notes;
};

// Keep explicit intent, then re-admit auto constraints in declaration order only while
// converged and independent. Refill remaining DOF with soft fully_define constraints.
// An inconsistent explicit system is returned unchanged for normal conflict reporting.
SettleAutoResult settle_auto_constraints(const nlohmann::json& sketch,
                                        const nlohmann::json& env);

}  // namespace chudware::sketch
