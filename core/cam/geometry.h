// core/cam/geometry.h — 2D machining geometry: polygon offset / boolean / pocket
// clearing chains, lead geometry, arc fitting. Clipper2-backed (Boost license),
// OCCT-free.
//
// All inputs/outputs are in millimetres. Internally everything scales to Clipper2's
// 64-bit integer space at kScale counts/mm and back, so callers never see integers.
#pragma once

#include <vector>

#include "cam/model.h"

namespace aicad {
namespace cam {
namespace geo {

// counts per millimetre used for the integer clip space (0.1 micron resolution).
constexpr double kScale = 10000.0;

enum class JoinType { Round, Square, Miter };
enum class ClipOp { Union, Difference, Intersection, Xor };

// ── basic measures ───────────────────────────────────────────────────────────
double signed_area(const Path2&);          // CCW positive
double area(const Path2&);                  // |signed_area|
double perimeter(const Path2&, bool closed = true);
bool is_ccw(const Path2&);
Path2 ensure_ccw(const Path2&);
Path2 ensure_cw(const Path2&);
bool point_in_polygon(const Vec2&, const Path2&);
bool point_in_region(const Vec2&, const Region&);
void bbox(const Path2&, Vec2& lo, Vec2& hi);
void region_bbox(const std::vector<Region>&, Vec2& lo, Vec2& hi);
double region_area(const Region&);

// ── resampling / cleanup ─────────────────────────────────────────────────────
// Split long segments so no edge exceeds max_seg (for raster intersection, sim).
Path2 resample_max_seg(const Path2&, double max_seg);
// Remove (near-)duplicate and collinear points within tol.
Path2 simplify(const Path2&, double tol);
double polyline_length(const Path2&, bool closed);

// ── offset / boolean (Clipper2) ──────────────────────────────────────────────
// Offset closed regions by `delta` (positive grows the solid, negative shrinks).
// Holes are inset/outset consistently. Empty result when the offset collapses.
std::vector<Region> offset_regions(const std::vector<Region>&, double delta,
                                   JoinType jt = JoinType::Round, double miter = 2.0,
                                   double arc_tol = 0.01);
std::vector<Region> offset_region(const Region&, double delta,
                                  JoinType jt = JoinType::Round, double miter = 2.0,
                                  double arc_tol = 0.01);
// Offset OPEN paths into closed ribbons (kerf, engrave width) — returns the boundary.
std::vector<Path2> offset_open(const std::vector<Path2>&, double delta,
                               JoinType jt = JoinType::Round, bool round_caps = true,
                               double arc_tol = 0.01);

// Boolean over region sets. `clips` may be empty for Union of subjects.
std::vector<Region> boolean_op(ClipOp, const std::vector<Region>& subjects,
                               const std::vector<Region>& clips,
                               bool even_odd = false);

// ── pocket / area clearing ───────────────────────────────────────────────────
// Concentric clearing offsets of a region for a tool of `tool_radius`, stepping in
// by `stepover`, leaving `stock_to_leave` radial allowance. Outermost first (for
// climb you machine from inside out or reverse per direction). Each element is the
// set of disjoint loops at that offset level.
std::vector<std::vector<Path2>> pocket_offsets(const Region&, double tool_radius,
                                               double stepover, double stock_to_leave,
                                               JoinType jt = JoinType::Round);

// The machinable region (medial-safe) for a tool: region inset by tool_radius +
// stock_to_leave. Used as the boundary for raster/zigzag clearing.
std::vector<Region> machinable_region(const Region&, double tool_radius,
                                      double stock_to_leave);

// Zig-zag / one-way raster fill of a region's interior at the given line spacing
// and angle (deg). Returns ordered open paths (the cutting strokes, already linked
// where adjacent strokes share the boundary). `bidirectional` true = zigzag.
std::vector<Path2> raster_fill(const std::vector<Region>&, double spacing,
                               double angle_deg, bool bidirectional = true);

// ── lead geometry ────────────────────────────────────────────────────────────
// Build a lead-in path that arrives tangent at `p0` heading in `dir` (unit), on the
// `comp_side`. Returns the points from the lead start to (but not including) p0.
Path2 lead_in_path(const Vec2& p0, const Vec2& dir, const Lead&, CompSide, bool climb);
Path2 lead_out_path(const Vec2& p1, const Vec2& dir, const Lead&, CompSide, bool climb);

// ── arc fitting (for post output / smoother paths) ───────────────────────────
struct ArcSeg {
    bool is_arc = false;
    Vec2 end;       // segment end
    Vec2 center;    // arc centre (when is_arc)
    bool ccw = false;
};
// Fit a polyline to a mix of lines and biarcs within `tol`. Lines stay lines.
std::vector<ArcSeg> fit_arcs(const Path2&, double tol);

}  // namespace geo
}  // namespace cam
}  // namespace aicad
