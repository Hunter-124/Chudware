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

// ---- T-W2 sketch completion suite ------------------------------------------
//
// Free-variable narration and endpoint enumeration reused by the doc-layer
// doc_sketch_health / doc_sketch_auto_constrain RPCs. Both are read-only scans over
// a solved sketch — they reuse the SAME solver internals (allocate_entity variable
// layout + the compute_statuses SVD null-space projection) so the narration lines up
// exactly with the per-entity under/full/over statuses SketchSolution already reports.

// One free (unconstrained) degree of freedom, named in plain words for the advisor.
// `entity` is the entity id; `param` is the semantic name of the loose parameter
// ("radius", "center x", "end x", "p2 y", "point 3 x"); `var` is the global solver
// variable index (for cross-referencing). Produced by free_variables().
struct FreeVar {
    std::string entity;
    std::string param;
    int var = 0;
};

// Named free variables of `sketch` (the entities/constraints dict form solve_sketch
// takes) under `env`. Solves cold, runs the SVD null-space projection compute_statuses
// uses, and maps every loose variable index back to a semantic ("circle C1 radius;
// line L4 end x/y") via a per-entity-type table mirroring allocate_entity's layout.
// The list is stable-ordered by entity declaration order then local variable index.
std::vector<FreeVar> free_variables(const nlohmann::json& sketch, const nlohmann::json& env);

// One endpoint of a sketch entity: its world (u,v) plus the handle string that
// addresses it in a constraint ref ("L4.p2", "A1.start", "C1.center", "P3"). Produced
// by entity_endpoints() over SOLVED coords. Endpoints are the connection points a
// coincident constraint can pin; interior/derived points (line mid) are excluded.
struct Endpoint {
    std::string handle;
    double x = 0.0;
    double y = 0.0;
};

// All coincidence-eligible endpoints of every non-construction entity in the SOLVED
// `coords` dict (the SketchSolution::entities id->coord form). Lines contribute p1/p2,
// arcs their center + start/end rim points, slots p1/p2, splines their endpoint knots,
// points themselves, circles their center. Used by the auto-coincident proximity scan.
std::vector<Endpoint> entity_endpoints(const nlohmann::json& coords);

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
