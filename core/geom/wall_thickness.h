// Inscribed-sphere ("rolling ball") wall thickness on a triangulated solid boundary.
//
// Normal rays read the wrong dimension wherever the thin direction is not the surface
// normal (a rib's top face reads its height, a neck between two lobes reads the lobes)
// and collapse to zero at every convex edge. Printability tools measure the largest
// sphere that fits inside the material instead; this is that measurement:
//
//  * Each sample p (outward unit normal n) grows the largest ball tangent at p, centre
//    on -n, that holds no OPPOSING surface sample strictly inside. A sample q opposes p
//    only when n.n_q <= cos(wall_separation_deg): p's own face and the faces meeting it
//    at a convex edge never pinch the ball, so square edges read the wall behind them,
//    not zero. The ball diameter is the local wall thickness.
//  * Coverage: every sample then takes the smallest diameter among the balls that
//    contain it, so the top of a thin rib or the end of a thin pin reads the rib/pin
//    width that its side balls measured, not the long dimension its own ball found.
//
// Pure geometry (no OCCT): callers hand in samples; core/doc/session.cpp feeds B-rep
// tessellations, the mesh_wall_thickness RPC feeds raw meshes.
#pragma once

#include <array>
#include <cstdint>
#include <vector>

namespace chudware::geom {

// A triangulated surface with outward unit vertex normals. `tri_group` tags each triangle
// with the face it belongs to (a B-rep FaceRec index, or the source triangle of a raw
// mesh); refinement keeps every child in its parent's group and in its parent's place.
struct ThicknessMesh {
    std::vector<float> pos;           // 3 * n_verts
    std::vector<float> nrm;           // 3 * n_verts, outward unit
    std::vector<uint32_t> idx;        // 3 * n_tris, outward winding
    std::vector<uint32_t> tri_group;  // n_tris

    std::size_t n_verts() const { return pos.size() / 3; }
    std::size_t n_tris() const { return idx.size() / 3; }
};

// Total triangle area (mm^2).
double mesh_area(const ThicknessMesh& mesh);

// Bisect triangles along their longest edge until no edge exceeds `spacing` (mm).
// Midpoints are shared per edge, so the result stays conforming wherever the input
// shares vertices. A midpoint is lifted off the chord by the quadratic edge correction
// ((n_b - n_a).(b - a) / 8 along the mean normal), which puts it on a circular arc
// through both ends: exact for planes, second-order for curved faces.
ThicknessMesh refine_mesh(const ThicknessMesh& mesh, double spacing);

// Area attributed to each vertex: one third of every incident triangle.
std::vector<double> vertex_areas(const ThicknessMesh& mesh);

struct ThicknessOptions {
    // q opposes p only when n_p . n_q <= cos(wall_separation_deg).
    double wall_separation_deg = 120.0;
    // Largest ball radius (mm) the coverage pass tests a point against; <= 0 disables
    // coverage. Bigger balls only ever cover thick material, which needs no correction.
    double coverage_radius_mm = 0.0;
    // How far (mm) outside a ball's surface a point still counts as covered: absorbs the
    // sampling of the ball's own tangent points.
    double coverage_tolerance_mm = 0.0;
};

struct ThicknessResult {
    std::vector<float> thickness;     // per query, mm; NaN = no opposing wall found
    std::vector<float> opposing_cos;  // n_p . n_q of the wall pair that set the value; NaN
    std::vector<float> opposite;      // 3 per query: the opposing wall point
    std::vector<float> center;        // 3 per query: the centre of the measuring ball
};

// Thickness at each query sample (query_pos/query_nrm, 3 floats each), measured against
// the dense wall samples blocker_pos/blocker_nrm. The coverage pass runs among the query
// balls, so pass the blockers themselves as queries for a full surface field.
ThicknessResult measure_wall_thickness(const std::vector<float>& blocker_pos,
                                       const std::vector<float>& blocker_nrm,
                                       const std::vector<float>& query_pos,
                                       const std::vector<float>& query_nrm,
                                       const ThicknessOptions& options);

// A connected patch of surface thinner than a threshold.
struct ThinRegion {
    float min_thickness = 0.0f;
    uint32_t min_vertex = 0;
    double area = 0.0;                      // mm^2, from vertex_areas
    std::array<double, 3> centroid{};       // area-weighted
    std::array<float, 3> lo{}, hi{};        // axis-aligned bounds
    std::vector<uint32_t> groups;           // sorted tri_group values the patch touches
};

// Vertices with thickness < threshold, linked into regions by proximity (<= link_mm; pass
// at least the refinement spacing so mesh edges link — the link also joins patches across
// B-rep face seams, whose vertices are not shared). Sorted thinnest first.
std::vector<ThinRegion> thin_regions(const ThicknessMesh& mesh,
                                     const std::vector<float>& thickness,
                                     const std::vector<double>& areas, double threshold,
                                     double link_mm);

}  // namespace chudware::geom
