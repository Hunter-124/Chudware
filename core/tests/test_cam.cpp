// Unit tests for the CAM suite (aicad::cam). OCCT-free; links aicad_cam
// (+ Clipper2 + miniz). Grows with each CAM wave: model serialization, geometry
// (offset/boolean/pocket/raster), feeds, operation generators, post, sim.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>

#include "cam/geometry.h"
#include "cam/model.h"

using namespace aicad::cam;

namespace {
Region square(double cx, double cy, double half) {
    Region r;
    r.outer = {{cx - half, cy - half}, {cx + half, cy - half},
               {cx + half, cy + half}, {cx - half, cy + half}};
    return r;
}
}  // namespace

TEST_CASE("model: enum string round-trips") {
    CHECK(std::string(to_string(OpType::Pocket2D)) == "pocket2d");
    CHECK(op_type_from_string("adaptive2d") == OpType::Adaptive2D);
    CHECK(op_type_from_string("turn_thread") == OpType::TurnThread);
    CHECK(std::string(to_string(ToolType::EndmillBall)) == "endmill_ball");
    CHECK(tool_type_from_string("plasma_torch") == ToolType::PlasmaTorch);
    CHECK(std::string(to_string(MachineKind::Waterjet)) == "waterjet");
    CHECK(tool_class(ToolType::Drill) == "drill");
    CHECK(tool_class(ToolType::Tap) == "tap");
    CHECK(tool_class(ToolType::EndmillFlat) == "endmill");
    // unknown -> default (never crash)
    CHECK(op_type_from_string("nonsense") == OpType::Contour2D);
}

TEST_CASE("model: job/setup/operation JSON round-trip") {
    Job j;
    j.name = "Bracket";
    j.post = "fanuc";
    Tool t;
    t.number = 3;
    t.type = ToolType::EndmillBull;
    t.diameter = 8;
    t.corner_radius = 1.0;
    t.flutes = 4;
    j.tools.tools.push_back(t);

    Setup s;
    s.name = "Op1";
    s.machine.kind = MachineKind::Mill3;
    s.stock.type = StockType::BoxOffset;
    s.stock.off_ztop = 1.0;
    Operation op;
    op.id = "op1";
    op.type = OpType::Pocket2D;
    op.tool_number = 3;
    op.stepover = 3.2;
    op.stepdown = 2.0;
    op.heights.bottom = -10;
    op.direction = CutDirection::Climb;
    op.lead_in.type = LeadType::Arc;
    op.geometry = {{"regions", json::array()}};
    s.operations.push_back(op);
    j.setups.push_back(s);

    json doc = j.to_json();
    Job back = Job::from_json(doc);
    CHECK(back.name == "Bracket");
    CHECK(back.post == "fanuc");
    REQUIRE(back.tools.tools.size() == 1);
    CHECK(back.tools.tools[0].number == 3);
    CHECK(back.tools.tools[0].type == ToolType::EndmillBull);
    CHECK(back.tools.tools[0].corner_radius == doctest::Approx(1.0));
    REQUIRE(back.setups.size() == 1);
    REQUIRE(back.setups[0].operations.size() == 1);
    const Operation& o = back.setups[0].operations[0];
    CHECK(o.type == OpType::Pocket2D);
    CHECK(o.stepover == doctest::Approx(3.2));
    CHECK(o.heights.bottom == doctest::Approx(-10));
    CHECK(o.direction == CutDirection::Climb);
    CHECK(o.lead_in.type == LeadType::Arc);
    // idempotent: re-serialize equals
    CHECK(back.to_json() == doc);
}

TEST_CASE("geo: signed area + orientation + containment") {
    Region r = square(0, 0, 10);  // 20x20, CCW
    CHECK(geo::signed_area(r.outer) > 0);
    CHECK(geo::is_ccw(r.outer));
    CHECK(geo::area(r.outer) == doctest::Approx(400.0));
    CHECK(geo::point_in_polygon({0, 0}, r.outer));
    CHECK_FALSE(geo::point_in_polygon({100, 0}, r.outer));
    // region with a hole
    r.holes.push_back(square(0, 0, 3).outer);  // 6x6 hole
    CHECK(geo::region_area(r) == doctest::Approx(400.0 - 36.0));
    CHECK_FALSE(geo::point_in_region({0, 0}, r));  // inside hole = not in region
    CHECK(geo::point_in_region({8, 0}, r));
}

TEST_CASE("geo: offset grows and shrinks a square") {
    Region r = square(0, 0, 10);  // 20x20
    auto grown = geo::offset_region(r, 2.0);     // 24x24 (rounded corners)
    REQUIRE(grown.size() == 1);
    CHECK(geo::region_area(grown[0]) > 400.0);
    CHECK(geo::region_area(grown[0]) == doctest::Approx(24.0 * 24.0).epsilon(0.02));
    auto shrunk = geo::offset_region(r, -2.0);   // 16x16
    REQUIRE(shrunk.size() == 1);
    CHECK(geo::region_area(shrunk[0]) == doctest::Approx(16.0 * 16.0).epsilon(0.01));
    // over-shrink collapses to nothing
    auto gone = geo::offset_region(r, -50.0);
    CHECK(gone.empty());
}

TEST_CASE("geo: offset preserves a hole (inset grows the island)") {
    Region r = square(0, 0, 20);          // 40x40
    r.holes.push_back(square(0, 0, 5).outer);  // 10x10 island
    auto inset = geo::offset_region(r, -2.0);  // outer 36x36, hole 14x14
    REQUIRE(inset.size() == 1);
    REQUIRE(inset[0].holes.size() == 1);
    CHECK(geo::area(inset[0].outer) == doctest::Approx(36.0 * 36.0).epsilon(0.02));
    CHECK(geo::area(inset[0].holes[0]) == doctest::Approx(14.0 * 14.0).epsilon(0.05));
}

TEST_CASE("geo: boolean difference cuts a notch") {
    Region a = square(0, 0, 10);          // 20x20
    Region b = square(10, 0, 5);          // 10x10 straddling the right edge
    auto res = geo::boolean_op(geo::ClipOp::Difference, {a}, {b}, false);
    REQUIRE(res.size() == 1);
    // removed the 5x10 overlap (50 mm^2) from 400
    CHECK(geo::region_area(res[0]) == doctest::Approx(400.0 - 50.0).epsilon(0.01));
}

TEST_CASE("geo: pocket offsets clear a square inward") {
    Region r = square(0, 0, 20);   // 40x40
    double tool_r = 3.0, stepover = 4.0;
    auto levels = geo::pocket_offsets(r, tool_r, stepover, 0.0);
    REQUIRE(levels.size() >= 3);
    // first level is inset by tool_r -> ~34x34 perimeter loop
    REQUIRE(!levels.front().empty());
    CHECK(geo::area(levels.front()[0]) == doctest::Approx(34.0 * 34.0).epsilon(0.05));
    // levels shrink monotonically
    double prev = 1e18;
    for (auto& lv : levels) {
        REQUIRE(!lv.empty());
        double a = geo::area(lv[0]);
        CHECK(a < prev);
        prev = a;
    }
}

TEST_CASE("geo: raster fill covers a square with correct spacing") {
    Region r = square(0, 0, 10);  // 20x20
    auto strokes = geo::raster_fill({r}, 2.0, 0.0, true);
    REQUIRE(strokes.size() >= 8);   // ~20/2 rows
    // every stroke spans roughly the full width (20mm) at y inside the square
    for (auto& s : strokes) {
        REQUIRE(s.size() == 2);
        double w = std::hypot(s[1].x - s[0].x, s[1].y - s[0].y);
        CHECK(w == doctest::Approx(20.0).epsilon(0.001));
    }
}

TEST_CASE("geo: raster fill skips an island hole") {
    Region r = square(0, 0, 20);            // 40x40
    r.holes.push_back(square(0, 0, 10).outer);  // 20x20 hole in the middle
    auto strokes = geo::raster_fill({r}, 2.0, 0.0, true);
    REQUIRE(!strokes.empty());
    // a scan line through the middle must be split into two strokes by the island
    int split_rows = 0;
    for (auto& s : strokes)
        if (std::fabs(s[0].y) < 6.0) ++split_rows;  // rows crossing the hole band
    CHECK(split_rows >= 2);
}

TEST_CASE("geo: open offset makes a kerf ribbon") {
    Path2 line = {{0, 0}, {20, 0}};
    auto ribbons = geo::offset_open({line}, 1.0, geo::JoinType::Round, true);
    REQUIRE(ribbons.size() == 1);
    // a 20mm line offset by 1mm both sides + round caps ~ area 2*20 + pi*1^2
    CHECK(geo::area(ribbons[0]) == doctest::Approx(40.0 + M_PI).epsilon(0.1));
}
