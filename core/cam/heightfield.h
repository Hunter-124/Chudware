// core/cam/heightfield.h — a top-down (Z+) heightfield sampled from a triangle
// mesh, the standard substrate for 3-axis 3D finishing (parallel/waterline/
// scallop/pencil). Also the "dexel-lite" model the material-removal sim sweeps a
// tool through. OCCT-free.
#pragma once

#include <vector>

#include "cam/model.h"

namespace aicad {
namespace cam {

// A regular grid over [min..max] in XY storing the highest mesh Z at each node
// (the visible top surface for a +Z tool axis). Cells with no triangle hold `floor`.
class HeightField {
public:
    HeightField() = default;
    // Build from a mesh over the XY bbox (expanded by `pad`), at `res` mm spacing.
    static HeightField from_mesh(const TriMesh&, double res, double pad = 0,
                                 double floor_z = 0);

    int nx() const { return nx_; }
    int ny() const { return ny_; }
    double res() const { return res_; }
    Vec2 lo() const { return lo_; }
    Vec2 hi() const { return hi_; }
    double floor() const { return floor_; }

    // Surface height at world (x,y) (bilinear). Returns floor outside the grid.
    double height_at(double x, double y) const;
    // Lower the tip of a tool of the given geometry centred at (x,y) onto the
    // surface: the highest contact Z so the tool just grazes the field (gouge-free
    // 3-axis drop-cutter). `corner_r`==radius -> ball; 0 -> flat; else bull-nose.
    double drop_tool(double x, double y, double radius, double corner_r,
                     double sample_step = 0) const;

    double raw(int i, int j) const;  // node height (clamped indices)
    void set_raw(int i, int j, double z);

private:
    int nx_ = 0, ny_ = 0;
    double res_ = 1.0;
    Vec2 lo_{0, 0}, hi_{0, 0};
    double floor_ = 0;
    std::vector<float> z_;  // nx_*ny_, row-major (j*nx_+i)
};

// Z-level (waterline) cross sections of a mesh at the given Z planes: each plane
// yields a set of closed contours (the silhouette of material at/above that Z).
std::vector<std::vector<Path2>> waterline_sections(const TriMesh&,
                                                   const std::vector<double>& z_levels,
                                                   double tolerance = 0.01);

}  // namespace cam
}  // namespace aicad
