// Sketch-arc + path-wire suite — covers the profile.cpp edge/wire builder that feeds
// sweeps and profiles. Locks two round-3 fixes:
//   N-2: an `arc` entity HONORS its `ccw` flag (and reads angles as DEGREES), so a CW
//        sweep is the complementary arc of the CCW one — not byte-identical to it.
//   N-3: a path that MIXES `line` and `arc` entities stitches into ONE connected wire
//        even across a small float gap between an analytic line end and a trig arc end.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>

#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Wire.hxx>
#include <nlohmann/json.hpp>

#include "geom/plane.h"
#include "geom/profile.h"

using nlohmann::json;
using chudware::geom::SketchPlane;
using chudware::geom::wire_from_solved_sketch;

namespace {
constexpr double kPi = 3.14159265358979323846;

SketchPlane xy() {
    return SketchPlane::from(Eigen::Vector3d(0, 0, 0), Eigen::Vector3d(0, 0, 1));
}
double wire_length(const TopoDS_Wire& w) {
    GProp_GProps g;
    BRepGProp::LinearProperties(w, g);
    return g.Mass();
}
int edge_count(const TopoDS_Wire& w) {
    int n = 0;
    for (TopExp_Explorer ex(w, TopAbs_EDGE); ex.More(); ex.Next()) ++n;
    return n;
}
}  // namespace

// --- N-2: the `ccw` flag must change the arc that gets built. A 0->90 deg arc of r=10
//          is the 90 deg MINOR arc when ccw, and the 270 deg MAJOR arc when not.
TEST_CASE("arc entity honors ccw: minor vs major arc length") {
    const double r = 10.0;
    auto arc = [&](bool ccw) {
        json ents;
        ents["e1"] = {{"type", "arc"}, {"center", json::array({0.0, 0.0})}, {"radius", r},
                      {"start", 0.0}, {"end", 90.0}, {"ccw", ccw}};
        return wire_from_solved_sketch(ents, xy());
    };
    TopoDS_Wire minor = arc(true);
    TopoDS_Wire major = arc(false);
    REQUIRE_FALSE(minor.IsNull());
    REQUIRE_FALSE(major.IsNull());
    // Minor = 1/4 circumference; major = 3/4. Distinct (the bug made them identical).
    CHECK(wire_length(minor) == doctest::Approx(0.25 * 2.0 * kPi * r).epsilon(1e-3));
    CHECK(wire_length(major) == doctest::Approx(0.75 * 2.0 * kPi * r).epsilon(1e-3));
    CHECK(wire_length(major) > wire_length(minor) + 1.0);
}

// --- N-2b: angles are DEGREES, not radians — a half-circle (0..180) is pi*r long.
TEST_CASE("arc angles are degrees") {
    const double r = 5.0;
    json ents;
    ents["e1"] = {{"type", "arc"}, {"center", json::array({0.0, 0.0})}, {"radius", r},
                  {"start", 0.0}, {"end", 180.0}, {"ccw", true}};
    TopoDS_Wire w = wire_from_solved_sketch(ents, xy());
    REQUIRE_FALSE(w.IsNull());
    CHECK(wire_length(w) == doctest::Approx(kPi * r).epsilon(1e-3));
}

// --- N-3: a mixed line+arc path stitches into ONE wire across a small float gap.
TEST_CASE("mixed line+arc path stitches into one connected wire") {
    const double r = 10.0;
    json ents;
    // Arc 0->90 deg (minor): ends at (10,0) and (0,10).
    ents["e1"] = {{"type", "arc"}, {"center", json::array({0.0, 0.0})}, {"radius", r},
                  {"start", 0.0}, {"end", 90.0}, {"ccw", true}};
    // Line continues from (0,10) up to (0,25), but its start is 5e-5 off — a realistic
    // rounded-coordinate gap that the strict wire builder rejects and the tolerant
    // fallback must bridge.
    ents["e2"] = {{"type", "line"}, {"p1", json::array({0.0, 10.00005})},
                  {"p2", json::array({0.0, 25.0})}};
    TopoDS_Wire w = wire_from_solved_sketch(ents, xy());
    REQUIRE_FALSE(w.IsNull());          // the bug returned a NULL wire here
    CHECK(edge_count(w) == 2);          // both edges made it into the single wire
}

// --- N-3b: a genuinely BROKEN path (two disjoint pieces) still returns a null wire.
TEST_CASE("disconnected path returns no wire") {
    json ents;
    ents["e1"] = {{"type", "line"}, {"p1", json::array({0.0, 0.0})},
                  {"p2", json::array({10.0, 0.0})}};
    ents["e2"] = {{"type", "line"}, {"p1", json::array({50.0, 50.0})},
                  {"p2", json::array({60.0, 50.0})}};  // far away, no shared endpoint
    TopoDS_Wire w = wire_from_solved_sketch(ents, xy());
    CHECK(w.IsNull());
}
