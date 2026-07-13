// Regression tests for T-BL43 — Convert Entities / external sketch references.
//
// THE BUG(S): contracts/doc_ops.md contracted a `sketch_convert` op in C53 and BOTH GUIs
// (host sketch_mode.cpp applyConvertRef + ui/imgui sketch_overlay.cpp) emit it, but the C++
// engine never implemented the op — every in-GUI Convert Entities click failed with
// `unsupported`. Additionally the only projection math available (sketch_project_ref) was
// line/axis-parallel-circle only: faces projected to a bare plane-intersection line and an
// OBLIQUE circular edge was rejected outright.
//
// THE FIX (see doc_ops.md's dated T-BL43 amendment): op_sketch_convert (session.cpp) +
// aicad::doc::project_edges_to_plane (recon.cpp) — true orthographic projection along the
// sketch normal via GeomProjLib::ProjectOnPlane, then classification of the PROJECTED curve,
// so the result is correct by construction:
//   * oblique circle -> "ellipse" (rx = R, ry = R*cos(theta));
//   * in-plane circle -> "circle";
//   * edge (near-)perpendicular to the plane -> a single "point";
//   * a face ref projects ALL its boundary wires, appended as REAL (non-construction)
//     fix-pinned sketch entities the user can extrude (SolidWorks Convert Entities).
// ONE-SHOT projection — no associative link to the source (follow-up: T-BL49).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
// MSVC only exposes M_PI from <cmath> when _USE_MATH_DEFINES is set (before the include).
#define _USE_MATH_DEFINES
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include <BRepBuilderAPI_MakeEdge.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "doc/recon.h"    // project_edges_to_plane / face_boundary_edges (the kernel)
#include "doc/session.h"  // doc_new / doc_apply_ops / doc_query / doc_sketch_enter (the op)
#include "geom/plane.h"

using json = nlohmann::json;
using aicad::doc::doc_apply_ops;
using aicad::doc::doc_new;
using aicad::doc::doc_query;
using aicad::doc::doc_sketch_enter;
using aicad::doc::project_edges_to_plane;
using aicad::doc::ProjectedEntity;

namespace {

// ---- sketch DSL (mirrors test_sketch_on_face.cpp) ---------------------------
json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}
json dimv(double v) { return json{{"value", v}, {"expr", json(nullptr)}}; }

json rect_entities(double w, double d) {
    return json::array({
        line("e1", 0, 0, w, 0),
        line("e2", w, 0, w, d),
        line("e3", w, d, 0, d),
        line("e4", 0, d, 0, 0),
    });
}

// Build a W x D x H box and return its doc_id (feature ids "$0" sketch, "$1" extrude).
std::string make_box(const char* name, double W, double D, double H) {
    json nr = doc_new("part", name);
    std::string doc_id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "Base"},
                 {"params", {{"entities", rect_entities(W, D)},
                             {"constraints", json::array()}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "extrude"}, {"name", "Body"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(H)}}},
                 {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"},
                            {"centroid", json::array({W / 2.0, D / 2.0})},
                            {"hint_index", 0}}})}}}}}},
    });
    json r = doc_apply_ops(doc_id, -1, ops, false);
    for (const auto& st : r.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(st.value("ok", false), "box feature failed: " << st.dump());
    return doc_id;
}

}  // namespace

// ---------------------------------------------------------------------------
// Kernel level: project_edges_to_plane classification correctness
// ---------------------------------------------------------------------------

TEST_CASE("kernel: oblique circle projects to an analytic ellipse (rx=R, ry=R*cos45)") {
    // Circle of radius 10 in the XY plane (normal +Z); target plane normal 45 deg off Z.
    gp_Circ c(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0);
    TopoDS_Edge e = BRepBuilderAPI_MakeEdge(c);
    auto plane = aicad::geom::SketchPlane::from(Eigen::Vector3d(0, 0, 0),
                                                Eigen::Vector3d(0, 1, 1));  // 45 deg
    bool approx = false;
    std::vector<ProjectedEntity> out = project_edges_to_plane({e}, plane, approx);
    REQUIRE(out.size() == 1);
    const json& ent = out[0].entity;
    // The load-bearing claim: the projected image is an ANALYTIC ellipse, not a sampled
    // polyline (approx) and not a wrong-radius circle (the old sketch_project_ref math
    // rejected this case entirely).
    REQUIRE_MESSAGE(ent.value("type", std::string()) == "ellipse",
                    "oblique circle must project to 'ellipse', got: " << ent.dump());
    CHECK_FALSE(approx);
    CHECK(ent.value("rx", 0.0) == doctest::Approx(10.0).epsilon(1e-6));
    CHECK(ent.value("ry", 0.0) == doctest::Approx(10.0 * std::cos(M_PI / 4)).epsilon(1e-6));
    CHECK(ent.value("construction", false));  // kernel default: reference geometry
}

TEST_CASE("kernel: in-plane circle stays a circle; perpendicular line collapses to a point") {
    gp_Circ c(gp_Ax2(gp_Pnt(3, 4, 7), gp_Dir(0, 0, 1)), 5.0);
    TopoDS_Edge circle_edge = BRepBuilderAPI_MakeEdge(c);
    TopoDS_Edge vertical_edge = BRepBuilderAPI_MakeEdge(gp_Pnt(2, 2, 0), gp_Pnt(2, 2, 9));
    auto top = aicad::geom::SketchPlane::top();

    bool approx = false;
    std::vector<ProjectedEntity> ents =
        project_edges_to_plane({circle_edge, vertical_edge}, top, approx);
    REQUIRE(ents.size() == 2);
    CHECK_FALSE(approx);

    const json& circ = ents[0].entity;
    CHECK(circ.value("type", std::string()) == "circle");
    CHECK(circ.value("radius", 0.0) == doctest::Approx(5.0).epsilon(1e-9));
    REQUIRE(circ.contains("center"));
    CHECK(circ["center"][0].get<double>() == doctest::Approx(3.0).epsilon(1e-9));
    CHECK(circ["center"][1].get<double>() == doctest::Approx(4.0).epsilon(1e-9));

    const json& pt = ents[1].entity;
    REQUIRE_MESSAGE(pt.value("type", std::string()) == "point",
                    "a plane-perpendicular edge must collapse to 'point', got: " << pt.dump());
    REQUIRE(pt.contains("p"));
    CHECK(pt["p"][0].get<double>() == doctest::Approx(2.0).epsilon(1e-9));
    CHECK(pt["p"][1].get<double>() == doctest::Approx(2.0).epsilon(1e-9));
}

// ---------------------------------------------------------------------------
// Op level: sketch_convert through doc_apply_ops (the exact op both GUIs emit)
// ---------------------------------------------------------------------------

TEST_CASE("op: sketch_convert projects a face loop as REAL entities and they extrude") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("ConvertOp", W, D, H);

    // A second, empty sketch ON the top face (the resolver's own selector).
    json qt = doc_query(doc_id, json{{"of", "face"}, {"side", "top"}});
    REQUIRE(qt.value("count", 0) >= 1);
    json top_sel = qt.at("matches").at(0);
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "SkTop"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", top_sel}}}}}}});
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    // Convert Entities on the BOTTOM face — an external face, not the sketch's own plane.
    json qb = doc_query(doc_id, json{{"of", "face"}, {"side", "bottom"}});
    REQUIRE(qb.value("count", 0) >= 1);
    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "face"}, {"selector", qb.at("matches").at(0)}}})}}});
    json cr = doc_apply_ops(doc_id, -1, cv, false);

    // Contract: $0 = first new entity, $0#1.. = the rest (4 lines for a rectangular loop).
    const json& idm = cr.at("id_map");
    REQUIRE_MESSAGE(idm.contains("$0"),
                    "sketch_convert produced no id_map $0 — op rejected? " << cr.dump());
    std::vector<std::string> new_ids{idm.at("$0").get<std::string>()};
    for (int k = 1; idm.contains("$0#" + std::to_string(k)); ++k)
        new_ids.push_back(idm.at("$0#" + std::to_string(k)).get<std::string>());
    CHECK(new_ids.size() == 4);

    // The converted entities live in the sketch as REAL geometry (construction:false —
    // SolidWorks Convert Entities parity; the old GUI click produced NOTHING).
    json enter = doc_sketch_enter(doc_id, fid);
    REQUIRE(enter.contains("entities"));
    int real_converted = 0;
    for (const auto& e : enter["entities"]) {
        for (const auto& id : new_ids)
            if (e.value("id", std::string()) == id) {
                CHECK_FALSE(e.value("construction", true));
                ++real_converted;
            }
    }
    CHECK(real_converted == 4);
    aicad::doc::doc_sketch_exit(doc_id, fid);

    // And they are a usable profile: extrude the converted loop (region centroid at the
    // face-frame origin (0,0) — the top face's centroid frame).
    json ex = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "extrude"}, {"name", "Boss"},
             {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(8.0)}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", fid},
                        {"centroid", json::array({0.0, 0.0})}, {"hint_index", 0}}})}}}}}}});
    json er = doc_apply_ops(doc_id, -1, ex, false);
    for (const auto& st : er.at("rebuild").at("feature_status"))
        CHECK_MESSAGE(st.value("ok", false),
                      "extrude of the converted loop failed: " << st.dump());
}

// ---------------------------------------------------------------------------
// T-BL49 — ASSOCIATIVE external references
// ---------------------------------------------------------------------------

namespace {

// Fetch the sketch feature json out of a doc_apply_ops/doc_tree response tree.
json feature_of(const json& tree, const std::string& fid) {
    for (const auto& f : tree.at("features"))
        if (f.value("id", std::string()) == fid) return f;
    return json(nullptr);
}

}  // namespace

TEST_CASE("assoc: converted entities re-project (stable ids) when the source moves") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("AssocConvert", W, D, H);  // f1 sketch, f2 extrude

    // Sketch on a STABLE datum plane (plane.top offset 50 — no face-selector churn: this
    // test isolates the associativity of the CONVERT ref itself); Convert Entities on the
    // box's BOTTOM face (the external source).
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "SkAbove"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()},
                         {"offset", 50.0}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    json qb = doc_query(doc_id, json{{"of", "face"}, {"side", "bottom"}});
    REQUIRE(qb.value("count", 0) >= 1);
    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "face"}, {"selector", qb.at("matches").at(0)}}})}}});
    json cr = doc_apply_ops(doc_id, -1, cv, false);

    // Associative by default: the sketch carries ONE external_ref owning the 4 lines.
    json skf = feature_of(cr.at("tree"), fid);
    REQUIRE_FALSE(skf.is_null());
    REQUIRE(skf.at("params").contains("external_refs"));
    const json& xrs = skf["params"]["external_refs"];
    REQUIRE(xrs.size() == 1);
    CHECK(xrs[0].value("stale", true) == false);
    REQUIRE(xrs[0].at("entity_ids").size() == 4);
    std::vector<std::string> ids_before;
    for (const auto& e : xrs[0]["entity_ids"]) ids_before.push_back(e.get<std::string>());

    // Extrude the converted loop so a downstream feature rides on the converted geometry
    // (a separate body floating above the box — it must NOT erode the source face, or the
    // test would measure selector churn instead of associativity).
    json ex = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "extrude"}, {"name", "Boss"},
             {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(5.0)}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", fid},
                        {"centroid", json::array({W / 2.0, D / 2.0})},
                        {"hint_index", 0}}})}}}}}}});
    json er = doc_apply_ops(doc_id, -1, ex, false);
    for (const auto& st : er.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(st.value("ok", false), "pre-edit tree failed: " << st.dump());
    const double vol_before = er.value("volume", 0.0);
    CHECK(vol_before == doctest::Approx(W * D * H + W * D * 5.0).epsilon(1e-6));

    // EDIT THE SOURCE: widen the base rectangle 40 -> 60 (the bottom face grows).
    json ed = json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e1"},
             {"params", {{"p2", json::array({60.0, 0.0})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"},
             {"params", {{"p1", json::array({60.0, 0.0})}, {"p2", json::array({60.0, D})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e3"},
             {"params", {{"p1", json::array({60.0, D})}}}},
    });
    json rr = doc_apply_ops(doc_id, -1, ed, false);
    for (const auto& st : rr.at("rebuild").at("feature_status"))
        CHECK_MESSAGE(st.value("ok", false), "post-edit rebuild failed: " << st.dump());

    // The ref must still be live and own the SAME entity ids (constraints would survive).
    skf = feature_of(rr.at("tree"), fid);
    const json& xr2 = skf["params"]["external_refs"][0];
    CHECK(xr2.value("stale", true) == false);
    REQUIRE(xr2.at("entity_ids").size() == ids_before.size());
    for (std::size_t i = 0; i < ids_before.size(); ++i)
        CHECK(xr2["entity_ids"][i].get<std::string>() == ids_before[i]);

    // And the geometry MOVED: the converted loop now spans 60 in u (datum-plane frame =
    // absolute world x/y, so u reaches 60 instead of 40).
    double max_u = 0.0;
    for (const auto& e : skf["params"]["entities"]) {
        bool owned = false;
        for (const auto& id : ids_before)
            if (e.value("id", std::string()) == id) owned = true;
        if (!owned) continue;
        for (const char* key : {"p1", "p2"})
            if (e.contains(key))
                max_u = std::max(max_u, std::abs(e.at(key).at(0).get<double>()));
    }
    CHECK_MESSAGE(max_u == doctest::Approx(60.0).epsilon(1e-6),
                  "converted loop did not follow the widened source (max_u=" << max_u << ")");

    // Downstream extrude UPDATED: the boss riding on the converted loop widened with it.
    CHECK(rr.value("volume", 0.0) ==
          doctest::Approx(60.0 * D * H + 60.0 * D * 5.0).epsilon(1e-6));
}

TEST_CASE("assoc: deleting the source feature marks the ref stale, rebuild still succeeds") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("StaleConvert", W, D, H);

    // Cylinder boss on the box top (circle sketch + extrude).
    json qt = doc_query(doc_id, json{{"of", "face"}, {"side", "top"}});
    json boss = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "SkBoss"},
                 {"params", {{"entities", json::array({json{{"id", "e1"}, {"type", "circle"},
                              {"construction", false}, {"center", json::array({0.0, 0.0})},
                              {"radius", 5.0}}})},
                             {"constraints", json::array()}}},
                 {"refs", {{"plane", qt.at("matches").at(0)}}}}}},
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "extrude"}, {"name", "CylBoss"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(10.0)}}},
                 {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"},
                            {"centroid", json::array({0.0, 0.0})}, {"hint_index", 0}}})}}}}}},
    });
    json br = doc_apply_ops(doc_id, -1, boss, false);
    const std::string boss_fid = br.at("id_map").at("$1").get<std::string>();

    // New sketch above everything; convert the cylinder's TOP circular edge.
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "SkAbove"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()},
                         {"offset", 50.0}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    json qe = doc_query(doc_id, json{{"of", "edge"}});
    json circ_sel;
    double best_z = -1e30;
    for (const auto& m : qe.at("matches")) {
        if (m.value("surf", std::string()) != "circle") continue;
        const double z = m.at("centroid").at(2).get<double>();
        if (z > best_z) { best_z = z; circ_sel = m; }
    }
    REQUIRE_MESSAGE(!circ_sel.is_null(), "no circular edge found: " << qe.dump());

    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "edge"}, {"selector", circ_sel}}})}}});
    json cr = doc_apply_ops(doc_id, -1, cv, false);
    json skf = feature_of(cr.at("tree"), fid);
    REQUIRE(skf["params"]["external_refs"].size() == 1);
    CHECK(skf["params"]["external_refs"][0].value("stale", true) == false);
    const std::string owned_id =
        skf["params"]["external_refs"][0]["entity_ids"][0].get<std::string>();

    // DELETE the source (boss extrude + its sketch): the circular edge vanishes.
    json del = json::array({json{{"op", "delete_feature"}, {"id", boss_fid},
                                 {"cascade", true}}});
    json dr = doc_apply_ops(doc_id, -1, del, false);
    for (const auto& st : dr.at("rebuild").at("feature_status"))
        CHECK_MESSAGE(st.value("ok", false),
                      "rebuild after source delete failed: " << st.dump());

    // Stale, last-good geometry kept (same entity id, same radius).
    skf = feature_of(dr.at("tree"), fid);
    const json& xr = skf["params"]["external_refs"][0];
    CHECK_MESSAGE(xr.value("stale", false) == true,
                  "ref should be stale after the source was deleted: " << xr.dump());
    bool found = false;
    for (const auto& e : skf["params"]["entities"])
        if (e.value("id", std::string()) == owned_id) {
            found = true;
            CHECK(e.value("type", std::string()) == "circle");
            CHECK(e.value("radius", 0.0) == doctest::Approx(5.0).epsilon(1e-6));
        }
    CHECK_MESSAGE(found, "last-good converted entity must survive a stale ref");
}

TEST_CASE("assoc: associative=false is a one-shot copy (no external_refs, no update)") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("OneShotConvert", W, D, H);
    json qt = doc_query(doc_id, json{{"of", "face"}, {"side", "top"}});
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "Sk"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", qt.at("matches").at(0)}}}}}}});
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    json qb = doc_query(doc_id, json{{"of", "face"}, {"side", "bottom"}});
    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "face"}, {"selector", qb.at("matches").at(0)}}})},
        {"associative", false}}});
    json cr = doc_apply_ops(doc_id, -1, cv, false);
    json skf = feature_of(cr.at("tree"), fid);
    const json& params = skf.at("params");
    const bool has_refs = params.contains("external_refs") &&
                          !params["external_refs"].empty();
    CHECK_FALSE(has_refs);

    // Widen the base; the one-shot copy must NOT move.
    json ed = json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e1"},
             {"params", {{"p2", json::array({60.0, 0.0})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"},
             {"params", {{"p1", json::array({60.0, 0.0})}, {"p2", json::array({60.0, D})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e3"},
             {"params", {{"p1", json::array({60.0, D})}}}},
    });
    json rr = doc_apply_ops(doc_id, -1, ed, false);
    skf = feature_of(rr.at("tree"), fid);
    double max_u = 0.0;
    for (const auto& e : skf["params"]["entities"])
        for (const char* key : {"p1", "p2"})
            if (e.contains(key))
                max_u = std::max(max_u, std::abs(e.at(key).at(0).get<double>()));
    CHECK(max_u == doctest::Approx(20.0).epsilon(1e-6));
}

TEST_CASE("op: sketch_convert rejects empty refs with invalid_op") {
    std::string doc_id = make_box("ConvertOpBad", 20, 20, 10);
    json qt = doc_query(doc_id, json{{"of", "face"}, {"side", "top"}});
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "Sk"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", qt.at("matches").at(0)}}}}}}});
    json mr = doc_apply_ops(doc_id, -1, mk, false);
    const std::string fid = mr.at("id_map").at("$0").get<std::string>();

    json bad = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
                                 {"refs", json::array()}}});
    CHECK_THROWS_AS(doc_apply_ops(doc_id, -1, bad, false), aicad::doc::OpError);
}
