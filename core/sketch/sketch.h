// Sketch constraint solver — C++ port of engine/doc/sketch_solver.py (core subset).
//
// Parameterizes sketch entities into the nsolve variable vector and wires each
// schema constraint to one (or more) residual blocks with analytic Jacobians.
// dict-in / dict-out via nlohmann::json, mirroring the Python solve_sketch.
//
// Coverage: entities point/line/circle/arc/ellipse/ellipse_arc/parabola/conic/
// slot/arc_slot/spline/style_spline/text (allocation + handle resolution +
// extraction; conic-family derived handles carry their chain-rule Jacobians).
// Constraints coincident, horizontal, vertical, parallel, perpendicular,
// collinear, coradial, concentric, equal (line/slot length + circle/arc radius),
// midpoint, symmetric, angle, point_on(line/circle/arc + numeric closest-point
// projection on ellipse_arc/parabola/conic), fix (whole-entity + sub-handle +
// conic-family handle pin), tangent(line-circle + circle-circle + line-conic
// [ellipse_arc exact support / parabola+conic numeric] + line-spline + general
// curve-curve via mutual-closest signed gap), distance (point-point aligned +
// axis h/v, point-line, and the C50 min/max rim cond with radius as a variable),
// radius, diameter (incl. slot width mapping). Polar (r, theta) placement = angle
// + distance-to-origin. The numeric closest-point projections (conic/spline
// point_on + tangent) match the Python scan resolutions / Newton iteration counts
// / finite-difference steps verbatim for differential-harness parity. Line-line
// tangent stays deferred (ill-defined). Unknown entities/constraints are skipped
// forward-tolerantly, exactly like the Python.
#pragma once

#include <nlohmann/json.hpp>
#include <Eigen/Dense>

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace aicad::sketch {

// Opaque warm-start state — C++ port of sketch_solver.py's SolveState. Returned by
// solve_sketch (inside SketchSolution::state) and accepted by both solve_sketch (warm)
// and drag. Carries the last solved vector, the per-entity variable layout, and a
// per-entity doc-geometry signature used to gate warm-starting an UNCHANGED entity
// while cold-starting one whose params were rewritten under the same id.
struct SolveState {
    Eigen::VectorXd x;                                 // last solved variable vector
    std::map<std::string, std::vector<int>> var_map;   // entity id -> variable indices
    int n_vars = 0;                                    // total variable count
    // id -> json.dumps(entity, sort_keys=True) signature at the time this state was
    // built. Empty == legacy whole-vector warm behavior (states without signatures).
    std::map<std::string, std::string> ent_sig;
};

struct SketchSolution {
    nlohmann::json entities;  // entity id -> solved param dict
    std::map<std::string, std::string> statuses;  // id -> "under"|"full"|"over"
    int dof = 0;
    std::vector<std::string> redundant;    // constraint ids
    std::vector<std::string> conflicting;  // constraint ids
    bool converged = false;
    Eigen::VectorXd x;  // solved variable vector (warm-start state)
    std::map<std::string, std::vector<int>> var_map;  // id -> variable indices
    SolveState state;   // opaque warm-start state (mirror of SketchSolution.state)
};

// Solve a sketch. `sketch` has "entities" and "constraints" arrays (doc_schema
// §3). `env` maps expression variable names to numeric values (for expression
// dims); pass an empty object when there are none. `warm` (optional) is a prior
// SolveState for per-entity warm-starting; omit it (or pass nullopt) for a cold solve.
SketchSolution solve_sketch(const nlohmann::json& sketch, const nlohmann::json& env);
SketchSolution solve_sketch(const nlohmann::json& sketch, const nlohmann::json& env,
                            const std::optional<SolveState>& warm);

// Current solved measurement for a DIMENSIONAL constraint, in canonical units —
// C++ port of sketch_solver.py's measure_constraint. distance -> mm (point-point
// Euclidean / axis per-coordinate rim / point-line), angle -> degrees in [0, 180),
// radius/diameter -> mm. Non-dimensional relations and unresolvable refs return
// nullopt. `entity_coords` is a solved-entity dict (the SketchSolution::entities form).
std::optional<double> measure_constraint(const nlohmann::json& constraint,
                                         const nlohmann::json& entity_coords);

// Per-entity warm-start gating decision for `sketch` against a prior `warm` state:
// "warm" (this entity warm-started from state), "cold" (signature/var-count changed,
// or no warm entry — cold-started). Mirrors the exact gate in solve_sketch's warm
// path; exposed for the warm differential harness to compare the DECISION across
// engines. Entity order follows `sketch`'s entity list.
std::map<std::string, std::string> warm_gate_decision(const nlohmann::json& sketch,
                                                      const SolveState& warm);

// Constrained drag — C++ port of sketch_solver.py's drag. Rebuilds the system from
// `sketch`, soft-targets the dragged handle(s) at weight 0.01, warm-starts from
// state.x when the topology matches, and solves with a 10 ms time budget. Returns a
// JSON object {ok, entities, statuses, converged, budget_exceeded, rejected_handles}.
// Single-point drag passes `handle`+`target_xy`; multi-target drag passes `targets`
// (a list of {"handle":..,"xy":[x,y]}), in which case handle/target_xy are ignored.
nlohmann::json drag(const SolveState& state, const std::string& handle,
                    const std::pair<double, double>& target_xy,
                    const nlohmann::json& sketch, const nlohmann::json& env,
                    const nlohmann::json& targets = nlohmann::json());

}  // namespace aicad::sketch
