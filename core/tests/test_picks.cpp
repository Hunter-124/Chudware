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
#include "doc/selectors.h"
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
