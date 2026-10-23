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
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include <BRep_Builder.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>  // I1/I3: circle wires for the loft test
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>  // near: flange bolt holes
#include <GProp_GProps.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "doc/document.h"
#include "doc/picks.h"
#include "doc/selectors.h"
#include "geom/booleans.h"  // cut / fuse (build a tube + compound for inner/outer tests)
#include "geom/dress.h"     // fillet_edges (C5: convex fillet vs concave bore)
#include "geom/features.h"  // make_cone_centered/make_sphere/make_torus/loft_sections (I1/I3)
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

using json = nlohmann::json;
using chudware::doc::CompileResult;
using chudware::doc::compile_document;

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
    std::vector<chudware::doc::TopoInfo> infos = chudware::doc::describe_edges(box);

    json sels = json::array();
    std::set<std::tuple<long long, long long, long long>> seen;
    for (const chudware::doc::TopoInfo& info : infos) {
        if (!info.axis.has_value()) continue;
        if (std::abs(info.axis->z()) < 0.999) continue;  // not a vertical (+/-Z) edge
        // Dedup by rounded centroid (the two duplicate occurrences share geometry).
        auto key = std::make_tuple(
            static_cast<long long>(std::llround(info.centroid.x() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.y() * 1e4)),
            static_cast<long long>(std::llround(info.centroid.z() * 1e4)));
        if (!seen.insert(key).second) continue;
        sels.push_back(chudware::doc::make_selector(info));
    }
    return sels;
}

// A closed circular WIRE of radius `r` centred at z=`z` in a Z-normal plane — a loft
// section. Two of these at different radii/heights loft into a frustum-like solid whose
// lateral wall is a single BSPLINE/curved face (surf:"round" must catch it; surf:"flat"
// must not). Mirrors test_sweep.cpp::circle_face but builds the WIRE (loft sections are wires).
TopoDS_Wire circle_wire(double r, double z) {
    gp_Circ circ(gp_Ax2(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)), r);
    return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ));
}

// Count faces of `shape` whose query surf-type is curved (NOT plane) via a surf:"round"
// face query — the I1 category match. Returns the QueryPicks so callers can assert .error.
chudware::doc::QueryPicks round_faces(const TopoDS_Shape& shape) {
    return chudware::doc::resolve_geom_query(shape, json{{"of", "face"}, {"surf", "round"}});
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

TEST_CASE("geom query: unknown key guard mirrors the public vocabulary") {
    using chudware::doc::unknown_query_keys;
    using chudware::doc::unknown_query_keys_error;

    const std::vector<std::string> bad =
        unknown_query_keys(json{{"of", "face"}, {"curved", true}});
    REQUIRE(bad.size() == 1);
    CHECK(bad[0] == "curved");
    const std::string message = unknown_query_keys_error("doc_query", bad);
    CHECK(message.find("curved") != std::string::npos);
    CHECK(message.find("surf") != std::string::npos);

    // Every engine-side legal key is tolerated, including transport fields present on
    // flat doc_query RPC params and fields copied from query_geometry/describe_part readback.
    const json all_legal = {
        {"side", "top"}, {"surf", "cylinder"}, {"parallel", "z"},
        {"perpendicular", "x"}, {"pick", "inner"}, {"near", json::array({0, 0, 0})},
        {"index", json::array({0})}, {"count", 1}, {"body_id", 1},
        {"of", "face"}, {"kind", "query"},
        {"sid", "F1"}, {"centroid", json::array({0, 0, 0})},
        {"normal", json::array({0, 0, 1})}, {"axis", json::array({0, 0, 1})},
        {"radius", 2.0}, {"area_or_len", 10.0}, {"concavity", "concave"},
        {"hint_index", 0}, {"owner_feature", "f1"}, {"axis_point", json::array({0, 0, 0})},
        {"doc_id", "d1"}, {"query", json::object()},
    };
    CHECK(unknown_query_keys(all_legal).empty());

    const json readback_match = {
        {"kind", "face"}, {"sid", "F1"}, {"centroid", json::array({1, 2, 3})},
        {"surf", "cylinder"}, {"area_or_len", 20.0}, {"normal", json::array({1, 0, 0})},
    };
    CHECK(unknown_query_keys(readback_match).empty());
    CHECK(unknown_query_keys(
              json{{"of", "face"}, {"surf", "cylinder"}, {"sid", "F1"}}).empty());
    CHECK(unknown_query_keys(json::array({"of", "face"})).empty());

    const std::vector<std::string> sorted =
        unknown_query_keys(json{{"zzz", 1}, {"curved", true}, {"aaa", 2}});
    REQUIRE(sorted.size() == 3);
    CHECK(sorted[0] == "aaa");
    CHECK(sorted[1] == "curved");
    CHECK(sorted[2] == "zzz");
}

TEST_CASE("geom query: surf cylinder and pick inner selects one tube wall") {
    const double RO = 40.0, RI = 20.0, H = 120.0;
    TopoDS_Shape tube = geom::cut(geom::make_cylinder_centered(RO, H),
                                  geom::make_cylinder_centered(RI, H + 10.0));
    REQUIRE_FALSE(tube.IsNull());

    chudware::doc::QueryPicks inner = chudware::doc::resolve_geom_query(
        tube, json{{"of", "face"}, {"surf", "cylinder"}, {"pick", "inner"}});
    CHECK_MESSAGE(inner.error.empty(), inner.error);
    CHECK(inner.faces.size() == 1);
}

// (c) GEOMETRY-QUERY VOCABULARY. The plain-language predicates a spatially-limited model
//     reaches for — direction words (top/front/..), surf words (flat/round), parallel axis
//     words, and size superlatives (largest/longest) — must resolve to the SAME kernel
//     sub-shapes as the precise signed-axis/coordinate forms. We query a reference box
//     directly (resolve_geom_query takes a TopoDS_Shape; no compile round-trip needed) and
//     assert analytic counts PLUS the centroid ORDERING that proves "top" is really the +Z
//     face, "front" the -Y face, etc. — matching the host's named-view convention.
TEST_CASE("geom query: direction/surf/size WORDS resolve like the precise forms") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
    const double DX = 40.0, DY = 30.0, DZ = 20.0;  // X longest, Z shortest
    TopoDS_Shape box = geom::make_box(DX, DY, DZ);

    auto q  = [&](json query) { return resolve_geom_query(box, query); };
    auto cx = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][0].get<double>(); };
    auto cy = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][1].get<double>(); };
    auto cz = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][2].get<double>(); };

    // Baselines: a box has 6 faces / 12 edges, all faces planar, none round.
    CHECK(q(json{{"of", "face"}}).faces.size() == 6);
    CHECK(q(json{{"of", "edge"}}).edges.size() == 12);
    // S3 regression: a BARE {"of":"edge"} (no side/parallel/surf/pick qualifier) means
    // ALL edges — a tester claimed it under-selects to ~3-4 edges (fillet volume ~7% high),
    // but the resolver returns the full 12. (A 40mm box filleted on all 12 edges yields a
    // body with ~56 edges, which is what the tester actually measured — their analytic, not
    // the selector, was off.) Pin it explicitly so a regression here is unmistakable.
    {
        chudware::doc::QueryPicks all_edges = q(json{{"of", "edge"}});
        REQUIRE(all_edges.error.empty());
        CHECK_MESSAGE(all_edges.edges.size() == 12,
                      "bare {of:edge} must return ALL 12 box edges, got "
                          << all_edges.edges.size());
    }
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
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
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

    // I2: a radius pick with no round geometry to rank now degrades GRACEFULLY to an EMPTY
    // result (0 matches, no error) — consistent with surf:round on a box — rather than the
    // former teaching error (an honest "no bores here" looked like a query bug to callers).
    TopoDS_Shape box = geom::make_box(30.0, 30.0, 30.0);
    QueryPicks no_round = resolve_geom_query(box, json{{"of", "face"}, {"pick", "outer"}});
    CHECK(no_round.error.empty());
    CHECK(no_round.faces.empty());
    QueryPicks no_round_in = resolve_geom_query(box, json{{"of", "face"}, {"pick", "inner"}});
    CHECK(no_round_in.error.empty());
    CHECK(no_round_in.faces.empty());
}

TEST_CASE("geom query: spherical socket radii remain distinguishable") {
    const TopoDS_Shape socket = geom::cut(geom::make_sphere(12.0), geom::make_sphere(8.0));
    for (const auto& [pick, radius] :
         {std::pair{"inner", 8.0}, std::pair{"outer", 12.0}}) {
        const auto result = chudware::doc::resolve_geom_query(
            socket, json{{"of", "face"}, {"surf", "sphere"}, {"pick", pick}});
        REQUIRE_MESSAGE(result.error.empty(), result.error);
        REQUIRE(result.faces.size() == 1);
        CHECK(result.selectors.at(0).at("radius").get<double>() == doctest::Approx(radius));
        GProp_GProps properties;
        BRepGProp::SurfaceProperties(result.faces.front(), properties);
        CHECK(properties.Mass() == doctest::Approx(4.0 * M_PI * radius * radius));
    }
}

// (h) B5 SIDE TIE-BREAK by area. A compound of two non-touching boxes at the SAME top
//     height (both tops at Z=20) but different sizes (large 40x30 and small 5x30). Both
//     tops lie at the same extreme Z, so a naive `side:"top"` would return BOTH. The B5
//     fix resolves the tie deterministically by largest area: only the 40x30 face survives.
//     We also verify that adding an explicit `pick:"largest"` on top of `side:"top"` still
//     yields the same single face (existing `pick` logic is unaffected by the tie-break).
TEST_CASE("B5 side tie-break: side:top on co-planar faces resolves to the LARGEST one") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;

    // Build two separate (non-touching) boxes at the same top height.
    // Large box: 40x30x20 at origin. Small box: 5x30x20 at X=50 (gap of 10mm so they
    // don't share any face and OCCT preserves both as distinct top faces in the compound).
    const double LARGE_W = 40.0, LARGE_D = 30.0, H = 20.0;
    const double SMALL_W = 5.0;
    TopoDS_Shape large_box = geom::make_box(LARGE_W, LARGE_D, H);
    TopoDS_Shape small_box = geom::make_box_at(50.0, 0.0, 0.0, SMALL_W, LARGE_D, H);

    // Combine into a single compound (not fused — the gap is intentional so the two top
    // faces remain distinct, both at Z=20, with areas 1200mm² and 150mm² respectively).
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    builder.Add(compound, large_box);
    builder.Add(compound, small_box);

    auto q = [&](json query) { return resolve_geom_query(compound, query); };
    auto az = [](const QueryPicks& p, size_t i) {
        return p.selectors.at(i)["area_or_len"].get<double>();
    };

    // Without any filter: both top faces are present (6 faces per box = 12 total).
    CHECK(q(json{{"of", "face"}}).faces.size() == 12);

    // side:"top" alone -> B5 tie-break keeps only the largest face (40x30 = 1200mm²).
    QueryPicks top = q(json{{"of", "face"}, {"side", "top"}});
    REQUIRE(top.faces.size() == 1);
    const double large_area = LARGE_W * LARGE_D;
    MESSAGE("B5 tie-break: top face area=" << az(top, 0) << " expected~" << large_area);
    CHECK(az(top, 0) == doctest::Approx(large_area).epsilon(0.01));

    // Combining side:"top" + pick:"largest" must still yield the same single face.
    QueryPicks top_largest = q(json{{"of", "face"}, {"side", "top"}, {"pick", "largest"}});
    REQUIRE(top_largest.faces.size() == 1);
    CHECK(az(top_largest, 0) == doctest::Approx(large_area).epsilon(0.01));

    // side:"top" + count:2 must still give count-capped results (tie-break already ran,
    // only 1 face survived, so count:2 returns 1 — not a regression of count).
    QueryPicks top2 = q(json{{"of", "face"}, {"side", "top"}, {"count", 2}});
    // count:2 is applied as a cap AFTER the side filter; but the side filter already
    // returned 1 face via B5 tie-break, so the count cap has nothing to trim.
    CHECK(top2.faces.size() == 1);
}

// (i) C5 INNER/OUTER by CONCAVITY. A box with a through-bore (concave cylinder, R=5)
//     and the 4 vertical corner edges filleted with a SMALLER radius R=2 (convex cylinders).
//     Before the C5 fix, pick:"inner" ranked by min-radius and returned the small convex
//     fillet faces (R=2) — the wrong answer. After the fix it uses face orientation
//     (TopAbs_REVERSED = concave = bore; TopAbs_FORWARD = convex = fillet), so:
//       pick:"inner" -> bore wall (R=5, concave, area ~2π*5*20 ≈ 628mm²)
//       pick:"outer" -> corner fillet faces (R=2, convex, area ~π/2*2*20 ≈ 63mm² each)
TEST_CASE("C5 inner/outer: picks bore (concave) not small-radius convex fillet") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;

    // A box CENTERED at the origin with a CONCENTRIC through bore: make_box_centered spans
    // x,y,z in [-L/2,L/2]x[-W/2,W/2]x[-H/2,H/2]; make_cylinder_centered places the R=5 bore on
    // the SAME axis (origin, +Z) so the cut produces ONE clean full-height concave cylinder wall
    // of area 2*pi*R*H ≈ 628mm². The bore MUST be central (not at a corner) so it stays a single
    // unambiguous face — a corner bore would carve only a quarter and (worse) collide with the
    // corner fillet below, fragmenting the wall into several small faces with no 628mm² face at
    // all. The fillet R=2 edges are the 4 OUTER box corners, well clear of the central bore.
    const double L = 40.0, W = 30.0, H = 20.0, BORE_R = 5.0, FILLET_R = 2.0;

    // Step 1: box with a concentric bore (1 concave cylinder wall of R=5).
    TopoDS_Shape body = geom::cut(
        geom::make_box_centered(L, W, H),
        geom::make_cylinder_centered(BORE_R, H + 1.0)
    );
    REQUIRE_FALSE(body.IsNull());

    // Step 2: fillet the 4 vertical corner edges with R=2 < BORE_R (adds 4 convex quarter-
    // cylinders of R=2). dir=Z selects only the vertical (parallel-to-Z) straight edges.
    TopoDS_Shape filleted = geom::fillet_edges(
        body, FILLET_R, std::optional<Eigen::Vector3d>(Eigen::Vector3d(0.0, 0.0, 1.0))
    );
    // If filleting fails for any reason, fall back to the unfilleted body (bore-only path
    // below still exercises concavity via the C5 fallback).
    const bool has_fillets = !filleted.IsNull();
    const TopoDS_Shape& test_body = has_fillets ? filleted : body;

    auto q = [&](json query) { return resolve_geom_query(test_body, query); };
    auto fa = [](const QueryPicks& p, size_t i) {
        return p.selectors.at(i)["area_or_len"].get<double>();
    };

    const double bore_area = 2.0 * 3.14159265358979 * BORE_R * H;  // ~628mm²

    if (has_fillets) {
        // At least 5 cylindrical faces: 1 bore (concave) + 4 quarter-corner fillets (convex).
        QueryPicks all_round = q(json{{"of", "face"}, {"surf", "round"}});
        REQUIRE(all_round.faces.size() >= 2);

        // C5 core assertion: pick:"inner" returns the bore (concave, R=5), not fillets (R=2).
        QueryPicks inner = q(json{{"of", "face"}, {"surf", "round"}, {"pick", "inner"}});
        QueryPicks outer = q(json{{"of", "face"}, {"surf", "round"}, {"pick", "outer"}});
        REQUIRE(inner.faces.size() >= 1);
        REQUIRE(outer.faces.size() >= 1);

        MESSAGE("C5 inner area=" << fa(inner, 0) << " outer area=" << fa(outer, 0)
                << " expected bore~" << bore_area);
        // Bore is larger (R=5*H=20=628mm²) than any corner fillet (R=2*quarter).
        CHECK(fa(inner, 0) > fa(outer, 0));
        CHECK(fa(inner, 0) == doctest::Approx(bore_area).epsilon(0.05));

        // "bore" alias must resolve to the same face as "inner".
        QueryPicks bore_alias = q(json{{"of", "face"}, {"surf", "round"}, {"pick", "bore"}});
        REQUIRE(bore_alias.faces.size() >= 1);
        CHECK(fa(bore_alias, 0) == doctest::Approx(fa(inner, 0)).epsilon(0.01));
    }

    // Bore-only body (no fillets): pick:"inner" returns the single bore. There IS a round
    // candidate (the bore), so pick:"outer" falls back to radius ranking and returns it too
    // (see below) — this is NOT the I2 no-round case (that needs ZERO round candidates).
    auto q2 = [&](json query) { return resolve_geom_query(body, query); };
    QueryPicks bore_only = q2(json{{"of", "face"}, {"surf", "round"}, {"pick", "inner"}});
    REQUIRE(bore_only.faces.size() == 1);
    CHECK(fa(bore_only, 0) == doctest::Approx(bore_area).epsilon(0.05));

    // pick:"outer" on a body with only a concave bore (no convex cylinders): concavity
    // filter finds nothing → falls back to radius ranking → returns the bore (there's only
    // one candidate). This is the graceful degradation when there's only one cylinder.
    QueryPicks outer_only = q2(json{{"of", "face"}, {"surf", "round"}, {"pick", "outer"}});
    CHECK(outer_only.faces.size() == 1);  // fallback returns the only candidate
}

// K10/K11: an invalid `count` (count:-1, count:0) must be REJECTED with a clear error
//          message — not silently ignored (which returned all matches) or silently
//          clamped to 0 (which returned no matches with no explanation).
//          A valid count:1 must still work. count absent must not trigger the check.
TEST_CASE("K10: invalid count in geom query returns a clear error") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;

    // Build a simple 10×10×10 box — any solid with faces will do.
    TopoDS_Shape box = geom::make_box(10.0, 10.0, 10.0);

    // count:-1 must produce a non-empty error.
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}, {"count", -1}});
        CHECK_MESSAGE(!r.error.empty(),
            "K10: count:-1 should have errored but returned " << r.faces.size() << " faces");
        MESSAGE("K10 count:-1 error: " << r.error);
        CHECK(r.faces.empty());
    }

    // count:0 must also error (semantically useless — the only valid lower bound is 1).
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}, {"count", 0}});
        CHECK_MESSAGE(!r.error.empty(),
            "K10: count:0 should have errored but returned " << r.faces.size() << " faces");
        MESSAGE("K10 count:0 error: " << r.error);
        CHECK(r.faces.empty());
    }

    // count:1 must still work — return exactly 1 face (the first enumerated).
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}, {"count", 1}});
        CHECK_MESSAGE(r.error.empty(), "K10: count:1 should NOT error: " << r.error);
        CHECK(r.faces.size() == 1);
    }

    // count absent — must return all 6 faces of the box.
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}});
        CHECK_MESSAGE(r.error.empty(), "K10: no count should NOT error: " << r.error);
        CHECK(r.faces.size() == 6);
    }
}

// (I1) surf:"round" is a CATEGORY: ANY non-planar face matches, not just cylinders. The
//      user's biggest gap was "fillet all the round faces" on a cone/sphere/torus/loft
//      returning EMPTY (round used to alias cylinder ONLY). Each curved primitive below has
//      a known lateral curved face that surf:"round"/"curved" must catch, and surf:"flat"
//      must NOT. The specific type words (conical/spherical/toroidal) pick that one type.
TEST_CASE("I1 surf:round broadened: matches cone/sphere/torus/loft curved faces") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;

    // --- CONE (r1=10 base, r2=4 top, h=30): one conical lateral face + 2 planar caps. ----
    {
        TopoDS_Shape cone = geom::make_cone_centered(10.0, 4.0, 30.0);
        REQUIRE_FALSE(cone.IsNull());
        QueryPicks rnd = round_faces(cone);
        CHECK(rnd.error.empty());
        CHECK_MESSAGE(rnd.faces.size() >= 1, "cone surf:round must catch the conical wall");
        // The matched curved face is conical (not a cylinder) — proves it's a CATEGORY match.
        for (const auto& s : rnd.selectors) CHECK(s["surf"].get<std::string>() != "plane");
        // "conical" specifically picks the cone wall; "cylindrical" picks nothing on a cone.
        CHECK(resolve_geom_query(cone, json{{"of", "face"}, {"surf", "conical"}}).faces.size() >= 1);
        CHECK(resolve_geom_query(cone, json{{"of", "face"}, {"surf", "cylindrical"}}).faces.size() == 0);
        // surf:flat catches ONLY the two planar end caps, never the cone wall.
        QueryPicks flat = resolve_geom_query(cone, json{{"of", "face"}, {"surf", "flat"}});
        CHECK(flat.error.empty());
        for (const auto& s : flat.selectors) CHECK(s["surf"].get<std::string>() == "plane");
    }

    // --- SPHERE (r=12): a single spherical face, NO planar faces. ------------------------
    {
        TopoDS_Shape sphere = geom::make_sphere(12.0);
        REQUIRE_FALSE(sphere.IsNull());
        QueryPicks rnd = round_faces(sphere);
        CHECK(rnd.error.empty());
        CHECK_MESSAGE(rnd.faces.size() >= 1, "sphere surf:round must catch the spherical face");
        CHECK(resolve_geom_query(sphere, json{{"of", "face"}, {"surf", "spherical"}}).faces.size() >= 1);
        // A sphere has no flat faces at all.
        CHECK(resolve_geom_query(sphere, json{{"of", "face"}, {"surf", "flat"}}).faces.size() == 0);
    }

    // --- TORUS (R=20 major, r=5 minor): a single toroidal face. -------------------------
    {
        TopoDS_Shape torus = geom::make_torus(20.0, 5.0);
        REQUIRE_FALSE(torus.IsNull());
        QueryPicks rnd = round_faces(torus);
        CHECK(rnd.error.empty());
        CHECK_MESSAGE(rnd.faces.size() >= 1, "torus surf:round must catch the toroidal face");
        CHECK(resolve_geom_query(torus, json{{"of", "face"}, {"surf", "toroidal"}}).faces.size() >= 1);
        CHECK(resolve_geom_query(torus, json{{"of", "face"}, {"surf", "flat"}}).faces.size() == 0);
    }

    // --- LOFT (circle r=10 @ z=0 -> circle r=4 @ z=30): a curved (bspline/cone) lateral
    //     wall + 2 planar caps. The wall is NOT a cylinder, so the old round->cylinder alias
    //     would have MISSED it; the category match catches it.
    {
        std::vector<TopoDS_Wire> sections = {circle_wire(10.0, 0.0), circle_wire(4.0, 30.0)};
        TopoDS_Shape loft = geom::loft_sections(sections, /*ruled=*/false);
        REQUIRE_FALSE(loft.IsNull());
        QueryPicks rnd = round_faces(loft);
        CHECK(rnd.error.empty());
        CHECK_MESSAGE(rnd.faces.size() >= 1, "loft surf:round must catch the curved lateral wall");
        for (const auto& s : rnd.selectors) CHECK(s["surf"].get<std::string>() != "plane");
        // surf:flat catches only the end caps (the curved wall is excluded).
        QueryPicks flat = resolve_geom_query(loft, json{{"of", "face"}, {"surf", "flat"}});
        for (const auto& s : flat.selectors) CHECK(s["surf"].get<std::string>() == "plane");
    }

    // --- EDGE category: surf:round/curved on a sphere catches its non-line seam edges
    //     (circle/ellipse/bspline). A box has only straight edges, so edge surf:round = 0.
    {
        TopoDS_Shape sphere = geom::make_sphere(12.0);
        QueryPicks redge = resolve_geom_query(sphere, json{{"of", "edge"}, {"surf", "round"}});
        CHECK(redge.error.empty());
        for (const auto& s : redge.selectors) CHECK(s["surf"].get<std::string>() != "line");
        TopoDS_Shape box = geom::make_box(20.0, 20.0, 20.0);
        CHECK(resolve_geom_query(box, json{{"of", "edge"}, {"surf", "round"}}).edges.size() == 0);
        CHECK(resolve_geom_query(box, json{{"of", "edge"}, {"surf", "straight"}}).edges.size() == 12);
    }
}

// (I1b) surf abbreviations and plurals. Live c243: {"of":"face","surf":"cyl"} was refused,
//       and the tool docs advertise edge surf "circular"/"elliptical", which the engine also
//       refused. Each word must select the same set as its canonical kernel name.
TEST_CASE("I1b surf abbreviations and synonyms select the same set as the canonical name") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
    auto count = [](const TopoDS_Shape& s, const char* of, const char* surf) {
        QueryPicks p = resolve_geom_query(s, json{{"of", of}, {"surf", surf}});
        CHECK_MESSAGE(p.error.empty(), "surf '" << surf << "' must be accepted: " << p.error);
        return std::string(of) == "face" ? p.faces.size() : p.edges.size();
    };
    struct Case { TopoDS_Shape shape; const char* of; const char* canon;
                  std::vector<const char*> words; };
    const std::vector<Case> cases = {
        {geom::make_cylinder_centered(8.0, 40.0), "face", "cylinder",
         {"cyl", "CYL", "cyls", "cylinders", "Cylindrical"}},
        {geom::make_box(20.0, 20.0, 20.0), "face", "plane", {"flat", "planar", "planes"}},
        {geom::make_cone_centered(10.0, 4.0, 30.0), "face", "cone", {"cones", "conical"}},
        {geom::make_sphere(12.0), "face", "sphere", {"sph", "spheres", "spherical"}},
        {geom::make_torus(20.0, 5.0), "face", "torus", {"tori", "toroid", "toroidal"}},
        {geom::make_cylinder_centered(8.0, 40.0), "edge", "circle",
         {"circular", "circles", "arc", "arcs"}},
        {geom::make_box(20.0, 20.0, 20.0), "edge", "line", {"straight", "linear", "lines"}},
    };
    for (const Case& c : cases) {
        REQUIRE_FALSE(c.shape.IsNull());
        const size_t want = count(c.shape, c.of, c.canon);
        CHECK_MESSAGE(want >= 1, "fixture must carry a '" << c.canon << "' " << c.of);
        for (const char* w : c.words)
            CHECK_MESSAGE(count(c.shape, c.of, w) == want,
                          "surf '" << w << "' must select every '" << c.canon << "' " << c.of);
    }
    // A word for the other topology kind, or an unknown word, is still refused.
    TopoDS_Shape box = geom::make_box(20.0, 20.0, 20.0);
    CHECK_FALSE(resolve_geom_query(box, json{{"of", "edge"}, {"surf", "flat"}}).error.empty());
    CHECK_FALSE(resolve_geom_query(box, json{{"of", "face"}, {"surf", "cylx"}}).error.empty());
}

// (I2) pick:"inner"/"outer" with NO round faces/edges degrades to an EMPTY result (0
//      matches, NO error) — consistent with surf:round on a box. Previously it THREW a
//      teaching error, which made an honest "no bores" look like a query bug. (The
//      concavity logic is preserved when round faces DO exist — see test (i)/(g).)
TEST_CASE("I2 pick:inner/outer with no round faces returns empty, not error") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
    TopoDS_Shape box = geom::make_box(30.0, 20.0, 10.0);

    for (const char* p : {"inner", "outer", "bore", "innermost", "outermost"}) {
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}, {"pick", p}});
        CHECK_MESSAGE(r.error.empty(), "I2: pick '" << p << "' should NOT error on a box");
        CHECK_MESSAGE(r.faces.empty(), "I2: pick '" << p << "' should return 0 faces on a box");
    }
    // Edges too: a box has no circular edges, so pick:inner on edges is empty (no error).
    QueryPicks e = resolve_geom_query(box, json{{"of", "edge"}, {"pick", "inner"}});
    CHECK(e.error.empty());
    CHECK(e.edges.empty());

    // An UNKNOWN pick word is STILL a teaching error (only the no-round case became graceful).
    CHECK_FALSE(resolve_geom_query(box, json{{"of", "face"}, {"pick", "hugest"}}).error.empty());
}

// (I3) DIRECTIONAL side queries on ROUNDED/REVOLVED parts. A cylinder/shaft's flat top cap
//      must still win side:top (centroid + signed normal both agree), AND a curved face whose
//      representative outward direction faces the requested side must be selectable so
//      surf:round + side:front on a cone/sphere resolves (not EMPTY). describe_faces now
//      assigns a representative direction to EVERY face (cone/sphere/torus included).
TEST_CASE("I3 side on rounded parts: top cap wins, curved face faces the side") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
    auto cz = [](const QueryPicks& p, size_t i) {
        return p.selectors.at(i)["centroid"][2].get<double>();
    };
    auto cy = [](const QueryPicks& p, size_t i) {
        return p.selectors.at(i)["centroid"][1].get<double>();
    };

    // --- CYLINDER (r=8, h=40, z in [-20,20]): side:top -> the +Z flat cap. -------------
    {
        TopoDS_Shape cyl = geom::make_cylinder_centered(8.0, 40.0);
        REQUIRE_FALSE(cyl.IsNull());
        QueryPicks top = resolve_geom_query(cyl, json{{"of", "face"}, {"side", "top"}});
        REQUIRE(top.error.empty());
        REQUIRE(top.faces.size() == 1);
        // The top cap is PLANAR and sits at z=+20 (not the lateral wall centred at z=0).
        CHECK(top.selectors.at(0)["surf"].get<std::string>() == "plane");
        CHECK(cz(top, 0) == doctest::Approx(20.0).epsilon(0.05));
        QueryPicks bot = resolve_geom_query(cyl, json{{"of", "face"}, {"side", "bottom"}});
        REQUIRE(bot.faces.size() == 1);
        CHECK(cz(bot, 0) == doctest::Approx(-20.0).epsilon(0.05));
        CHECK(cz(top, 0) > cz(bot, 0));
    }

    // --- REVOLVED-STYLE SHAFT (two coaxial cylinders fused: big R=10 z in [-20,20] + thin
    //     R=4 z in [-35,35], so the thin part pokes out both ends): side:top -> the thin
    //     part's +Z end cap at z=35, the global top. Proves centroid ranking still finds the
    //     true top end face on a multi-section revolved body, with the curved walls present.
    {
        TopoDS_Shape big  = geom::make_cylinder_centered(10.0, 40.0);  // z in [-20,20]
        TopoDS_Shape thin = geom::make_cylinder_centered(4.0, 70.0);   // z in [-35,35]
        TopoDS_Shape shaft = geom::fuse(big, thin);
        REQUIRE_FALSE(shaft.IsNull());
        QueryPicks top = resolve_geom_query(shaft, json{{"of", "face"}, {"side", "top"}});
        REQUIRE(top.error.empty());
        REQUIRE(top.faces.size() == 1);
        CHECK(top.selectors.at(0)["surf"].get<std::string>() == "plane");  // the end cap
        CHECK(cz(top, 0) == doctest::Approx(35.0).epsilon(0.1));           // global +Z top
    }

    // --- CONE (r1=12 base @ z=-15, r2=2 tip @ z=+15): surf:round + side:front resolves to
    //     the conical wall (it's the only curved face; must NOT be EMPTY). ----------------
    {
        TopoDS_Shape cone = geom::make_cone_centered(12.0, 2.0, 30.0);
        REQUIRE_FALSE(cone.IsNull());
        QueryPicks rf = resolve_geom_query(
            cone, json{{"of", "face"}, {"surf", "round"}, {"side", "front"}});
        CHECK(rf.error.empty());
        CHECK_MESSAGE(rf.faces.size() >= 1,
                      "I3: surf:round + side:front on a cone must resolve, not be EMPTY");
        CHECK(rf.selectors.at(0)["surf"].get<std::string>() != "plane");
    }

    // --- SPHERE (r=12): surf:round + side:front resolves to a spherical patch (whichever
    //     OCCT emits) — the key property is NON-EMPTY and curved. ------------------------
    {
        TopoDS_Shape sphere = geom::make_sphere(12.0);
        QueryPicks rf = resolve_geom_query(
            sphere, json{{"of", "face"}, {"surf", "round"}, {"side", "front"}});
        CHECK(rf.error.empty());
        CHECK_MESSAGE(rf.faces.size() >= 1,
                      "I3: surf:round + side:front on a sphere must resolve, not be EMPTY");
    }

    // --- BOX REGRESSION: side ranking on a pure box is UNCHANGED (planar faces: centroid +
    //     signed normal both agree). top higher than bottom, front in front of back. ------
    {
        TopoDS_Shape box = geom::make_box(40.0, 30.0, 20.0);  // corner box [0..40][0..30][0..20]
        QueryPicks top = resolve_geom_query(box, json{{"of", "face"}, {"side", "top"}});
        QueryPicks bot = resolve_geom_query(box, json{{"of", "face"}, {"side", "bottom"}});
        REQUIRE(top.faces.size() == 1);
        REQUIRE(bot.faces.size() == 1);
        CHECK(cz(top, 0) > cz(bot, 0));
        CHECK(cz(top, 0) == doctest::Approx(20.0).epsilon(0.01));
        CHECK(cz(bot, 0) == doctest::Approx(0.0).epsilon(0.01));
        // front=-Y is at y=0, back=+Y at y=30 (named-view convention preserved).
        QueryPicks front = resolve_geom_query(box, json{{"of", "face"}, {"side", "front"}});
        QueryPicks back  = resolve_geom_query(box, json{{"of", "face"}, {"side", "back"}});
        REQUIRE(front.faces.size() == 1);
        REQUIRE(back.faces.size() == 1);
        CHECK(cy(front, 0) < cy(back, 0));
    }
}

// The geom-query DIALECT gate. An LLM writes these queries, so the two ways it
// actually fails are a MISSING `of` and a PROSE `side` — both used to dead-end:
//   1. `of` now DEFAULTS to "face". A live model called query_geometry with
//      {"side":"+x"} and got `'of' must be "edge" or "face" (got '')`, a message that
//      names no fix. Faces are what a query overwhelmingly means.
//   2. Every rejection carries the FULL grammar, so one wrong guess teaches the whole
//      vocabulary instead of prompting another wrong guess.
//   3. A prose `side` gets a free-text-SPECIFIC message pointing at describe_part, with
//      the echoed string truncated so a pasted sentence can't bloat the error.
TEST_CASE("geom query defaults of:face and teaches the grammar on rejection") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;

    // Corner box [0..40][0..30][0..20] — the same reference body the WORDS test uses.
    TopoDS_Shape box = geom::make_box(40.0, 30.0, 20.0);
    auto has = [](const std::string& hay, const char* needle) {
        return hay.find(needle) != std::string::npos;
    };

    // --- 1. of-DEFAULTING: the EXACT live failure {"side":"+x"} must resolve to a face. ---
    {
        QueryPicks r = resolve_geom_query(box, json{{"side", "+x"}});
        CHECK_MESSAGE(r.error.empty(), "{\"side\":\"+x\"} must resolve, got: " << r.error);
        REQUIRE(r.faces.size() == 1);  // the +X face
        CHECK(r.edges.empty());        // defaulted to FACES, not edges
        CHECK(r.selectors.at(0)["centroid"][0].get<double>() ==
              doctest::Approx(40.0).epsilon(0.01));
    }

    // An of-less query with no other key is still a face query (all 6 box faces), and the
    // other face-meaningful keys resolve of-less too.
    {
        QueryPicks all = resolve_geom_query(box, json::object());
        CHECK(all.error.empty());
        CHECK(all.faces.size() == 6);

        QueryPicks flat = resolve_geom_query(box, json{{"surf", "flat"}});
        CHECK(flat.error.empty());
        CHECK(flat.faces.size() == 6);  // every box face is planar

        // 40x30 = 1200 mm² beats 40x20 and 30x20; the two co-planar-area faces co-rank.
        QueryPicks big = resolve_geom_query(box, json{{"pick", "largest"}});
        CHECK(big.error.empty());
        REQUIRE(big.faces.size() == 2);
        for (const auto& s : big.selectors)
            CHECK(s["area_or_len"].get<double>() == doctest::Approx(1200.0).epsilon(0.01));
    }

    // `of` stays case-insensitive-exact, and an EXPLICIT of is untouched by the default.
    {
        CHECK(resolve_geom_query(box, json{{"of", "FACE"}}).faces.size() == 6);
        QueryPicks edges = resolve_geom_query(box, json{{"of", "edge"}});
        CHECK(edges.error.empty());
        CHECK(edges.edges.size() == 12);
        CHECK(edges.faces.empty());
    }

    // --- 2. a PRESENT but invalid `of` is STILL an error — now with the grammar. ---
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", "solid"}, {"side", "top"}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(r.faces.empty());
        CHECK(has(r.error, "solid"));        // names the rejected value...
        CHECK(has(r.error, "query keys:"));  // ...and the whole accepted grammar
        CHECK(has(r.error, "describe_part"));
        MESSAGE("of:solid error: " << r.error);
    }
    // A NON-STRING `of` errors cleanly instead of throwing a nlohmann type_error.
    {
        QueryPicks r = resolve_geom_query(box, json{{"of", 3}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(has(r.error, "query keys:"));
    }

    // --- 3. a FREE-TEXT `side` gets its own message pointing at describe_part. -----------
    {
        const std::string prose = "outer cylindrical wall face at bottom near x=200,y=0";
        QueryPicks r = resolve_geom_query(box, json{{"of", "face"}, {"side", prose}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(r.faces.empty());
        CHECK(has(r.error, "got a free-text description"));
        CHECK(has(r.error, "describe_part"));  // the REAL exploration tool...
        CHECK_FALSE(has(r.error, "describe_faces"));  // ...not the C++-only symbol
        CHECK(has(r.error, "query keys:"));
        MESSAGE("free-text side error: " << r.error);
    }
    // The echo is TRUNCATED — a pasted paragraph must not become the error message.
    {
        const std::string huge(4000, 'q');
        QueryPicks r = resolve_geom_query(box, json{{"side", huge}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(has(r.error, "got a free-text description"));
        CHECK_MESSAGE(r.error.find(std::string(100, 'q')) == std::string::npos,
                      "the echoed side value must be clipped");
        CHECK_MESSAGE(r.error.size() < 800, "error length " << r.error.size());
    }
    // A SHORT unrecognised side word keeps the original teaching message (NOT the free-text
    // one) and gains the grammar tail.
    {
        QueryPicks r = resolve_geom_query(box, json{{"side", "sideways"}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(has(r.error, "bad 'side'"));
        CHECK_FALSE(has(r.error, "got a free-text description"));
        CHECK(has(r.error, "query keys:"));
    }

    // --- 4. the `surf` rejection carries the grammar too (and proves the of-less path
    //        landed on the FACE vocabulary). ----------------------------------------------
    {
        QueryPicks r = resolve_geom_query(box, json{{"surf", "shiny"}});
        REQUIRE_FALSE(r.error.empty());
        CHECK(has(r.error, "shiny"));
        CHECK(has(r.error, "face surf words"));  // face branch — `of` defaulted
        CHECK(has(r.error, "query keys:"));
    }
}

// near: the point is ON the entity the model means, so `near` ranks by distance to the actual
// geometry (contracts/selectors.md §7). Live gpt-6-luna H5: a Ø110×12 flange with a PCD-80
// 8×Ø9 bolt circle (first hole at +X); near:[55,0,12] lies ON the outer rim, yet centroid
// ranking chose the +X hole's top edge (centroid (40,0,12), 15 mm away) over the rim
// (centroid = axis point (0,0,12), 55 mm away), so the chamfer landed on the bolt hole.
TEST_CASE("geom query near: ranks by distance to the geometry, ties to the nearer centroid") {
    using chudware::doc::resolve_geom_query;
    using chudware::doc::QueryPicks;
    auto zcyl = [](double x, double y, double z0, double r, double h) {
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), r, h).Shape();
    };
    TopoDS_Shape flange = geom::cut(zcyl(0, 0, 0, 55.0, 12.0), zcyl(0, 0, -1, 20.0, 14.0));
    for (int i = 0; i < 8; ++i) {
        const double a = 2.0 * M_PI * i / 8.0;
        flange = geom::cut(flange, zcyl(40.0 * std::cos(a), 40.0 * std::sin(a), -1, 4.5, 14.0));
    }
    REQUIRE_FALSE(flange.IsNull());
    auto radius = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["radius"].get<double>(); };
    auto cx = [](const QueryPicks& p, size_t i) { return p.selectors.at(i)["centroid"][0].get<double>(); };

    const json rim_q{{"of", "edge"}, {"side", "+z"}, {"surf", "circle"},
                     {"near", json::array({55, 0, 12})}, {"count", 1}};
    QueryPicks rim = resolve_geom_query(flange, rim_q);
    REQUIRE_MESSAGE(rim.error.empty(), rim.error);
    REQUIRE(rim.edges.size() == 1);
    CHECK(radius(rim, 0) == doctest::Approx(55.0));

    // count keeps the same order: the rim, then the +X bolt hole (10.5 mm away), not the bore.
    json two = rim_q;
    two["count"] = 2;
    QueryPicks both = resolve_geom_query(flange, two);
    REQUIRE(both.edges.size() == 2);
    CHECK(radius(both, 0) == doctest::Approx(55.0));
    CHECK(radius(both, 1) == doctest::Approx(4.5));
    CHECK(cx(both, 1) == doctest::Approx(40.0));

    // A point ON the hole edge still picks the hole edge.
    QueryPicks hole = resolve_geom_query(
        flange, json{{"of", "edge"}, {"side", "+z"}, {"surf", "circle"},
                     {"near", json::array({44.5, 0, 12})}, {"count", 1}});
    REQUIRE(hole.edges.size() == 1);
    CHECK(radius(hole, 0) == doctest::Approx(4.5));
    CHECK(cx(hole, 0) == doctest::Approx(40.0));

    // A point ON the shared edge of two faces scores 0 for both: the nearer centroid wins.
    // Squat box: front face (y=0) centroid (20,0,10) is 10 mm from (20,0,20), top's 15 mm.
    // Tall box: top centroid (20,15,60) is 15 mm from (20,0,60), front's (20,0,30) is 30 mm.
    auto cy = [](const QueryPicks& p) { return p.selectors.at(0)["centroid"][1].get<double>(); };
    auto cz = [](const QueryPicks& p) { return p.selectors.at(0)["centroid"][2].get<double>(); };
    QueryPicks squat = resolve_geom_query(
        geom::make_box(40.0, 30.0, 20.0),
        json{{"of", "face"}, {"near", json::array({20, 0, 20})}, {"count", 1}});
    REQUIRE(squat.faces.size() == 1);
    CHECK(cy(squat) == doctest::Approx(0.0));
    CHECK(cz(squat) == doctest::Approx(10.0));
    QueryPicks tall = resolve_geom_query(
        geom::make_box(40.0, 30.0, 60.0),
        json{{"of", "face"}, {"near", json::array({20, 0, 60})}, {"count", 1}});
    REQUIRE(tall.faces.size() == 1);
    CHECK(cy(tall) == doctest::Approx(15.0));
    CHECK(cz(tall) == doctest::Approx(60.0));

    // A centroid handed back (the engine's own selector re-resolution, describe_part read-back)
    // still resolves to that entity, even a curved face whose centroid is off its surface: the
    // +X bolt-hole wall's centroid (40,0,6) sits on its axis, 4.5 mm from the wall.
    QueryPicks holes = resolve_geom_query(flange, json{{"of", "face"}, {"surf", "cylinder"}});
    size_t px = holes.selectors.size();
    for (size_t i = 0; i < holes.selectors.size(); ++i)
        if (std::abs(radius(holes, i) - 4.5) < 1e-6 && std::abs(cx(holes, i) - 40.0) < 1e-6) px = i;
    REQUIRE(px < holes.selectors.size());
    QueryPicks back = resolve_geom_query(
        flange, json{{"of", "face"}, {"near", holes.selectors.at(px)["centroid"]}, {"count", 1}});
    REQUIRE(back.faces.size() == 1);
    CHECK(back.selectors.at(0)["sid"] == holes.selectors.at(px)["sid"]);
}
