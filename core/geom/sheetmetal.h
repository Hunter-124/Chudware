// Sheet-metal feature geometry (C++ port of engine/doc/features/sheetmetal.py +
// sheetmetal_flat.py). These build REAL solids — a base sheet, bent walls (edge
// flange), folded edges (hem), two-bend steps (jog), and the unfolded flat blank —
// so the differential parity gate (exact BRep volume) applies directly.
//
// WHY a separate translation unit (mirrors the Python split): the bend math
// (bend allowance / k-factor neutral fibre) and the analytic cross-section walkers
// (_build_flange_solid / _strip_from_centerline) are sheet-metal-specific; keeping
// them out of features.cpp matches the oracle file layout and keeps each builder a
// clean value->shape function.
//
// FRAME CONVENTION (identical to the oracle so the geometry — hence the volume —
// matches EXACTLY):
//   - e_dir      : along the bend line (the prism axis).
//   - length_dir : in the parent broad-face plane, pointing away from the body
//                  across the thickness face (the direction the flat sheet continues).
//   - fold_dir   : perpendicular to the broad face, the side the wall folds toward.
// The cross-section is built in the 2-D (length_dir, fold_dir) plane anchored at
// p_start (a vertex of the picked bend edge) and prismed along e_dir by edge_length.
#pragma once

#include <vector>

#include <Eigen/Dense>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>

namespace geom {

// ---- bend math (engine/doc/features/sheetmetal.py bend_allowance/bend_deduction) ----
double sm_bend_allowance(double angle_deg, double inner_radius, double thickness,
                         double k_factor);
double sm_bend_deduction(double angle_deg, double inner_radius, double thickness,
                         double k_factor);

// ---- bend-edge frame -------------------------------------------------------
// The resolved frame for a picked straight bend edge on a sheet body. Mirrors the
// oracle's _edge_dir_and_endpoints + _classify_bend_edge + the length_dir/fold_dir
// derivation. `ok` is false (with `error` set) when the edge is not a valid
// sheet-metal bend edge (curved, not on a thickness face, etc.).
struct BendFrame {
    bool ok = false;
    std::string error;
    Eigen::Vector3d p_start;      // bend-edge first vertex (cross-section anchor)
    Eigen::Vector3d p_end;        // bend-edge last vertex
    Eigen::Vector3d e_dir;        // along the bend line (prism axis), unit
    Eigen::Vector3d length_dir;   // away from body across the thickness face, unit
    Eigen::Vector3d n_broad;      // outward normal of the parent broad face, unit
    double edge_length = 0.0;
};

// Resolve the bend frame from a sheet `body` + a straight bend `edge` + sheet
// thickness. `flip` is NOT applied here — the caller derives fold_dir = ±n_broad.
BendFrame sm_bend_frame(const TopoDS_Shape& body, const TopoDS_Edge& edge,
                        double thickness);

// ---- edge flange (bend + flat wall) ----------------------------------------
// Build the bend region + flat wall solid (oracle _build_flange_solid), anchored
// at the bend edge, in true geometry. `fold_dir` = n_broad * (flip ? -1 : 1).
// `position` ∈ "material_inside" | "material_outside" | "bend_outside".
TopoDS_Shape sm_edge_flange_solid(const Eigen::Vector3d& p_start,
                                  const Eigen::Vector3d& e_dir,
                                  const Eigen::Vector3d& length_dir,
                                  const Eigen::Vector3d& fold_dir,
                                  double edge_length, double thickness,
                                  double inner_radius, double angle_deg,
                                  double wall_length, const std::string& position);

// ---- constant-thickness strip (hem / jog) ----------------------------------
// One move applied to the centerline walker (oracle _strip_from_centerline). A
// `run` is a straight segment of length `value`; a `bend` is an arc sweeping
// `value` degrees about a centre-radius `r_center` (ccw = left turn).
struct StripOp {
    enum Kind { Run, Bend } kind;
    double value;     // run length (mm) OR bend angle (deg)
    double r_center;  // bend only: centerline radius
    bool ccw;         // bend only
};

// Build a constant-thickness strip walking `ops` from 2-D start (start_x,start_y)
// heading +length_dir, then prism along e_dir (oracle _strip_from_centerline).
TopoDS_Shape sm_strip_from_centerline(const Eigen::Vector3d& p_start,
                                      const Eigen::Vector3d& e_dir,
                                      const Eigen::Vector3d& length_dir,
                                      const Eigen::Vector3d& fold_dir,
                                      double edge_length, double thickness,
                                      const std::vector<StripOp>& ops,
                                      double start_x, double start_y);

// ---- flat pattern (unfold) -------------------------------------------------
// One stored bend record needed to develop the flat blank (the subset of the
// oracle's bend record the flat-pattern math reads).
struct BendRecord {
    double angle = 0.0;
    double radius = 0.0;
    double thickness = 0.0;
    double bend_allowance = 0.0;
    double wall_length = 0.0;
    std::string position;
    Eigen::Vector3d p_start;
    Eigen::Vector3d p_end;       // bend-line far endpoint (edge length = |p_end-p_start|)
    Eigen::Vector3d e_dir;
    Eigen::Vector3d length_dir;
};

// Develop the folded `body` into its flat blank (oracle compile_sm_flat_pattern):
// rebuild the flat base block from the largest broad face, then add one developed
// strip (BA + wall) per bend record, all unioned. Empty shape on failure.
TopoDS_Shape sm_flat_pattern_solid(const TopoDS_Shape& body, double thickness,
                                   const std::vector<BendRecord>& bends);

}  // namespace geom
