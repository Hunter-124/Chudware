// 3D free-space sketch constraint solver (feature type "sketch3d").
//
// Parameterizes 3D sketch entities into the shared chudware::solver::System variable
// vector (core/solver/nsolve.h — the same residual/Jacobian framework the 2D
// sketch solver and the mate solver ride) and wires each schema constraint to one
// or more residual blocks. dict-in / dict-out via nlohmann::json, mirroring
// solve_sketch's shape so tooling treats 2D and 3D sketches uniformly.
//
// ENTITIES (world mm; every coordinate is a solver variable):
//   point  {p:[x,y,z]}                                       3 vars
//   line   {p1:[x,y,z], p2:[x,y,z]}                          6 vars
//   arc    {p1:[x,y,z], pm:[x,y,z], p2:[x,y,z]}              9 vars (THREE-POINT:
//          start / interior "through" point / end. Chosen over center+normal+
//          radius+angles because it is a uniform point parameterization — no
//          unit-normal normalization constraint in the solve, natural endpoint
//          handles for chaining/coincident, and GUI drags map 1:1 to points.
//          Collinear p1/pm/p2 is degenerate and fails edge compile downstream.)
//   spline {points:[[x,y,z],…]}                              3·N vars (cubic
//          interpolation through every point, like the 2D `spline`)
// All entities carry construction:bool (construction geometry never joins wires).
//
// HANDLES: "e3" whole entity; "e3.p" (point); "e3.p1"/"e3.p2" (line + arc
// endpoints); "e3.pm" (arc through-point); "e3.p0"…"e3.pN" (spline points).
//
// CONSTRAINTS: coincident, fix, distance, length, parallel, perpendicular, angle,
// along_axis, point_on (line / arc circumcircle / interpolating spline), tangent
// (line-arc, arc-arc, line-spline endpoint), equal (line lengths / arc radii),
// midpoint (line / arc mid-sweep), collinear, concentric, coradial, on_plane,
// parallel_plane, normal_plane and symmetric (two points mirrored about a plane).
// Plane = front (XZ, -Y), top (XY, +Z), right (YZ, +X), or {origin,normal}.
// Unsupported or unresolvable constraints are reported in Sketch3DSolution::skipped.
#pragma once

#include <nlohmann/json.hpp>
#include <Eigen/Dense>

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace chudware::sketch3d {

// Opaque warm-start state (mirror of chudware::sketch::SolveState).
struct SolveState3 {
    Eigen::VectorXd x;                                 // last solved variable vector
    std::map<std::string, std::vector<int>> var_map;   // entity id -> variable indices
    int n_vars = 0;
    std::map<std::string, std::string> ent_sig;        // id -> entity JSON signature
};

// Solution shape mirrors 2D SketchSolution so sketch_health-style consumers and
// tests read both the same way.
struct Sketch3DSolution {
    nlohmann::json entities;  // entity id -> solved param dict (with "type")
    std::map<std::string, std::string> statuses;  // id -> "under"|"full"|"over"
    int dof = 0;
    std::vector<std::string> redundant;    // constraint ids
    std::vector<std::string> conflicting;  // constraint ids
    std::vector<std::pair<std::string, std::string>> skipped; // (constraint id, reason)
    bool converged = false;
    SolveState3 state;  // warm-start state for the next solve / drag
};

// Solve a 3D sketch. `sketch` has "entities" and "constraints" arrays (doc_schema
// §3B). `env` maps expression variable names to numeric values for dimension
// expressions; pass an empty object when there are none. `warm` (optional) is a
// prior SolveState3 for per-entity warm-starting, gated on the entity's JSON
// signature exactly like the 2D solver.
Sketch3DSolution solve_sketch3d(const nlohmann::json& sketch, const nlohmann::json& env);
Sketch3DSolution solve_sketch3d(const nlohmann::json& sketch, const nlohmann::json& env,
                                const std::optional<SolveState3>& warm);

// One free (unconstrained) degree of freedom, named in plain words for the GUI /
// advisor ("p1 x", "point 2 z"). Mirrors chudware::sketch::FreeVar.
struct FreeVar3 {
    std::string entity;
    std::string param;
    int var = 0;
};

// Named free variables of `sketch` under `env` (cold solve + SVD null-space
// projection, same machinery as the per-entity statuses).
std::vector<FreeVar3> free_variables3(const nlohmann::json& sketch,
                                      const nlohmann::json& env);

// Constrained drag — 3D mirror of chudware::sketch::drag. Rebuilds the system from
// `sketch`, soft-targets the dragged handle at weight 0.01, warm-starts from
// state.x when the topology matches, and solves with a 10 ms time budget.
// Returns {ok, entities, statuses, converged, budget_exceeded, rejected_handles}.
// Single-point drag passes `handle` + `target_xyz`; multi-target drag passes
// `targets` (a list of {"handle":…, "xyz":[x,y,z]}), in which case handle/
// target_xyz are ignored.
nlohmann::json drag3(const SolveState3& state, const std::string& handle,
                     const Eigen::Vector3d& target_xyz,
                     const nlohmann::json& sketch, const nlohmann::json& env,
                     const nlohmann::json& targets = nlohmann::json());

}  // namespace chudware::sketch3d
