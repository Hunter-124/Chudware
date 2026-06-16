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
// midpoint, symmetric, angle, point_on(line/circle/arc), fix (whole-entity +
// sub-handle + conic-family handle pin), tangent(line-circle + circle-circle),
// distance (point-point aligned + axis h/v, point-line, and the C50 min/max rim
// cond with radius as a variable), radius, diameter (incl. slot width mapping).
// Polar (r, theta) placement = angle + distance-to-origin. DEFERRED (numeric
// closest-point projection): point_on/tangent on conics + splines. Unknown
// entities/constraints are skipped forward-tolerantly, exactly like the Python.
#pragma once

#include <nlohmann/json.hpp>
#include <Eigen/Dense>

#include <map>
#include <string>
#include <vector>

namespace aicad::sketch {

struct SketchSolution {
    nlohmann::json entities;  // entity id -> solved param dict
    std::map<std::string, std::string> statuses;  // id -> "under"|"full"|"over"
    int dof = 0;
    std::vector<std::string> redundant;    // constraint ids
    std::vector<std::string> conflicting;  // constraint ids
    bool converged = false;
    Eigen::VectorXd x;  // solved variable vector (warm-start state)
    std::map<std::string, std::vector<int>> var_map;  // id -> variable indices
};

// Solve a sketch. `sketch` has "entities" and "constraints" arrays (doc_schema
// §3). `env` maps expression variable names to numeric values (for expression
// dims); pass an empty object when there are none.
SketchSolution solve_sketch(const nlohmann::json& sketch, const nlohmann::json& env);

}  // namespace aicad::sketch
