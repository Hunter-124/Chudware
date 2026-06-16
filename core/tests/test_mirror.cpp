// A4 milestone verification: the MIRROR feature compiler (core/doc/document.cpp) —
// which REFLECTS a referenced seed solid across a plane via a mirror gp_Trsf
// (geom/transform.cpp: mirror_copy) and either FUSES the reflection into the body
// (merge:true, the default) or keeps it as a SEPARATE body (merge:false), in the
// simplified id-ref model of the doc.
//
// It follows the EXACT round-trip discipline of test_pattern.cpp / test_document.cpp:
// compile a document, parse the resulting AICM with the HOST loader
// (host/src/render/aicm_loader = the contract oracle), and assert TIGHT, ANALYTIC,
// OCCT-version-INDEPENDENT invariants on the parsed body (planar/cylindrical face
// counts, bbox extents, mesh VOLUME computed from the tessellation) plus the
// CompileResult bookkeeping (status ok, surviving body_count — the body is consumed
// when merge:true, both bodies survive when merge:false). The assertions ARE the
// product — they are NOT weakened to pass.
//
// The plane comes either from an EXPLICIT plane_point/normal or from a FACE SELECTOR
// harvested off a reference box's topology (describe_faces + make_selector) — proving
// the selector -> kernel-face -> mirror-plane path actually drives a real reflection.
// The orientation WATCH-POINT: a mirror trsf is a reflection (negative determinant);
// the mesh_volume helper uses fabs and a clean fuse confirms the reflected solid is
// not inside-out (a bad orientation would make the fuse fail or the volume wrong).
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

// ---- sketch DSL (copied from test_pattern.cpp) -----------------------------
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

// Build a fully-constrained rectangle (min corner at x0,y0; width w, height h).
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

// ---- AICM inspection helpers (identical contract to test_pattern.cpp) -------
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

// Mesh volume by the signed-tetrahedra (divergence) formula (copied from
// test_pattern.cpp): V = (1/6) * |sum over triangles of v0 . (v1 x v2)|. EXACT for
// planar-faceted solids, independent of OCCT version / tessellation density. fabs()
// guards an overall orientation flip — the mirror orientation watch-point.
double mesh_volume(const render::BodyMesh& b) {
    double six_v = 0.0;
    for (uint32_t t = 0; t < b.n_tris; ++t) {
        const uint32_t i0 = b.idx[3ull * t + 0];
        const uint32_t i1 = b.idx[3ull * t + 1];
        const uint32_t i2 = b.idx[3ull * t + 2];
        const double v0[3] = {b.pos[3ull * i0 + 0], b.pos[3ull * i0 + 1], b.pos[3ull * i0 + 2]};
        const double v1[3] = {b.pos[3ull * i1 + 0], b.pos[3ull * i1 + 1], b.pos[3ull * i1 + 2]};
        const double v2[3] = {b.pos[3ull * i2 + 0], b.pos[3ull * i2 + 1], b.pos[3ull * i2 + 2]};
        const double cx = v1[1] * v2[2] - v1[2] * v2[1];
        const double cy = v1[2] * v2[0] - v1[0] * v2[2];
        const double cz = v1[0] * v2[1] - v1[1] * v2[0];
        six_v += v0[0] * cx + v0[1] * cy + v0[2] * cz;
    }
    return std::fabs(six_v) / 6.0;
}

render::DocMesh parse(const CompileResult& r) {
    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(r.aicm.data(), r.aicm.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return doc;
}

// Harvest the +X face selector off a reference box [0,W]x[0,H]x[0,D]: describe_faces
// enumerates a box's 6 faces once each, so we pick the planar face whose centroid.x
// sits at W (the +X face) with an axis essentially +/-X, and make_selector it. The
// returned selector resolves the SAME +X face on a rebuilt box of the same size.
json plus_x_face_selector(double W, double H, double D) {
    TopoDS_Shape box = geom::make_box(W, H, D);
    std::vector<aicad::doc::TopoInfo> infos = aicad::doc::describe_faces(box);
    for (const aicad::doc::TopoInfo& info : infos) {
        if (!info.axis.has_value()) continue;
        if (std::abs(info.axis->x()) < 0.999) continue;     // not an +/-X-normal face
        if (std::abs(info.centroid.x() - W) > 1e-6) continue; // not the +X (x=W) face
        return aicad::doc::make_selector(info);
    }
    return json(nullptr);  // not found (the test REQUIREs a real selector)
}

}  // namespace

// (a) EXPLICIT-PLANE MERGE. Body box [5,20]x[0,10]x[0,5] (sketch rect 5,0,15,10 on top
//     -> extrude 5), then mirror{body:x1, plane_point:[0,0,0], plane_normal:[1,0,0]}.
//     The reflection across the YZ plane (x=0) is [-20,-5]x[0,10]x[0,5]; fusing the two
//     DISJOINT boxes (gap [-5,5]) consumes the seed, so ONE body survives: a 40 x 10 x 5
//     bbox (x in [-20,20]) carrying 12 planar faces (2 boxes * 6, none merged), no
//     cylinders, and a total volume of 2 * 750 = 1500.
TEST_CASE("mirror explicit-plane merge: reflection fused into one body") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(5, 0, 15, 10)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 5.0}},
        json{{"id", "mir"}, {"type", "mirror"}, {"body", "x1"},
             {"plane_point", json::array({0, 0, 0})},
             {"plane_normal", json::array({1, 0, 0})}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // body consumed into the merged result
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    MESSAGE("mirror explicit merge: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected 1500)");

    CHECK(count_surf(b, 0) == 12);  // 2 disjoint boxes * 6 planar faces
    CHECK(count_surf(b, 1) == 0);   // no curved faces
    CHECK((hi[0] - lo[0]) == doctest::Approx(40.0).epsilon(1e-4));  // x in [-20,20]
    CHECK((hi[1] - lo[1]) == doctest::Approx(10.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(5.0).epsilon(1e-4));
    CHECK(vol == doctest::Approx(1500.0).epsilon(1e-3));
}

// (b) FACE-SELECTOR MERGE (selector integration). Harvest the +X face selector off a
//     reference box make_box(15,10,5). Body box [0,15]x[0,10]x[0,5] (sketch rect
//     0,0,15,10 on top -> extrude 5), then mirror{body:x1, plane:<that face selector>}.
//     Mirroring about the +X face (x=15) reflects [0,15]->[15,30]; the two halves abut
//     on the x=15 plane and fuse into a SINGLE box-shaped solid [0,30]x[0,10]x[0,5]
//     (bbox 30 x 10 x 5, volume 1500, ONE surviving body) — the load-bearing proof that
//     the face-selector plane drove a correct, coincident-face merge.
//
//     FACE COUNT — OCCT actual = 10 (NOT the 6 of a pristine box). BRepAlgoAPI_Fuse
//     removes the two interior x=15 cap faces but does NOT run a coplanar-unify pass, so
//     the four side faces (top z=5, bottom z=0, +Y y=10, -Y y=0) stay SPLIT at x=15 as
//     two coplanar pieces each: 5 faces of A + 5 of B = 10. Collapsing them to a 6-face
//     box would require an explicit ShapeUpgrade_UnifySameDomain step, which the doc
//     model's fuse() (a plain BRepAlgoAPI_Fuse, matching boolean.py) deliberately does
//     not do. We assert the kernel's true count (10) — the geometry is a single solid of
//     the right size/volume, just with un-unified coplanar seams.
TEST_CASE("mirror face-selector merge: reflection about a picked +X face") {
    json face_sel = plus_x_face_selector(15.0, 10.0, 5.0);
    REQUIRE(face_sel.is_object());  // a real +X face selector was harvested

    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(0, 0, 15, 10)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 5.0}},
        json{{"id", "mir"}, {"type", "mirror"}, {"body", "x1"}, {"plane", face_sel}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);  // body consumed into the merged result
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 1);
    const render::BodyMesh& b = dm.bodies[0];
    check_common(b);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    const double vol = mesh_volume(b);
    MESSAGE("mirror face-selector merge: body_count=" << r.body_count
            << " faces=" << b.faces.size() << " planar=" << count_surf(b, 0)
            << " cyl=" << count_surf(b, 1)
            << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
            << " volume=" << vol << " (expected 1500)");

    CHECK(count_surf(b, 0) == 10);  // 5+5 (x=15 cap removed; coplanar sides NOT unified)
    CHECK(count_surf(b, 1) == 0);   // no curved faces
    CHECK((hi[0] - lo[0]) == doctest::Approx(30.0).epsilon(1e-4));  // x in [0,30]
    CHECK((hi[1] - lo[1]) == doctest::Approx(10.0).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(5.0).epsilon(1e-4));
    CHECK(vol == doctest::Approx(1500.0).epsilon(1e-3));
}

// (c) EXPLICIT-PLANE NO-MERGE. Body box [5,20]x[0,10]x[0,5] (as in (a)), then
//     mirror{body:x1, plane_point:[0,0,0], plane_normal:[1,0,0], merge:false}. The
//     reflection [-20,-5] becomes a SEPARATE body; the original [5,20] is NOT consumed,
//     so BOTH survive (body_count==2). Each is a 15 x 10 x 5 box with 6 planar faces:
//     one spans x[5,20], the other x[-20,-5]; their summed volume is 2 * 750 = 1500.
TEST_CASE("mirror explicit-plane no-merge: reflection is a separate body") {
    json doc;
    doc["features"] = json::array({
        sketch_feature("s1", "top", rect_entities(5, 0, 15, 10)),
        json{{"id", "x1"}, {"type", "extrude"}, {"profile", "s1"}, {"depth", 5.0}},
        json{{"id", "mir"}, {"type", "mirror"}, {"body", "x1"},
             {"plane_point", json::array({0, 0, 0})},
             {"plane_normal", json::array({1, 0, 0})}, {"merge", false}},
    });

    CompileResult r = compile_document(doc, json::object());
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 2);  // original + reflection both survive
    CHECK(r.statuses.size() == 3);
    for (const auto& s : r.statuses) CHECK(s.ok);

    render::DocMesh dm = parse(r);
    REQUIRE(dm.bodies.size() == 2);

    double total_vol = 0.0;
    bool saw_pos = false, saw_neg = false;  // the +x[5,20] body and the -x[-20,-5] body
    for (const render::BodyMesh& b : dm.bodies) {
        check_common(b);
        double lo[3], hi[3];
        bbox(b, lo, hi);
        const double vol = mesh_volume(b);
        total_vol += vol;
        MESSAGE("mirror no-merge body: planar=" << count_surf(b, 0)
                << " cyl=" << count_surf(b, 1)
                << " bbox=" << (hi[0]-lo[0]) << "x" << (hi[1]-lo[1]) << "x" << (hi[2]-lo[2])
                << " x=[" << lo[0] << "," << hi[0] << "]"
                << " volume=" << vol << " (expected 750)");

        CHECK(count_surf(b, 0) == 6);   // each a plain 6-face box
        CHECK(count_surf(b, 1) == 0);   // no curved faces
        CHECK((hi[0] - lo[0]) == doctest::Approx(15.0).epsilon(1e-4));
        CHECK((hi[1] - lo[1]) == doctest::Approx(10.0).epsilon(1e-4));
        CHECK((hi[2] - lo[2]) == doctest::Approx(5.0).epsilon(1e-4));
        CHECK(vol == doctest::Approx(750.0).epsilon(1e-3));
        if (std::abs(lo[0] - 5.0) < 1e-3 && std::abs(hi[0] - 20.0) < 1e-3) saw_pos = true;
        if (std::abs(lo[0] + 20.0) < 1e-3 && std::abs(hi[0] + 5.0) < 1e-3) saw_neg = true;
    }

    MESSAGE("mirror no-merge total: body_count=" << r.body_count
            << " total_volume=" << total_vol << " (expected 1500)");
    CHECK(saw_pos);  // the original [5,20] body
    CHECK(saw_neg);  // the reflected [-20,-5] body
    CHECK(total_vol == doctest::Approx(1500.0).epsilon(1e-3));
}
