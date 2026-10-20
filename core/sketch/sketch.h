// Sketch constraint solver — C++ port of engine/doc/sketch_solver.py (core subset).
//
// Parameterizes sketch entities into the nsolve variable vector and wires each
// schema constraint to one (or more) residual blocks with analytic Jacobians.
// dict-in / dict-out via nlohmann::json, mirroring the Python solve_sketch.
//
// Coverage: 2D geometric, dimensional, symmetry, intersection, curve-length,
// endpoint G1/G2 (including coincident tangent junctions), and external-geometry
// relations. Analytic Jacobians cover normalized line and elementary geometry;
// conic/spline projections and endpoint curvature use numeric derivatives.
// origin and axis.h/axis.v are fixed virtual geometry. Unsupported constraints
// are reported in skipped.
#pragma once

#include <nlohmann/json.hpp>
#include <Eigen/Dense>

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace chudware::sketch {

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
    std::vector<std::pair<std::string, std::string>> skipped;  // constraint id, reason
    bool converged = false;
    Eigen::VectorXd x;  // solved variable vector (warm-start state)
    std::map<std::string, std::vector<int>> var_map;  // id -> variable indices
    SolveState state;   // opaque warm-start state (mirror of SketchSolution.state)
};

// Solve a sketch. `sketch` has "entities" and "constraints" arrays (doc_schema
// §3). `env` maps expression variable names to numeric values (for expression
// dims); pass an empty object when there are none. `warm` (optional) is a prior
// SolveState for per-entity warm-starting; omit it (or pass nullopt) for a cold solve.
// `time_budget_ms` (optional) caps the solve wall-clock (nsolve returns best-so-far,
// possibly with converged=false, once it elapses) — ONLY for interactive edit paths
// that run under the global doc lock (the apply_ops auto-coincident pass); parity /
// oracle-facing callers (check_sketch, the compile pipeline) leave it unset so their
// convergence behavior is unchanged.
// `interactive` (T-PERF8) enables LDLT-first LM steps, in-trial deadline checks,
// single-QR redundancy attribution, and spline-tangency (p,q) warm-start. Default
// false keeps the exact Python-oracle parity path byte-identical.
SketchSolution solve_sketch(const nlohmann::json& sketch, const nlohmann::json& env);
SketchSolution solve_sketch(const nlohmann::json& sketch, const nlohmann::json& env,
                            const std::optional<SolveState>& warm,
                            std::optional<double> time_budget_ms = std::nullopt,
                            bool interactive = false);

// The reason a solve did not converge, as one agent-facing error string that starts
// with "sketch did not converge". Names the conflicting constraints (sol.conflicting,
// with their types). If coincident constraints join points that `fix` constraints hold,
// it also gives the gap between the AUTHORED points, because a fix pins the authored
// coordinates and not the solved position. `sketch` is the input given to solve_sketch.
std::string nonconvergence_reason(const nlohmann::json& sketch, const SketchSolution& sol);

// Write the solved coordinates of every entity into `sketch["entities"]` (the authored
// seeds), with the cold exact solve the compile path uses. The solved geometry does not
// change; only the seeds move to it. Keys are overwritten only where the authored entity
// already has the key with the same shape; id/type/construction/ccw stay. Returns false,
// with `sketch` unchanged, when the sketch does not converge. The sketch_add_constraint
// op calls it before it adds a `fix`, so the fix holds the shape the readback shows.
bool adopt_solved_coordinates(nlohmann::json& sketch, const nlohmann::json& env);

// Current solved measurement for a DIMENSIONAL constraint, in canonical units —
// C++ port of sketch_solver.py's measure_constraint. distance -> mm (point-point
// Euclidean / axis per-coordinate rim / point-line / line-line), angle -> degrees
// in [0, 180), radius/diameter/arc_length -> mm. Non-dimensional relations and
// unresolvable refs return nullopt. `entity_coords` is a solved-entity dict.
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
// Same, reusing `solved_x` — a prior solve_sketch's SketchSolution::x over the SAME
// sketch/env — instead of re-running the cold solve (the expensive step; that cold solve
// and solve_sketch's build the identical deterministic system, so the vectors match).
// Falls back to a fresh cold solve when the vector's size doesn't fit the rebuilt system.
std::vector<FreeVar> free_variables(const nlohmann::json& sketch, const nlohmann::json& env,
                                    const Eigen::VectorXd& solved_x);

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

// Opaque per-drag solve context (defined in sketch.cpp). Caches everything drag()
// otherwise rebuilds EVERY mouse-move frame: the parsed entity list, the entity ->
// variable layout, and the built solver System with all hard-constraint residual
// blocks registered. A host serving an interactive drag passes the SAME shared_ptr
// slot on every frame: the first call fills it, later calls reuse it (clearing only
// the previous frame's soft targets before registering this frame's, so the block
// set is identical to a fresh build). The context is only valid for the exact
// sketch/env it was built from — reset the pointer on ANY document change.
struct DragContext;

// Constrained drag — C++ port of sketch_solver.py's drag. Rebuilds the system from
// `sketch`, soft-targets the dragged handle(s) at weight 0.01, warm-starts from
// state.x when the topology matches, and solves with a 10 ms time budget. Returns a
// JSON object {ok, entities, statuses, converged, budget_exceeded, rejected_handles}.
// Single-point drag passes `handle`+`target_xy`; multi-target drag passes `targets`
// (a list of {"handle":..,"xy":[x,y]}), in which case handle/target_xy are ignored.
//
// `ctx_io` (optional) enables the cached per-frame fast path. When non-null, the
// System/var layout is built once into *ctx_io (first frame) and reused on later
// frames, and the per-frame DIAGNOSTICS — analyze(), real_jacobian(), and the
// per-entity compute_statuses SVD — are SKIPPED: the returned "statuses" is an empty
// object and the caller is expected to serve its drag-start snapshot, then run full
// diagnostics once on commit. Solved ENTITY POSITIONS are identical with or without
// a context (same residual blocks, same warm start, same 10 ms budget). When ctx_io
// is null, behavior is exactly the historical per-call rebuild + full diagnostics.
nlohmann::json drag(const SolveState& state, const std::string& handle,
                    const std::pair<double, double>& target_xy,
                    const nlohmann::json& sketch, const nlohmann::json& env,
                    const nlohmann::json& targets = nlohmann::json(),
                    std::shared_ptr<DragContext>* ctx_io = nullptr);

}  // namespace chudware::sketch
