// Weldment feature geometry — C++ port of engine/doc/features/weldment.py
// (+ engine/doc/weld_profiles.py). These build REAL solids so the differential
// parity gate (exact BRep volume) applies directly:
//   * weld_member  — sweep a structural PROFILE perpendicular to a sketch segment
//                    (BRepOffsetAPI_MakePipe), one SEPARATE body per segment.
//   * weld_end_cap — a plate closing the open END of a member (outer/inner).
//   * weld_gusset  — a triangular/polygonal reinforcing plate welded into an
//                    L-corner between two members.
//
// PARITY DISCIPLINE: the profile cross-sections (hollow tubes subtract the bore,
// the L-angle is a true L, the C-channel an open C) and the plate outlines mirror
// the oracle EXACTLY so the swept / extruded volume matches to the 1e-3 gate.
//
// WHY a separate translation unit (mirrors the Python split): the profile library
// (build_profile_face) and the dress-plate builders are weldment-specific; keeping
// them out of features.cpp matches the oracle file layout.
#pragma once

#include <string>
#include <vector>

#include <Eigen/Dense>
#include <nlohmann/json.hpp>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

namespace geom {

// ---- structural-profile library (engine/doc/weld_profiles.py) --------------
// Build the profile cross-section as a planar TopoDS_Face in its LOCAL XY frame
// (centred on the origin, normal +Z), with the SAME outline + bore as the oracle.
// `params` carries the per-profile dimension keys (mm) as JSON numbers; any
// missing key falls back to the library default. `angle` (deg) rotates the
// section about +Z; `justify` ∈ "center" | "corner" shifts it. Returns a null
// face (and sets `error`) on an unknown profile or degenerate dimensions, and
// writes the analytic cross-section area to `area_out`.
TopoDS_Face weld_profile_face(const std::string& profile, const nlohmann::json& params,
                              const std::string& justify, double angle,
                              double& area_out, std::string& error,
                              double* env_w_out = nullptr, double* env_h_out = nullptr);

// True if `name` is a known library profile.
bool weld_profile_known(const std::string& name);

// Resolve a profile's dimension dict the way the oracle's ProfileSpec.resolve does:
// start from the per-profile defaults (mm), then override each KNOWN key with the
// supplied value (cast to a double) when present and non-null. Unknown keys in
// `params` are ignored. Returns a JSON object {key: number} in the profile's
// declared key order (matching weld_profiles.PROFILES[name].params). An unknown
// profile yields an empty object. This is the `dims` the cut list groups + describes
// members by, so it must match the oracle's resolved dims EXACTLY.
nlohmann::json weld_profile_dims(const std::string& profile, const nlohmann::json& params);

// ---- member sweep (weldment.py _sweep_member) ------------------------------
// Orient `face_local` (drawn in its own XY plane) perpendicular to the path at
// `start` with the path tangent as the section normal, then sweep it along
// `path_edge` (BRepOffsetAPI_MakePipe). Returns a null shape on failure.
TopoDS_Shape weld_sweep_member(const TopoDS_Face& face_local,
                               const TopoDS_Edge& path_edge,
                               const Eigen::Vector3d& start,
                               const Eigen::Vector3d& tangent);

// ---- corner trim (weldment.py _mitre_trim / _butt_cope_trim) ---------------
// One structural member as the trim sees it: the swept solid plus the straight
// axis geometry the oracle stores on each member dict.
struct WeldMember {
    TopoDS_Shape solid;        // mutated in place by the trim
    Eigen::Vector3d start;     // world-space segment start
    Eigen::Vector3d end;       // world-space segment end
    Eigen::Vector3d t_start;   // unit tangent at start (into the member)
    Eigen::Vector3d t_end;     // unit tangent at end
    double length = 0.0;
    bool straight = true;      // a line member (only lines are trimmed)
    double env_w = 0.0;        // section bounding-box envelope (w, h)
    double env_h = 0.0;
};

// Trim members that meet at a joint, mutating each member's `solid` in place.
// `joint` ∈ "miter" | "butt" | "cope" (mirrors the oracle's three Trim/Extend
// modes); `reach` sizes the half-space cut box (oracle uses max_len*4). Best
// effort: a kernel hiccup leaves the offending member untouched, exactly like
// the oracle's contained try/except. No-op for <2 members.
void weld_corner_trim(std::vector<WeldMember>& members, double reach,
                      const std::string& joint);

// ---- gusset plate (weldment.py weld_gusset) --------------------------------
// Closest point between two infinite lines L1=(p1+s*d1), L2=(p2+t*d2) — the
// corner apex (midpoint of the shortest connecting segment). Falls back to p1 on
// a parallel singularity. Exposed so document.cpp replicates the apex exactly.
Eigen::Vector3d weld_closest_point_between_lines(const Eigen::Vector3d& p1,
                                                 const Eigen::Vector3d& d1,
                                                 const Eigen::Vector3d& p2,
                                                 const Eigen::Vector3d& d2);

// Longest straight chord direction among a face's edges (weldment.py
// _longest_edge_dir): recovers a swept member's axis from a picked side/top face.
// Writes the chord length to `len_out`; returns +X (len 0) when no edge qualifies.
Eigen::Vector3d weld_longest_edge_dir(const TopoDS_Face& f, double& len_out);

// Extrude the closed polygon `corners` by `thickness` along `normal`, centred on
// the corners' plane then shifted by `offset` along `normal` (weldment.py
// _make_solid_plate). Returns a null shape on a degenerate polygon/extrude.
TopoDS_Shape weld_make_solid_plate(const std::vector<Eigen::Vector3d>& corners,
                                   const Eigen::Vector3d& normal, double thickness,
                                   double offset);

// ---- end cap (weldment.py weld_end_cap) ------------------------------------
// Build the end-cap plate from the member END face `end_face` whose outward
// normal is `n` (= the member axis). `cap_type` ∈ "outer" | "inner":
//   outer — plate from the face's OUTER boundary (covers the whole end incl. the
//           bore), grown by `overhang`, mounted on the end + `offset`·n, extruded
//           outward by `thickness`.
//   inner — plate matching the bore (the face's inner ring), inset into the tube.
// Returns a null shape (sets `error`) on failure / a solid section asked for an
// inner cap.
TopoDS_Shape weld_end_cap_plate(const TopoDS_Face& end_face, const Eigen::Vector3d& n,
                                const std::string& cap_type, double thickness,
                                double offset, double overhang, std::string& error);

}  // namespace geom
