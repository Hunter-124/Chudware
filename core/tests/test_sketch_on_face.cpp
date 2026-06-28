// Regression test for the SKETCH-ON-FACE DISPLAY fix in core/doc/session.cpp.
//
// THE BUG: when a sketch's plane reference is a FACE SELECTOR (kind=="face"), the live
// sketch overlay was drawn on the base plane THROUGH THE WORLD ORIGIN instead of on the
// picked face. The modeling path (document.cpp plane_of -> face_plane(resolve_face_sel))
// already placed the committed geometry correctly; only the interactive overlay was wrong
// because resolve_sketch_plane() did not understand a face-selector plane ref and fell
// back to SketchPlane::top() (origin 0,0,0).
//
// THE FIX: resolve_sketch_plane() now handles a kind=="face" plane ref — it recompiles the
// doc, resolves the picked face off the last solid body, and returns that face's ACTUAL
// plane (area-centroid origin + orientation-aware normal), mirroring the modeling path.
//
// HOW WE OBSERVE IT: doc_sketch_enter(doc_id, sketch_fid) returns a result dict carrying
// `plane_frame` — a 16-float matrix. session.cpp::plane_frame packs it per affine row i as
// [uAxis[i], vAxis[i], normal[i], origin[i]], so the origin (the matrix's 4th column) lands
// at flat indices 3 (x), 7 (y), 11 (z); origin z == plane_frame[11].
//
// THE TEST: build a box of known height H by extruding a W x D rectangle on the TOP (XY)
// plane, so the new top face sits at z=H. Add a SECOND sketch whose plane ref is a face
// selector targeting that top face (centroid at the face centre, z=H — the make_selector
// shape the host builds for a sketch-on-face). Enter it and assert the plane_frame origin
// is the face's centroid (z ≈ H, the FACE OFFSET), NOT the z=0 base-plane fallback (the
// bug). The assertion is on the OFFSET, never an OCCT-version-specific face index, so it is
// kernel-version-independent. Drives the in-process session API only (no AICM round-trip).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"  // doc_new / doc_apply_ops / doc_sketch_enter

using json = nlohmann::json;
using aicad::doc::doc_new;
using aicad::doc::doc_apply_ops;
using aicad::doc::doc_sketch_enter;
using aicad::doc::doc_query;

namespace {

// ---- sketch DSL (mirrors test_profile_extrude.cpp / test_document.cpp) ------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double v) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r},
                {"value", json{{"value", v}, {"expr", json(nullptr)}}}, {"driven", false}};
}
// A {value, expr} dimension param (the shape an extrude `depth` is given).
json dimv(double v) { return json{{"value", v}, {"expr", json(nullptr)}}; }

// Entities of a fully-constrained rectangle: min corner (0,0), width w, height h.
json rect_entities(double w, double h) {
    return json::array({
        line("e1", 0, 0, w, 0),
        line("e2", w, 0, w, h),
        line("e3", w, h, 0, h),
        line("e4", 0, h, 0, 0),
    });
}
json rect_constraints(double w, double h) {
    return json::array({
        rel("c1", "coincident", {"e1.p2", "e2.p1"}),
        rel("c2", "coincident", {"e2.p2", "e3.p1"}),
        rel("c3", "coincident", {"e3.p2", "e4.p1"}),
        rel("c4", "coincident", {"e4.p2", "e1.p1"}),
        rel("c5", "horizontal", {"e1"}),
        rel("c6", "horizontal", {"e3"}),
        rel("c7", "vertical", {"e2"}),
        rel("c8", "vertical", {"e4"}),
        rel("c9", "fix", {"e1.p1"}),
        dim("c10", "distance", {"e1.p1", "e1.p2"}, w),
        dim("c11", "distance", {"e2.p1", "e2.p2"}, h),
    });
}

}  // namespace

TEST_CASE("sketch-on-face: face-selector plane_frame origin lands on the face (z=H), not z=0") {
    // Box footprint W x D, extruded by H on the TOP (XY) plane -> a solid from z=0 to z=H,
    // so the NEW top face sits at z=H with area centroid (W/2, D/2, H). W, D and H are
    // pairwise distinct (20, 15, 25) so the origin can't accidentally satisfy a
    // swapped-index assertion.
    const double W = 40.0, D = 30.0, H = 25.0;

    json nr = doc_new("part", "SketchOnFaceDisplay");
    const std::string doc_id = nr.at("doc_id").get<std::string>();

    // The sketch-on-face plane ref: a kind=="face" selector targeting the top face at its
    // centre (centroid z=H). This is the make_selector shape the host emits for a picked
    // face (kind/surf/centroid[3]/axis/owner_feature) — resolve_sketch_plane keys off the
    // unique centroid via a centroid-near query.
    const json top_face_plane = json{
        {"kind", "face"},
        {"surf", "plane"},
        {"centroid", json::array({W / 2.0, D / 2.0, H})},
        {"axis", json::array({0.0, 0.0, 1.0})},
        {"owner_feature", nullptr},
    };

    // op0: base rect sketch on plane.top. op1: extrude it by H (boss, blind) -> the box.
    // op2: a SECOND, empty sketch whose plane ref is the top-face selector above.
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "Base"},
                 {"params", {{"entities", rect_entities(W, D)},
                             {"constraints", rect_constraints(W, D)}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "extrude"}, {"name", "Body"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(H)}}},
                 {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"},
                            {"centroid", json::array({W / 2.0, D / 2.0})},
                            {"hint_index", 0}}})}}}}}},
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "OnTopFace"},
                 {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
                 {"refs", {{"plane", top_face_plane}}}}}},
    });

    json resp = doc_apply_ops(doc_id, /*base_revision=*/-1, ops, /*coalesce=*/false);

    // The base box (sketch + extrude) and the face-sketch must all build — otherwise there
    // is no body for resolve_sketch_plane to resolve the face against.
    const json& fs = resp.at("rebuild").at("feature_status");
    REQUIRE(fs.size() == 3);
    for (const auto& st : fs)
        REQUIRE_MESSAGE(st.value("ok", false), "feature failed to build: " << st.dump());

    // The face-sketch is op index 2 -> its created id is id_map["$2"].
    const std::string face_sketch_fid = resp.at("id_map").at("$2").get<std::string>();

    // Enter the face-sketch: plane_frame is exactly what the fix corrects.
    json enter = doc_sketch_enter(doc_id, face_sketch_fid);
    REQUIRE(enter.contains("plane_frame"));
    const std::vector<double> frame = enter.at("plane_frame").get<std::vector<double>>();
    REQUIRE(frame.size() == 16);

    // plane_frame packs the origin in its 4th column -> flat indices 3 (x), 7 (y), 11 (z)
    // (see session.cpp::plane_frame: per affine row i it pushes uAxis[i],vAxis[i],normal[i],origin[i]).
    const double ox = frame[3], oy = frame[7], oz = frame[11];
    MESSAGE("plane_frame origin = (" << ox << ", " << oy << ", " << oz << "); expected ("
            << W / 2.0 << ", " << D / 2.0 << ", " << H << ")");

    // THE FIX: the sketch plane is the top FACE's actual plane — area-centroid origin at
    // z=H — instead of SketchPlane::top() (origin 0,0,0), which is what the bug produced.
    REQUIRE_MESSAGE(oz == doctest::Approx(H).epsilon(1e-3),
        "sketch-on-face plane_frame origin z must equal the face offset H=" << H
        << " but was " << oz << " -- an origin at z=0 means resolve_sketch_plane fell back "
        "to SketchPlane::top() (the display bug)");
    // Explicit guard against the bug's exact z=0 base-plane fallback.
    CHECK(std::abs(oz) > 1.0);
    // The origin is the face CENTROID, further proof it resolved the real face (the
    // top()-fallback bug would also zero the x and y origin, not just z).
    CHECK(ox == doctest::Approx(W / 2.0).epsilon(1e-3));
    CHECK(oy == doctest::Approx(D / 2.0).epsilon(1e-3));
}

TEST_CASE("sketch-on-face: a REAL resolver (doc_query) face selector resolves onto the face") {
    // Stronger than the synthetic-centroid case: build the box, then ask the engine's OWN
    // resolver (doc_query) for the top-face selector — a selector with a resolver-computed
    // centroid, like the host emits from a face pick — and sketch on THAT. Proves
    // resolve_sketch_plane's centroid-near query matches a real resolver centroid, not a
    // hand-fed one (the gap that hid this bug behind a green synthetic test).
    const double W = 40.0, D = 30.0, H = 25.0;
    json nr = doc_new("part", "SketchOnRealFace");
    const std::string doc_id = nr.at("doc_id").get<std::string>();

    json box = json::array({
        json{{"op","add_feature"},{"feature",
            json{{"type","sketch"},{"name","Base"},
                 {"params",{{"entities",rect_entities(W,D)},{"constraints",rect_constraints(W,D)}}},
                 {"refs",{{"plane","plane.top"}}}}}},
        json{{"op","add_feature"},{"feature",
            json{{"type","extrude"},{"name","Body"},
                 {"params",{{"mode","boss"},{"end","blind"},{"depth",dimv(H)}}},
                 {"refs",{{"profile",json::array({json{{"kind","region"},{"sketch","$0"},
                    {"centroid",json::array({W/2.0,D/2.0})},{"hint_index",0}}})}}}}}},
    });
    json br = doc_apply_ops(doc_id, -1, box, false);
    for (const auto& st : br.at("rebuild").at("feature_status")) REQUIRE(st.value("ok", false));

    // The resolver's own top-face selector (computed centroid + kind).
    json q = doc_query(doc_id, json{{"of","face"},{"side","top"}});
    REQUIRE_MESSAGE(q.value("count", 0) >= 1, "doc_query found no top face: " << q.dump());
    json face_sel = q.at("matches").at(0);
    MESSAGE("real top-face selector = " << face_sel.dump());

    json mk = json::array({
        json{{"op","add_feature"},{"feature",
            json{{"type","sketch"},{"name","OnRealFace"},
                 {"params",{{"entities",json::array()},{"constraints",json::array()}}},
                 {"refs",{{"plane", face_sel}}}}}},
    });
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    json enter = doc_sketch_enter(doc_id, fid);
    const std::vector<double> frame = enter.at("plane_frame").get<std::vector<double>>();
    REQUIRE(frame.size() == 16);
    const double oz = frame[11];
    MESSAGE("real-selector plane_frame origin z = " << oz << " (expected " << H << ")");
    REQUIRE_MESSAGE(oz == doctest::Approx(H).epsilon(1e-3),
        "a real resolver face selector must enter on the face (z=H=" << H
        << "), not the z=0 base-plane fallback; got " << oz);
}
