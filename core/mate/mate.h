// 3D rigid-transform mate solver.
//
// SolidWorks-style assembly mates solved as a least-squares constraint system on
// the shared C++ numeric core. Each floating component is parameterized by 6
// variables: translation t = (tx,ty,tz) in mm and an absolute rotation vector
// w = (wx,wy,wz) using Rodrigues / exp-map coordinates. Fixed components are
// pinned and contribute no variables.
//
// The residual/Jacobian structure originated in engine/doc/mate_solver.py and
// covers coincident, parallel, perpendicular, concentric, distance, angle,
// tangent, lock, width, symmetric, distance_limit, angle_limit, profile_center,
// gear, rack_pinion, screw, hinge, slot and cam_follower. This implementation
// also contains C++-side correctness fixes (including a true revolute hinge and
// warm-start-relative mechanical coupling), so Python-oracle parity is not a
// correctness requirement.
//
// Dict-in/dict-out via nlohmann::json keeps the document and harness seams
// simple. OCCT-free (only solver/Eigen/json).
#pragma once

#include <nlohmann/json.hpp>
#include <Eigen/Dense>

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace chudware::mate {

// Local-frame geometry a single mate ref resolves to. kind in
// {plane, axis, point, sphere}. Fields are in the owning component's local frame.
struct MateGeom {
    std::string kind = "point";
    Eigen::Vector3d point = Eigen::Vector3d::Zero();
    Eigen::Vector3d normal = Eigen::Vector3d(0.0, 0.0, 1.0);
    Eigen::Vector3d direction = Eigen::Vector3d(0.0, 0.0, 1.0);
    double radius = 0.0;

    // The orientation vector this geom contributes (plane normal or axis dir).
    Eigen::Vector3d dir_vector() const {
        return kind == "plane" ? normal : direction;
    }
};

// Per-mate status. status in {ok, redundant, conflicting, suppressed, unresolved}.
struct MateStatus {
    std::string mate_id;
    std::string type;
    std::string status;
};

struct MateSolveResult {
    std::map<std::string, std::vector<double>> transforms;  // id -> 16-float row-major
    std::map<std::string, std::pair<Eigen::Vector3d, Eigen::Vector3d>> params;  // id -> (t,w)
    std::vector<MateStatus> mate_status;
    int dof = 0;
    bool converged = false;
    double max_residual = 0.0;
    bool over_defined = false;
    std::string summary;
};

// Optional warm start: component_id -> (t, w).
using WarmMap = std::map<std::string, std::pair<Eigen::Vector3d, Eigen::Vector3d>>;

// Optional drag: pull a component's translation toward a world target along the
// free DOF (constrained drag).
struct Drag {
    std::string component;
    Eigen::Vector3d target = Eigen::Vector3d::Zero();
};

// Resolved geometry key: (mate_id, ref_index).
using ResolvedGeometry = std::map<std::pair<std::string, int>, MateGeom>;

// Public solve. `components` and `mates` are doc-schema dicts; `resolved_geometry`
// maps (mate_id, ref_index) -> MateGeom in the owning component's local frame.
MateSolveResult solve_mates(const nlohmann::json& components,
                            const nlohmann::json& mates,
                            const ResolvedGeometry& resolved_geometry,
                            const std::optional<WarmMap>& warm = std::nullopt,
                            const std::optional<Drag>& drag = std::nullopt,
                            double tol = 1e-9, int max_iter = 80,
                            std::optional<double> time_budget_ms = std::nullopt);

// --- rotation helpers (exposed for the CLI / tests; match the oracle exactly) ---
Eigen::Vector3d unit(const Eigen::Vector3d& v);
Eigen::Matrix3d rodrigues(const Eigen::Vector3d& w);
Eigen::Vector3d mat_to_rotvec(const Eigen::Matrix3d& R);

// JSON-in/JSON-out convenience for the differential harness. `input` is a JSON
// object { "components":[...], "mates":[...], "resolved_geometry":[...],
// "warm":{...optional}, "drag":{...optional} } where resolved_geometry is a list
// of { "mate_id":.., "ref_index":.., "geom":{kind,point,normal,direction,radius} }.
// Returns the result as a JSON object (see mate_cli.cpp for the schema).
nlohmann::json solve_mates_json(const nlohmann::json& input);

}  // namespace chudware::mate
