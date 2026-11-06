// 3D rigid-transform mate solver.
//
// SolidWorks-style assembly mates solved as a least-squares constraint system on
// the shared C++ numeric core. Each floating component is parameterized by 6
// variables: translation t = (tx,ty,tz) in mm and an absolute rotation vector
// w = (wx,wy,wz) using Rodrigues / exp-map coordinates. Fixed components are
// pinned and contribute no variables: they are a HARD CONSTRAINT, so their
// authored transform is used verbatim in the residuals (never re-derived through
// the exp-map, which cannot represent a measured 3x3 block exactly) and echoed
// back bit-identically.
//
// The residual/Jacobian structure originated in engine/doc/mate_solver.py and
// covers coincident, parallel, perpendicular, concentric, distance, angle,
// tangent, lock, width, symmetric, distance_limit, angle_limit, profile_center,
// gear, rack_pinion, screw, hinge, slot and cam_follower. This implementation
// also contains C++-side correctness fixes (including a true revolute hinge and
// warm-start-relative mechanical coupling), so Python-oracle parity is not a
// correctness requirement.
//
// An ANGLE mate is a signed angle about a reference axis. The axis may be authored on
// the mate — `value: {"value": <deg>, "axis": [x,y,z]}`, in the FIRST ref's component
// local frame (alias `reference_axis`) — and is otherwise derived once per mate from
// the mate's own geometry and seed poses (mate.cpp::angle_reference_local). Author it
// for any mate that sits at, or is driven through, 0 deg / 180 deg: there the
// derivation has no relative rotation to read the joint axis off, and it falls back to
// the geoms' companion vector (a plane's in-plane `direction`, an axis's `normal`).
// The sign of a derived axis is canonical, so a driven sweep measures the same signed
// angle on both sides of zero.
// With an explicit axis AND axis_point (also in A's local frame), the angle mate
// additionally binds a virtual revolute pin to both authored component poses:
// three anchor-coincidence rows and two axis-parallel rows. A prescribed angle
// then leaves zero residual DOF; releasing that drive leaves one mechanism DOF.
// Without axis_point the single angular residual is unchanged.
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
    std::string reason;  // diagnostic for unresolved residuals
};

// Per-mate residual at the solved configuration. `max_abs` is the largest absolute
// entry of the mate's residual block: mm for the positional rows (offset from the
// axis / plane it asks for), dimensionless for the direction rows (oriented
// direction-vector differences or dot products), RADIANS for an angle mate (which is
// a signed angle about its reference axis, not a dot product — see mate.cpp::angle).
// `norm` is the block's 2-norm.
// Reported for every mate that produced residual rows, worst first, so a solve that
// does not converge can NAME the mate that is holding it back instead of leaving the
// caller to bisect a 20-mate network by hand.
struct MateResidual {
    std::string mate_id;
    std::string type;
    double max_abs = 0.0;
    double norm = 0.0;
};

// One first-order rotational freedom of a component at the solved pose.
//
// `axis` is a unit direction (world frame) and `point` is the point of the axis line
// nearest the world origin (mm). `pitch_mm_per_rad` is the translation ALONG the axis that
// accompanies one radian of rotation: 0 for a pure rotation (a pin, a hinge), non-zero
// for a helical freedom (a screw). The sign of `axis` is arbitrary; rotation about -axis
// is the same freedom.
struct FreeRotation {
    Eigen::Vector3d axis = Eigen::Vector3d::UnitZ();
    Eigen::Vector3d point = Eigen::Vector3d::Zero();
    double pitch_mm_per_rad = 0.0;
};

// First-order motion projected from the null space of the accepted-mate Jacobian.
// Freedoms of different components may be coupled: their counts do not generally sum
// to the assembly DOF, and another component may have to move simultaneously.
//
// `translations` spans the projected motions whose angular part is zero. `rotations`
// describes a basis of projected rotational motion (see FreeRotation), with its translation
// part orthogonal to the pure translation subspace. Both use world coordinates.
// `free_dof` = translations.size() + rotations.size().
//
// `grounded` is the document's `fixed` flag. `mated` is true when the component takes part
// in at least one mate that the solver built (an unresolved or suppressed mate does not
// count); a component that is not grounded and not mated is a floating body.
// `kind` is a short name derived from the motion space: "grounded", "fixed" (all mates
// leave no freedom), "prismatic", "revolute", "cylindrical", "helical", "planar",
// "spherical", "free" (6 DOF), or "other".
struct ComponentFreeMotion {
    std::string component;
    bool grounded = false;
    bool mated = false;
    int free_dof = 0;
    std::vector<Eigen::Vector3d> translations;
    std::vector<FreeRotation> rotations;
    std::string kind;
};

// A set of components coupled to each other (and possibly to ground) through mates.
// `dof` is the group's own contribution to MateSolveResult::dof. A group with no grounded
// component can move rigidly through space, which always contributes 6 of its `dof`;
// `relative_dof` removes those 6 so it counts only motion between the group's components.
struct MateGroup {
    std::vector<std::string> components;
    std::vector<std::string> mates;
    bool grounded = false;
    int dof = 0;
    int relative_dof = 0;
};

struct MateSolveResult {
    std::map<std::string, std::vector<double>> transforms;  // id -> 16-float row-major
    std::map<std::string, std::pair<Eigen::Vector3d, Eigen::Vector3d>> params;  // id -> (t,w)
    std::vector<MateStatus> mate_status;
    std::vector<MateResidual> residuals;  // worst first; only mates with residual rows
    std::string worst_mate;               // residuals.front().mate_id, or "" when empty
    int dof = 0;
    bool converged = false;
    double max_residual = 0.0;
    // The convergence threshold actually applied: `tol` widened for assembly size
    // (see solve_mates). Reported so a caller can say how close "not converged" was.
    double tolerance = 0.0;
    bool over_defined = false;
    std::string summary;
    // Filled only when solve_mates is called with free_motion = true: one entry per
    // non-suppressed component (document order) and one per mate-connected group of two or
    // more components. See ComponentFreeMotion / MateGroup.
    std::vector<ComponentFreeMotion> free_motion;
    std::vector<MateGroup> groups;
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
//
// `tol` is quoted for a ~100 mm part. The solve measures the assembly's own size L
// (largest world coordinate any seed pose or mate geometry reaches) and applies
// `tol * max(1, L/100)` — an absolute picometre threshold is not reachable across a
// 2 m rover, where a single residual evaluation already carries ~1e-12 mm of
// cancellation noise. The widening is CAPPED at 1e-6 mm: the positional rows are
// lengths in mm, so no assembly size may make a nanometre of misplacement count as a
// seated mate. `MateSolveResult::tolerance` reports what was applied; nothing changes
// for L <= 100 mm.
//
// The LM step is bounded by a trust region derived from the same L (translation) and
// half a radian (rotation), and the random-restart escape used when the first solve
// fails is scoped to the mate-connected group that actually failed and is accepted
// only when it genuinely rescues the solve. Both exist because the least-squares step
// on the normal equations can amplify a near-null direction without bound: before
// them, ONE unsatisfiable mate on one corner of an assembly threw components on
// unrelated, fully-satisfied corners hundreds to thousands of mm out of place.
//
// `free_motion` additionally extracts per-component first-order free motion from the
// Jacobian null space (MateSolveResult::free_motion / groups). It costs one more SVD per
// mate-connected group, so the motion-sweep callers leave it off.
MateSolveResult solve_mates(const nlohmann::json& components,
                            const nlohmann::json& mates,
                            const ResolvedGeometry& resolved_geometry,
                            const std::optional<WarmMap>& warm = std::nullopt,
                            const std::optional<Drag>& drag = std::nullopt,
                            double tol = 1e-9, int max_iter = 80,
                            std::optional<double> time_budget_ms = std::nullopt,
                            bool free_motion = false);

// --- rotation helpers (exposed for the CLI / tests; match the oracle exactly) ---
Eigen::Vector3d unit(const Eigen::Vector3d& v);
Eigen::Vector3d canonical_sign(const Eigen::Vector3d& v);
Eigen::Matrix3d rodrigues(const Eigen::Vector3d& w);
Eigen::Vector3d mat_to_rotvec(const Eigen::Matrix3d& R);

// JSON-in/JSON-out convenience for the differential harness. `input` is a JSON
// object { "components":[...], "mates":[...], "resolved_geometry":[...],
// "warm":{...optional}, "drag":{...optional}, "free_motion":bool optional } where
// resolved_geometry is a list of { "mate_id":.., "ref_index":.., "geom":{kind,point,
// normal,direction,radius} }. Returns the result as a JSON object (see mate_cli.cpp for the
// schema); with free_motion set it also carries `free_motion` (free_motion_to_json).
nlohmann::json solve_mates_json(const nlohmann::json& input);

// JSON form of MateSolveResult::free_motion / groups:
//   {components:[{component, grounded, mated, free_dof, kind,
//                 translations:[[x,y,z]...], rotations:[{axis, point, pitch_mm_per_rad}]}],
//    groups:[{components, mates, grounded, dof, relative_dof}]}
// Vectors are world-frame; points and pitches are mm and mm/rad.
nlohmann::json free_motion_to_json(const MateSolveResult& result);

}  // namespace chudware::mate
