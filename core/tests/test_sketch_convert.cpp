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
// chudware::doc::project_edges_to_plane (recon.cpp) — true orthographic projection along the
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

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
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
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_new;
using chudware::doc::doc_query;
using chudware::doc::doc_sketch_enter;
using chudware::doc::project_edges_to_plane;
using chudware::doc::ProjectedEntity;

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

// Widest |u| reached by the entities a converted (external-ref) group owns — the scalar
// that says whether the projection followed its source. Ignores entities the user drew.
double converted_max_u(const json& sketch_feature, const std::vector<std::string>& owned) {
    double max_u = 0.0;
    for (const auto& e : sketch_feature.at("params").at("entities")) {
        const std::string eid = e.value("id", std::string());
        if (std::find(owned.begin(), owned.end(), eid) == owned.end()) continue;
        for (const char* key : {"p1", "p2"})
            if (e.contains(key))
                max_u = std::max(max_u, std::abs(e.at(key).at(0).get<double>()));
    }
    return max_u;
}

}  // namespace

// ---------------------------------------------------------------------------
// Kernel level: project_edges_to_plane classification correctness
// ---------------------------------------------------------------------------

TEST_CASE("kernel: oblique circle projects to an analytic ellipse (rx=R, ry=R*cos45)") {
    // Circle of radius 10 in the XY plane (normal +Z); target plane normal 45 deg off Z.
    gp_Circ c(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10.0);
    TopoDS_Edge e = BRepBuilderAPI_MakeEdge(c);
    auto plane = chudware::geom::SketchPlane::from(Eigen::Vector3d(0, 0, 0),
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
    auto top = chudware::geom::SketchPlane::top();

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
    chudware::doc::doc_sketch_exit(doc_id, fid);

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

TEST_CASE("assoc: undo/redo across a re-projection restores both states") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("UndoConvert", W, D, H);

    // Same rig as the re-projection test: stable datum-plane sketch 50 above the box,
    // Convert Entities on the box's bottom face, then a boss riding the converted loop.
    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "SkAbove"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()},
                         {"offset", 50.0}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    const std::string fid =
        doc_apply_ops(doc_id, -1, mk, false).at("id_map").at("$0").get<std::string>();

    json qb = doc_query(doc_id, json{{"of", "face"}, {"side", "bottom"}});
    REQUIRE(qb.value("count", 0) >= 1);
    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "face"}, {"selector", qb.at("matches").at(0)}}})}}});
    doc_apply_ops(doc_id, -1, cv, false);

    json ex = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "extrude"}, {"name", "Boss"},
             {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(5.0)}}},
             {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", fid},
                        {"centroid", json::array({W / 2.0, D / 2.0})},
                        {"hint_index", 0}}})}}}}}}});
    json er = doc_apply_ops(doc_id, -1, ex, false);
    json skf = feature_of(er.at("tree"), fid);
    REQUIRE(skf.at("params").at("external_refs").size() == 1);
    std::vector<std::string> ids_before;
    for (const auto& e : skf["params"]["external_refs"][0]["entity_ids"])
        ids_before.push_back(e.get<std::string>());
    REQUIRE(ids_before.size() == 4);
    const double vol_narrow = W * D * H + W * D * 5.0;
    const double vol_wide = 60.0 * D * H + 60.0 * D * 5.0;
    CHECK(er.value("volume", 0.0) == doctest::Approx(vol_narrow).epsilon(1e-6));
    CHECK(converted_max_u(skf, ids_before) == doctest::Approx(W).epsilon(1e-6));

    // ONE undoable batch widens the source 40 -> 60; the ref re-projects with it.
    json ed = json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e1"},
             {"params", {{"p2", json::array({60.0, 0.0})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"},
             {"params", {{"p1", json::array({60.0, 0.0})}, {"p2", json::array({60.0, D})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e3"},
             {"params", {{"p1", json::array({60.0, D})}}}},
    });
    json rr = doc_apply_ops(doc_id, -1, ed, false);
    CHECK(rr.value("volume", 0.0) == doctest::Approx(vol_wide).epsilon(1e-6));
    CHECK(converted_max_u(feature_of(rr.at("tree"), fid), ids_before) ==
          doctest::Approx(60.0).epsilon(1e-6));

    // UNDO: the re-projection is part of the same undo step as the source edit, so the
    // converted loop must snap back to 40 — not linger at the widened coordinates.
    json ur = chudware::doc::doc_undo(doc_id);
    skf = feature_of(ur.at("tree"), fid);
    const json& xr_undo = skf.at("params").at("external_refs")[0];
    CHECK(xr_undo.value("stale", true) == false);
    REQUIRE(xr_undo.at("entity_ids").size() == ids_before.size());
    for (std::size_t i = 0; i < ids_before.size(); ++i)
        CHECK(xr_undo["entity_ids"][i].get<std::string>() == ids_before[i]);
    CHECK_MESSAGE(converted_max_u(skf, ids_before) == doctest::Approx(W).epsilon(1e-6),
                  "undo left the converted loop at the post-edit projection");
    CHECK(ur.value("volume", 0.0) == doctest::Approx(vol_narrow).epsilon(1e-6));

    // REDO: back to the widened projection, same entity ids throughout.
    json rdr = chudware::doc::doc_redo(doc_id);
    skf = feature_of(rdr.at("tree"), fid);
    const json& xr_redo = skf.at("params").at("external_refs")[0];
    CHECK(xr_redo.value("stale", true) == false);
    REQUIRE(xr_redo.at("entity_ids").size() == ids_before.size());
    for (std::size_t i = 0; i < ids_before.size(); ++i)
        CHECK(xr_redo["entity_ids"][i].get<std::string>() == ids_before[i]);
    CHECK(converted_max_u(skf, ids_before) == doctest::Approx(60.0).epsilon(1e-6));
    CHECK(rdr.value("volume", 0.0) == doctest::Approx(vol_wide).epsilon(1e-6));
}

TEST_CASE("assoc: external refs survive a native .step+ save/open and stay associative") {
    namespace fs = std::filesystem;
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("SaveConvert", W, D, H);

    json mk = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "SkAbove"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()},
                         {"offset", 50.0}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    const std::string fid =
        doc_apply_ops(doc_id, -1, mk, false).at("id_map").at("$0").get<std::string>();

    json qb = doc_query(doc_id, json{{"of", "face"}, {"side", "bottom"}});
    REQUIRE(qb.value("count", 0) >= 1);
    json cv = json::array({json{{"op", "sketch_convert"}, {"sketch", fid},
        {"refs", json::array({json{{"kind", "face"}, {"selector", qb.at("matches").at(0)}}})}}});
    json cr = doc_apply_ops(doc_id, -1, cv, false);
    // NB: bind the feature to a named json — feature_of returns by value, and a temporary
    // in a range-for's sub-expression is not lifetime-extended (silently empty iteration).
    json created = feature_of(cr.at("tree"), fid);
    std::vector<std::string> ids_before;
    for (const auto& e : created.at("params").at("external_refs")[0].at("entity_ids"))
        ids_before.push_back(e.get<std::string>());
    REQUIRE(ids_before.size() == 4);

    fs::path out = fs::temp_directory_path() / "chudware_convert_xref_roundtrip.step+";
    chudware::doc::doc_save(doc_id, out.string());
    REQUIRE(fs::exists(out));
    json op = chudware::doc::doc_open(out.string());
    const std::string nid = op.at("doc_id").get<std::string>();

    // The record survived the container: same ref, same owned entity ids, still live.
    json skf = feature_of(op.at("tree"), fid);
    REQUIRE_MESSAGE(!skf.is_null(), "reopened doc lost the converted sketch: " << fid);
    REQUIRE(skf.at("params").at("external_refs").size() == 1);
    const json& xr = skf["params"]["external_refs"][0];
    CHECK(xr.value("stale", true) == false);
    CHECK(xr.contains("source"));
    REQUIRE(xr.at("entity_ids").size() == ids_before.size());
    for (std::size_t i = 0; i < ids_before.size(); ++i)
        CHECK(xr["entity_ids"][i].get<std::string>() == ids_before[i]);
    CHECK(converted_max_u(skf, ids_before) == doctest::Approx(W).epsilon(1e-6));

    // ...and it is still ASSOCIATIVE in the reopened session, not a dead record: widen the
    // source in the NEW doc and the converted loop follows.
    json ed = json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e1"},
             {"params", {{"p2", json::array({60.0, 0.0})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"},
             {"params", {{"p1", json::array({60.0, 0.0})}, {"p2", json::array({60.0, D})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e3"},
             {"params", {{"p1", json::array({60.0, D})}}}},
    });
    json rr = doc_apply_ops(nid, -1, ed, false);
    skf = feature_of(rr.at("tree"), fid);
    CHECK(skf["params"]["external_refs"][0].value("stale", true) == false);
    CHECK_MESSAGE(converted_max_u(skf, ids_before) == doctest::Approx(60.0).epsilon(1e-6),
                  "reopened external ref stopped following its source");

    std::error_code ec;
    fs::remove(out, ec);
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
    CHECK_THROWS_AS(doc_apply_ops(doc_id, -1, bad, false), chudware::doc::OpError);
}

// ---------------------------------------------------------------------------
// sketch_edit_entity shapes + profile picks that lose their region (2026-10-10 6-axis run)
// ---------------------------------------------------------------------------

namespace {

// Apply a batch that must refuse; return {message, detail.reason}.
std::pair<std::string, std::string> edit_refusal(const std::string& doc_id, const json& op) {
    try {
        doc_apply_ops(doc_id, -1, json::array({op}), false);
    } catch (const chudware::doc::OpError& e) {
        return {e.what(), e.detail.value("reason", std::string())};
    }
    return {std::string(), std::string()};
}

json entity_of(const json& sketch_feature, const std::string& eid) {
    for (const auto& e : sketch_feature.at("params").at("entities"))
        if (e.value("id", std::string()) == eid) return e;
    return json(nullptr);
}

bool at_point(const json& p, double x, double y) {
    return p.is_array() && p.size() == 2 && std::abs(p[0].get<double>() - x) < 1e-6 &&
           std::abs(p[1].get<double>() - y) < 1e-6;
}

// A sketch with two closed 10x10 rectangles at x0 = 0 and 20 (ids e1..e4, e5..e8) and a
// boss extrude whose profile picks regions by (centroid, hint_index). Returns the response.
json two_rect_boss(const std::string& doc_id,
                   const std::vector<std::pair<json, int>>& picks) {
    json ents = json::array();
    int n = 0;
    for (double x0 : {0.0, 20.0}) {
        const double xs[5][2] = {{x0, 0}, {x0 + 10, 0}, {x0 + 10, 10}, {x0, 10}, {x0, 0}};
        for (int k = 0; k < 4; ++k) {
            const std::string eid = "e" + std::to_string(++n);
            ents.push_back(line(eid.c_str(), xs[k][0], xs[k][1], xs[k + 1][0], xs[k + 1][1]));
        }
    }
    json profile = json::array();
    for (const auto& [centroid, hint] : picks)
        profile.push_back(json{{"kind", "region"}, {"sketch", "$0"}, {"centroid", centroid},
                               {"hint_index", hint}});
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "sketch"}, {"name", "TwoRects"},
                 {"params", {{"entities", ents}, {"constraints", json::array()}}},
                 {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "add_feature"}, {"feature",
            json{{"type", "extrude"}, {"name", "Boss"},
                 {"params", {{"mode", "boss"}, {"end", "blind"}, {"depth", dimv(5.0)}}},
                 {"refs", {{"profile", profile}}}}}},
    });
    return doc_apply_ops(doc_id, -1, ops, false);
}

}  // namespace

TEST_CASE("sketch_edit_entity: the agent 'entity' form changes the coordinates") {
    const double W = 40.0, D = 30.0, H = 20.0;
    std::string doc_id = make_box("EditEntityForm", W, D, H);  // f1 sketch e1..e4, f2 extrude

    // Live 2026-10-10 shapes: entity:{id,...} and op.id + entity:{type,...}. The first
    // failed "no entity ''"; the second returned ok:true and changed NOTHING.
    json ed = json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"},
             {"entity", {{"id", "e1"}, {"p2", json::array({60.0, 0.0})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"},
             {"entity", {{"type", "line"}, {"p1", json::array({60.0, 0.0})},
                         {"p2", json::array({60.0, D})}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e3"},
             {"entity", {{"id", "e3"}, {"p1", json::array({60.0, D})}}}},
    });
    json rr = doc_apply_ops(doc_id, -1, ed, false);
    for (const auto& st : rr.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(st.value("ok", false), "post-edit rebuild failed: " << st.dump());

    const json sk = feature_of(rr.at("tree"), "f1");
    CHECK(at_point(entity_of(sk, "e1").at("p2"), 60.0, 0.0));
    CHECK(at_point(entity_of(sk, "e2").at("p1"), 60.0, 0.0));
    CHECK(at_point(entity_of(sk, "e2").at("p2"), 60.0, D));
    CHECK(entity_of(sk, "e2").at("type") == "line");
    CHECK(at_point(entity_of(sk, "e3").at("p1"), 60.0, D));
    CHECK(rr.value("volume", 0.0) == doctest::Approx(60.0 * D * H).epsilon(1e-6));
}

TEST_CASE("sketch_edit_entity: a bad shape refuses with a named reason and changes nothing") {
    std::string doc_id = make_box("EditEntityBad", 40.0, 30.0, 20.0);
    const json base{{"op", "sketch_edit_entity"}, {"sketch", "f1"}};
    auto with = [&](json extra) {
        json op = base;
        for (auto it = extra.begin(); it != extra.end(); ++it) op[it.key()] = it.value();
        return op;
    };
    const json p = json::array({1.0, 2.0});
    struct Case { const char* name; json op; const char* reason; std::vector<std::string> says; };
    const std::vector<Case> cases = {
        {"no id", with({{"entity", {{"p1", p}}}}), "missing_entity_id", {"no entity id", "p1"}},
        {"unknown id", with({{"entity", {{"id", "e99"}, {"p1", p}}}}), "unknown_entity",
         {"'e99'", "e1, e2, e3, e4"}},
        {"conflicting ids", with({{"id", "e1"}, {"entity", {{"id", "e2"}, {"p1", p}}}}),
         "conflicting_entity_id", {"'e1'", "'e2'"}},
        {"empty entity", with({{"entity", {{"id", "e1"}}}}), "empty_patch", {"no fields"}},
        {"no patch at all", with({{"id", "e1"}}), "empty_patch", {"no fields"}},
        {"type change", with({{"entity", {{"id", "e1"}, {"type", "arc"}}}}), "type_change",
         {"is a line", "arc"}},
        {"field of another type", with({{"entity", {{"id", "e1"}, {"radius", 5.0}}}}),
         "unknown_entity_field", {"'radius'", "p1", "p2"}},
        {"both carriers", with({{"id", "e1"}, {"entity", {{"p1", p}}}, {"params", {{"p1", p}}}}),
         "two_patches", {"'entity'", "'params'"}},
        {"entity not an object", with({{"id", "e1"}, {"entity", "e1"}}), "patch_not_object",
         {"'entity'"}},
    };
    for (const Case& c : cases) {
        const auto refusal = edit_refusal(doc_id, c.op);
        const std::string& msg = refusal.first;
        const std::string& reason = refusal.second;
        INFO(c.name << ": " << msg);
        REQUIRE_MESSAGE(!msg.empty(), c.name << " must refuse, not return ok");
        CHECK(reason == c.reason);
        for (const auto& s : c.says) CHECK_MESSAGE(msg.find(s) != std::string::npos, "missing " << s);
    }
    // Nothing changed: the rectangle is still 40 wide.
    json tr = chudware::doc::doc_tree(doc_id);
    CHECK(at_point(entity_of(feature_of(tr.at("tree"), "f1"), "e1").at("p2"), 40.0, 0.0));
}

TEST_CASE("profile: a centroid pick whose region disappears after an edit fails the feature") {
    json nr = doc_new("part", "LostRegion");
    const std::string doc_id = nr.at("doc_id").get<std::string>();
    json built = two_rect_boss(doc_id, {{json::array({5.0, 5.0}), 0},
                                        {json::array({25.0, 5.0}), 1}});
    for (const auto& st : built.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(st.value("ok", false), "two-rect boss failed: " << st.dump());
    CHECK(built.value("volume", 0.0) == doctest::Approx(2 * 10.0 * 10.0 * 5.0).epsilon(1e-6));
    const std::string sk = built.at("id_map").at("$0").get<std::string>();
    const long rev = built.at("revision").get<long>();

    // Open the second rectangle. Before the fix the [25,5] pick snapped onto the first
    // rectangle, was de-duplicated, and the boss silently lost half its area with ok:true.
    json rr = doc_apply_ops(doc_id, -1, json::array({json{{"op", "sketch_delete_entity"},
                                                          {"sketch", sk}, {"id", "e8"}}}),
                            false);
    const json& rows = rr.at("rebuild").at("feature_status");
    const json& boss = rows.back();
    const std::string err = boss.value("error", json(nullptr)).is_string()
                                ? boss["error"].get<std::string>() : std::string();
    MESSAGE("lost region -> " << err);
    REQUIRE_MESSAGE(!boss.value("ok", true), "a lost region must fail the feature, not shrink it");
    CHECK(err.find("profile[1]") != std::string::npos);
    CHECK(err.find("outside") != std::string::npos);
    CHECK(err.find("1 material region") != std::string::npos);
    // The batch rolled back: same revision, e8 still in the sketch.
    CHECK(rr.at("revision").get<long>() == rev);
    CHECK(!entity_of(feature_of(rr.at("tree"), sk), "e8").is_null());
}

TEST_CASE("profile: two centroid picks that land on ONE region refuse (regions merged)") {
    json nr = doc_new("part", "MergedRegion");
    const std::string doc_id = nr.at("doc_id").get<std::string>();
    // Both picks sit inside the first rectangle at different points: the profile claims two
    // regions where the sketch has one.
    json built = two_rect_boss(doc_id, {{json::array({3.0, 5.0}), 0},
                                        {json::array({7.0, 5.0}), 0}});
    const json& boss = built.at("rebuild").at("feature_status").back();
    const std::string err = boss.value("error", json(nullptr)).is_string()
                                ? boss["error"].get<std::string>() : std::string();
    MESSAGE("merged picks -> " << err);
    REQUIRE_MESSAGE(!boss.value("ok", true), "two picks on one region must not silently merge");
    CHECK(err.find("same region") != std::string::npos);
    CHECK(err.find("profile[0]") != std::string::npos);
    CHECK(err.find("profile[1]") != std::string::npos);
}

namespace {
std::string add_projection_sketch(const std::string& doc, double offset = 0.0) {
    const json result = doc_apply_ops(doc, -1, json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "sketch"},
            {"params", {{"entities", json::array()}, {"constraints", json::array()}, {"offset", offset}}},
            {"refs", {{"plane", "plane.top"}}}}}}}), false);
    return result.at("id_map").at("$0").get<std::string>();
}
json convert_one(const std::string& doc, const std::string& sketch, const json& ref) {
    return doc_apply_ops(doc, -1, json::array({
        json{{"op", "sketch_convert"}, {"sketch", sketch}, {"refs", json::array({ref})}}}), false);
}
}

TEST_CASE("convert: model vertex becomes an associative projected point") {
    const std::string doc = make_box("VertexReference", 40, 30, 20);
    const std::string sketch = add_projection_sketch(doc);
    json selector;
    for (int i = 1; i <= 8; ++i) {
        const json candidate = chudware::doc::doc_resolve_pick(doc, 1, "vertex", i).at("selector");
        if (candidate.at("centroid") == json::array({40.0, 30.0, 20.0})) selector = candidate;
    }
    REQUIRE(selector.is_object());
    const json ref{{"kind", "vertex"}, {"selector", selector}};
    const json result = convert_one(doc, sketch, ref);
    const json feature = feature_of(result.at("tree"), sketch);
    const json point = entity_of(feature, result.at("id_map").at("$0"));
    REQUIRE(point.at("type") == "point");
    CHECK(at_point(point.at("p"), 40, 30));
    CHECK_FALSE(feature.at("params").at("external_refs").front().at("stale").get<bool>());
    const json preview = chudware::doc::doc_sketch_project_ref(doc, sketch, ref);
    CHECK(preview.at("entity").at("type") == "point");
    CHECK(at_point(preview.at("entity").at("p"), 40, 30));
    const auto start = std::chrono::steady_clock::now();
    for (int i = 0; i < 8; ++i) {
        const json refreshed = chudware::doc::doc_rebuild(doc);
        CHECK(feature_of(refreshed.at("tree"), sketch).at("params") == feature.at("params"));
    }
    MESSAGE("unchanged solid-backed refresh x8: " << std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - start).count() << " ms");
    const json edge = doc_query(doc, {{"of", "edge"}, {"near", {20, 0, 20}}, {"count", 1}});
    REQUIRE(edge.at("count").get<int>() == 1);
    bool refused = false;
    try { convert_one(doc, sketch, {{"kind", "edge"}, {"selector", edge.at("matches").front()},
                                    {"mode", "pierce"}}); }
    catch (const chudware::doc::OpError& error) {
        refused = true;
        CHECK(error.code == "convert_failed");
        CHECK(error.detail.at("reason") == "no plane crossing");
    }
    CHECK(refused);
    chudware::doc::doc_close(doc);
}

TEST_CASE("convert: pierce finds the plane crossing of a slanted edge and follows edits") {
    const std::string doc = doc_new("part", "PierceReference").at("doc_id");
    json entities = json::array({line("e1", 0, -10, 20, 10), line("e2", 20, 10, 40, 10),
                                 line("e3", 40, 10, 40, -10), line("e4", 40, -10, 0, -10)});
    const json body = doc_apply_ops(doc, -1, json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "sketch"},
            {"params", {{"entities", entities}, {"constraints", json::array()}}},
            {"refs", {{"plane", "plane.front"}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "extrude"},
            {"params", {{"mode", "boss"}, {"depth", dimv(30)}}},
            {"refs", {{"profile", "$0"}}}}}}}), false);
    for (const auto& status : body.at("rebuild").at("feature_status"))
        REQUIRE_MESSAGE(status.value("ok", false), status.dump());
    const std::string sketch = add_projection_sketch(doc, 5);
    const json query = doc_query(doc, {{"of", "edge"}, {"near", {10, 0, 0}}, {"count", 1}});
    REQUIRE(query.at("count").get<int>() == 1);
    const json ref{{"kind", "edge"}, {"selector", query.at("matches").front()},
                   {"mode", "pierce"}, {"near", {15, 0}}};
    const json converted = convert_one(doc, sketch, ref);
    const std::string eid = converted.at("id_map").at("$0");
    json feature = feature_of(converted.at("tree"), sketch);
    REQUIRE(entity_of(feature, eid).at("type") == "point");
    CHECK(at_point(entity_of(feature, eid).at("p"), 15, 0));
    CHECK(feature.at("params").at("external_refs").front().at("source") == ref);
    const json edited = doc_apply_ops(doc, -1, json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e1"}, {"params", {{"p2", {22, 10}}}}},
        json{{"op", "sketch_edit_entity"}, {"sketch", "f1"}, {"id", "e2"}, {"params", {{"p1", {22, 10}}}}}
    }), false);
    feature = feature_of(edited.at("tree"), sketch);
    CHECK_FALSE(feature.at("params").at("external_refs").front().at("stale").get<bool>());
    CHECK(at_point(entity_of(feature, eid).at("p"), 16.5, 0));
    const json shifted = doc_apply_ops(doc, -1, json::array({
        json{{"op", "edit_feature"}, {"id", sketch}, {"params", {{"offset", 6}}}}
    }), false);
    feature = feature_of(shifted.at("tree"), sketch);
    CHECK_FALSE(feature.at("params").at("external_refs").front().at("stale").get<bool>());
    CHECK(at_point(entity_of(feature, eid).at("p"), 17.6, 0));
    chudware::doc::doc_close(doc);
}

TEST_CASE("kernel: pierce chooses a nearby crossing and rejects coplanar or disjoint edges") {
    const TopoDS_Edge circle = BRepBuilderAPI_MakeEdge(
        gp_Circ(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 10));
    const auto front = chudware::geom::SketchPlane::front();
    const auto positive = chudware::doc::pierce_edge_to_plane(circle, front, Eigen::Vector2d(9, 0));
    const auto negative = chudware::doc::pierce_edge_to_plane(circle, front, Eigen::Vector2d(-9, 0));
    REQUIRE(positive.has_value());
    REQUIRE(negative.has_value());
    CHECK(at_point(positive->at("p"), 10, 0));
    CHECK(at_point(negative->at("p"), -10, 0));
    CHECK_FALSE(chudware::doc::pierce_edge_to_plane(circle, chudware::geom::SketchPlane::top()));
    const TopoDS_Edge above = BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 2), gp_Pnt(10, 0, 3));
    CHECK_FALSE(chudware::doc::pierce_edge_to_plane(above, chudware::geom::SketchPlane::top()));
}

TEST_CASE("convert: datum axes and origin work without a solid and remain stable on refresh") {
    const std::string doc = doc_new("part", "DatumReferences").at("doc_id");
    const std::string sketch = add_projection_sketch(doc);
    const json converted = doc_apply_ops(doc, -1, json::array({
        json{{"op", "sketch_convert"}, {"sketch", sketch},
             {"refs", json::array({json{{"datum", "axis.x"}}, json{{"datum", "axis.z"}},
                                    json{{"datum", "origin"}}})}}}), false);
    const json before = feature_of(converted.at("tree"), sketch).at("params");
    REQUIRE(before.at("entities").size() == 3);
    CHECK(before.at("entities")[0].at("type") == "line");
    CHECK(before.at("entities")[1].at("type") == "point");
    CHECK(at_point(before.at("entities")[1].at("p"), 0, 0));
    CHECK(at_point(before.at("entities")[2].at("p"), 0, 0));
    const auto start = std::chrono::steady_clock::now();
    for (int i = 0; i < 8; ++i) {
        const json refreshed = chudware::doc::doc_rebuild(doc);
        CHECK(feature_of(refreshed.at("tree"), sketch).at("params") == before);
    }
    MESSAGE("unchanged refresh x8: " << std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - start).count() << " ms");
    chudware::doc::doc_close(doc);
}

TEST_CASE("convert: another sketch entity is world lifted and re-resolved after its edit") {
    const std::string doc = doc_new("part", "SketchEntityReference").at("doc_id");
    const json source = doc_apply_ops(doc, -1, json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "sketch"},
            {"params", {{"entities", json::array({line("e1", 2, 3, 12, 9)})},
                        {"constraints", json::array()}, {"offset", 7}}},
            {"refs", {{"plane", "plane.front"}}}}}}}), false);
    const std::string source_id = source.at("id_map").at("$0");
    const std::string sketch = add_projection_sketch(doc);
    const json result = convert_one(doc, sketch,
        {{"kind", "sketch_entity"}, {"sketch", source_id}, {"entity", "e1"}});
    const std::string eid = result.at("id_map").at("$0");
    json projected = entity_of(feature_of(result.at("tree"), sketch), eid);
    CHECK(at_point(projected.at("p1"), 2, -7));
    CHECK(at_point(projected.at("p2"), 12, -7));
    const json edited = doc_apply_ops(doc, -1, json::array({
        json{{"op", "sketch_edit_entity"}, {"sketch", source_id}, {"id", "e1"},
             {"params", {{"p2", {15, 11}}}}}}), false);
    projected = entity_of(feature_of(edited.at("tree"), sketch), eid);
    CHECK(at_point(projected.at("p2"), 15, -7));
    chudware::doc::doc_close(doc);
}

TEST_CASE("fully define op: same-batch rectangle receives design-intent constraints and DOF zero") {
    const std::string doc = doc_new("part", "FullyDefineRectangle").at("doc_id");
    const json result = doc_apply_ops(doc, -1, json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "sketch"},
            {"params", {{"entities", json::array({json{{"type", "rectangle"},
                {"corner1", {0, 0}}, {"corner2", {40, 30}}}})}, {"constraints", json::array()}}},
            {"refs", {{"plane", "plane.top"}}}}}},
        json{{"op", "sketch_fully_define"}, {"sketch", "$0"}}}), false);
    REQUIRE(result.at("id_map").contains("$1"));
    const std::string sketch = result.at("id_map").at("$0");
    const json feature = feature_of(result.at("tree"), sketch);
    CHECK(feature.at("params").at("entities").size() == 4);
    CHECK(feature.at("params").at("constraints").size() > 8);
    for (const json& constraint : feature.at("params").at("constraints"))
        CHECK(constraint.at("type") != "fix");
    const json health = chudware::doc::doc_sketch_health(doc, sketch);
    CHECK_MESSAGE(health.at("dof") == 0, health.dump());
    CHECK(health.at("converged").get<bool>());
    CHECK(health.at("skipped_constraints").empty());
    const json again = doc_apply_ops(doc, -1,
        json::array({json{{"op", "sketch_fully_define"}, {"sketch", sketch}}}), false);
    CHECK_FALSE(again.at("id_map").contains("$0"));
    chudware::doc::doc_close(doc);
}

TEST_CASE("composites: polygon and polyline retain connected editable design intent") {
    const std::string doc = doc_new("part", "CompositeRelations").at("doc_id");
    const std::string sketch = add_projection_sketch(doc);
    chudware::doc::doc_set_meta(doc, "sketch.auto_coincident", false);
    const json result = doc_apply_ops(doc, -1, json::array({
        json{{"op", "sketch_add_entity"}, {"sketch", sketch},
             {"entity", {{"type", "polygon"}, {"sides", 5}, {"center", {0, 0}}, {"radius", 10}}}},
        json{{"op", "sketch_add_entity"}, {"sketch", sketch},
             {"entity", {{"type", "polyline"}, {"points", {{30, 0}, {40, 0}, {40, 10}}}}}}
    }), false);
    const json params = feature_of(result.at("tree"), sketch).at("params");
    int coincident = 0, equal = 0;
    for (const json& constraint : params.at("constraints")) {
        coincident += constraint.at("type") == "coincident";
        equal += constraint.at("type") == "equal";
    }
    CHECK(params.at("entities").size() == 7);
    CHECK(coincident == 6);
    CHECK(equal == 4);
    CHECK(chudware::doc::doc_sketch_health(doc, sketch).at("skipped_constraints").empty());
    chudware::doc::doc_close(doc);
}
