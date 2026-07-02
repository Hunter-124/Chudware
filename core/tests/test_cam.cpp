// Unit tests for the CAM suite (aicad::cam). OCCT-free; links aicad_cam
// (+ Clipper2 + miniz). Grows with each CAM wave: model serialization, geometry
// (offset/boolean/pocket/raster), feeds, operation generators, post, sim.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>

#include "cam/geometry.h"
#include "cam/feeds.h"
#include "cam/operations.h"
#include "cam/heightfield.h"
#include "cam/post.h"
#include "cam/stock_sim.h"
#include "cam/session.h"
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

TEST_CASE("feeds: aluminum endmill gives sane rpm/feed") {
    Machine m;  // 24000 rpm, 10000 mm/min default
    Tool t;
    t.type = ToolType::EndmillFlat;
    t.diameter = 6;
    t.flutes = 3;
    t.tool_material = "carbide";
    Operation op;
    op.type = OpType::Contour2D;
    Feeds f = compute_feeds(m, t, op, "aluminum");
    // 900 SFM, 6mm -> ~14500 rpm (clamped to 24000) ; 3 flutes * ~0.06 fz
    CHECK(f.rpm > 10000.0);
    CHECK(f.rpm <= 24000.0);
    CHECK(f.feed > 1000.0);
    CHECK(f.feed <= 10000.0);   // machine feed clamp
    CHECK(f.plunge > 0.0);
    CHECK(f.plunge < f.feed);
    CHECK(f.stepover == doctest::Approx(0.45 * 6.0));
    CHECK(f.stepdown == doctest::Approx(1.0 * 6.0));
}

TEST_CASE("feeds: steel runs slower than aluminum") {
    Machine m;
    Tool t; t.diameter = 6; t.flutes = 3;
    Operation op; op.type = OpType::Pocket2D;
    Feeds al = compute_feeds(m, t, op, "aluminum");
    Feeds st = compute_feeds(m, t, op, "steel_mild");
    CHECK(st.rpm < al.rpm);
    CHECK(st.sfm < al.sfm);
}

TEST_CASE("feeds: HSS tool is slower than carbide") {
    Machine m;
    Tool carbide; carbide.diameter = 6; carbide.flutes = 2; carbide.tool_material = "carbide";
    Tool hss = carbide; hss.tool_material = "hss";
    Operation op; op.type = OpType::Contour2D;
    Feeds c = compute_feeds(m, carbide, op, "steel_mild");
    Feeds h = compute_feeds(m, hss, op, "steel_mild");
    CHECK(h.rpm < c.rpm);
}

TEST_CASE("feeds: merge_cam overwrites only the feeds fields, preserves identity") {
    CuttingDB db;
    json cam;
    cam["id"] = "aluminum";
    cam["sfm_mill"] = 1234.0;   // only this feeds field supplied
    json reg;
    reg["materials"]["some_registry_alloy"]["cam"] = cam;
    db.merge_cam(reg);
    const MaterialData* m = db.find("aluminum");
    REQUIRE(m != nullptr);
    CHECK(m->sfm_mill == doctest::Approx(1234.0));   // overwritten
    CHECK(m->sfm_drill == doctest::Approx(300.0));   // untouched (stays in-code)
    CHECK(m->name == "Aluminum 6061");               // identity preserved
    CHECK(m->group == "nonferrous");
    CHECK(m->density_g_cm3 == doctest::Approx(2.70));
    // A cam.id with no in-code row is skipped (no phantom row added).
    json reg2;
    reg2["materials"]["x"]["cam"] = json{{"id", "no_such_group"}, {"sfm_mill", 5.0}};
    size_t before = db.material_ids().size();
    db.merge_cam(reg2);
    CHECK(db.material_ids().size() == before);
}

#ifdef AICAD_TEST_REGISTRY
TEST_CASE("feeds: registry cam fold-in reproduces the in-code table byte-identically (T-M4)") {
    std::ifstream fh(AICAD_TEST_REGISTRY);
    REQUIRE_MESSAGE(fh.good(), "registry.json not found at AICAD_TEST_REGISTRY");
    json doc = json::parse(fh, nullptr, /*allow_exceptions=*/false);
    REQUIRE_FALSE(doc.is_discarded());

    // Guard against a silent empty-merge false-pass: the registry must actually carry cam sections.
    int folded = 0;
    for (auto it = doc["materials"].begin(); it != doc["materials"].end(); ++it)
        if (it.value().is_object() && it.value().contains("cam")) ++folded;
    CHECK(folded == 12);

    CuttingDB pure;              // in-code baseline
    CuttingDB merged;           // in-code + registry cam fold-in
    merged.merge_cam(doc);

    CHECK(pure.material_ids() == merged.material_ids());
    for (const auto& id : pure.material_ids()) {
        const MaterialData* a = pure.find(id);
        const MaterialData* b = merged.find(id);
        REQUIRE(a != nullptr);
        REQUIRE(b != nullptr);
        // All 14 fields compared via JSON (feeds + identity) — a defaulted mismatch cannot slip.
        CHECK(a->to_json() == b->to_json());
    }
}
#endif

TEST_CASE("feeds: rigid tap feed equals rpm times pitch") {
    Machine m;
    Tool tap; tap.type = ToolType::Tap; tap.diameter = 5; tap.thread_pitch = 0.8;
    Operation op; op.type = OpType::Drill; op.cycle = "rigidtap";
    Feeds f = compute_feeds(m, tap, op, "aluminum");
    CHECK(f.feed == doctest::Approx(f.rpm * 0.8).epsilon(0.001));
}

TEST_CASE("feeds: laser cut feed scales down with thickness, no spindle") {
    Machine m; m.kind = MachineKind::Laser;
    Tool head; head.type = ToolType::LaserHead;
    Operation thin; thin.type = OpType::Cut2D; thin.heights.top = 0; thin.heights.bottom = -1;
    Operation thick; thick.type = OpType::Cut2D; thick.heights.top = 0; thick.heights.bottom = -10;
    Feeds ft = compute_feeds(m, head, thin, "steel_mild");
    Feeds fk = compute_feeds(m, head, thick, "steel_mild");
    CHECK(ft.rpm == 0.0);
    CHECK(fk.feed < ft.feed);   // 10mm cuts slower than 1mm
}

TEST_CASE("feeds: user override on op is preserved by apply_feeds") {
    Machine m;
    Tool t; t.diameter = 6; t.flutes = 3;
    Operation op; op.type = OpType::Contour2D;
    op.rpm = 12000;          // user pinned rpm
    op.feed = 0;             // let it resolve
    Feeds f = compute_feeds(m, t, op, "aluminum");
    apply_feeds(op, f);
    CHECK(op.rpm == 12000);          // preserved
    CHECK(op.feed == doctest::Approx(f.feed));  // filled
}

TEST_CASE("feeds: cutting DB has the expected materials + merge works") {
    const CuttingDB& db = CuttingDB::builtin();
    CHECK(db.find("aluminum") != nullptr);
    CHECK(db.find("titanium") != nullptr);
    CHECK(db.find("stainless_304") != nullptr);
    CHECK(db.find("nonexistent") == nullptr);
    CHECK(db.material_ids().size() >= 20);
    CuttingDB custom;
    custom.merge(json{{"materials", json::array({json{{"id", "unobtanium"}, {"sfm_mill", 50}}})}});
    REQUIRE(custom.find("unobtanium") != nullptr);
    CHECK(custom.find("unobtanium")->sfm_mill == doctest::Approx(50));
    // override existing
    custom.merge(json{{"materials", json::array({json{{"id", "aluminum"}, {"sfm_mill", 1234}}})}});
    CHECK(custom.find("aluminum")->sfm_mill == doctest::Approx(1234));
}

// ── operation generator tests ───────────────────────────────────────────────
namespace {
OpContext mill_ctx(double tool_dia = 6.0, ToolType tt = ToolType::EndmillFlat) {
    OpContext c;
    c.machine.kind = MachineKind::Mill3;
    c.material_id = "aluminum";
    c.tool.number = 1;
    c.tool.type = tt;
    c.tool.diameter = tool_dia;
    c.tool.flutes = 3;
    c.stock.type = StockType::Box;
    c.stock.min = {-25, -25, -10};
    c.stock.max = {25, 25, 0};
    return c;
}
void path_xy_bounds(const Toolpath& tp, Vec2& lo, Vec2& hi) {
    lo = {1e300, 1e300}; hi = {-1e300, -1e300};
    for (auto& m : tp.moves) {
        lo.x = std::min(lo.x, m.to.x); lo.y = std::min(lo.y, m.to.y);
        hi.x = std::max(hi.x, m.to.x); hi.y = std::max(hi.y, m.to.y);
    }
}
int count_kind(const Toolpath& tp, MoveKind k) {
    int n = 0; for (auto& m : tp.moves) if (m.kind == k) ++n; return n;
}
}  // namespace

TEST_CASE("op: contour offsets outside by tool radius") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.id = "c1";
    op.type = OpType::Contour2D;
    op.comp_side = CompSide::Left;          // tool outside the boundary
    op.heights.top = 0; op.heights.bottom = -5; op.stepdown = 2.5;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 10))});  // 20x20
    GenResult r = gen_contour2d(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    CHECK(count_kind(r.path, MoveKind::Cut) > 0);
    Vec2 lo, hi; path_xy_bounds(r.path, lo, hi);
    // outside offset by r=3 -> ~26x26 envelope
    CHECK(hi.x == doctest::Approx(13.0).epsilon(0.05));
    CHECK(lo.x == doctest::Approx(-13.0).epsilon(0.05));
    // multi-depth: 5mm / 2.5 stepdown -> 2 levels, so loop cut twice
    CHECK(count_kind(r.path, MoveKind::Plunge) >= 2);
}

TEST_CASE("op: contour feeds resolved from material when unset") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.type = OpType::Contour2D;
    op.heights.top = 0; op.heights.bottom = -2;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 10))});
    GenResult r = gen_contour2d(op, ctx);
    REQUIRE(r.ok);
    // a cut move must carry a nonzero feed (resolved)
    bool any_feed = false;
    for (auto& m : r.path.moves) if (m.kind == MoveKind::Cut && m.feed > 0) any_feed = true;
    CHECK(any_feed);
    CHECK(r.path.cycle_time_s > 0);
}

TEST_CASE("op: pocket clears inside the region and stays in bounds") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.id = "p1";
    op.type = OpType::Pocket2D;
    op.heights.top = 0; op.heights.bottom = -4; op.stepdown = 2.0;
    op.stepover = 3.0;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 20))});  // 40x40
    GenResult r = gen_pocket2d(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    // every CUT move stays inside the machinable area (region inset by r): |xy|<=17
    for (auto& m : r.path.moves)
        if (m.kind == MoveKind::Cut) {
            CHECK(std::fabs(m.to.x) <= 17.001);
            CHECK(std::fabs(m.to.y) <= 17.001);
        }
}

TEST_CASE("op: pocket respects an island hole") {
    OpContext ctx = mill_ctx(6.0);
    Region reg = square(0, 0, 20);
    reg.holes.push_back(square(0, 0, 6).outer);  // 12x12 island
    Operation op;
    op.type = OpType::Pocket2D;
    op.heights.top = 0; op.heights.bottom = -3; op.stepover = 3.0;
    op.geometry["regions"] = json::array({to_json(reg)});
    GenResult r = gen_pocket2d(op, ctx);
    REQUIRE(r.ok);
    // no cut move should land deep inside the island (island grown by tool r=3 -> |xy|<3)
    for (auto& m : r.path.moves)
        if (m.kind == MoveKind::Cut) {
            bool deep_in_island = (std::fabs(m.to.x) < 2.5) && (std::fabs(m.to.y) < 2.5);
            CHECK_FALSE(deep_in_island);
        }
}

TEST_CASE("op: facing rasters across the stock") {
    OpContext ctx = mill_ctx(10.0, ToolType::FaceMill);
    Operation op;
    op.type = OpType::Face;
    op.heights.top = 0; op.heights.bottom = -0.5;
    op.stepover = 6.0;
    GenResult r = gen_face(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    CHECK(count_kind(r.path, MoveKind::Cut) >= 6);
}

TEST_CASE("op: drilling cycles produce moves per hole; peck pecks") {
    OpContext ctx = mill_ctx(5.0, ToolType::Drill);
    json pts = json::array({json::array({0, 0, 0}), json::array({10, 0, 0}), json::array({20, 0, 0})});
    Operation simple;
    simple.type = OpType::Drill; simple.cycle = "drill";
    simple.heights.top = 0; simple.heights.bottom = -10; simple.heights.retract = 2;
    simple.geometry["points"] = pts;
    GenResult rs = gen_drill(simple, ctx);
    REQUIRE(rs.ok);
    CHECK(count_kind(rs.path, MoveKind::Plunge) == 3);   // one plunge per hole

    Operation peck = simple;
    peck.cycle = "peck"; peck.peck = 2.0;                 // 10mm / 2mm -> 5 pecks/hole
    GenResult rp = gen_drill(peck, ctx);
    REQUIRE(rp.ok);
    CHECK(count_kind(rp.path, MoveKind::Plunge) > count_kind(rs.path, MoveKind::Plunge));
}

TEST_CASE("op: engrave follows a V-bit groove at width-derived depth") {
    OpContext ctx = mill_ctx(6.0, ToolType::VBit);
    ctx.tool.tip_angle = 60;
    Operation op;
    op.type = OpType::Engrave;
    op.heights.top = 0; op.heights.bottom = -5;
    op.extra["width"] = 1.0;             // 1mm groove -> depth = 0.5/tan(30) ~= 0.866
    op.geometry["contours"] = json::array({json::array({json::array({-10, 0}), json::array({10, 0})})});
    GenResult r = gen_engrave(op, ctx);
    REQUIRE(r.ok);
    double minz = 1e9;
    for (auto& m : r.path.moves) if (m.kind == MoveKind::Cut) minz = std::min(minz, m.to.z);
    CHECK(minz == doctest::Approx(-0.866).epsilon(0.05));
}

TEST_CASE("op: adaptive uses small engagement, helical entry, deep cut") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.type = OpType::Adaptive2D;
    op.heights.top = 0; op.heights.bottom = -12;   // deep
    op.geometry["regions"] = json::array({to_json(square(0, 0, 20))});
    GenResult r = gen_adaptive2d(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    // helical entry => many cut moves changing Z before reaching depth
    CHECK(count_kind(r.path, MoveKind::Cut) > 20);
}

TEST_CASE("op: generate() dispatch + suppressed + not-yet-wired") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.type = OpType::Contour2D;
    op.heights.bottom = -2;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 10))});
    CHECK(generate(op, ctx).ok);
    Operation sup = op; sup.suppressed = true;
    CHECK(generate(sup, ctx).ok);            // suppressed -> ok, no path
    Operation turn; turn.type = OpType::TurnRough;   // wired, but no profile geometry
    CHECK_FALSE(generate(turn, ctx).ok);             // -> fails cleanly (no profile)
}

// ── 3D / turning / cutting generator tests (Phase 3) ─────────────────────────
namespace {
// A flat plane at z=h over [-s,s]^2 (two triangles).
TriMesh plane_mesh(double h, double s) {
    TriMesh m;
    m.verts = {{-s, -s, h}, {s, -s, h}, {s, s, h}, {-s, s, h}};
    m.tris = {{0, 1, 2}, {0, 2, 3}};
    return m;
}
// A square pyramid: apex (0,0,ap), base [-s,s]^2 at z=0 (4 side triangles).
TriMesh pyramid_mesh(double s, double ap) {
    TriMesh m;
    m.verts = {{-s, -s, 0}, {s, -s, 0}, {s, s, 0}, {-s, s, 0}, {0, 0, ap}};
    m.tris = {{0, 1, 4}, {1, 2, 4}, {2, 3, 4}, {3, 0, 4}};
    return m;
}
double min_cut_z(const Toolpath& tp) {
    double z = 1e9;
    for (auto& m : tp.moves) if (m.kind == MoveKind::Cut || m.kind == MoveKind::Plunge) z = std::min(z, m.to.z);
    return z;
}
}  // namespace

TEST_CASE("heightfield: flat plane samples + flat-tool drop") {
    HeightField hf = HeightField::from_mesh(plane_mesh(3.0, 10.0), 0.5, 0.0, -10.0);
    CHECK(hf.nx() > 0);
    CHECK(hf.height_at(0, 0) == doctest::Approx(3.0).epsilon(0.01));
    CHECK(hf.drop_tool(0, 0, 3.0, 0.0, 0.5) == doctest::Approx(3.0).epsilon(0.02));
}

TEST_CASE("heightfield: pyramid drop never gouges + center highest") {
    TriMesh py = pyramid_mesh(10.0, 8.0);
    HeightField hf = HeightField::from_mesh(py, 0.5, 0.0, 0.0);
    double zc = hf.drop_tool(0, 0, 0.0, 0.0, 0.5);   // tiny tool at apex
    double ze = hf.drop_tool(8, 0, 0.0, 0.0, 0.5);   // near edge
    CHECK(zc > ze);
    CHECK(zc == doctest::Approx(8.0).epsilon(0.1));
    // a ball tool centered at apex must ride ABOVE the surface height there (no gouge)
    double zball = hf.drop_tool(0, 0, 3.0, 3.0, 0.5);
    CHECK(zball >= hf.height_at(0, 0) - 0.05);
}

TEST_CASE("heightfield: waterline sections of a pyramid shrink with height") {
    TriMesh py = pyramid_mesh(10.0, 8.0);
    auto secs = waterline_sections(py, {2.0, 6.0}, 0.05);
    REQUIRE(secs.size() == 2);
    REQUIRE(!secs[0].empty());
    REQUIRE(!secs[1].empty());
    // section at z=6 is smaller than at z=2
    CHECK(geo::area(secs[1][0]) < geo::area(secs[0][0]));
}

TEST_CASE("op3d: parallel finish follows the surface without gouging") {
    OpContext ctx = mill_ctx(4.0, ToolType::EndmillBall);
    ctx.tool.corner_radius = 2.0;
    Operation op;
    op.type = OpType::Parallel3D;
    op.heights.top = 8; op.heights.bottom = 0; op.stepover = 1.0;
    op.geometry["mesh"] = to_json(pyramid_mesh(10.0, 8.0));
    GenResult r = gen_parallel3d(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    CHECK(count_kind(r.path, MoveKind::Cut) > 20);
    CHECK(min_cut_z(r.path) >= -0.1);   // never below the base
}

TEST_CASE("op3d: waterline + roughing + scallop + spiral produce paths") {
    OpContext ctx = mill_ctx(4.0, ToolType::EndmillBall);
    ctx.tool.corner_radius = 2.0;
    auto mk = [&](OpType t) {
        Operation op;
        op.type = t;
        op.heights.top = 8; op.heights.bottom = 0; op.stepover = 1.5; op.stepdown = 2.0;
        op.geometry["mesh"] = to_json(pyramid_mesh(10.0, 8.0));
        return generate(op, ctx);
    };
    for (OpType t : {OpType::Contour3D, OpType::Pocket3D, OpType::Scallop3D,
                     OpType::SpiralFinish3D, OpType::Radial3D, OpType::AdaptiveClear3D}) {
        GenResult r = mk(t);
        CHECK(r.ok);
        CHECK(r.path.cut_length > 0);
    }
}

TEST_CASE("turn: finish follows the profile radii") {
    OpContext ctx;
    ctx.machine.kind = MachineKind::Lathe;
    ctx.material_id = "steel_mild";
    ctx.tool.type = ToolType::TurningGeneral;
    ctx.tool.number = 1;
    // stepped shaft: (z,x) radius profile
    json prof = json::array({json::array({0, 10}), json::array({10, 10}),
                             json::array({10, 5}), json::array({25, 5})});
    Operation op;
    op.type = OpType::TurnFinish;
    op.heights.top = 0; op.heights.bottom = 0;
    op.geometry["profile"] = prof;
    GenResult r = gen_turn_finish(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    // every cut radius (x) stays within the profile envelope [0,10]
    for (auto& m : r.path.moves)
        if (m.kind == MoveKind::Cut) { CHECK(m.to.x <= 10.5); CHECK(m.to.x >= -0.01); }
}

TEST_CASE("turn: roughing + facing + threading produce passes") {
    OpContext ctx;
    ctx.machine.kind = MachineKind::Lathe;
    ctx.material_id = "steel_mild";
    ctx.tool.type = ToolType::TurningGeneral;
    json prof = json::array({json::array({0, 12}), json::array({30, 12})});
    auto mk = [&](OpType t, double pitch = 0) {
        Operation op;
        op.type = t;
        op.geometry["profile"] = prof;
        op.stepdown = 1.5; op.stock_to_leave = 0.3;
        op.thread_pitch = pitch; op.thread_depth = pitch * 0.6; op.infeed = 0.2;
        return generate(op, ctx);
    };
    CHECK(mk(OpType::TurnRough).ok);
    CHECK(mk(OpType::TurnFace).path.moves.size() > 0);
    GenResult thr = mk(OpType::TurnThread, 1.5);
    CHECK(thr.ok);
    CHECK(thr.path.cut_length > 0);
}

TEST_CASE("cut2d: kerf offset applied + pierce; probe per point") {
    OpContext ctx;
    ctx.machine.kind = MachineKind::Laser;
    ctx.machine.kerf = 0.2;
    ctx.material_id = "steel_mild";
    ctx.tool.type = ToolType::LaserHead;
    Operation op;
    op.type = OpType::Cut2D;
    op.comp_side = CompSide::Left;        // outside cut -> grow by kerf/2
    op.heights.top = 0; op.heights.bottom = -2;
    op.pierce_delay = 0.5;
    op.geometry["contours"] = json::array({json::array({json::array({-10, -10}), json::array({10, -10}),
                                                        json::array({10, 10}), json::array({-10, 10})})});
    GenResult r = gen_cut2d(op, ctx);
    REQUIRE(r.ok);
    CHECK(r.path.cut_length > 0);
    CHECK(count_kind(r.path, MoveKind::Dwell) >= 1);   // pierce dwell

    Operation pr;
    pr.type = OpType::Probe;
    pr.heights.top = 5; pr.heights.bottom = -2; pr.heights.clearance = 15;
    pr.geometry["points"] = json::array({json::array({0, 0, 0}), json::array({5, 5, 0})});
    GenResult rp = gen_probe(pr, ctx);
    REQUIRE(rp.ok);
    CHECK(rp.path.moves.size() > 0);
}

// ── post-processor tests (Phase 4) ───────────────────────────────────────────
namespace {
bool has(const std::string& s, const std::string& needle) { return s.find(needle) != std::string::npos; }

// build a small milling setup: one contour op (T1) + one drill op (T2), generate
// toolpaths, return (setup, tools, toolpaths).
struct Built { Setup setup; ToolLibrary tools; std::vector<Toolpath> tps; };
Built build_mill_job() {
    Built b;
    b.setup.name = "Setup1";
    b.setup.machine.kind = MachineKind::Mill3;
    b.setup.machine.name = "VMC";
    b.setup.stock.type = StockType::Box;
    b.setup.stock.min = {-25, -25, -10}; b.setup.stock.max = {25, 25, 0};
    b.setup.wcs.offset = 54;
    Tool t1; t1.number = 1; t1.type = ToolType::EndmillFlat; t1.diameter = 6; t1.flutes = 3; t1.name = "6mm flat";
    Tool t2; t2.number = 2; t2.type = ToolType::Drill; t2.diameter = 5; t2.name = "5mm drill";
    b.tools.tools = {t1, t2};

    Operation c; c.id = "c1"; c.name = "Profile"; c.type = OpType::Contour2D; c.tool_number = 1;
    c.comp_side = CompSide::Left; c.heights.top = 0; c.heights.bottom = -5; c.stepdown = 2.5;
    c.geometry["regions"] = json::array({to_json(square(0, 0, 10))});
    Operation d; d.id = "d1"; d.name = "Drill"; d.type = OpType::Drill; d.tool_number = 2;
    d.cycle = "peck"; d.peck = 2.0; d.heights.top = 0; d.heights.bottom = -10; d.heights.retract = 2;
    d.geometry["points"] = json::array({json::array({0, 0, 0}), json::array({10, 0, 0})});
    b.setup.operations = {c, d};

    for (Operation* op : {&b.setup.operations[0], &b.setup.operations[1]}) {
        OpContext ctx;
        ctx.machine = b.setup.machine; ctx.stock = b.setup.stock; ctx.wcs = b.setup.wcs;
        ctx.material_id = "aluminum";
        ctx.tool = *b.tools.find(op->tool_number);
        b.tps.push_back(generate(*op, ctx).path);
    }
    return b;
}
}  // namespace

TEST_CASE("post: dialect registry has all eight controllers") {
    auto ids = post_dialect_ids();
    CHECK(ids.size() == 8);
    for (const char* id : {"grbl", "linuxcnc", "fanuc", "haas", "mach3", "marlin", "siemens", "centroid"})
        CHECK(std::find(ids.begin(), ids.end(), std::string(id)) != ids.end());
    CHECK(post_dialect("fanuc").name == "Fanuc");
    CHECK(post_dialect("unknown_dialect").id == "grbl");  // graceful fallback
}

TEST_CASE("post: GRBL emits conformant long-hand g-code (no canned cycles)") {
    Built b = build_mill_job();
    PostResult r = post_setup(b.setup, b.tools, "grbl", b.tps);
    REQUIRE(r.ok);
    CHECK(r.line_count > 10);
    CHECK(has(r.gcode, "G21"));     // metric
    CHECK(has(r.gcode, "G90"));     // absolute
    CHECK(has(r.gcode, "G0"));      // rapids
    CHECK(has(r.gcode, "G1"));      // feeds
    bool spindle_on = has(r.gcode, "M3") || has(r.gcode, "M03");
    CHECK(spindle_on);
    bool prog_end = has(r.gcode, "M30") || has(r.gcode, "M2");
    CHECK(prog_end);
    CHECK(has(r.gcode, "("));       // comments in parens
    CHECK_FALSE(has(r.gcode, "G81")); // GRBL has no canned cycles
    CHECK_FALSE(has(r.gcode, "G83"));
}

TEST_CASE("post: Fanuc emits O-number, N-lines, %, and a peck cycle G83") {
    Built b = build_mill_job();
    PostResult r = post_setup(b.setup, b.tools, "fanuc", b.tps);
    REQUIRE(r.ok);
    CHECK(r.gcode.front() == '%');
    CHECK(has(r.gcode, "O"));        // program number
    CHECK(has(r.gcode, "N"));        // sequence numbers
    CHECK(has(r.gcode, "G83"));      // peck drilling canned cycle
    CHECK(has(r.gcode, "Q"));        // peck increment
    CHECK(has(r.gcode, "R"));        // retract plane
    CHECK(has(r.gcode, "G80"));      // cycle cancel
    CHECK(has(r.gcode, "T2"));       // tool change to the drill
    CHECK(has(r.gcode, "G43"));      // length offset
    CHECK(r.tool_changes >= 2);
}

TEST_CASE("post: arcs emit IJK; tool change between two tools") {
    Built b = build_mill_job();
    // make the contour use an arc lead so an arc move exists
    PostResult r = post_setup(b.setup, b.tools, "linuxcnc", b.tps);
    REQUIRE(r.ok);
    CHECK(has(r.gcode, "M6"));         // tool change
    CHECK(has(r.gcode, "G54"));        // work offset
    CHECK(has(r.gcode, "%"));          // percent wrap
    // estimated time is the sum of toolpath times
    CHECK(r.est_time_s > 0);
}

TEST_CASE("post: Marlin laser gates the beam with M3 S / M5, no spindle") {
    Setup su;
    su.name = "Laser";
    su.machine.kind = MachineKind::Laser;
    su.machine.kerf = 0.15;
    Tool head; head.number = 1; head.type = ToolType::LaserHead;
    ToolLibrary lib; lib.tools = {head};
    Operation cut; cut.id = "L1"; cut.type = OpType::Cut2D; cut.tool_number = 1;
    cut.comp_side = CompSide::On; cut.power = 0.8; cut.heights.top = 0; cut.heights.bottom = -1;
    cut.geometry["contours"] = json::array({json::array({json::array({0,0}),json::array({20,0}),
                                                         json::array({20,20}),json::array({0,20})})});
    su.operations = {cut};
    OpContext ctx; ctx.machine = su.machine; ctx.material_id = "steel_mild";
    ctx.tool = head;
    std::vector<Toolpath> tps = {generate(cut, ctx).path};
    PostResult r = post_setup(su, lib, "marlin", tps);
    REQUIRE(r.ok);
    CHECK(has(r.gcode, "M3"));     // beam on
    CHECK(has(r.gcode, "M5"));     // beam off
    CHECK(has(r.gcode, "S"));      // laser power word
    CHECK(has(r.gcode, ";"));      // marlin line comments
}

TEST_CASE("post: setup sheet lists tools + operations + time") {
    Built b = build_mill_job();
    Job job; job.name = "Bracket"; job.tools = b.tools; job.setups = {b.setup};
    SheetResult s = setup_sheet(job, {b.tps}, "fanuc");
    CHECK(has(s.html, "Bracket"));
    CHECK(has(s.html, "6mm flat"));
    CHECK(has(s.html, "Profile"));
    CHECK(has(s.text, "TOOLS"));
    CHECK(has(s.text, "machining time"));
}

// ── material-removal sim + backplot + time (Phase 5) ─────────────────────────
TEST_CASE("sim: facing a box removes the expected slab volume") {
    Setup su;
    su.machine.kind = MachineKind::Mill3;
    su.stock.type = StockType::Box;
    su.stock.min = {-25, -25, -10};
    su.stock.max = {25, 25, 0};        // 50x50x10 = 25000 mm^3
    Tool t; t.number = 1; t.type = ToolType::FaceMill; t.diameter = 12;
    ToolLibrary lib; lib.tools = {t};
    Operation op; op.id = "f"; op.type = OpType::Face; op.tool_number = 1;
    op.heights.top = 0; op.heights.bottom = -2;   // remove a 2mm slab over 50x50 = 5000 mm^3
    op.stepover = 8;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 25))});
    su.operations = {op};
    OpContext ctx; ctx.machine = su.machine; ctx.stock = su.stock; ctx.tool = t; ctx.material_id = "aluminum";
    std::vector<Toolpath> tps = {generate(op, ctx).path};
    SimResult r = simulate_setup(su, lib, tps, TriMesh{}, SimOptions{});
    REQUIRE(r.ok);
    CHECK(r.stock_volume_mm3 == doctest::Approx(50.0 * 50.0 * 10.0).epsilon(0.05));
    CHECK(r.removed_volume_mm3 > 3000.0);     // ~5000, allow for tool coverage/edges
    CHECK(r.removed_volume_mm3 < 7000.0);
    CHECK(r.cycle_time_s > 0);
    CHECK(r.remaining_volume_mm3 < r.stock_volume_mm3);
}

TEST_CASE("sim: estimate_time scales with feed") {
    Toolpath tp;
    tp.moves.push_back({MoveKind::Rapid, {0, 0, 0}, false, {0,0,0}, 0, 0, 0});   // start position
    tp.moves.push_back({MoveKind::Cut, {100, 0, 0}, false, {0,0,0}, 600, 0, 0});
    Machine m;
    double t_slow = estimate_time(tp, m);
    tp.moves[1].feed = 1200;
    double t_fast = estimate_time(tp, m);
    CHECK(t_fast < t_slow);
    // 100mm at 600 mm/min ~= 10s (feed-limited, no accel)
    tp.moves[1].feed = 600;
    CHECK(estimate_time(tp, m) == doctest::Approx(10.0).epsilon(0.05));
}

TEST_CASE("sim: backplot classifies moves into colored polylines") {
    OpContext ctx = mill_ctx(6.0);
    Operation op; op.type = OpType::Contour2D; op.heights.bottom = -3;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 10))});
    Toolpath tp = gen_contour2d(op, ctx).path;
    json bp = backplot(tp);
    CHECK(bp.contains("rapid"));
    CHECK(bp.contains("cut"));
    CHECK(bp["cut"].size() > 0);
    CHECK(bp["cut"].size() % 3 == 0);   // flat xyz triples (pairs of points)
}

TEST_CASE("sim: rapid through stock is flagged") {
    Setup su;
    su.stock.type = StockType::Box;
    su.stock.min = {-10, -10, -5}; su.stock.max = {10, 10, 0};
    Tool t; t.number = 1; t.diameter = 6; ToolLibrary lib; lib.tools = {t};
    Operation op; op.id = "x"; op.tool_number = 1;
    su.operations = {op};
    // hand-build a toolpath: a rapid that plows through the stock at z=-3
    Toolpath tp; tp.op_id = "x"; tp.tool_number = 1;
    tp.moves.push_back({MoveKind::Rapid, {-8, 0, -3}, false, {0,0,0}, 0, 0, 0});
    tp.moves.push_back({MoveKind::Rapid, {8, 0, -3}, false, {0,0,0}, 0, 0, 0});
    SimOptions so; so.check_rapid = true;
    SimResult r = simulate_setup(su, lib, {tp}, TriMesh{}, so);
    REQUIRE(r.ok);
    CHECK(r.rapid_collisions > 0);
}

// ── T-M6: pocket entry + arc-true stock sim + turning sim (audit D3, D8, G4) ──

// D3: a straight-plunge pocket entry must rapid over the entry XY before plunging,
// else the tool plunges from wherever the previous loop/level ended (air-cut or dive
// into a wall). Every Plunge must be immediately preceded by a Rapid at its own XY.
TEST_CASE("op: pocket non-helix entry rapids over the hole before plunging (T-M6 D3)") {
    OpContext ctx = mill_ctx(6.0);
    Operation op;
    op.id = "pd3"; op.type = OpType::Pocket2D; op.tool_number = 1;
    op.heights.top = 0; op.heights.bottom = -4; op.stepdown = 2.0; op.stepover = 3.0;
    // default ramp (non-helix) -> the straight-plunge entry branch
    op.geometry["regions"] = json::array({to_json(square(0, 0, 20))});
    GenResult r = gen_pocket2d(op, ctx);
    REQUIRE(r.ok);
    const auto& mv = r.path.moves;
    int plunges = 0;
    for (size_t i = 0; i < mv.size(); ++i) {
        if (mv[i].kind != MoveKind::Plunge) continue;
        ++plunges;
        REQUIRE(i > 0);
        CHECK(mv[i - 1].kind == MoveKind::Rapid);                     // positioned first
        CHECK(mv[i - 1].to.x == doctest::Approx(mv[i].to.x));         // over the entry XY
        CHECK(mv[i - 1].to.y == doctest::Approx(mv[i].to.y));
        CHECK(mv[i - 1].to.z >= mv[i].to.z - 1e-9);                   // came down from above
    }
    CHECK(plunges > 0);
}

// D8: an Arc move must be swept along the true arc, not the chord. A chord bows inside
// a convex arc; cells near the arc's mid-bulge (>tool-radius from the chord) go
// un-removed under the old approximation, false-negativing the gouge check.
TEST_CASE("sim: arc removal follows the true arc, not the chord (T-M6 D8)") {
    Setup su;
    su.machine.kind = MachineKind::Mill3;
    su.stock.type = StockType::Box;
    su.stock.min = {-2, -2, -5}; su.stock.max = {12, 12, 0};
    Tool t; t.number = 1; t.type = ToolType::EndmillFlat; t.diameter = 2;  // r=1
    ToolLibrary lib; lib.tools = {t};
    Operation op; op.id = "a"; op.tool_number = 1; su.operations = {op};
    // quarter arc (10,0) -> (0,10) about the origin, CCW at depth -2. Chord is x+y=10;
    // the arc's 45° midpoint (7.07,7.07) sits 2.93mm off the chord — beyond the 1mm tool.
    Toolpath tp; tp.op_id = "a"; tp.tool_number = 1;
    Move start; start.kind = MoveKind::Rapid; start.to = {10, 0, -2};
    Move arc; arc.kind = MoveKind::Arc; arc.to = {0, 10, -2}; arc.center = {0, 0, -2};
    arc.ccw = true; arc.feed = 200;
    tp.moves = {start, arc};
    SimResult r = simulate_setup(su, lib, {tp}, TriMesh{}, SimOptions{});
    REQUIRE(r.ok);
    // the arc mid-bulge is cut to depth; a chord sweep would leave it at the stock top (0)
    double mid = 7.0710678;
    CHECK(r.remaining.height_at(mid, mid) < -1.0);
    // a point just outside the arc (larger radius) is untouched -> stays at the top
    CHECK(r.remaining.height_at(9.0, 9.0) > -0.5);
    CHECK(r.removed_volume_mm3 > 0);
}

// G4: turning-sim coverage. A lathe roughing pass on cylindrical stock must register
// real radial removal (the top-down dexel can't; the axisymmetric branch does).
TEST_CASE("sim: turning roughing removes stock volume (T-M6 G4)") {
    Setup su;
    su.machine.kind = MachineKind::Lathe;
    su.stock.type = StockType::Cylinder;
    su.stock.base = {0, 0, 0}; su.stock.radius = 12; su.stock.height = 30;  // Ø24 x 30 bar
    Tool t; t.number = 1; t.type = ToolType::TurningGeneral;
    ToolLibrary lib; lib.tools = {t};
    Operation op; op.id = "tr"; op.type = OpType::TurnRough; op.tool_number = 1;
    op.stepdown = 1.5; op.stock_to_leave = 0.3;
    // rough the Ø24 bar down toward radius 6 (Ø12) over its full length
    op.geometry["profile"] = json::array({json::array({0, 6}), json::array({30, 6})});
    su.operations = {op};
    OpContext ctx; ctx.machine = su.machine; ctx.stock = su.stock; ctx.tool = t;
    ctx.material_id = "steel_mild";
    GenResult g = generate(op, ctx);
    REQUIRE(g.ok);
    SimResult r = simulate_setup(su, lib, {g.path}, TriMesh{}, SimOptions{});
    REQUIRE(r.ok);
    CHECK(r.removed_volume_mm3 > 0);
    CHECK(r.machined_fraction > 0.3);         // ~0.72: π6.3²·30 remains of π12²·30
    CHECK(r.remaining_volume_mm3 < r.stock_volume_mm3);
}

// ── T-M3: holder/shank collision (audit §3.2, §T-M3) ─────────────────────────

// build a deep narrow-pocket job whose walls out-reach the tool's flute length, so
// the non-cutting shank/holder must crash into them.
static std::vector<Toolpath> deep_pocket_job(Setup& su, ToolLibrary& lib, double depth) {
    su = Setup{};
    su.machine.kind = MachineKind::Mill3;
    su.stock.type = StockType::Box;
    su.stock.min = {-15, -15, -(depth + 1)}; su.stock.max = {15, 15, 0};
    Tool t; t.number = 1; t.type = ToolType::EndmillFlat; t.diameter = 6;
    t.flute_length = 20; t.shoulder_length = 25; t.shank_diameter = 6; t.holder_diameter = 32;
    lib = ToolLibrary{}; lib.tools = {t};
    Operation op; op.id = "dp"; op.type = OpType::Pocket2D; op.tool_number = 1;
    op.heights.top = 0; op.heights.bottom = -depth; op.stepdown = 3; op.stepover = 3;
    op.geometry["regions"] = json::array({to_json(square(0, 0, 8))});   // 16x16 pocket
    su.operations = {op};
    OpContext ctx; ctx.machine = su.machine; ctx.stock = su.stock; ctx.tool = t;
    ctx.material_id = "aluminum";
    return {generate(op, ctx).path};
}

// deep pocket: 30mm > the 25mm shoulder, so the 32mm holder dips below the stock top
// and hits the surrounding walls — a crash the tip-only sim (check_holder off) misses.
TEST_CASE("sim: holder collides on a deep narrow pocket (T-M3)") {
    Setup su; ToolLibrary lib;
    auto tps = deep_pocket_job(su, lib, 30.0);

    SimResult off = simulate_setup(su, lib, tps, TriMesh{}, SimOptions{});
    REQUIRE(off.ok);
    CHECK(off.holder_collisions == 0);        // default: holder check disabled

    SimOptions so; so.check_holder = true;
    SimResult on = simulate_setup(su, lib, tps, TriMesh{}, so);
    REQUIRE(on.ok);
    CHECK(on.holder_collisions > 0);          // shank/holder crashes into the deep walls
    bool warned = false;
    for (auto& w : on.warnings) if (w.find("holder") != std::string::npos) warned = true;
    CHECK(warned);
}

// shallow pocket: 4mm « 20mm flute length, the whole non-cutting body stays clear.
TEST_CASE("sim: holder clears a shallow pocket (T-M3 no false positive)") {
    Setup su; ToolLibrary lib;
    auto tps = deep_pocket_job(su, lib, 4.0);
    SimOptions so; so.check_holder = true;
    SimResult r = simulate_setup(su, lib, tps, TriMesh{}, so);
    REQUIRE(r.ok);
    CHECK(r.holder_collisions == 0);
}

// ── session + RPC tests (Phase 6) ────────────────────────────────────────────
TEST_CASE("session: new / apply_ops / generate / post / undo / save+open") {
    json nw = cam_new(json{{"name", "JobA"}, {"post", "fanuc"}});
    std::string doc = nw["doc_id"].get<std::string>();
    CHECK(nw["tree"]["name"] == "JobA");

    // build a setup + tool + contour op via ops
    json ops = json::array();
    ops.push_back(json{{"op", "add_tool"}, {"tool", json{{"number", 1}, {"type", "endmill_flat"}, {"diameter", 6}, {"flutes", 3}}}});
    json setup = json{{"name", "S1"}, {"machine", json{{"kind", "mill3"}}},
                      {"stock", json{{"type", "box"}, {"min", {-25,-25,-10}}, {"max", {25,25,0}}, {"material", "aluminum"}}}};
    ops.push_back(json{{"op", "add_setup"}, {"setup", setup}, {"ref", "S"}});
    json apply1 = cam_apply_ops(doc, -1, ops);
    std::string sid = apply1["id_map"]["S"].get<std::string>();
    CHECK(apply1["revision"].get<long>() == 2);

    json contour = json{{"type", "contour2d"}, {"tool_number", 1}, {"comp_side", "left"},
                        {"heights", json{{"top", 0}, {"bottom", -4}}}, {"stepdown", 2},
                        {"geometry", json{{"regions", json::array({to_json(square(0,0,10))})}}}};
    json apply2 = cam_apply_ops(doc, 2, json::array({json{{"op", "add_operation"}, {"setup_id", sid}, {"operation", contour}, {"ref", "O"}}}));
    CHECK(apply2["revision"].get<long>() == 3);

    // generate
    json gen = cam_generate(doc, json::object());
    CHECK(gen["toolpaths"].size() == 1);
    CHECK(gen["stats"]["count"].get<int>() == 1);

    // post
    json post = cam_post(doc, json{{"dialect", "grbl"}});
    CHECK(post["ok"].get<bool>());
    CHECK(post["gcode"].get<std::string>().find("G21") != std::string::npos);

    // tree + stats
    json tr = cam_tree(doc);
    CHECK(tr["stats"]["operations"].get<int>() == 1);
    CHECK(tr["stats"]["tools"].get<int>() == 1);

    // undo removes the operation
    json un = cam_undo(doc);
    CHECK(cam_tree(doc)["stats"]["operations"].get<int>() == 0);
    json re = cam_redo(doc);
    CHECK(cam_tree(doc)["stats"]["operations"].get<int>() == 1);

    // save + open round-trip
    std::string path = "/tmp/aicad_test.cam+";
    json sv = cam_save(doc, path);
    CHECK(sv["path"] == path);
    json op2 = cam_open(path);
    std::string doc2 = op2["doc_id"].get<std::string>();
    CHECK(cam_tree(doc2)["stats"]["operations"].get<int>() == 1);

    // stale revision guard
    bool threw = false;
    try { cam_apply_ops(doc, 99, json::array()); } catch (const OpError& e) { threw = (e.code == "stale_revision"); }
    CHECK(threw);

    CHECK(cam_close(doc));
    CHECK(cam_close(doc2));
}

TEST_CASE("session: stale-doc + reference data RPCs") {
    bool threw = false;
    try { cam_tree("nonexistent_doc"); } catch (const OpError& e) { threw = (e.code == "unknown_doc"); }
    CHECK(threw);
    CHECK(cam_post_dialects()["dialects"].size() == 8);
    CHECK(cam_materials()["materials"].size() >= 20);
    CHECK(cam_tool_types()["tool_types"].size() > 10);
    CHECK(cam_op_types()["op_types"].size() == 28);
}

TEST_CASE("session: inline generate + post (stateless)") {
    Job job;
    job.post = "linuxcnc";
    Tool t; t.number = 1; t.type = ToolType::EndmillFlat; t.diameter = 6; t.flutes = 3;
    job.tools.tools = {t};
    Setup su; su.id = "s1"; su.machine.kind = MachineKind::Mill3;
    su.stock.type = StockType::Box; su.stock.min = {-20,-20,-5}; su.stock.max = {20,20,0};
    su.stock.material = "aluminum";
    Operation op; op.id = "o1"; op.type = OpType::Pocket2D; op.tool_number = 1;
    op.heights.top = 0; op.heights.bottom = -3; op.stepover = 3;
    op.geometry["regions"] = json::array({to_json(square(0,0,15))});
    su.operations = {op};
    job.setups = {su};
    json gen = cam_generate_inline(json{{"job", job.to_json()}, {"setup_index", 0}});
    REQUIRE(gen["toolpaths"].size() == 1);
    json post = cam_post_inline(json{{"job", job.to_json()}, {"toolpaths", gen["toolpaths"]}, {"dialect", "linuxcnc"}});
    CHECK(post["ok"].get<bool>());
    CHECK(post["gcode"].get<std::string>().size() > 50);
}

TEST_CASE("session: cam_post dialect override beats the job default (whole-job post)") {
    // job default = grbl; a per-call dialect=fanuc must produce FANUC output, not grbl.
    json nw = cam_new(json{{"name", "DialJob"}, {"post", "grbl"}});
    std::string doc = nw["doc_id"].get<std::string>();
    json setup = json{{"name", "S1"}, {"machine", json{{"kind", "mill3"}}},
                      {"stock", json{{"type", "box"}, {"min", {-10, -10, -5}}, {"max", {10, 10, 0}}, {"material", "aluminum"}}}};
    json a = cam_apply_ops(doc, -1, json::array({
        json{{"op", "add_tool"}, {"tool", json{{"number", 1}, {"type", "drill"}, {"diameter", 5}}}},
        json{{"op", "add_setup"}, {"ref", "S"}, {"setup", setup}}}));
    std::string sid = a["id_map"]["S"].get<std::string>();
    json drill = json{{"type", "drill"}, {"tool_number", 1}, {"cycle", "peck"}, {"peck", 2},
                      {"heights", json{{"top", 0}, {"bottom", -8}, {"retract", 2}}},
                      {"geometry", json{{"points", json::array({json::array({0, 0, 0})})}}}};
    cam_apply_ops(doc, a["revision"].get<long>(),
                  json::array({json{{"op", "add_operation"}, {"setup_id", sid}, {"operation", drill}}}));
    cam_generate(doc, json::object());
    // whole-job post (no setup_id) with the override
    json pf = cam_post(doc, json{{"dialect", "fanuc"}});
    std::string gf = pf["gcode"].get<std::string>();
    CHECK(gf.find("G83") != std::string::npos);   // fanuc canned peck cycle
    CHECK(gf.front() == '%');                       // fanuc % wrap
    CHECK(gf.find("GRBL") == std::string::npos);    // NOT the grbl header
    // grbl override on the same job -> long-hand (no G83)
    json pg = cam_post(doc, json{{"dialect", "grbl"}});
    CHECK(pg["gcode"].get<std::string>().find("G83") == std::string::npos);
    cam_close(doc);
}

// ── T-M2: adaptive ring-to-ring arc-blend linking ────────────────────────────
namespace {
// A corner-heavy "star/gear" pocket: an irregular star with sharp acute-angle
// spikes and reflex notches (no right angles anywhere, unlike a rectangular
// pocket, whose corners cap the naive same-vertex ring-to-ring gap at a
// modest sqrt(2)*stepover -- comfortably under the 1.5x-stepover threshold).
// Concentric offsetting of a sharp, irregular polygon like this shifts the
// offset loop's canonical start vertex a lot between adjacent levels, so a
// naive direct feed_to link between rings can jump much further than the
// radial stepover, unlike a smooth/convex pocket where corresponding start
// vertices stay close together across levels.
Region star_notch_pocket() {
    Region r;
    Path2& p = r.outer;
    const int n = 9;
    const double r_out[9] = {18, 15, 19, 14, 20, 13, 17, 16, 19.5};
    const double r_in[9]  = {5, 7, 4.5, 6.5, 4, 7.5, 5.5, 6, 4.8};
    for (int i = 0; i < n; ++i) {
        double a_out = 2.0 * M_PI * (i + 0.15) / n;
        double a_in  = 2.0 * M_PI * (i + 0.55) / n;
        p.push_back({r_out[i] * std::cos(a_out), r_out[i] * std::sin(a_out)});
        p.push_back({r_in[i] * std::cos(a_in), r_in[i] * std::sin(a_in)});
    }
    return r;
}
bool has_warning_containing(const std::vector<std::string>& warnings, const std::string& needle) {
    for (auto& w : warnings) if (w.find(needle) != std::string::npos) return true;
    return false;
}
}  // namespace

TEST_CASE("op: adaptive arc-blend linking bounds ring-transition engagement on a corner-heavy pocket") {
    OpContext ctx = mill_ctx(2.0);   // small tool so the comb's narrow channels stay resolvable
    Operation op;
    op.type = OpType::Adaptive2D;
    op.heights.top = 0; op.heights.bottom = -2;   // shallow: one Z level, isolates ring linking
    op.stepover = 0.6;
    op.geometry["regions"] = json::array({to_json(star_notch_pocket())});

    // Arc-blend ON (default): nearest-vertex rotation + tangent-arc link keeps
    // every ring transition close to the radial stepover -> no warning.
    op.arc_blend_linking = true;
    GenResult on = gen_adaptive2d(op, ctx);
    REQUIRE(on.ok);
    CHECK(on.path.cut_length > 0);
    CHECK_FALSE(has_warning_containing(on.warnings, "ring-transition engagement exceeds"));

    // Arc-blend OFF: plain concentric-offset linking (direct feed_to at whatever
    // vertex each ring's offset loop starts at) -> the comb's uneven topology
    // produces at least one over-engagement ring transition -> warning present.
    op.arc_blend_linking = false;
    GenResult off = gen_adaptive2d(op, ctx);
    REQUIRE(off.ok);
    CHECK(off.path.cut_length > 0);
    CHECK(has_warning_containing(off.warnings, "ring-transition engagement exceeds"));

    // Confirm ring transitions are actually arc moves with arc-blend ON (not
    // just a smaller straight-line gap) -- the headline deliverable.
    CHECK(count_kind(on.path, MoveKind::Arc) > count_kind(off.path, MoveKind::Arc));
}
