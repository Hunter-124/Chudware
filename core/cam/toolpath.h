// core/cam/toolpath.h — toolpath assembly helpers shared by the operation
// generators: a builder that appends classified moves, multi-depth stepping,
// lead-in/out emission, ramp/helix entry, retract/relink between disjoint loops,
// and holding-tab insertion. OCCT-free.
#pragma once

#include <vector>

#include "cam/model.h"

namespace aicad {
namespace cam {

// Incrementally assembles a Toolpath, tracking the current XYZ so callers append in
// machining order. Emits rapids at the clearance plane between disjoint moves.
class PathBuilder {
public:
    PathBuilder(const Heights& h, double feed, double plunge, double rapid_feed);

    void rapid_to_clearance();                 // retract Z to clearance plane
    void rapid_xy(const Vec2&);                // rapid in XY at clearance Z
    void rapid_to(const Vec3&);
    void plunge_to(double z);                  // feed Z down at plunge feed
    void feed_to(const Vec2&, double z);       // linear cut to (xy,z) at cut feed
    void feed_to(const Vec3&);
    void arc_to(const Vec2& end, const Vec2& center, bool ccw, double z);
    void lead(const Path2& pts, double z, double feed);  // lead-in/out at lead feed
    void dwell(double seconds);
    void set_spindle(int rpm);

    // Cut a closed loop at depth z, optionally with lead-in/out and tabs.
    void cut_loop(const Path2& loop, double z, const Lead& in, const Lead& out,
                  CompSide side, bool climb, int tab_count, double tab_w, double tab_h,
                  double top_z);

    // Cut an open path at depth z (engrave/raster stroke).
    void cut_open(const Path2& path, double z, bool lead_link);

    // Helical ramp entry into a hole/pocket from top_z down to z at the given XY.
    void helix_entry(const Vec2& center, double radius, double top_z, double z,
                     double ramp_feed, double pitch);
    // Zig-zag ramp entry along a short segment.
    void zigzag_entry(const Vec2& a, const Vec2& b, double top_z, double z,
                      double ramp_feed);

    Vec3 pos() const { return cur_; }
    Toolpath take(int tool_number, const Machine& m);  // finalize + stats

private:
    Toolpath tp_;
    Heights h_;
    double feed_, plunge_, rapid_;
    int rpm_ = 0;
    Vec3 cur_{0, 0, 1e9};  // unknown until first move
    bool at_clear_ = false;
};

// Stepped depths from top to bottom: returns the list of cut Z levels honoring
// stepdown (>0) and the axial finish allowance. Always includes the final depth.
std::vector<double> depth_levels(double top, double bottom, double stepdown,
                                 double stock_to_leave_axial);

}  // namespace cam
}  // namespace aicad
