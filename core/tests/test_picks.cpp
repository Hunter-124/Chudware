// A2 milestone verification: the fillet/chamfer feature compilers now accept an EDGE
// SELECTOR (a selector json dict, or a json array of them) that resolves against the
// body's live B-rep topology — the doc-model port of the Python oracle's two-stage
// edge resolution (engine/doc/features/dress.py). This is the test that proves the
// selector -> kernel-edge mapping actually drives a real fillet, and that a DANGLING
// selector degrades to a per-feature error status without crashing the rebuild.
//
// It follows the EXACT round-trip discipline of test_document.cpp: build a reference
// box, harvest real edge selectors off its topology (describe_edges + make_selector),
// embed them in a fillet feature, compile the document, parse the resulting AICM with
// the HOST loader (host/src/render/aicm_loader = the contract oracle), and assert
// TIGHT, ANALYTIC, OCCT-version-INDEPENDENT invariants (cylindrical-face count, bbox
// extents) plus the CompileResult bookkeeping (status ok/error, surviving body_count).
// The assertions ARE the product — they are NOT weakened to pass.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <cstdint>
#include <limits>
#include <set>
#include <string>
#include <tuple>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/document.h"
#include "doc/picks.h"
#include "doc/selectors.h"
#include "geom/booleans.h"  // cut (build a tube for the inner/outer test)
#include "geom/features.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

using json = nlohmann::json;
using aicad::doc::CompileResult;
using aicad::doc::compile_document;

namespace {

// ---- sketch DSL (copied from test_document.cpp) ----------------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs,
         double value, const json& expr = json(nullptr)) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    json val = json{{"value", value}, {"expr", expr}};
    return json{{"id", id}, {"type", type}, {"refs", r}, {"value", val}, {"driven", false}};
}

struct RectSketch {
    json entities;
    json constraints;
};
RectSketch rect_entities(double x0, double y0, double w, double h,
                         const json& w_expr = json(nullptr),
                         const json& h_expr = json(nullptr)) {
    RectSketch s;
    s.entities = json::array({
        line("e1", x0, y0, x0 + w, y0),
        line("e2", x0 + w, y0, x0 + w, y0 + h),
        line("e3", x0 + w, y0 + h, x0, y0 + h),
        line("e4", x0, y0 + h, x0, y0),
    });
    s.constraints = json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", "distance", {"e1.p1", "e1.p2"}, w, w_expr),
        dim("c11", "distance", {"e2.p1", "e2.p2"}, h, h_expr),
    });
    return s;
}

json sketch_feature(const char* id, const char* plane, const RectSketch& rs) {
    return json{{"id", id}, {"type", "sketch"}, {"plane", plane},
                {"entities", rs.entities}, {"constraints", rs.constraints}};
}

// ---- AICM inspection helpers (identical contract to test_document.cpp) ------
void check_common(const render::BodyMesh& b) {
    CHECK(b.pos.size() == 3ull * b.n_verts);
    CHECK(b.nrm.size() == 3ull * b.n_verts);
    CHECK(b.idx.size() == 3ull * b.n_tris);
    for (uint32_t i : b.idx) CHECK(i < b.n_verts);
    uint32_t running = 0;
    for (const render::FaceRange& f : b.faces) {
        CHECK(f.first_tri == running);
        running += f.tri_count;
    }
    CHECK(running == b.n_tris);
}

void bbox(const render::BodyMesh& b, double lo[3], double hi[3]) {
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        for (int k = 0; k < 3; ++k) {
            double c = b.pos[3ull * v + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    }
}

int count_surf(const render::BodyMesh& b, uint8_t surf_type) {
    int n = 0;
    for (const render::FaceRange& f : b.faces)
        if (f.surf_type == surf_type) ++n;
    return n;
}

// Rough centroid (mean of unique vertices) of all faces of a given surf_type. Used to
// check WHERE a feature landed — e.g. a bore's cylindrical wall verts average to its axis.
bool surf_centroid(const render::BodyMesh& b, uint8_t surf_type, double out[3]) {
    std::set<uint32_t> verts;
    for (const render::FaceRange& f : b.faces) {
        if (f.surf_type != surf_type) continue;
        for (uint32_t t = 0; t < f.tri_count; ++t)
            for (int j = 0; j < 3; ++j)
                verts.insert(b.idx[3ull * (f.first_tri + t) + j]);
    }
    if (verts.empty()) return false;
    double sum[3] = {0, 0, 0};
    for (uint32_t vi : verts)
        for (int k = 0; k < 3; ++k) sum[k] += b.pos[3ull * vi + k];
    for (int k = 0; k < 3; ++k) out[k] = sum[k] / static_cast<double>(verts.size());
    return true;
}

// A corner box [0,L]x[0,W]x[0,H] (centered:false) — its top face sits at z=H, centroid
// (L/2, W/2, H), so a sketch/hole on "the top face" has a known, checkable world position.
json corner_box(const char* id, double L, double W, double H) {
    return json{{"id", id}, {"type", "box"}, {"length", L}, {"width", W},
                {"height", H}, {"centered", false}};
}

render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

// Harvest the 4 VERTICAL edge selectors of a reference box [0,W]x[0,H]x[0,D].
// describe_edges enumerates each unique edge twice (one descriptor per incident face),
// so we filter to edges whose axis is +/-Z and dedup by rounded centroid to exactly 4.
json vertical_edge_selectors(double W, double H, double D) {
    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<aicad::doc::TopoInfo> infos = aicad::doc::describe_edges(box);

    json sels = json::array();
    std::set<std::tuple<long long, long long, long long>> seen;
    for (const aicad::doc::TopoInfo& info : infos) {
        if (!info.axis.has_value()) continue;
        if (std::abs(info.axis->z()) < 0.999) continue;  // not a vertical (+/-Z) edge
        // Dedup by rounded centroid (the two duplicate occurrences share geometry).
        auto key = std::make_tuple(
            static_cast<long long>(std::llround(info.centroid.x() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.y() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.z() * 1e4)));
        if (!seen.insert(key).second) continue;
        sels.push_back(aicad::doc::make_selector(info));
    }
    return sels;
}

}  // namespace

// (a) FILLET BY SELECTOR (happy path). A reference box's 4 vertical edge selectors are
//     harvested off its topology, then fed to a fillet feature on a sketch+extrude that
//     rebuilds the SAME box. The fillet must resolve all 4 selectors to real kernel
//     edges and round them: the result carries >= 4 cylindrical quarter-corner faces,
//     the extrude is consumed (one surviving body), and the outer bbox is unchanged.
TEST_CASE("fillet by edge selector: 4 vertical picks -> rounded corners survive") {
    const double W = 40.0, H = 30.0, D = 20.0, R = 5.0;

    json edge_sels = vertical_edge_selectors(W, H, D);
    REQUIRE(edge_sels.size() == 4);  // exactly the 4 vertical edges, de-duplicated

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        json{{"id", "f1"}, {"type", "fillet"}, {"body", "x1"}, {"radius", R},
             {"edges", edge_sels}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // extrude consumed by the fillet
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("fillet-by-selector: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " cylinder=" << count_surf(b, 1)
            << " planar=" << count_surf(b, 0)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(count_surf(b, 1) >= 4);  // 4 rounded vertical corners
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// (b) DANGLING SELECTOR (happy path). The same sketch+extrude, but the fillet carries a
//     SINGLE edge selector whose centroid sits far outside the body ([1000,1000,1000]).
//     No unique body edge lies within tolerance of it, so the selector dangles: the
//     fillet throws (per-feature error status), result.ok==false, but the rebuild NEVER
//     crashes. The fillet threw BEFORE consuming the extrude, so the extrude body
//     survives unchanged — a plain box (no cylindrical faces, exact W x H x D bbox).
TEST_CASE("dangling edge selector: fillet errors, extrude body survives as a box") {
    const double W = 40.0, H = 30.0, D = 20.0, R = 5.0;

    // A selector pointing at a non-existent edge far outside the body.
    json far_sel = json{
        {"kind", "edge"},
        {"surf", "line"},
        {"centroid", json::array({1000.0, 1000.0, 1000.0})},
        {"axis", json::array({0.0, 0.0, 1.0})},
        {"area_or_len", 20.0},
        {"owner_feature", nullptr},
        {"hint_index", 0},
    };

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, W, H)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", D}},
        json{{"id", "f1"}, {"type", "fillet"}, {"body", "x1"}, {"radius", R},
             {"edges", json::array({far_sel})}},
    });

    CompileResult r = compile_document(doc, json::object());
    CHECK_FALSE(r.ok);  // the dangling fillet is a contained error
    REQUIRE(r.statuses.size() == 3);

    // Locate the fillet feature's status: !ok with a non-empty error mentioning dangling.
    bool found_fillet = false;
    for (const auto& s : r.statuses) {
        if (s.id == "f1") {
            found_fillet = true;
            CHECK_FALSE(s.ok);
            CHECK_FALSE(s.error.empty());
            CHECK(s.error.find("dangling") != std::string::npos);
        }
    }
    CHECK(found_fillet);

    // The fillet threw before consuming the extrude, so the plain box survives.
    REQUIRE(r.body_count == 1);
    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("dangling: body_count=" << r.body_count << " (plain box survived)"
            << " cylinder=" << count_surf(b, 1)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));

    CHECK(count_surf(b, 1) == 0);  // no fillet applied -> still a plain box
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

// (c) GEOMETRY-QUERY VOCABULARY. The plain-language predicates a spatially-limited model
//     reaches for — direction words (top/front/..), surf words (flat/round), parallel axis
//     words, and size superlatives (largest/longest) — must resolve to the SAME kernel
//     sub-shapes as the precise signed-axis/coordinate forms. We query a reference box
//     directly (resolve_geom_query takes a TopoDS_Shape; no compile round-trip needed) and
//     assert analytic counts PLUS the centroid ORDERING that proves "top" is really the +Z
//     face, "front" the -Y face, etc. — matching the host's named-view convention.
TEST_CASE("geom query: direction/surf/size WORDS resolve like the precise forms") {
    using aicad::doc::resolve_geom_query;
    using aicad::doc::QueryPicks;
    const double DX = 40.0, DY = 30.0, DZ = 20.0;  // X longest, Z shortest
    TopoDS_Shape box = geom::make_box(DX, DY, DZ);

    auto q  = [&](json query) { return resolve_geom_query(box, query); };
    auto cx = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][0].get<double>(); };
    auto cy = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][1].get<double>(); };
    auto cz = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][2].get<double>(); };

    // Baselines: a box has 6 faces / 12 edges, all faces planar, none round.
    CHECK(q(json{{"of", "face"}}).faces.size() == 6);
    CHECK(q(json{{"of", "edge"}}).edges.size() == 12);
    CHECK(q(json{{"of", "face"}, {"surf", "flat"}}).faces.size() == 6);
    CHECK(q(json{{"of", "face"}, {"surf", "round"}}).faces.size() == 0);
    CHECK(q(json{{"of", "edge"}, {"surf", "straight"}}).edges.size() == 12);

    // Direction WORD resolves identically to the signed-axis form, and "top" is really +Z.
    QueryPicks top_w = q(json{{"of", "face"}, {"side", "top"}});
    QueryPicks top_a = q(json{{"of", "face"}, {"side", "+z"}});
    QueryPicks bot_w = q(json{{"of", "face"}, {"side", "bottom"}});
    REQUIRE(top_w.faces.size() == 1);
    REQUIRE(top_a.faces.size() == 1);
    REQUIRE(bot_w.faces.size() == 1);
    CHECK(cz(top_w, 0) == doctest::Approx(cz(top_a, 0)));  // word == axis
    CHECK(cz(top_w, 0) > cz(bot_w, 0));                    // top higher than bottom

    // front=-Y, back=+Y, right=+X, left=-X (named-view convention from viewport_panel.cpp).
    CHECK(cy(q(json{{"of", "face"}, {"side", "front"}}), 0) <
          cy(q(json{{"of", "face"}, {"side", "back"}}), 0));
    CHECK(cx(q(json{{"of", "face"}, {"side", "right"}}), 0) >
          cx(q(json{{"of", "face"}, {"side", "left"}}), 0));

    // Top rim = 4 edges; the vertical (parallel:"z") edges = the 4 corners of the block.
    CHECK(q(json{{"of", "edge"}, {"side", "top"}}).edges.size() == 4);
    CHECK(q(json{{"of", "edge"}, {"parallel", "z"}}).edges.size() == 4);

    // Size superlatives. Longest edges run along X (len 40) -> 4; shortest along Z (20) -> 4.
    CHECK(q(json{{"of", "edge"}, {"pick", "longest"}}).edges.size() == 4);
    CHECK(q(json{{"of", "edge"}, {"pick", "shortest"}}).edges.size() == 4);
    // Largest face area = X*Y; top+bottom tie -> 2. Pin to one by combining with a side.
    CHECK(q(json{{"of", "face"}, {"pick", "largest"}}).faces.size() == 2);
    CHECK(q(json{{"of", "face"}, {"pick", "largest"}, {"side", "top"}}).faces.size() == 1);

    // Case-insensitive vocabulary.
    CHECK(q(json{{"of", "face"}, {"side", "TOP"}}).faces.size() == 1);

    // Unknown words are TEACHING errors (non-empty .error, no geometry touched), not silent
    // empty results — so a confused model gets told the valid vocabulary.
    CHECK_FALSE(q(json{{"of", "face"}, {"side", "sideways"}}).error.empty());
    CHECK_FALSE(q(json{{"of", "face"}, {"surf", "blobby"}}).error.empty());
    CHECK_FALSE(q(json{{"of", "face"}, {"pick", "hugest"}}).error.empty());
}

// (d) SKETCH ON A NAMED FACE (Layer 3). A sketch whose plane is a word query
//     {"of":"face","side":"top"} must lift onto that face's own frame (origin at the face
//     centroid), NOT the base datum. We drop a 10x10 boss on the top of a 40x30x20 corner
//     box (top face at z=20) and extrude it 5mm: the boss grows the body to z=25. Had the
//     sketch landed on plane.top (z=0) the boss would be buried inside and height stay 20 —
//     so the bbox height is the discriminating assertion.
TEST_CASE("sketch on a named face: boss grows from the top face, not the base plane") {
    RectSketch rs = rect_entities(-5.0, -5.0, 10.0, 10.0);  // 10x10 centred on the face
    json doc;
    doc["features"] = json::array({
        corner_box("b1", 40.0, 30.0, 20.0),
        json{{"id", "s2"}, {"type", "sketch"},
             {"plane", json{{"of", "face"}, {"side", "top"}}},
             {"entities", rs.entities}, {"constraints", rs.constraints}},
        json{{"id", "x2"}, {"type", "extrude"}, {"profile", "s2"},
             {"mode", "boss"}, {"depth", 5.0}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("sketch-on-face: bbox z=" << lo[2] << ".." << hi[2]);
    CHECK(hi[2] == doctest::Approx(25.0).epsilon(1e-3));  // boss sat ON the z=20 face
    CHECK(lo[2] == doctest::Approx(0.0).epsilon(1e-3));
}

// (e) BOLT_CIRCLE intent feature (Layer 4). One feature drills a ring of holes around the
//     top-face centroid — no per-hole coordinates. Four 4mm holes on a 20mm bolt circle
//     through a 40x30x20 box: the outer box is unchanged and >=4 cylindrical bore walls
//     appear. (If the ring fell off the face the material-removed guard would fail compile.)
TEST_CASE("bolt_circle: a ring of holes drilled around the top-face centroid") {
    json doc;
    doc["features"] = json::array({
        corner_box("b1", 40.0, 30.0, 20.0),
        json{{"id", "bc"}, {"type", "bolt_circle"}, {"diameter", 4.0},
             {"count", 4}, {"pcd", 20.0}, {"end", "through_all"}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);
    REQUIRE(r.body_count == 1);

    render::DocMesh dm = parse(r);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);
    double lo[3], hi[3];
    bbox(b, lo, hi);
    MESSAGE("bolt_circle: cyl_faces=" << count_surf(b, 1) << " bbox="
            << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2]));
    CHECK(count_surf(b, 1) >= 4);                                    // four bore walls
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-3));   // outer box intact
    CHECK((hi[1] - lo[1]) == doctest::Approx(30.0).epsilon(1e-3));
    CHECK((hi[2] - lo[2]) == doctest::Approx(20.0).epsilon(1e-3));
}

// (f) RELATIONAL `at` placement (Layer 2). A primitive's `at` may be a word query instead
//     of [x,y,z]: the primitive centres on the matched face/edge centroid. A cut cylinder
//     placed at {"of":"face","side":"top"} bores straight down the top-face centre — its
//     cylindrical wall must sit at (20,15), the top-face centroid of the box.
TEST_CASE("relational at: a cut cylinder centres on the queried top face") {
    json doc;
    doc["features"] = json::array({
        corner_box("b1", 40.0, 30.0, 20.0),
        json{{"id", "c1"}, {"type", "cylinder"}, {"radius", 3.0}, {"height", 60.0},
             {"mode", "cut"}, {"at", json{{"of", "face"}, {"side", "top"}}}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    double c[3];
    REQUIRE(surf_centroid(b, 1, c));  // the cylindrical bore wall
    MESSAGE("relational-at: bore centroid=(" << c[0] << "," << c[1] << "," << c[2] << ")");
    CHECK(c[0] == doctest::Approx(20.0).epsilon(0.05));  // top-face centroid X
    CHECK(c[1] == doctest::Approx(15.0).epsilon(0.05));  // top-face centroid Y
}

// (g) INNER/OUTER (radius pick). A tube — outer cylinder R=20 with a coaxial R=12 bore —
//     has TWO cylindrical walls that surf:"round" alone cannot tell apart. pick:"outer"
//     keeps the R=20 wall, pick:"inner" the R=12 wall (verified by wall area, which grows
//     with radius). The top rim likewise has an outer + inner circular edge. A radius pick
//     on a body with no round geometry is a teaching error, not a silent empty.
TEST_CASE("geom query: inner/outer tells the coaxial walls of a tube apart") {
    using aicad::doc::resolve_geom_query;
    using aicad::doc::QueryPicks;
    const double RO = 20.0, RI = 12.0, H = 50.0;
    TopoDS_Shape tube = geom::cut(geom::make_cylinder_centered(RO, H),
                                  geom::make_cylinder_centered(RI, H + 10.0));
    auto q = [&](json query) { return resolve_geom_query(tube, query); };
    auto area = [](const QueryPicks& p) { return p.selectors.at(0)["area_or_len"].get<double>(); };

    CHECK(q(json{{"of", "face"}, {"surf", "round"}}).faces.size() == 2);  // both walls
    QueryPicks outer = q(json{{"of", "face"}, {"surf", "round"}, {"pick", "outer"}});
    QueryPicks inner = q(json{{"of", "face"}, {"surf", "round"}, {"pick", "inner"}});
    REQUIRE(outer.faces.size() == 1);
    REQUIRE(inner.faces.size() == 1);
    MESSAGE("tube walls: outer area=" << area(outer) << " inner area=" << area(inner));
    CHECK(area(outer) > area(inner));                       // R=20 wall vs R=12 wall
    CHECK(area(outer) == doctest::Approx(2 * 3.14159265 * RO * H).epsilon(0.02));
    CHECK(area(inner) == doctest::Approx(2 * 3.14159265 * RI * H).epsilon(0.02));

    // "bore" is an alias for inner; the rim's inner circular edge is the smaller circle.
    CHECK(q(json{{"of", "face"}, {"surf", "round"}, {"pick", "bore"}}).faces.size() == 1);
    QueryPicks rim_in = q(json{{"of", "edge"}, {"surf", "round"}, {"pick", "inner"}});
    QueryPicks rim_out = q(json{{"of", "edge"}, {"surf", "round"}, {"pick", "outer"}});
    REQUIRE(rim_in.edges.size() >= 1);
    REQUIRE(rim_out.edges.size() >= 1);
    CHECK(area(rim_out) > area(rim_in));  // outer circumference > inner

    // A radius pick with no round geometry to rank is a teaching error.
    TopoDS_Shape box = geom::make_box(30.0, 30.0, 30.0);
    CHECK_FALSE(resolve_geom_query(box, json{{"of", "face"}, {"pick", "outer"}}).error.empty());
}
